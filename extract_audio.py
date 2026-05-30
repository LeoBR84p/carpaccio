#!/usr/bin/env python3
"""
Extrai URLs de áudio de HTML e gera comandos ffmpeg.

Uso:
    python extract_audio.py              # lê da área de transferência (pyperclip)
    python extract_audio.py arquivo.html # lê de um arquivo
    python extract_audio.py -            # lê do stdin (ctrl+z para encerrar no Windows)
"""

import json
import re
import subprocess
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
import urllib.request

FFMPEG = r".\ffmpeg\bin\ffmpeg.exe"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

_BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_AUDIO_EXTS = r"mp3|aac|m4a|ogg|oga|opus|flac|wav|wma|m4b|aiff"
_AUDIO_EXT_RE = re.compile(rf"\.({_AUDIO_EXTS})(?:[?#]|$)", re.IGNORECASE)

# Preferred output extension per source extension
_EXT_MAP = {
    "mp3": "mp3", "aac": "aac", "m4a": "m4a", "ogg": "ogg", "oga": "ogg",
    "opus": "opus", "flac": "flac", "wav": "wav", "wma": "wma",
    "m4b": "m4a", "aiff": "flac",
}


def _out_ext(url: str) -> str:
    m = _AUDIO_EXT_RE.search(urlparse(url).path)
    if m:
        return _EXT_MAP.get(m.group(1).lower(), "mp3")
    return "mp3"


def _ffmpeg_cmd(source: dict[str, str], out: str) -> str:
    ref = source.get("referer")
    if ref:
        headers = f"Referer: {ref}\\r\\nUser-Agent: {_UA}\\r\\n"
        return f'{FFMPEG} -headers "{headers}" -i "{source["url"]}" -vn -c:a copy "{out}"'
    return f'{FFMPEG} -i "{source["url"]}" -vn -c:a copy "{out}"'


def _json_unescape(s: str) -> str:
    s = s.replace("\\/", "/")
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)


def extract_sources(html: str) -> list[dict[str, str]]:
    html = unescape(html)
    sources: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(url: str, label: str, **extra: str) -> None:
        url = _json_unescape(url)
        if url and url not in seen:
            seen.add(url)
            sources.append({"url": url, "label": label, **extra})

    # <source src="..." type="audio/..."> inside <audio>
    for m in re.finditer(
        r'<source[^>]+src=["\']([^"\']+)["\'][^>]*type=["\']audio/([^"\']+)["\']', html
    ):
        _add(m.group(1), m.group(2).split(";")[0])

    # <source src="..."> pointing to audio extension
    for m in re.finditer(r'<source[^>]+src=["\']([^"\']+)["\']', html):
        if _AUDIO_EXT_RE.search(m.group(1)):
            _add(m.group(1), "original")

    # <audio src="...">
    if not sources:
        m = re.search(r'<audio[^>]+src=["\']([^"\']+)["\']', html)
        if m:
            _add(m.group(1), "original")

    # og:audio / og:audio:url meta tags
    if not sources:
        for pat in [
            r'<meta[^>]+property=["\']og:audio(?::url)?["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:audio(?::url)?["\']',
        ]:
            m = re.search(pat, html)
            if m:
                _add(m.group(1), "original")
                break

    # JSON keys: "audio_url", "audio", "audioUrl", "stream_url", "preview_url"
    if not sources:
        _url_pat = rf'https?://[^"\']+\.(?:{_AUDIO_EXTS})(?:\?[^"\']*)?'
        for key in ("audio_url", "audioUrl", "stream_url", "preview_url", "audio"):
            for m in re.finditer(rf'["\']?{key}["\']?\s*:\s*["\']({_url_pat})["\']', html):
                _add(m.group(1), "original")

    # JWPlayer / Playerjs: file:"url.mp3"
    if not sources:
        _f = rf'https?://[^"\']+\.(?:{_AUDIO_EXTS})(?:\?[^"\']*)?'
        for pat in [
            rf'"file"\s*:\s*"({_f})"',
            rf'"file"\s*:\s*\'({_f})\'',
            rf"'file'\s*:\s*'({_f})'",
            rf'"src"\s*:\s*"({_f})"',
            rf'"url"\s*:\s*"({_f})"',
        ]:
            for m in re.finditer(pat, html):
                _add(m.group(1), "original")

    # Brute-force: any URL pointing to an audio file
    if not sources:
        for m in re.finditer(
            rf'(https?://[^\s"\'\\<>{{}}\[\]]+\.(?:{_AUDIO_EXTS})(?:\?[^\s"\'\\<>{{}}\[\]]*)?)',
            html,
        ):
            url = m.group(1).rstrip(".,;)")
            _add(url, "original")

    return sources


class YtdlpError(Exception):
    pass


def extract_sources_ytdlp(url: str) -> tuple[list[dict[str, str]], str]:
    """Retorna (sources, safe_title) extraindo formatos de áudio via yt-dlp."""
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
        raise YtdlpError(e.stderr)

    title = info.get("title") or info.get("id", "audio")
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)

    sources: list[dict[str, str]] = []
    seen: set[str] = set()

    # Prefer audio-only formats (vcodec == "none", acodec != "none")
    audio_fmts = [
        f for f in info.get("formats", [])
        if f.get("vcodec", "none") == "none" and f.get("acodec", "none") != "none"
    ]

    # Sort by bitrate descending so highest quality comes first
    audio_fmts.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)

    for fmt in audio_fmts:
        acodec = fmt.get("acodec", "")
        abr = fmt.get("abr") or fmt.get("tbr")
        label = f"{int(abr)}k" if abr else (acodec or "original")
        if label in seen:
            continue
        seen.add(label)
        s: dict[str, str] = {"url": fmt["url"], "label": label}
        ref = (fmt.get("http_headers") or {}).get("Referer")
        if ref:
            s["referer"] = ref
        sources.append(s)

    # Fallback: direct URL with no format list
    if not sources and info.get("url"):
        abr = info.get("abr")
        label = f"{int(abr)}k" if abr else "original"
        ref = (info.get("http_headers") or {}).get("Referer")
        s = {"url": info["url"], "label": label}
        if ref:
            s["referer"] = ref
        sources.append(s)

    if sources and not any(s.get("referer") for s in sources):
        for s in sources:
            s["referer"] = url

    return sources, safe_title


def fetch_page_html(url: str, referer: str | None = None) -> str:
    headers = dict(_BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def filename_from_url(url: str, label: str) -> str:
    path = urlparse(url).path
    stem = Path(path).stem
    ext = _out_ext(url)
    suffix = "" if label in ("original", "") else f"_{label}"
    return f"{stem}{suffix}.{ext}"


SPOTIFY_RE = re.compile(r"^https?://open\.spotify\.com/")
URL_RE = re.compile(r"^https?://\S+$")


def is_plain_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


_SPOTIFY_UA = "Mozilla/5.0"


def _spotify_meta(url: str) -> tuple[str, str]:
    """Return (artist, title) from Spotify page og meta tags."""
    req = urllib.request.Request(url, headers={"User-Agent": _SPOTIFY_UA})
    html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")

    title = ""
    for pat in [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    ]:
        m = re.search(pat, html)
        if m:
            title = unescape(m.group(1))
            break

    artist = ""
    for pat in [
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
    ]:
        m = re.search(pat, html)
        if m:
            # "Artist · Title · Song · Year"
            artist = unescape(m.group(1)).split("·")[0].strip()
            break

    return artist, title


def _spotify_download(url: str) -> None:
    """Download Spotify track by searching YouTube with yt-dlp, then exit."""
    print(f"[spotify] Buscando metadados: {url}")
    try:
        artist, title = _spotify_meta(url)
    except Exception as e:
        print(f"Erro ao buscar metadados do Spotify: {e}", file=sys.stderr)
        sys.exit(1)

    if not title:
        print("Não foi possível extrair título da página do Spotify.", file=sys.stderr)
        sys.exit(1)

    query = f"{artist} - {title}" if artist else title
    safe = re.sub(r'[\\/*?:"<>|]', "_", query)
    out = f"{safe}.mp3"
    print(f"[spotify] Pesquisando no YouTube: {query}")
    print(f"[spotify] Salvando em: {out}")

    result = subprocess.run([
        "yt-dlp",
        f"ytsearch1:{query}",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--ffmpeg-location", str(Path(__file__).parent / "ffmpeg" / "bin"),
        "-o", out,
        "--no-playlist",
    ])
    sys.exit(result.returncode)


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

    if is_plain_url(raw.strip()):
        url = raw.strip()
        sources: list[dict[str, str]] = []
        safe_title = ""

        # Spotify: DRM protegido — usa spotdl diretamente
        if SPOTIFY_RE.match(url):
            _spotify_download(url)

        # Direct audio file URL — no need for yt-dlp
        if _AUDIO_EXT_RE.search(urlparse(url).path):
            ext = _out_ext(url)
            stem = Path(urlparse(url).path).stem
            out = f"{stem}.{ext}"
            print(f'\n  {FFMPEG} -i "{url}" -vn -c:a copy "{out}"')
            return

        try:
            sources, safe_title = extract_sources_ytdlp(url)
        except YtdlpError:
            print("[yt-dlp não suporta esta URL diretamente, buscando HTML...]")
            try:
                page_html = fetch_page_html(url)
            except Exception as e:
                print(f"Erro ao buscar a página: {e}", file=sys.stderr)
                sys.exit(1)
            sources = extract_sources(page_html)
            if not sources:
                print("Nenhuma fonte de áudio encontrada na página.")
                sys.exit(1)
            _title_m = re.search(r"<title>([^<]+)</title>", page_html)
            safe_title = re.sub(r'[\\/*?:"<>|]', "_",
                _title_m.group(1).split(" - ")[0].strip() if _title_m else "audio"
            )

        if not sources:
            print("Nenhum formato de áudio encontrado.")
            sys.exit(1)

        print(f"\nEncontradas {len(sources)} qualidades para '{safe_title}':\n")
        for s in sources:
            label = s["label"]
            ext = _out_ext(s["url"])
            out = f"{safe_title}_{label}.{ext}" if label != "original" else f"{safe_title}.{ext}"
            print(f"  [{label}]  {out}")
            print(f"  {_ffmpeg_cmd(s, out)}")
            print()
        return

    # HTML input
    sources = extract_sources(raw)
    if not sources:
        print("Nenhuma fonte de áudio encontrada no HTML.")
        sys.exit(1)

    print(f"\nEncontradas {len(sources)} qualidades:\n")
    for s in sources:
        out = filename_from_url(s["url"], s["label"])
        print(f"  [{s['label']}]  {out}")
        print(f"  {_ffmpeg_cmd(s, out)}")
        print()


if __name__ == "__main__":
    main()
