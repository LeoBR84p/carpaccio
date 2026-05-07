#!/usr/bin/env python3
"""Re-encode existing video files with h264_nvenc (GPU) for smaller file size."""

import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypedDict

try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:
    print("tkinter não disponível.")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import (
        Progress, BarColumn, TaskProgressColumn,
        TextColumn, TimeElapsedColumn, TimeRemainingColumn, TaskID,
    )
    from rich.live import Live
    from rich.text import Text
except ImportError:
    print("Missing dependencies. Run: pip install rich")
    sys.exit(1)

_SCRIPT_DIR      = Path(__file__).parent
_BUNDLED_FFMPEG  = _SCRIPT_DIR / "ffmpeg" / "bin" / "ffmpeg.exe"
_BUNDLED_FFPROBE = _SCRIPT_DIR / "ffmpeg" / "bin" / "ffprobe.exe"
FFMPEG  = str(_BUNDLED_FFMPEG)  if _BUNDLED_FFMPEG.exists()  else "ffmpeg"
FFPROBE = str(_BUNDLED_FFPROBE) if _BUNDLED_FFPROBE.exists() else "ffprobe"

# Full GPU pipeline: decode on GPU, keep frames in CUDA memory, encode on GPU
HWACCEL_FLAGS = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]

ENCODE_FLAGS_H264 = [
    "-map", "0:v:0", "-map", "0:a:0",
    "-c:v", "h264_nvenc", "-cq", "26", "-b:v", "0", "-preset", "p6",
    "-c:a", "copy",
]

ENCODE_FLAGS_AV1 = [
    "-map", "0:v:0", "-map", "0:a:0",
    "-c:v", "av1_nvenc", "-preset", "p7", "-cq", "38",
    "-c:a", "copy",
]

ENCODE_FLAGS_AV1_CAPPED = [
    "-map", "0:v:0", "-map", "0:a:0",
    "-c:v", "av1_nvenc", "-preset", "p7", "-cq", "32",
    "-maxrate", "900k", "-bufsize", "1800k",
    "-c:a", "copy",
]


DEFAULT_WORKERS = 2

console = Console()


class _Stream(TypedDict, total=False):
    codec_type: str
    codec_name: str
    width: int
    height: int
    avg_frame_rate: str


class _Format(TypedDict, total=False):
    duration: str
    size: str
    bit_rate: str


class _ProbeResult(TypedDict):
    video: _Stream
    format: _Format


class _Analysis(TypedDict):
    path: Path
    w: int | str
    h: int | str
    codec: str
    fps: str
    kbps: int
    duration: float
    current_size: int
    est_size: int | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_duration(secs: float) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _unique_output(path: Path, stem_suffix: str = "_cq26", ext: str | None = None) -> Path:
    out_ext = ext or path.suffix
    candidate = Path(path.parent) / (path.stem + stem_suffix + out_ext)
    counter = 1
    while candidate.exists():
        candidate = Path(path.parent) / (path.stem + stem_suffix + f"_{counter}" + out_ext)
        counter += 1
    return candidate


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

class EncodeMode:
    def __init__(self, flags: list[str], stem_suffix: str, ext: str, label: str) -> None:
        self.flags       = flags
        self.stem_suffix = stem_suffix
        self.ext         = ext
        self.label       = label


def _select_mode() -> EncodeMode:
    console.print("\n[bold]Encode mode:[/bold]")
    console.print("  [cyan][1][/cyan] H.264       · h264_nvenc · CQ 26 · p6 · MP4  [dim](size reduction)[/dim]")
    console.print("  [cyan][2][/cyan] AV1         · av1_nvenc  · CQ 38 · p7 · MKV  [dim](maximum compression)[/dim]")
    console.print("  [cyan][3][/cyan] AV1 capped  · av1_nvenc  · CQ 32 · p7 · MKV  [dim](maxrate 900k — guaranteed reduction)[/dim]")
    choice = console.input("\nChoice [bold][1/2/3][/bold] (default: 1): ").strip()
    if choice == "2":
        return EncodeMode(ENCODE_FLAGS_AV1, "_av1", ".mkv", "av1_nvenc · CQ 38 · p7 · audio copy")
    if choice == "3":
        return EncodeMode(ENCODE_FLAGS_AV1_CAPPED, "_av1cap", ".mkv", "av1_nvenc · CQ 32 · maxrate 900k · p7 · audio copy")
    return EncodeMode(ENCODE_FLAGS_H264, "_cq26", ".mp4", "h264_nvenc · CQ 26 · p6 · audio copy")


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def probe_file(path: Path) -> _ProbeResult:
    r = subprocess.run([
        FFPROBE, "-v", "error",
        "-show_entries",
        "stream=width,height,codec_name,avg_frame_rate,codec_type"
        ":format=duration,size,bit_rate",
        "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30)
    data: dict[str, Any] = json.loads(r.stdout)

    _audio_codecs = {"aac", "mp3", "ac3", "eac3", "opus", "vorbis", "flac", "pcm_s16le"}
    streams: list[_Stream] = data.get("streams", [])
    video: _Stream = next(
        (s for s in streams
         if s.get("codec_type") == "video" or s.get("codec_name") not in _audio_codecs),
        _Stream(),
    )
    fmt: _Format = data.get("format", {})
    return {"video": video, "format": fmt}


# ---------------------------------------------------------------------------
# 1-minute preview encode
# ---------------------------------------------------------------------------

def _make_preview(path: Path, duration: float, encode_flags: list[str], ext: str) -> Path | None:
    seek_to = max(0.0, duration / 2 - 30)
    preview_path = path.with_stem(path.stem + "_preview").with_suffix(ext)

    cmd = [
        FFMPEG, "-y",
        "-ss", str(seek_to),
        *HWACCEL_FLAGS, "-i", str(path),
        "-t", "60",
        *encode_flags,
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        str(preview_path),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(),
        TimeElapsedColumn(), TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Encoding preview", total=60.0)
        for line in proc.stdout:
            if line.strip().startswith("out_time_ms="):
                try:
                    secs = int(line.strip().split("=")[1]) / 1_000_000
                    progress.update(task, completed=min(secs, 60.0))
                except ValueError:
                    pass
        progress.update(task, completed=60.0)

    proc.wait()
    return preview_path if proc.returncode == 0 and preview_path.exists() else None


# ---------------------------------------------------------------------------
# Sample encode → size estimate
# ---------------------------------------------------------------------------

def estimate_output_size(path: Path, duration: float, encode_flags: list[str]) -> int | None:
    if duration <= 0:
        return None
    sample_dur = min(30.0, duration)
    seek_to = min(max(duration * 0.05, 30.0), max(duration - sample_dur - 5, 0))

    with tempfile.TemporaryDirectory() as tmp:
        sample_out = Path(tmp) / "sample.mp4"
        cmd = [
            FFMPEG, "-y",
            "-ss", str(seek_to),
            *HWACCEL_FLAGS, "-i", str(path),
            "-t", str(sample_dur),
            *encode_flags,
            "-loglevel", "error",
            str(sample_out),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not sample_out.exists():
            cmd_cpu = [
                FFMPEG, "-y",
                "-ss", str(seek_to), "-i", str(path),
                "-t", str(sample_dur),
                *encode_flags,
                "-loglevel", "error",
                str(sample_out),
            ]
            r = subprocess.run(cmd_cpu, capture_output=True, timeout=120)
            if r.returncode != 0 or not sample_out.exists():
                return None
        sample_bytes = sample_out.stat().st_size
        return int(sample_bytes / sample_dur * duration)


# ---------------------------------------------------------------------------
# Encode a single file — writes progress to a shared Progress object
# ---------------------------------------------------------------------------

def encode_file(
    path: Path,
    output: Path,
    duration: float,
    progress: Progress,
    task_id: TaskID,
    encode_flags: list[str] = ENCODE_FLAGS_H264,
) -> None:
    cmd = [
        FFMPEG, "-y",
        *HWACCEL_FLAGS, "-i", str(path),
        *encode_flags,
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        str(output),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout and proc.stderr

    stderr_buf: list[str] = []
    _proc_stderr = proc.stderr
    threading.Thread(target=lambda: stderr_buf.extend(_proc_stderr), daemon=True).start()

    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                secs = int(line.split("=")[1]) / 1_000_000
                progress.update(task_id, completed=min(secs, duration))
            except ValueError:
                pass

    proc.wait()

    if proc.returncode != 0:
        # hwaccel failed mid-encode — retry without GPU decode
        err = "".join(stderr_buf)
        if "cuda" in err.lower() or "hwaccel" in err.lower():
            _encode_file_cpu(path, output, duration, progress, task_id, encode_flags)
        else:
            raise RuntimeError(f"ffmpeg falhou:\n{err}")
    else:
        if duration:
            progress.update(task_id, completed=duration)


def _encode_file_cpu(
    path: Path,
    output: Path,
    duration: float,
    progress: Progress,
    task_id: TaskID,
    encode_flags: list[str] = ENCODE_FLAGS_H264,
) -> None:
    """Fallback encode without GPU decode (for codecs unsupported by CUVID)."""
    cmd = [
        FFMPEG, "-y", "-i", str(path),
        *encode_flags,
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        str(output),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout and proc.stderr

    stderr_buf: list[str] = []
    _proc_stderr = proc.stderr
    threading.Thread(target=lambda: stderr_buf.extend(_proc_stderr), daemon=True).start()

    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                secs = int(line.split("=")[1]) / 1_000_000
                progress.update(task_id, completed=min(secs, duration))
            except ValueError:
                pass

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou:\n{''.join(stderr_buf)}")
    if duration:
        progress.update(task_id, completed=duration)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        from logo import print_logo
        print_logo(console)
    except Exception:
        pass

    # File selection first — before any console.input() to avoid tkinter focus issues
    root = tk.Tk()
    root.attributes("-topmost", True)  # type: ignore[misc]
    root.withdraw()
    root.update()
    selected = filedialog.askopenfilenames(
        parent=root,
        title="Select videos to re-encode",
        filetypes=[
            ("Video files", "*.mp4 *.mkv *.avi *.mov *.ts *.wmv"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()

    if not selected:
        console.print("[yellow]No file selected.[/yellow]")
        sys.exit(0)

    files = [Path(p) for p in selected]
    console.print(f"\n[bold]{len(files)} file(s) selected[/bold]\n")

    mode = _select_mode()

    # Analyze files (sequentially — I/O bound, no need to parallelize)
    analyses: list[_Analysis] = []

    for path in files:
        console.print(f"[dim]►[/dim] [cyan]{path.name}[/cyan]")

        info = probe_file(path)
        video = info["video"]
        fmt   = info["format"]

        current_size = path.stat().st_size
        duration     = float(fmt.get("duration") or 0)
        kbps         = int(fmt.get("bit_rate") or 0) // 1000
        w            = video.get("width",  "?")
        h            = video.get("height", "?")
        codec        = video.get("codec_name", "?")
        try:
            a, b = video.get("avg_frame_rate", "0/1").split("/")
            fps = f"{round(int(a) / int(b), 1)}" if int(b) else "?"
        except Exception:
            fps = "?"

        with Live(
            Text("  Estimating output size...", style="dim"),
            console=console, refresh_per_second=4,
        ):
            est_size = estimate_output_size(path, duration, mode.flags)

        if est_size:
            reduction = (1 - est_size / current_size) * 100
            console.print(
                f"  {_fmt_size(current_size)} → "
                f"[cyan]{_fmt_size(est_size)}[/cyan]  "
                f"([green]-{reduction:.0f}%[/green])"
            )
        else:
            console.print(f"  {_fmt_size(current_size)} → [yellow]estimate unavailable[/yellow]")

        analyses.append({
            "path": path, "w": w, "h": h, "codec": codec, "fps": fps,
            "kbps": kbps, "duration": duration,
            "current_size": current_size, "est_size": est_size,
        })

    # Summary table
    console.print()
    table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
    table.add_column("File",       style="dim", no_wrap=True)
    table.add_column("Resolution", justify="right")
    table.add_column("Codec")
    table.add_column("Bitrate",    justify="right")
    table.add_column("Duration",   justify="right")
    table.add_column("Current",    justify="right")
    table.add_column("Estimated",  justify="right")
    table.add_column("Reduction",  justify="right")

    total_current = total_estimated = 0

    for a in analyses:
        est_str = red_str = "[yellow]?[/yellow]"
        if a["est_size"]:
            reduction = (1 - a["est_size"] / a["current_size"]) * 100
            est_str = f"[cyan]{_fmt_size(a['est_size'])}[/cyan]"
            red_str = f"[green]-{reduction:.0f}%[/green]"
            total_estimated += a["est_size"]
        total_current += a["current_size"]

        table.add_row(
            a["path"].name[:42],
            f"{a['w']}x{a['h']}",
            a["codec"],
            f"{a['kbps']} kbps",
            _fmt_duration(a["duration"]),
            _fmt_size(a["current_size"]),
            est_str,
            red_str,
        )

    if len(files) > 1 and total_estimated:
        reduction_total = (1 - total_estimated / total_current) * 100
        table.add_row("", "", "", "", "", "", "", "")
        table.add_row(
            "[bold]TOTAL[/bold]", "", "", "", "",
            f"[bold]{_fmt_size(total_current)}[/bold]",
            f"[bold cyan]{_fmt_size(total_estimated)}[/bold cyan]",
            f"[bold green]-{reduction_total:.0f}%[/bold green]",
        )

    console.print(Panel(
        table,
        title="[bold]Re-Encode Summary[/bold]",
        border_style="cyan",
    ))
    workers = min(DEFAULT_WORKERS, len(files))
    console.print(f"[dim]Encoder: {mode.label} · Workers: {workers}[/dim]")

    # Optional preview
    want_preview = console.input("\nGenerate a 1-minute preview before proceeding? [bold][Y/n][/bold]: ").strip().lower()
    if want_preview in ("", "y", "yes"):
        first = analyses[0]
        console.print(f"\nEncoding preview for [cyan]{first['path'].name}[/cyan] (middle segment)...")
        preview_path = _make_preview(first["path"], first["duration"], mode.flags, mode.ext)
        if preview_path:
            console.print(f"[green]Preview saved:[/green] {preview_path}")
            os.startfile(str(preview_path))
            cont = console.input("\nContinue with the full re-encode? [bold][Y/n][/bold]: ").strip().lower()
            if cont not in ("", "y", "yes"):
                preview_path.unlink(missing_ok=True)
                console.print("[yellow]Cancelled.[/yellow]")
                sys.exit(0)
        else:
            console.print("[yellow]Preview failed — proceeding without preview.[/yellow]")
    else:
        answer = console.input("\nProceed with re-encoding? [bold][Y/n][/bold]: ").strip().lower()
        if answer not in ("", "y", "yes"):
            console.print("[yellow]Cancelled.[/yellow]")
            sys.exit(0)

    # Parallel encode with shared progress bar
    console.print()
    outputs = {a["path"]: _unique_output(a["path"], mode.stem_suffix, mode.ext) for a in analyses}
    results: dict[Path, tuple[bool, str]] = {}  # path → (ok, message)
    lock = threading.Lock()

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(),
        TimeElapsedColumn(), TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_ids = {
            a["path"]: progress.add_task(a["path"].name[:45], total=a["duration"] or None)
            for a in analyses
        }

        def _run(a: _Analysis) -> None:
            path   = a["path"]
            output = outputs[path]
            try:
                encode_file(path, output, a["duration"], progress, task_ids[path], mode.flags)
                final_size = output.stat().st_size
                reduction  = (1 - final_size / a["current_size"]) * 100
                msg = (
                    f"[green]✓[/green] [cyan]{path.name}[/cyan]  "
                    f"{_fmt_size(a['current_size'])} → "
                    f"[cyan]{_fmt_size(final_size)}[/cyan]  "
                    f"([green]-{reduction:.0f}%[/green])"
                )
                with lock:
                    results[path] = (True, msg)
            except RuntimeError as exc:
                with lock:
                    results[path] = (False, f"[red]✗ {path.name}:[/red] {exc}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: dict[Future[None], _Analysis] = {pool.submit(_run, a): a for a in analyses}
            for f in as_completed(futures):
                f.result()  # re-raise any unexpected exception

    # Print results in original order
    console.print()
    errors: list[str] = []
    for a in analyses:
        ok, msg = results.get(a["path"], (False, f"[red]✗ {a['path'].name}: no result[/red]"))
        console.print(msg)
        if not ok:
            errors.append(a["path"].name)

    console.print()
    if errors:
        console.print(f"[red]Failed:[/red] {', '.join(errors)}")
    else:
        console.print("[bold green]All files re-encoded successfully.[/bold green]")


if __name__ == "__main__":
    main()
