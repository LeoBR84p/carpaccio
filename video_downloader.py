#!/usr/bin/env python3
"""
Download a list of .ts video segments, sort them, merge into a single video,
and optionally download subtitles.

Usage:
    python video_downloader.py --urls urls.txt --output video.mp4
    python video_downloader.py --urls urls.txt --output video.mp4 --subtitles subs.vtt
    python video_downloader.py --m3u8 playlist.m3u8 --output video.mp4
"""

import argparse
import functools
import json
import re
import sys
import tempfile
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

# Prefer the bundled ffmpeg binary when available
_SCRIPT_DIR = Path(__file__).parent
_BUNDLED_FFMPEG = _SCRIPT_DIR / "ffmpeg" / "bin" / "ffmpeg.exe"
_BUNDLED_FFPROBE = _SCRIPT_DIR / "ffmpeg" / "bin" / "ffprobe.exe"
FFMPEG  = str(_BUNDLED_FFMPEG)  if _BUNDLED_FFMPEG.exists()  else "ffmpeg"
FFPROBE = str(_BUNDLED_FFPROBE) if _BUNDLED_FFPROBE.exists() else "ffprobe"


@functools.lru_cache(maxsize=1)
def _nvenc_available() -> bool:
    try:
        return subprocess.run(
            [FFMPEG, "-f", "lavfi", "-i", "nullsrc=s=320x240:d=0.1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except Exception:
        return False

try:
    import requests
except ImportError:
    print("Missing dependencies. Run: pip install requests")
    sys.exit(1)

from rich.console import Console
from rich.progress import (
    Progress, BarColumn, FileSizeColumn, ProgressColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
    TimeRemainingColumn, TotalFileSizeColumn,
)

_console_err = Console(stderr=True)

from logo import print_logo

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def natural_sort_key(text: str) -> list[int | str]:
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", text)]


def sort_urls(urls: list[str]) -> list[str]:
    return sorted(urls, key=lambda u: natural_sort_key(urlparse(u).path))


def download_segment(url: str, dest: Path, session: requests.Session, retries: int = 3) -> Path:
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            return dest
        except Exception as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"Failed to download {url}: {exc}") from exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to download {url} after {retries} attempts")


# ---------------------------------------------------------------------------
# M3U8 parsing
# ---------------------------------------------------------------------------

def parse_m3u8(source: str, session: requests.Session) -> tuple[list[str], list[float], str | None]:
    """
    Parse an M3U8 playlist from a URL or local file.
    Returns (segment_urls, durations_in_seconds, subtitle_url_or_none).
    """
    is_url = source.startswith("http://") or source.startswith("https://")

    if is_url:
        content = session.get(source, timeout=30).text
    else:
        content = Path(source).read_text()

    # Handle master playlist (selects highest-bandwidth media playlist)
    if "#EXT-X-STREAM-INF" in content:
        best_bandwidth = -1
        best_url = None
        lines = content.splitlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bandwidth = int(m.group(1)) if m else 0
                for next_line in lines[i + 1:]:
                    next_line = next_line.strip()
                    if next_line:
                        if bandwidth > best_bandwidth:
                            best_bandwidth = bandwidth
                            best_url = next_line if next_line.startswith("http") else urljoin(source, next_line)
                        break
        if best_url:
            console.print(f"Selected stream: [cyan]{best_bandwidth // 1000}k[/cyan] bps")
            return parse_m3u8(best_url, session)

    segments: list[str] = []
    durations: list[float] = []
    subtitle_url: str | None = None
    next_duration: float | None = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                next_duration = float(line[8:].split(",")[0])
            except ValueError:
                next_duration = None
        elif line.startswith("#"):
            if "TYPE=SUBTITLES" in line or "TYPE=CLOSED-CAPTIONS" in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    uri = m.group(1)
                    subtitle_url = uri if uri.startswith("http") else urljoin(source, uri)
        else:
            url = line if line.startswith("http") else (urljoin(source, line) if is_url else line)
            segments.append(url)
            durations.append(next_duration or 0.0)
            next_duration = None

    return segments, durations, subtitle_url


def estimate_size(
    urls: list[str],
    session: requests.Session,
    durations: list[float] | None = None,
    sample: int = 16,
) -> int | None:
    """
    Estimate total download size via HEAD requests on a spread sample.

    Strategy: compute bytes/second for each sampled segment (accounts for
    variable segment duration), take the median (robust to outliers), then
    multiply by total duration. Falls back to median_size * n_segments when
    durations are unavailable.
    """
    if not urls:
        return None

    # Spread sample evenly across the playlist
    indices = [int(i * (len(urls) - 1) / (sample - 1)) for i in range(sample)] if len(urls) >= sample else list(range(len(urls)))

    rates:  list[float] = []   # bytes / second
    sizes:  list[float] = []   # fallback when no durations

    for idx in indices:
        url = urls[idx]
        dur = durations[idx] if durations and idx < len(durations) else None
        try:
            resp = session.head(url, timeout=10, allow_redirects=True)
            cl = resp.headers.get("Content-Length")
            if not cl:
                continue
            seg_bytes = int(cl)
            if dur and dur > 0:
                rates.append(seg_bytes / dur)
            else:
                sizes.append(seg_bytes)
        except Exception:
            pass

    def _median(lst: list[float]) -> float:
        s = sorted(lst)
        mid = len(s) // 2
        return (s[mid - 1] + s[mid]) / 2 if len(s) % 2 == 0 else s[mid]

    if rates and durations:
        total_dur = sum(durations)
        return int(_median(rates) * total_dur)

    if sizes:
        return int(_median(sizes) * len(urls))

    return None


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ---------------------------------------------------------------------------
# Download & merge
# ---------------------------------------------------------------------------

def download_segments(
    urls: list[str],
    work_dir: Path,
    workers: int = 8,
) -> list[Path]:
    """Download all segments in parallel, return sorted local paths."""
    session = requests.Session()
    total = len(urls)
    results: dict[int, Path] = {}

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Downloading segments", total=total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    download_segment,
                    url,
                    work_dir / f"seg_{idx:06d}.ts",
                    session,
                ): idx
                for idx, url in enumerate(urls)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    _console_err.print(f"[red][ERROR] Segment {idx}: {exc}[/red]")
                    sys.exit(1)
                progress.advance(task)

    return [results[i] for i in sorted(results)]


def merge_segments(
    segment_files: list[Path],
    output: Path,
    sharpen: bool = False,
    total_seconds: float = 0.0,
    est_bytes: int = 0,
) -> None:
    """Concatenate .ts files and remux via ffmpeg with a live progress bar."""
    concat_list = segment_files[0].parent / "concat_list.txt"
    with open(concat_list, "w") as f:
        for seg in segment_files:
            f.write(f"file '{seg.resolve()}'\n")

    base = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list)]
    if sharpen:
        if not _nvenc_available():
            raise RuntimeError(
                "NVENC não disponível nesta build do ffmpeg. "
                "Instale uma build com suporte CUDA (ex: https://github.com/BtbN/FFmpeg-Builds/releases) "
                "ou remova --sharpen para usar cópia direta sem reencoding."
            )
        console.print("[cyan]Encoder:[/cyan] h264_nvenc (GPU) + unsharp (CPU)")
        encode = [
            "-map", "0:v:0", "-map", "0:a:0",
            "-vf", "unsharp=5:5:1.5:5:5:0.0",
            "-c:v", "h264_nvenc", "-cq", "26", "-b:v", "0", "-preset", "p6", "-c:a", "copy",
        ]
    else:
        encode = ["-c", "copy"]
    cmd = base + encode + ["-progress", "pipe:1", "-nostats", "-loglevel", "error", str(output)]

    console.print(f"\nMerging [cyan]{len(segment_files)}[/cyan] segments → [green]{output}[/green]")

    use_duration = total_seconds > 0
    total = total_seconds if use_duration else (est_bytes if est_bytes > 0 else None)

    columns: list[ProgressColumn] = (
        [TextColumn("[progress.description]{task.description}"),
         BarColumn(), TaskProgressColumn(),
         TimeElapsedColumn(), TimeRemainingColumn()]
        if use_duration else
        [TextColumn("[progress.description]{task.description}"),
         BarColumn(), FileSizeColumn(), TextColumn("/"), TotalFileSizeColumn(),
         TimeElapsedColumn(), TimeRemainingColumn()]
    )

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stderr is not None

    stderr_buf: list[str] = []
    _proc_stderr = proc.stderr
    threading.Thread(target=lambda: stderr_buf.extend(_proc_stderr), daemon=True).start()

    with Progress(*columns, console=console) as progress:
        task = progress.add_task("Merging", total=total)
        for line in proc.stdout:
            line = line.strip()
            if use_duration and line.startswith("out_time_ms="):
                try:
                    secs = int(line.split("=")[1]) / 1_000_000
                    progress.update(task, completed=min(secs, total_seconds))
                except ValueError:
                    pass
            elif not use_duration and est_bytes and line.startswith("total_size="):
                try:
                    size = int(line.split("=")[1])
                    progress.update(task, completed=min(size, est_bytes))
                except ValueError:
                    pass
        if total:
            progress.update(task, completed=total)

    proc.wait()
    concat_list.unlink(missing_ok=True)

    if proc.returncode != 0:
        console.print("".join(stderr_buf), style="red")
        raise RuntimeError("ffmpeg merge failed")
    console.print(f"[green]Video saved:[/green] {output}")


def download_subtitle(url: str, dest: Path, session: requests.Session) -> None:
    console.print(f"Downloading subtitles from [cyan]{url}[/cyan]")
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    console.print(f"[green]Subtitles saved:[/green] {dest}")


# ---------------------------------------------------------------------------
# Quality probe
# ---------------------------------------------------------------------------

_QualityRef = tuple[str, int, int, int, int | None]
_QUALITY_REFS: list[_QualityRef] = [
    ("240p",   240,  300,   700,   None),
    ("360p",   360,  400,  1000,   None),
    ("480p",   480,  500,  2000,   1750),
    ("720p",   720, 1500,  4000,   3000),
    ("1080p", 1080, 3000,  8000,   5000),
    ("1440p", 1440, 6000, 16000,   None),
    ("4K",    2160,13000, 51000,  15000),
]


def probe_quality(url: str) -> dict[str, Any]:
    """Run ffprobe on the first segment of the M3U8 and return stream info."""
    try:
        result = subprocess.run([
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name,avg_frame_rate",
            "-of", "json", url,
        ], capture_output=True, text=True, timeout=20)
        streams = json.loads(result.stdout).get("streams", [])
        return streams[0] if streams else {}
    except Exception:
        return {}


def _quality_grade(height: int, kbps: int) -> tuple[str, str, str, int | None]:
    """Return (tier_name, grade_label, grade_color, netflix_kbps)."""
    tier: _QualityRef | None = None
    for ref in _QUALITY_REFS:
        if height >= ref[1]:
            tier = ref
    if not tier:
        return "?", "desconhecido", "dim", None
    name, _, yt_min, yt_max, nf = tier
    pct: float = (kbps - yt_min) / max(yt_max - yt_min, 1) * 100
    if kbps < yt_min:
        label, color = "abaixo do recomendado", "red"
    elif pct < 30:
        label, color = "ok", "yellow"
    elif pct < 70:
        label, color = "bom", "green"
    else:
        label, color = "excelente", "bright_green"
    return name, label, color, nf


def show_quality_report(url: str, kbps: int, sharpen: bool) -> tuple[bool, bool]:
    """
    Display quality info and ask for confirmation.
    Returns (proceed, use_sharpen).
    """
    from rich.table import Table
    from rich.panel import Panel

    console.print("\n[bold]Analisando qualidade do stream...[/bold]")
    info = probe_quality(url)

    w = info.get("width", "?")
    h = info.get("height", 0)
    codec = info.get("codec_name", "?")
    try:
        a, b = info.get("avg_frame_rate", "0/1").split("/")
        fps = f"{round(int(a)/int(b), 1)}" if int(b) else "?"
    except Exception:
        fps = "?"

    tier, label, color, nf_kbps = _quality_grade(h or 0, kbps)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()

    table.add_row("Resolucao", f"[cyan]{w}x{h}[/cyan]")
    table.add_row("Codec",     f"[cyan]{codec}[/cyan]")
    table.add_row("FPS",       f"[cyan]{fps}[/cyan]")
    table.add_row("Bitrate",   f"[cyan]{kbps} kbps[/cyan]")
    table.add_row("Tier",      f"[cyan]{tier}[/cyan]")
    table.add_row("Qualidade", f"[{color}]{label}[/{color}]")
    if nf_kbps:
        ref_color = "green" if kbps >= nf_kbps else "yellow"
        table.add_row("Netflix ref", f"[{ref_color}]{nf_kbps} kbps ({'OK' if kbps >= nf_kbps else 'abaixo'})[/{ref_color}]")

    console.print(Panel(table, title="[bold]Qualidade do Video[/bold]", border_style="cyan"))

    use_sharpen = sharpen
    if not sharpen and kbps < 5000:
        console.print(
            f"[yellow]Sugestao:[/yellow] bitrate {kbps} kbps < 5000 kbps. "
            "Ativar [bold]--sharpen[/bold] melhora nitidez de texto e slides."
        )
        sharpen_answer = console.input("Ativar --sharpen? [bold][Y/n][/bold]: ").strip().lower()
        use_sharpen = sharpen_answer in ("", "y", "yes", "s", "sim")

    answer = console.input("\nProsseguir com o download? [bold][Y/n][/bold]: ").strip().lower()
    return answer in ("", "y", "yes", "s", "sim"), use_sharpen


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download .ts video segments, merge them, and optionally fetch subtitles."
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--urls", metavar="FILE",
                     help="Text file with one segment URL per line")
    src.add_argument("--m3u8", metavar="URL_OR_FILE",
                     help="M3U8 playlist URL or local file path")

    p.add_argument("--output", "-o", required=True, metavar="FILE",
                   help="Output video file (e.g. video.mp4)")
    p.add_argument("--subtitles", "-s", metavar="URL",
                   help="Subtitle URL to download (VTT/SRT/etc.)")
    p.add_argument("--subtitle-out", metavar="FILE",
                   help="Subtitle output filename (default: <output>.vtt)")
    p.add_argument("--workers", "-w", type=int, default=8,
                   help="Parallel download workers (default: 8)")
    p.add_argument("--keep-segments", action="store_true",
                   help="Keep individual .ts segment files after merging")
    p.add_argument("--sharpen", action="store_true",
                   help="Apply unsharp filter + re-encode (better text clarity, slower)")
    return p


def main():
    print_logo(console)
    args = build_parser().parse_args()
    session = requests.Session()
    output = Path(args.output)

    # -- Output file conflict check --------------------------------------------
    if output.exists():
        console.print(f"[yellow]Aviso:[/yellow] [cyan]{output}[/cyan] já existe.")
        overwrite = console.input("Sobrescrever? [bold][Y/n][/bold]: ").strip().lower()
        if overwrite not in ("", "y", "yes", "s", "sim"):
            rename = console.input("Renomear automaticamente? [bold][Y/n][/bold]: ").strip().lower()
            if rename not in ("", "y", "yes", "s", "sim"):
                console.print("[yellow]Cancelado.[/yellow]")
                sys.exit(0)
            counter = 1
            while output.exists():
                output = Path(args.output).with_stem(Path(args.output).stem + f"_{counter}")
                counter += 1
            console.print(f"Salvando como [cyan]{output}[/cyan]")

    # -- Collect segment URLs --------------------------------------------------
    subtitle_url_from_m3u8: str | None = None

    if args.urls:
        raw = Path(args.urls).read_text().splitlines()
        urls = sort_urls([u.strip() for u in raw if u.strip()])
        durations: list[float] = []
        console.print(f"Loaded [cyan]{len(urls)}[/cyan] URLs from {args.urls}")
    else:
        console.print(f"Parsing M3U8: [cyan]{args.m3u8}[/cyan]")
        urls, durations, subtitle_url_from_m3u8 = parse_m3u8(args.m3u8, session)
        console.print(f"Found [cyan]{len(urls)}[/cyan] segments")

    if not urls:
        _console_err.print("[red]No segment URLs found. Aborting.[/red]")
        sys.exit(1)

    # -- Size & duration estimate ----------------------------------------------
    total_seconds = sum(durations)
    if total_seconds > 0:
        h, rem = divmod(int(total_seconds), 3600)
        m, s = divmod(rem, 60)
        duration_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        console.print(f"Duration : [cyan]{duration_str}[/cyan]")

    console.print("Estimating file size...", end=" ")
    est_bytes = estimate_size(urls, session, durations=durations if durations else None)
    if est_bytes:
        est_bytes = int(est_bytes * 1.25)
        console.print(f"[cyan]~{_fmt_size(est_bytes)}[/cyan]")
    else:
        console.print("[yellow]unavailable (server did not return Content-Length)[/yellow]")

    # -- Quality probe & confirmation ------------------------------------------
    probe_url = args.m3u8 if args.m3u8 else urls[0]
    proceed, use_sharpen = show_quality_report(
        probe_url,
        est_bytes * 8 // max(int(total_seconds), 1) // 1000 if est_bytes and total_seconds else 0,
        args.sharpen,
    )
    if not proceed:
        console.print("[yellow]Download cancelado.[/yellow]")
        sys.exit(0)

    # -- Download & merge ------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="ts_segments_") as tmp:
        work_dir = Path(tmp)
        segment_files = download_segments(urls, work_dir, workers=args.workers)
        merge_segments(segment_files, output, sharpen=use_sharpen,
                       total_seconds=total_seconds, est_bytes=est_bytes or 0)

        if args.keep_segments:
            kept_dir = output.parent / (output.stem + "_segments")
            kept_dir.mkdir(exist_ok=True)
            for seg in segment_files:
                seg.rename(kept_dir / seg.name)
            console.print(f"Segments kept in: [cyan]{kept_dir}[/cyan]")

    # -- Subtitles -------------------------------------------------------------
    sub_url = args.subtitles or subtitle_url_from_m3u8
    if sub_url:
        if args.subtitle_out:
            sub_dest = Path(args.subtitle_out)
        else:
            ext = Path(urlparse(sub_url).path).suffix or ".vtt"
            sub_dest = output.with_suffix(ext)
        download_subtitle(sub_url, sub_dest, session)


if __name__ == "__main__":
    main()
