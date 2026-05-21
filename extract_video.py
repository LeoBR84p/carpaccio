#!/usr/bin/env python3
"""
Extrai URLs de vídeo de HTML e gera comandos ffmpeg.

Uso:
    python extract_video.py              # lê da área de transferência (pyperclip)
    python extract_video.py arquivo.html # lê de um arquivo
    python extract_video.py -            # lê do stdin (ctrl+z para encerrar no Windows)
"""

import re
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


def filename_from_url(url: str, label: str) -> str:
    path = urlparse(url).path          # /files/7/7b/.../0hkh..._720p.mp4
    stem = Path(path).stem             # 0hkh..._720p
    # normaliza sufixo
    base = re.sub(r"_(240p|480p|720p|1080p|source)$", "", stem)
    suffix = "_source" if "source" in label else f"_{label}p" if label.isdigit() else f"_{label}"
    return f"{base}{suffix}.mp4"


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        html = Path(sys.argv[1]).read_text(encoding="utf-8")
    elif len(sys.argv) > 1 and sys.argv[1] == "-":
        html = sys.stdin.read()
    else:
        try:
            import pyperclip
            html = pyperclip.paste()
            print("[área de transferência]")
        except ImportError:
            print("pyperclip não instalado. Cole o HTML abaixo e pressione Ctrl+Z + Enter:")
            html = sys.stdin.read()

    sources = extract_sources(html)
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
