import random
import struct
import zlib
from pathlib import Path

from rich.console import Console
from rich.text import Text

try:
    import pyfiglet as _pyfiglet
except ImportError:
    _pyfiglet = None  # type: ignore[assignment]

_LOGO_PATH = Path(__file__).parent / "carpaccio.png"
_BARS  = "▁▂▃▄▅▆▇█"
_WAVE  = "▁▁▂▂▃▄▄▅▅▆▆▇▇██▇▇▆▆▅▅▄▄▃▃▂▂▁▁"
_FONTS = ["doom", "slant", "big", "standard", "small", "mini"]

Pixel = tuple[int, int, int, int]


# ---------------------------------------------------------------------------
# Pure-stdlib PNG reader + half-block terminal renderer
# ---------------------------------------------------------------------------

def _parse_png(data: bytes) -> tuple[int, int, list[Pixel]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "Not a PNG file"

    pos: int = 8
    ihdr: bytes | None = None
    idat_parts: list[bytes] = []

    while pos < len(data):
        length: int = struct.unpack(">I", data[pos : pos + 4])[0]
        tag:    bytes = data[pos + 4 : pos + 8]
        chunk:  bytes = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            ihdr = chunk
        elif tag == b"IDAT":
            idat_parts.append(chunk)
        elif tag == b"IEND":
            break

    assert ihdr is not None
    width, height = struct.unpack(">II", ihdr[:8])
    bit_depth: int = ihdr[8]
    color_type: int = ihdr[9]
    assert bit_depth == 8

    channels_map: dict[int, int] = {2: 3, 6: 4}
    channels = channels_map.get(color_type)
    assert channels is not None, f"Unsupported PNG color type {color_type}"

    raw:    bytes     = zlib.decompress(b"".join(idat_parts))
    stride: int       = width * channels
    pixels: list[Pixel] = []
    prev:   bytes     = bytes(stride)

    for y in range(height):
        filt: int = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])

        if filt == 1:
            for x in range(channels, stride):
                row[x] = (row[x] + row[x - channels]) & 0xFF
        elif filt == 2:
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 0xFF
        elif filt == 3:
            for x in range(stride):
                a: int = row[x - channels] if x >= channels else 0
                row[x] = (row[x] + (a + prev[x]) // 2) & 0xFF
        elif filt == 4:
            for x in range(stride):
                a = row[x - channels] if x >= channels else 0
                b: int = prev[x]
                c: int = prev[x - channels] if x >= channels else 0
                p: int = a + b - c
                pr: int = a if abs(p - a) <= abs(p - b) and abs(p - a) <= abs(p - c) else (b if abs(p - b) <= abs(p - c) else c)
                row[x] = (row[x] + pr) & 0xFF

        prev = bytes(row)
        for x in range(width):
            o = x * channels
            r, g, b2 = row[o], row[o + 1], row[o + 2]
            alpha: int = row[o + 3] if channels == 4 else 255
            pixels.append((r, g, b2, alpha))

    return width, height, pixels


def _sample(
    pixels: list[Pixel],
    sw: int, sh: int,
    dx: int, dy: int,
    dw: int, dh: int,
) -> Pixel:
    sx = min(int(dx / dw * sw), sw - 1)
    sy = min(int(dy / dh * sh), sh - 1)
    return pixels[sy * sw + sx]


def _render_png_halfblock(path: Path, term_width: int = 80) -> str | None:
    try:
        data = path.read_bytes()
        w, h, pixels = _parse_png(data)
    except Exception:
        return None

    dw = term_width
    dh = max(1, int(h / w * dw * 0.5))
    lines: list[str] = []

    for row in range(dh):
        parts: list[str] = []
        for col in range(dw):
            tr, tg, tb, ta = _sample(pixels, w, h, col, row * 2,     dw, dh * 2)
            br, bg, bb, ba = _sample(pixels, w, h, col, row * 2 + 1, dw, dh * 2)
            if ta < 128: tr, tg, tb = 0, 0, 0
            if ba < 128: br, bg, bb = 0, 0, 0
            parts.append(f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bg};{bb}m▀")
        lines.append("".join(parts) + "\x1b[0m")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ASCII logo helpers
# ---------------------------------------------------------------------------

def _eq_row(n: int, colors: list[str], min_h: int = 1, max_h: int = 7) -> str:
    parts: list[str] = []
    for i in range(n):
        h = random.randint(min_h, max_h)
        c = colors[i % len(colors)]
        parts.append(f"[{c}]{_BARS[h]}[/{c}]")
    return "".join(parts)


def _waveform(width: int) -> str:
    parts: list[str] = []
    for i in range(width):
        ch = _WAVE[i % len(_WAVE)]
        c = "bright_magenta" if i % 2 == 0 else "magenta"
        parts.append(f"[{c}]{ch}[/{c}]")
    return "".join(parts)


def _figlet_text(term_width: int) -> list[str]:
    if _pyfiglet is None:
        return ["  Carpaccio"]
    for font in _FONTS:
        try:
            rendered: str = _pyfiglet.figlet_format("Carpaccio", font=font)
            lines = [ln for ln in rendered.splitlines() if ln.strip()]
            if lines and max(len(ln) for ln in lines) <= term_width:
                return lines
        except Exception:
            continue
    return ["  Carpaccio"]


def _center(text: str, width: int) -> str:
    """Return spaces to left-pad a string of given visual width to center it."""
    return " " * max(0, (width - len(text)) // 2)


def _print_ascii(console: Console) -> None:
    W = console.width or 100

    # --- equalizer + play button -------------------------------------------------
    # Cap bars so the section never exceeds ~90 chars total, then center it.
    box_w  = 19       # visual width of "╔ ─ ─ ─ ─ ─ ─ ─ ╗"
    n_bars = min(22, max(6, (min(W, 100) - box_w - 8) // 2))
    eq_section_w = n_bars + 4 + box_w + 4 + n_bars
    eq_pad = " " * max(0, (W - eq_section_w) // 2)

    lc = ["bright_cyan", "cyan", "magenta"]
    rc = ["bright_magenta", "magenta", "bright_cyan"]

    eq_l = [_eq_row(n_bars, lc, 1, 3), _eq_row(n_bars, lc, 3, 6), _eq_row(n_bars, lc, 6, 7)]
    eq_r = [_eq_row(n_bars, rc, 1, 3), _eq_row(n_bars, rc, 3, 6), _eq_row(n_bars, rc, 6, 7)]

    box = [
        "[bright_cyan]╔ ─ ─ ─ ─ ─ ─ ─ ╗[/bright_cyan]",
        "[bright_cyan]║[/bright_cyan]  [bright_magenta]    [bold]▷[/bold]    [/bright_magenta]  [bright_cyan]║[/bright_cyan]",
        "[bright_cyan]╚ ─ ─ ─ ─ ─ ─ ─ ╝[/bright_cyan]",
    ]

    console.print()
    for i in range(3):
        console.print(f"{eq_pad}{eq_l[i]}  {box[i]}  {eq_r[i]}")

    # --- title -------------------------------------------------------------------
    # Limit figlet to 80 chars so it stays legible, then center each line.
    title_max_w = min(W, 90)
    console.print()
    for line in _figlet_text(title_max_w):
        pad = " " * max(0, (W - len(line)) // 2)
        t = Text(pad + line)
        t.stylize("bold bright_cyan")
        for idx, ch in enumerate(pad + line):
            if ch not in (" ", "\t") and idx % 3 == 1:
                t.stylize("bright_magenta", idx, idx + 1)
        console.print(t)

    # --- waveform ----------------------------------------------------------------
    wf_w  = min(46, W - 12)
    wf    = _waveform(wf_w)
    wf_visual_w = wf_w + 8          # "─ ─ " + waveform + " ─ ─"
    wf_pad = " " * max(0, (W - wf_visual_w) // 2)
    console.print()
    console.print(f"{wf_pad}[bright_cyan]─ ─[/bright_cyan] {wf} [bright_cyan]─ ─[/bright_cyan]")

    # --- controls ----------------------------------------------------------------
    ctrl  = "<<  ║  ▷  >>"
    dashes = "·─·─·─·─·─·─·─·"
    ctrl_line = f"{dashes}  {ctrl}  {dashes}"
    ctrl_pad = " " * max(0, (W - len(ctrl_line)) // 2)
    console.print(
        f"\n{ctrl_pad}"
        f"[bright_cyan]{dashes}[/bright_cyan]  "
        "[bright_cyan]<<[/bright_cyan]  "
        "[bright_cyan]║[/bright_cyan]  "
        "[bright_magenta bold]▷[/bright_magenta bold]  "
        "[bright_magenta]>>[/bright_magenta]  "
        f"[bright_cyan]{dashes}[/bright_cyan]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def print_logo(console: Console) -> None:
    _print_ascii(console)
