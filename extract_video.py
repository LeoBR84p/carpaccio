#!/usr/bin/env python3
"""
Extrai URLs de vídeo de HTML e gera comandos ffmpeg.

Uso:
    python extract_video.py              # lê da área de transferência (pyperclip)
    python extract_video.py arquivo.html # lê de um arquivo
    python extract_video.py -            # lê do stdin (ctrl+z para encerrar no Windows)
"""

import json
import re
import subprocess
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

FFMPEG = r".\ffmpeg\bin\ffmpeg.exe"


def _json_unescape(s: str) -> str:
    s = s.replace("\\/", "/")
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)


def extract_sources(html: str) -> list[dict[str, str]]:
    html = unescape(html)
    sources: list[dict[str, str]] = []

    # <source ... src="..." label="...">
    for m in re.finditer(r'<source[^>]+src="([^"]+)"[^>]*label="([^"]+)"', html):
        sources.append({"url": m.group(1), "label": m.group(2)})

    # <video ... src="..."> — skip blob: (browser-only), fallback para src direto
    if not sources:
        m = re.search(r'<video[^>]+src="([^"]+)"', html)
        if m and not m.group(1).startswith("blob:"):
            sources.append({"url": m.group(1), "label": "720p"})

    # og:video meta tag (Instagram, Facebook, etc.)
    if not sources:
        for pat in [
            r'<meta[^>]+property="og:video"[^>]+content="([^"]+)"',
            r'<meta[^>]+content="([^"]+)"[^>]+property="og:video"',
        ]:
            m = re.search(pat, html)
            if m:
                sources.append({"url": m.group(1), "label": "original"})
                break

    # Instagram: "video_versions":[{"width":W,"height":H,"url":"..."}]
    #        or: "video_versions":[{"type":N,"url":"..."}]
    if not sources:
        m = re.search(r'"video_versions"\s*:\s*\[', html)
        if m:
            block = html[m.end():m.end() + 5000]
            seen_urls: set[str] = set()
            # width/height format (label = height)
            for entry in re.finditer(r'\{"width":(\d+),"height":(\d+),"url":"([^"]+)"', block):
                url = _json_unescape(entry.group(3))
                if url not in seen_urls:
                    seen_urls.add(url)
                    sources.append({"url": url, "label": entry.group(2)})
            # type format (label = "original"; dedup collapses identical URLs)
            if not sources:
                for entry in re.finditer(r'"type":\d+,"url":"([^"]+)"', block):
                    url = _json_unescape(entry.group(1))
                    if url not in seen_urls:
                        seen_urls.add(url)
                        sources.append({"url": url, "label": "original"})

    # Instagram: "video_url":"..." in embedded JSON (older format)
    if not sources:
        m = re.search(r'"video_url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', html)
        if m:
            sources.append({"url": _json_unescape(m.group(1)), "label": "original"})

    return sources


YOUTUBE_RE = re.compile(
    r"^https?://(?:www\.)?(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)([A-Za-z0-9_-]{11})"
)

URL_RE = re.compile(r"^https?://\S+$")


def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_RE.match(text.strip()))


def is_plain_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


def extract_sources_ytdlp(url: str) -> tuple[list[dict[str, str]], str]:
    """Retorna (sources, safe_title) para qualquer URL suportada pelo yt-dlp."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-playlist", "-j", url],
            capture_output=True, text=True, check=True,
        )
        info = json.loads(result.stdout)
    except FileNotFoundError:
        print("yt-dlp não encontrado. Instale com: uv add yt-dlp", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"yt-dlp falhou:\n{e.stderr}", file=sys.stderr)
        sys.exit(1)

    title = info.get("title") or info.get("id", "video")
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)

    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for fmt in info.get("formats", []):
        if fmt.get("vcodec", "none") == "none":
            continue
        height = fmt.get("height")
        if not height:
            continue
        label = str(height)
        if label in seen:
            continue
        seen.add(label)
        sources.append({"url": fmt["url"], "label": label})

    # fallback: formato único sem lista de formats (e.g. direct stream)
    if not sources and info.get("url"):
        height = info.get("height")
        label = str(height) if height else "original"
        sources.append({"url": info["url"], "label": label})

    return sources, safe_title


def filename_from_url(url: str, label: str) -> str:
    path = urlparse(url).path          # /files/7/7b/.../0hkh..._720p.mp4
    stem = Path(path).stem             # 0hkh..._720p
    # normaliza sufixo
    base = re.sub(r"_(240p|480p|720p|1080p|source)$", "", stem)
    suffix = "_source" if "source" in label else f"_{label}p" if label.isdigit() else f"_{label}"
    return f"{base}{suffix}.mp4"


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    elif len(sys.argv) > 1 and sys.argv[1] == "-":
        raw = sys.stdin.read()
    else:
        try:
            import pyperclip
            raw = pyperclip.paste()
            print("[área de transferência]")
        except ImportError:
            print("pyperclip não instalado. Cole o HTML/URL abaixo e pressione Ctrl+Z + Enter:")
            raw = sys.stdin.read()

    # URL direta: usa yt-dlp para qualquer site suportado
    if is_plain_url(raw.strip()):
        sources, safe_title = extract_sources_ytdlp(raw.strip())
        if not sources:
            print("Nenhum formato de vídeo encontrado via yt-dlp.")
            sys.exit(1)
        print(f"\nEncontradas {len(sources)} qualidades para '{safe_title}':\n")
        for s in sources:
            label = s["label"]
            suffix = f"_{label}p" if label.isdigit() else f"_{label}"
            out = f"{safe_title}{suffix}.mp4"
            print(f"  [{label}]  {out}")
            print(f'  {FFMPEG} -i "{s["url"]}" -c copy "{out}"')
            print()
        return

    sources = extract_sources(raw)
    if not sources:
        print("Nenhuma fonte de vídeo encontrada no HTML.")
        sys.exit(1)

    print(f"\nEncontradas {len(sources)} qualidades:\n")
    for s in sources:
        out = filename_from_url(s["url"], s["label"])
        print(f"  [{s['label']}]  {out}")
        print(f'  {FFMPEG} -i "{s["url"]}" -c copy {out}')
        print()


if __name__ == "__main__":
    main()
