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
# CUDA decode without pinning frames in GPU memory — required when CPU filters (setpts) are used
HWACCEL_FLAGS_DECODE = ["-hwaccel", "cuda"]

_SHARPEN = "unsharp=5:5:1.5:5:5:0.0"

ENCODE_FLAGS_H264 = [
    "-map", "0:v:0", "-map", "0:a:0",
    "-c:v", "h264_nvenc", "-cq", "26", "-b:v", "0", "-preset", "p6",
    "-vf", _SHARPEN,
    "-c:a", "copy",
]

ENCODE_FLAGS_AV1 = [
    "-map", "0:v:0", "-map", "0:a:0",
    "-c:v", "av1_nvenc", "-preset", "p7", "-cq", "38",
    "-vf", _SHARPEN,
    "-c:a", "copy",
]

ENCODE_FLAGS_AV1_CAPPED = [
    "-map", "0:v:0", "-map", "0:a:0",
    "-c:v", "av1_nvenc", "-preset", "p7", "-cq", "32",
    "-maxrate", "900k", "-bufsize", "1800k",
    "-vf", _SHARPEN,
    "-c:a", "copy",
]

_NVENC_MAP = {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"}

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
    encode_flags: list[str]
    out_ext: str


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


def _fmt_reduction(reduction: float) -> str:
    if reduction >= 0:
        return f"[green]-{reduction:.0f}%[/green]"
    return f"[red]+{-reduction:.0f}%[/red]"


def _unique_output(path: Path, stem_suffix: str = "_cq26", ext: str | None = None) -> Path:
    out_ext = ext or path.suffix
    candidate = Path(path.parent) / (path.stem + stem_suffix + out_ext)
    counter = 1
    while candidate.exists():
        candidate = Path(path.parent) / (path.stem + stem_suffix + f"_{counter}" + out_ext)
        counter += 1
    return candidate


# ---------------------------------------------------------------------------
# Mode & speed selection
# ---------------------------------------------------------------------------

class EncodeMode:
    def __init__(
        self, flags: list[str], stem_suffix: str, ext: str, label: str,
        passthrough: bool = False,
    ) -> None:
        self.flags       = flags
        self.stem_suffix = stem_suffix
        self.ext         = ext
        self.label       = label
        self.passthrough = passthrough  # True = keep source codec (mode 0)


class SpeedMode:
    def __init__(self, multiplier: float, label: str, suffix: str) -> None:
        self.multiplier = multiplier
        self.label      = label
        self.suffix     = suffix


def _select_mode() -> EncodeMode:
    console.print("\n[bold]Encode mode:[/bold]")
    console.print("  [cyan][0][/cyan] Keep format · detect source codec         [dim](speed-change only)[/dim]")
    console.print("  [cyan][1][/cyan] H.264       · h264_nvenc · CQ 26 · p6 · MP4  [dim](size reduction)[/dim]")
    console.print("  [cyan][2][/cyan] AV1         · av1_nvenc  · CQ 38 · p7 · MKV  [dim](maximum compression)[/dim]")
    console.print("  [cyan][3][/cyan] AV1 capped  · av1_nvenc  · CQ 32 · p7 · MKV  [dim](maxrate 900k — guaranteed reduction)[/dim]")
    choice = console.input("\nChoice [bold][0/1/2/3][/bold] (default: 1): ").strip()
    if choice == "0":
        return EncodeMode([], "_spd", "", "Keep format — detect source codec", passthrough=True)
    if choice == "2":
        return EncodeMode(ENCODE_FLAGS_AV1, "_av1", ".mkv", "av1_nvenc · CQ 38 · p7 · audio copy")
    if choice == "3":
        return EncodeMode(ENCODE_FLAGS_AV1_CAPPED, "_av1cap", ".mkv", "av1_nvenc · CQ 32 · maxrate 900k · p7 · audio copy")
    return EncodeMode(ENCODE_FLAGS_H264, "_cq26", ".mp4", "h264_nvenc · CQ 26 · p6 · audio copy")


def _select_speed() -> SpeedMode:
    console.print("\n[bold]Output speed:[/bold]")
    console.print("  [cyan][1][/cyan] 0.5x  · Slow down          [dim](2x longer)[/dim]")
    console.print("  [cyan][2][/cyan] 1x    · Normal speed        [dim](no change)[/dim]")
    console.print("  [cyan][3][/cyan] 1.5x  · Hurry               [dim](33% shorter)[/dim]")
    console.print("  [cyan][4][/cyan] 2x    · No time to lose     [dim](50% shorter)[/dim]")
    console.print("  [cyan][5][/cyan] 2.5x  · Quick               [dim](60% shorter)[/dim]")
    console.print("  [cyan][6][/cyan] 3x    · Flash               [dim](67% shorter)[/dim]")
    console.print("  [cyan][7][/cyan] Custom · Enter your own value [dim](e.g. 1.75)[/dim]")
    choice = console.input("\nChoice [bold][1/2/3/4/5/6/7][/bold] (default: 2): ").strip()
    if choice == "1":
        return SpeedMode(0.5, "0.5x · Slow down", "_0.5x")
    if choice == "3":
        return SpeedMode(1.5, "1.5x · Hurry", "_1.5x")
    if choice == "4":
        return SpeedMode(2.0, "2x · No time to lose", "_2x")
    if choice == "5":
        return SpeedMode(2.5, "2.5x · Quick", "_2.5x")
    if choice == "6":
        return SpeedMode(3.0, "3x · Flash", "_3x")
    if choice == "7":
        return _custom_speed()
    return SpeedMode(1.0, "1x · Normal", "")


def _custom_speed() -> SpeedMode:
    """Prompt for a float speed with up to two decimals (e.g. 1.75)."""
    while True:
        raw = console.input(
            "\nEnter output speed (float, up to 2 decimals, e.g. [cyan]1.75[/cyan]): "
        ).strip()
        try:
            speed = round(float(raw), 2)
        except ValueError:
            console.print("[red]Invalid number — use a dot as decimal separator (e.g. 1.75).[/red]")
            continue
        if speed <= 0:
            console.print("[red]Speed must be greater than 0.[/red]")
            continue
        label  = f"{speed:g}x · Custom"
        suffix = "" if speed == 1.0 else f"_{speed:g}x"
        return SpeedMode(speed, label, suffix)


# ---------------------------------------------------------------------------
# Speed helpers
# ---------------------------------------------------------------------------

def _apply_speed_to_flags(flags: list[str], speed: float) -> list[str]:
    """Insert setpts/atempo filters. Merges with existing -vf. Replaces -c:a copy with aac."""
    if speed == 1.0:
        return flags
    flags = list(flags)
    try:
        idx = flags.index("-c:a")
        if idx + 1 < len(flags) and flags[idx + 1] == "copy":
            flags[idx + 1] = "aac"
            flags[idx + 2:idx + 2] = ["-b:a", "192k"]
    except ValueError:
        flags += ["-c:a", "aac", "-b:a", "192k"]
    setpts = f"setpts=PTS/{speed}"
    af = f"atempo={speed}" if speed <= 2.0 else f"atempo=2.0,atempo={speed / 2.0:.4f}"
    try:
        vf_idx = flags.index("-vf")
        flags[vf_idx + 1] = flags[vf_idx + 1] + f",{setpts}"
    except ValueError:
        flags += ["-vf", setpts]
    flags += ["-af", af]
    return flags


def _passthrough_flags(
    source_codec: str, speed: float, source_kbps: int = 0,
) -> tuple[list[str], str]:
    """Build flags for mode 0 (keep source codec). Returns (flags, ext_override).
    ext_override='' means keep the source file extension."""
    base = ["-map", "0:v:0", "-map", "0:a:0"]
    if speed == 1.0:
        return base + ["-c:v", "copy", "-c:a", "copy"], ""
    nvenc     = _NVENC_MAP.get(source_codec, "h264_nvenc")
    ext       = ".mkv" if nvenc == "av1_nvenc" else ".mp4"
    vf        = f"setpts=PTS/{speed}"
    af        = f"atempo={speed}" if speed <= 2.0 else f"atempo=2.0,atempo={speed / 2.0:.4f}"
    # Target source bitrate so output size scales proportionally with speed:
    #   new_size ≈ (duration / speed) × source_kbps / 8 = current_size / speed
    audio_kbps = 192
    video_kbps = max(500, source_kbps - audio_kbps) if source_kbps else 2000
    return (base + [
        "-c:v", nvenc,
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.5)}k",
        "-bufsize", f"{video_kbps * 3}k",
        "-preset", "p4",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-vf", vf, "-af", af,
    ], ext)


def _hwaccel_for(flags: list[str]) -> list[str]:
    """Use plain CUDA decode (no output_format=cuda) when CPU filters like setpts are present."""
    return HWACCEL_FLAGS_DECODE if "-vf" in flags else HWACCEL_FLAGS


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

def _make_preview(
    path: Path, duration: float, encode_flags: list[str], ext: str, speed: float = 1.0,
) -> Path | None:
    # Use input duration limit (before -i) so output is always ~60s regardless of speed
    input_dur = 60.0 * speed
    seek_to   = max(0.0, duration / 2 - input_dur / 2)
    preview_path = path.with_stem(path.stem + "_preview").with_suffix(ext)

    hwaccel = _hwaccel_for(encode_flags)
    cmd = [
        FFMPEG, "-y",
        "-ss", str(seek_to),
        "-t", str(input_dur),   # input limit — placed before -i
        *hwaccel, "-i", str(path),
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

def estimate_output_size(
    path: Path, duration: float, encode_flags: list[str],
    ext: str = ".mp4", speed: float = 1.0,
) -> int | None:
    if duration <= 0:
        return None
    # -t is placed before -i (input limit): consumes sample_dur seconds of input,
    # producing sample_dur/speed seconds of output. Formula scales to output duration.
    sample_dur = min(30.0, duration)
    seek_to = min(max(duration * 0.05, 30.0), max(duration - sample_dur - 5, 0))
    output_duration = duration / speed

    hwaccel = _hwaccel_for(encode_flags)

    with tempfile.TemporaryDirectory() as tmp:
        sample_out = Path(tmp) / f"sample{ext}"
        cmd = [
            FFMPEG, "-y",
            "-ss", str(seek_to),
            "-t", str(sample_dur),   # input limit — before -i
            *hwaccel, "-i", str(path),
            *encode_flags,
            "-loglevel", "error",
            str(sample_out),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not sample_out.exists():
            cmd_cpu = [
                FFMPEG, "-y",
                "-ss", str(seek_to),
                "-t", str(sample_dur),   # input limit — before -i
                "-i", str(path),
                *encode_flags,
                "-loglevel", "error",
                str(sample_out),
            ]
            r = subprocess.run(cmd_cpu, capture_output=True, timeout=120)
            if r.returncode != 0 or not sample_out.exists():
                return None
        sample_bytes = sample_out.stat().st_size
        # sample encodes sample_dur/speed seconds of output → bytes per output second:
        sample_output_dur = sample_dur / speed
        return int(sample_bytes / sample_output_dur * output_duration)


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
    hwaccel = _hwaccel_for(encode_flags)
    cmd = [
        FFMPEG, "-y",
        *hwaccel, "-i", str(path),
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

    mode  = _select_mode()
    speed = _select_speed()

    # e.g. "_cq26_1.5x", "_spd_2x", "_av1"
    final_suffix = mode.stem_suffix + speed.suffix

    # Analyze files (sequentially — I/O bound)
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
        codec        = video.get("codec_name", "h264")
        try:
            a, b = video.get("avg_frame_rate", "0/1").split("/")
            fps = f"{round(int(a) / int(b), 1)}" if int(b) else "?"
        except Exception:
            fps = "?"

        if mode.passthrough:
            final_flags, ext_override = _passthrough_flags(codec, speed.multiplier, kbps)
            out_ext = ext_override if ext_override else path.suffix
        else:
            final_flags = _apply_speed_to_flags(mode.flags, speed.multiplier)
            out_ext = mode.ext

        is_copy = mode.passthrough and speed.multiplier == 1.0
        if is_copy:
            est_size = current_size
        else:
            with Live(
                Text("  Estimating output size...", style="dim"),
                console=console, refresh_per_second=4,
            ):
                est_size = estimate_output_size(
                    path, duration, final_flags, out_ext or ".mp4", speed.multiplier,
                )

        if is_copy:
            console.print(f"  {_fmt_size(current_size)} → [dim]copy (same size)[/dim]")
        elif est_size:
            reduction = (1 - est_size / current_size) * 100
            console.print(
                f"  {_fmt_size(current_size)} → "
                f"[cyan]{_fmt_size(est_size)}[/cyan]  "
                f"({_fmt_reduction(reduction)})"
            )
        else:
            console.print(f"  {_fmt_size(current_size)} → [yellow]estimate unavailable[/yellow]")

        analyses.append({
            "path": path, "w": w, "h": h, "codec": codec, "fps": fps,
            "kbps": kbps, "duration": duration,
            "current_size": current_size, "est_size": est_size,
            "encode_flags": final_flags, "out_ext": out_ext,
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
    is_copy_mode = mode.passthrough and speed.multiplier == 1.0

    for a in analyses:
        if is_copy_mode:
            est_str = f"[dim]{_fmt_size(a['current_size'])}[/dim]"
            red_str = "[dim]copy[/dim]"
            total_estimated += a["current_size"]
        elif a["est_size"]:
            reduction = (1 - a["est_size"] / a["current_size"]) * 100
            est_str = f"[cyan]{_fmt_size(a['est_size'])}[/cyan]"
            red_str = _fmt_reduction(reduction)
            total_estimated += a["est_size"]
        else:
            est_str = red_str = "[yellow]?[/yellow]"
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
            f"[bold]{_fmt_reduction(reduction_total)}[/bold]",
        )

    console.print(Panel(
        table,
        title="[bold]Re-Encode Summary[/bold]",
        border_style="cyan",
    ))
    workers = min(DEFAULT_WORKERS, len(files))
    console.print(f"[dim]Encoder: {mode.label} · Speed: {speed.label} · Workers: {workers}[/dim]")

    # Optional 1-minute preview (1 min of output; input window = 60s × speed)
    first = analyses[0]
    input_needed = 60.0 * speed.multiplier
    if speed.multiplier != 1.0:
        preview_label = (
            f"1 min preview  "
            f"[dim](reads {_fmt_duration(input_needed)} of input → 1:00 of output)[/dim]"
        )
    else:
        preview_label = "1 min preview"

    want_preview = console.input(
        f"\nGenerate {preview_label} before proceeding? [bold][Y/n][/bold]: "
    ).strip().lower()

    if want_preview in ("", "y", "yes"):
        console.print(f"\nEncoding preview for [cyan]{first['path'].name}[/cyan] (middle segment)...")
        preview_ext = first["out_ext"] if first["out_ext"] else first["path"].suffix
        preview_path = _make_preview(
            first["path"], first["duration"],
            first["encode_flags"], preview_ext, speed.multiplier,
        )
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
    outputs = {
        a["path"]: _unique_output(a["path"], final_suffix, a["out_ext"] or None)
        for a in analyses
    }
    results: dict[Path, tuple[bool, str]] = {}
    lock = threading.Lock()

    def _out_dur(a: _Analysis) -> float:
        return (a["duration"] / speed.multiplier) if a["duration"] else 0.0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(),
        TimeElapsedColumn(), TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_ids = {
            a["path"]: progress.add_task(
                a["path"].name[:45],
                total=_out_dur(a) or None,
            )
            for a in analyses
        }

        def _run(a: _Analysis) -> None:
            path   = a["path"]
            output = outputs[path]
            dur    = _out_dur(a)
            try:
                encode_file(path, output, dur, progress, task_ids[path], a["encode_flags"])
                final_size = output.stat().st_size
                reduction  = (1 - final_size / a["current_size"]) * 100
                msg = (
                    f"[green]✓[/green] [cyan]{path.name}[/cyan]  "
                    f"{_fmt_size(a['current_size'])} → "
                    f"[cyan]{_fmt_size(final_size)}[/cyan]  "
                    f"({_fmt_reduction(reduction)})"
                )
                with lock:
                    results[path] = (True, msg)
            except RuntimeError as exc:
                with lock:
                    results[path] = (False, f"[red]✗ {path.name}:[/red] {exc}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: dict[Future[None], _Analysis] = {pool.submit(_run, a): a for a in analyses}
            for f in as_completed(futures):
                f.result()

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
