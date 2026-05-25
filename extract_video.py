#!/usr/bin/env python3
"""
Extrai URLs de vídeo de HTML e gera comandos ffmpeg.

Uso:
    python extract_video.py              # lê da área de transferência (pyperclip)
    python extract_video.py arquivo.html # lê de um arquivo
    python extract_video.py -            # lê do stdin (ctrl+z para encerrar no Windows)
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

FFMPEG = r".\ffmpeg\bin\ffmpeg.exe"
_CF_CACHE = Path(__file__).parent / ".cf_cookies.json"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


def _ffmpeg_cmd(source: dict[str, str], out: str) -> str:
    """Build ffmpeg command, adding -headers for sources that require Referer."""
    ref = source.get("referer")
    if ref:
        headers = f"Referer: {ref}\\r\\nUser-Agent: {_UA}\\r\\n"
        return f'{FFMPEG} -headers "{headers}" -i "{source["url"]}" -c copy "{out}"'
    return f'{FFMPEG} -i "{source["url"]}" -c copy "{out}"'


def _get_cf_clearance(domain: str) -> str | None:
    """Get Cloudflare cf_clearance cookie: cache → env var → Chrome/Firefox via yt-dlp."""
    # 1. env var override
    env_val = os.environ.get("CF_CLEARANCE")
    if env_val:
        return env_val

    # 2. local cache file
    if _CF_CACHE.exists():
        try:
            cache = json.loads(_CF_CACHE.read_text())
            entry = cache.get(domain, {})
            if entry.get("expires", 0) > time.time():
                return entry["value"]
        except Exception:
            pass

    # 3. extract from browser via yt-dlp
    tmp = tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False)
    tmp.close()
    try:
        for browser in ("chrome", "firefox", "edge", "chromium"):
            try:
                subprocess.run(
                    ["yt-dlp", "--cookies-from-browser", browser,
                     "--cookies", tmp.name, "--simulate", "--ignore-errors",
                     f"https://{domain}/"],
                    capture_output=True, text=True, timeout=20,
                )
                with open(tmp.name) as f:
                    for line in f:
                        parts = line.strip().split("\t")
                        if len(parts) >= 7 and parts[5] == "cf_clearance" and domain in parts[0]:
                            value = parts[6]
                            # cache it
                            try:
                                cache = json.loads(_CF_CACHE.read_text()) if _CF_CACHE.exists() else {}
                                cache[domain] = {"value": value, "expires": time.time() + 86400 * 365}
                                _CF_CACHE.write_text(json.dumps(cache))
                            except Exception:
                                pass
                            return value
            except Exception:
                continue
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    return None


def _doodstream_extract(embed_url: str) -> list[dict[str, str]]:
    """DoodStream / playmogo pass_md5 extraction flow."""
    domain = urlparse(embed_url).netloc
    cf = _get_cf_clearance(domain)
    if not cf:
        print(
            f"[doodstream] Sem cookie cf_clearance para {domain}.\n"
            f"  Abra {embed_url} no Chrome, copie o cf_clearance de DevTools > Application > Cookies\n"
            f"  e defina: set CF_CLEARANCE=<valor>",
            file=sys.stderr,
        )
        return []

    def _fetch(url: str, referer: str | None = None, xhr: bool = False) -> str:
        h = {**_BROWSER_HEADERS, "Cookie": f"lang=1; cf_clearance={cf}"}
        if referer:
            h["Referer"] = referer
        if xhr:
            h["X-Requested-With"] = "XMLHttpRequest"
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")

    try:
        html = _fetch(embed_url)
    except Exception as e:
        print(f"[doodstream] Erro ao buscar embed: {e}", file=sys.stderr)
        return []

    m = re.search(r"(/pass_md5/[^\s'\"<>]{10,200})", html)
    if not m:
        return []

    pass_md5_path = m.group(1)
    token = pass_md5_path.split("/")[-1]
    # expiry is the 4th numeric segment in the path
    exp_m = re.search(r"/pass_md5/[^-]+-[^-]+-[^-]+-(\d+)-", pass_md5_path)
    expiry = exp_m.group(1) if exp_m else str(int(time.time()) + 3600)

    base_url = f"https://{domain}"
    try:
        cdn_base = _fetch(base_url + pass_md5_path, referer=embed_url, xhr=True).strip()
    except Exception as e:
        print(f"[doodstream] Erro ao buscar pass_md5: {e}", file=sys.stderr)
        return []

    if not cdn_base.startswith("http"):
        try:
            cdn_base = json.loads(cdn_base).get("result", "")
        except Exception:
            return []

    final_url = cdn_base + token + "?expiry=" + expiry
    return [{"url": final_url, "label": "original", "referer": f"https://{domain}/"}]


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

    # JWPlayer / Playerjs / videojs: file:"url.mp4" or src:"url.m3u8" in JS
    if not sources:
        _vext = r'https?://[^"\']+\.(?:mp4|m3u8|webm|mkv)(?:\?[^"\']*)?'
        seen: set[str] = set()
        for pat in [
            rf'"file"\s*:\s*"({_vext})"',
            rf'"file"\s*:\s*\'({_vext})\'',
            rf"'file'\s*:\s*'({_vext})'",
            rf'"src"\s*:\s*"({_vext})"',
            rf'"hls"\s*:\s*"({_vext})"',
        ]:
            for m in re.finditer(pat, html):
                url = _json_unescape(m.group(1))
                if url not in seen:
                    seen.add(url)
                    sources.append({"url": url, "label": "original"})

    # KT Player / cnnamador-style flashvars: video_url_hd / video_url
    if not sources:
        for label, key in [("hd", "video_url_hd"), ("original", "video_url")]:
            m = re.search(rf"['\"]?{key}['\"]?\s*:\s*'(https?://[^']+)'", html)
            if m:
                sources.append({"url": m.group(1), "label": label})

    # base64 encoded URLs: atob("...")
    if not sources:
        for m in re.finditer(r'atob\s*\(\s*["\']([A-Za-z0-9+/=]{20,})["\']\s*\)', html):
            try:
                decoded = base64.b64decode(m.group(1) + "==").decode("utf-8", errors="replace")
                for vm in re.finditer(r'https?://\S+\.(?:mp4|m3u8|webm)', decoded):
                    sources.append({"url": vm.group(0), "label": "original"})
            except Exception:
                pass

    # Brute-force: any https URL pointing to a video file anywhere in the page
    if not sources:
        seen_bf: set[str] = set()
        for m in re.finditer(
            r'(https?://[^\s"\'\\<>{}\[\]]+\.(?:mp4|m3u8|webm|mkv)(?:\?[^\s"\'\\<>{}\[\]]*)?)',
            html,
        ):
            url = m.group(1).rstrip(".,;)")
            if url not in seen_bf:
                seen_bf.add(url)
                sources.append({"url": url, "label": "original"})

    return sources


EMBED_DOMAINS = re.compile(
    r"https?://(?:"
    r"playmogo\.com"
    r"|streamtape\.com"
    r"|doodstream\.com"
    r"|filemoon\.sx"
    r"|mixdrop\.co"
    r"|vidmoly\.to"
    r"|voe\.sx"
    r"|upstream\.to"
    r"|vtube\.to"
    r"|speedvid\.net"
    r"|supervideo\.tv"
    r")[^\s\"']+"
)


def _attr(name: str, value: str | None = None) -> str:
    """Regex fragment matching an HTML attribute with single or double quotes."""
    q = r'["\']'
    if value is None:
        return rf'{name}={q}([^"\']+){q}'
    return rf'{name}={q}{re.escape(value)}{q}'


def extract_embed_info(html: str) -> tuple[str | None, str | None]:
    """Return (embed_url, title) found in page HTML, or (None, None)."""
    html = unescape(html)
    embed_url: str | None = None

    # schema.org VideoObject itemprop="embedURL" — both attribute orderings, both quote styles
    for pat in [
        rf'<meta[^>]+{_attr("itemprop","embedURL")}[^>]+{_attr("content")}',
        rf'<meta[^>]+{_attr("content")}[^>]+{_attr("itemprop","embedURL")}',
    ]:
        m = re.search(pat, html)
        if m:
            embed_url = m.group(1)
            break

    # fallback: any iframe src matching a known embed domain
    if not embed_url:
        m = re.search(rf'<iframe[^>]+src=["\']({EMBED_DOMAINS.pattern})["\']', html)
        if m:
            embed_url = m.group(1)

    # last resort: any iframe src with a URL (catches unknown embed hosts)
    if not embed_url:
        m = re.search(r'<iframe[^>]+src=["\']?(https?://[^\s"\'<>]+)["\']?', html)
        if m:
            embed_url = m.group(1)

    title: str | None = None

    # itemprop="name" — both orderings
    for pat in [
        rf'<meta[^>]+{_attr("itemprop","name")}[^>]+{_attr("content")}',
        rf'<meta[^>]+{_attr("content")}[^>]+{_attr("itemprop","name")}',
    ]:
        m = re.search(pat, html)
        if m:
            title = m.group(1)
            break

    # fallback: og:title
    if not title:
        for pat in [
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        ]:
            m = re.search(pat, html)
            if m:
                title = m.group(1)
                break

    # fallback: <title> tag (strip " - SiteName" suffix)
    if not title:
        m = re.search(r"<title>([^<]+)</title>", html)
        if m:
            title = m.group(1).split(" - ")[0].strip()

    return embed_url, title


YOUTUBE_RE = re.compile(
    r"^https?://(?:www\.)?(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)([A-Za-z0-9_-]{11})"
)

URL_RE = re.compile(r"^https?://\S+$")


def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_RE.match(text.strip()))


def is_plain_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


class YtdlpError(Exception):
    pass


REDGIFS_API = "https://api.redgifs.com/v2"


def _redgifs_token() -> str:
    req = urllib.request.Request(
        f"{REDGIFS_API}/auth/temporary",
        headers={"User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["token"]


def _redgifs_request(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{REDGIFS_API}{path}",
        headers={"User-Agent": _UA, "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _redgifs_gif_sources(gif: dict) -> list[dict[str, str]]:
    urls = gif.get("urls", {})
    sources = []
    for label, key in [("hd", "hd"), ("sd", "sd")]:
        u = urls.get(key)
        if u:
            sources.append({"url": u, "label": label, "referer": "https://www.redgifs.com/"})
    return sources


def _redgifs_extract(url: str) -> tuple[list[dict[str, str]], str, bool] | None:
    """Returns (sources, title, is_user_page) or None on failure.

    is_user_page=True: each source is a different video (label = gif_id).
    is_user_page=False: sources are quality variants of a single video.
    """
    path = urlparse(url).path

    try:
        token = _redgifs_token()
    except Exception as e:
        print(f"[redgifs] Erro ao obter token: {e}", file=sys.stderr)
        return None

    m = re.match(r"^/watch/([A-Za-z0-9_-]+)", path)
    if m:
        gif_id = m.group(1)
        try:
            data = _redgifs_request(f"/gifs/{gif_id}", token)
            gif = data.get("gif", {})
            sources = _redgifs_gif_sources(gif)
            title = re.sub(r'[\\/*?:"<>|]', "_", gif.get("id", gif_id))
            return sources, title, False
        except Exception as e:
            print(f"[redgifs] Erro ao buscar gif: {e}", file=sys.stderr)
            return None

    m = re.match(r"^/users/([A-Za-z0-9_-]+)", path)
    if m:
        username = m.group(1)
        print(f"[redgifs] Buscando vídeos de '{username}'...")
        all_sources: list[dict[str, str]] = []
        page = 1
        try:
            while True:
                data = _redgifs_request(
                    f"/users/{username}/search?order=new&count=80&page={page}", token
                )
                gifs = data.get("gifs") or []
                for gif in gifs:
                    gif_id = gif.get("id", "")
                    urls_map = gif.get("urls", {})
                    u = urls_map.get("hd") or urls_map.get("sd")
                    if u and gif_id:
                        all_sources.append({
                            "url": u,
                            "label": gif_id,
                            "referer": "https://www.redgifs.com/",
                        })
                if page >= (data.get("pages") or 1):
                    break
                page += 1
        except Exception as e:
            print(f"[redgifs] Erro ao buscar usuário: {e}", file=sys.stderr)
            return None
        return all_sources, username, True

    return None


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def fetch_page_html(url: str, referer: str | None = None) -> str:
    headers = dict(_BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _resolve_embed(
    embed_url: str, page_title: str | None, referer: str | None = None
) -> tuple[list[dict[str, str]], str]:
    """Try yt-dlp on embed_url; if unsupported, scrape the embed page directly."""
    try:
        sources, ytdlp_title = extract_sources_ytdlp(embed_url)
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", page_title or ytdlp_title)
        return sources, safe_title
    except YtdlpError:
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", page_title or "video")

        # DoodStream / playmogo pass_md5 protocol
        print("[tentando extração DoodStream...]")
        sources = _doodstream_extract(embed_url)
        if sources:
            return sources, safe_title

        # Generic: fetch embed page HTML and parse
        print("[buscando HTML do embed...]")
        try:
            embed_html = fetch_page_html(embed_url, referer=referer)
        except Exception as e:
            print(f"Erro ao buscar embed: {e}", file=sys.stderr)
            sys.exit(1)
        sources = extract_sources(embed_html)
        return sources, safe_title


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
        raise YtdlpError(e.stderr)

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
        s: dict[str, str] = {"url": fmt["url"], "label": label}
        ref = (fmt.get("http_headers") or {}).get("Referer")
        if ref:
            s["referer"] = ref
        sources.append(s)

    # fallback: formato único sem lista de formats (e.g. direct stream)
    if not sources and info.get("url"):
        height = info.get("height")
        label = str(height) if height else "original"
        ref = (info.get("http_headers") or {}).get("Referer")
        s = {"url": info["url"], "label": label}
        if ref:
            s["referer"] = ref
        sources.append(s)

    # if yt-dlp provided no Referer in any format, use the input page URL
    if sources and not any(s.get("referer") for s in sources):
        for s in sources:
            s["referer"] = url

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

    # URL direta: tenta yt-dlp; se falhar, busca o HTML e extrai embed
    if is_plain_url(raw.strip()):
        url = raw.strip()
        sources: list[dict[str, str]] = []
        safe_title = ""

        # RedGifs: usa API pública diretamente (SPA — scraping não funciona)
        if "redgifs.com" in urlparse(url).netloc:
            result = _redgifs_extract(url)
            if result is None:
                print("Falha ao acessar a API do RedGifs.", file=sys.stderr)
                sys.exit(1)
            sources, safe_title, is_user_page = result
            if not sources:
                print("Nenhum vídeo encontrado.")
                sys.exit(1)
            if is_user_page:
                print(f"\nEncontrados {len(sources)} vídeos de '{safe_title}':\n")
                for s in sources:
                    out = f"{s['label']}.mp4"
                    print(f"  {out}")
                    print(f"  {_ffmpeg_cmd(s, out)}")
                    print()
            else:
                print(f"\nEncontradas {len(sources)} qualidades para '{safe_title}':\n")
                for s in sources:
                    label = s["label"]
                    out = f"{safe_title}_{label}.mp4"
                    print(f"  [{label}]  {out}")
                    print(f"  {_ffmpeg_cmd(s, out)}")
                    print()
            return

        try:
            sources, safe_title = extract_sources_ytdlp(url)
        except YtdlpError:
            print(f"[yt-dlp não suporta esta URL diretamente, buscando HTML...]")
            try:
                page_html = fetch_page_html(url)
            except Exception as e:
                print(f"Erro ao buscar a página: {e}", file=sys.stderr)
                print("Dica: copie o HTML da página (Ctrl+U → Ctrl+A → Ctrl+C) e rode o script sem argumento.", file=sys.stderr)
                sys.exit(1)
            embed_url, page_title = extract_embed_info(page_html)
            if not embed_url:
                print("Nenhum embed de vídeo encontrado na página.")
                sys.exit(1)
            print(f"[embed detectado] {embed_url}")
            sources, safe_title = _resolve_embed(embed_url, page_title, referer=url)
        if not sources:
            print("Nenhum formato de vídeo encontrado.")
            sys.exit(1)
        print(f"\nEncontradas {len(sources)} qualidades para '{safe_title}':\n")
        for s in sources:
            label = s["label"]
            suffix = f"_{label}p" if label.isdigit() else f"_{label}"
            out = f"{safe_title}{suffix}.mp4"
            print(f"  [{label}]  {out}")
            print(f'  {_ffmpeg_cmd(s, out)}')
            print()
        return

    sources = extract_sources(raw)

    if not sources:
        embed_url, page_title = extract_embed_info(raw)
        if embed_url:
            print(f"[embed detectado] {embed_url}")
            sources, safe_title = _resolve_embed(embed_url, page_title)
            if not sources:
                print("Nenhum formato de vídeo encontrado.")
                sys.exit(1)
            print(f"\nEncontradas {len(sources)} qualidades para '{safe_title}':\n")
            for s in sources:
                label = s["label"]
                suffix = f"_{label}p" if label.isdigit() else f"_{label}"
                out = f"{safe_title}{suffix}.mp4"
                print(f"  [{label}]  {out}")
                print(f'  {_ffmpeg_cmd(s, out)}')
                print()
            return
        print("Nenhuma fonte de vídeo encontrada no HTML.")
        sys.exit(1)

    print(f"\nEncontradas {len(sources)} qualidades:\n")
    for s in sources:
        out = filename_from_url(s["url"], s["label"])
        print(f"  [{s['label']}]  {out}")
        print(f'  {_ffmpeg_cmd(s, out)}')
        print()


if __name__ == "__main__":
    main()
