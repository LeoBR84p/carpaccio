#!/usr/bin/env python3
"""
Extrai URLs de vídeo de HTML e gera comandos ffmpeg.

Uso:
    python extract_video.py              # lê da área de transferência (pyperclip)
    python extract_video.py arquivo.html # lê de um arquivo
    python extract_video.py -            # lê do stdin (ctrl+z para encerrar no Windows)
"""

import base64
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse


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


_TIME_RE = r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)"


def _short(text: str, width: int = 50) -> str:
    """Trunca textos longos para não estourar a largura do terminal."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _make_progress(show_size: bool = False):
    """Barra rich: percentual, tamanho (opcional), decorrido e restante.

    Retorna None se rich não estiver instalado. show_size adiciona a coluna
    de tamanho (baixado/total) — usada no download por bytes (yt-dlp).
    """
    try:
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )
    except ImportError:
        return None
    columns = [
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    ]
    if show_size:
        columns.append(DownloadColumn())
    columns += [
        TextColumn("decorrido"),
        TimeElapsedColumn(),
        TextColumn("restante"),
        TimeRemainingColumn(),
    ]
    return Progress(*columns)


def _to_seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


def _num(tok: str) -> float | None:
    """Converte token do yt-dlp em float; 'NA'/vazio viram None."""
    try:
        return float(tok)
    except ValueError:
        return None


# campos do --progress-template (espaço-separados, prefixados por DLP):
# baixado, total, total estimado
_DLP_TEMPLATE = (
    "download:DLP %(progress.downloaded_bytes)s "
    "%(progress.total_bytes)s %(progress.total_bytes_estimate)s"
)


def _download_ytdlp(source: dict[str, str], out: str) -> bool:
    """Baixa via yt-dlp com fragmentos concorrentes (-N 8), barra rich.

    Muito mais rápido que ffmpeg para HLS/DASH (YouTube live/DVR), que o
    ffmpeg baixa segmento a segmento numa única conexão. yt-dlp também
    junta áudio+vídeo. O progresso é lido via --progress-template e
    desenhado na mesma barra rich usada pelo download por ffmpeg.

    Usado só sem limite de duração: para baixar o vídeo inteiro. Com limite
    em minutos o download passa pelo ffmpeg -t (ver _download), que finaliza
    o mp4 mesmo com a live ainda no ar.
    """
    label = source["label"]
    if label.isdigit():
        fmt = f"bestvideo[height<={label}][ext!=svg]+bestaudio/best[height<={label}][ext!=svg]/best"
    else:
        fmt = "bestvideo[ext!=svg]+bestaudio/best[ext!=svg]/best"
    cmd = [
        "yt-dlp", "--no-playlist", "-N", "8",
        "-f", fmt, "--remux-video", "mp4",
        "--newline", "--no-warnings", "--progress-template", _DLP_TEMPLATE,
    ]
    if Path(FFMPEG).exists():  # garante que o yt-dlp ache o ffmpeg p/ remuxar
        cmd += ["--ffmpeg-location", str(Path(FFMPEG).parent)]
    cmd += ["-o", out, source["page_url"]]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("yt-dlp não encontrado. Instale com: uv add yt-dlp", file=sys.stderr)
        return False

    progress = _make_progress(show_size=True)
    if progress is None:  # rich não instalado: baixa sem barra
        print("[rich não instalado — sem barra de progresso; uv add rich]")
        proc.communicate()
        return proc.returncode == 0

    tail: deque[str] = deque(maxlen=15)  # linhas não-progresso, p/ erro
    try:
        with progress:
            task = progress.add_task(_short(out, 30), total=None)
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                parts = line.split()
                if parts[:1] != ["DLP"] or len(parts) < 4:
                    if line:
                        tail.append(line)
                    continue
                downloaded = _num(parts[1])
                total = _num(parts[2]) or _num(parts[3])  # real ou estimado
                if downloaded is None:
                    continue
                if total:  # total conhecido → %/restante/tamanho completos
                    progress.update(task, completed=downloaded, total=total)
                else:  # total desconhecido (live): mostra tamanho baixado
                    progress.update(task, completed=downloaded)
            proc.wait()
            if proc.returncode == 0 and progress.tasks[0].total is not None:
                progress.update(task, completed=progress.tasks[0].total)
    except KeyboardInterrupt:
        proc.kill()
        print("\n[download cancelado]")
        return False

    if proc.returncode != 0:
        print("\n".join(tail), file=sys.stderr)
    return proc.returncode == 0


def _http_get(url: str, referer: str | None = None, retries: int = 3) -> bytes:
    """GET com User-Agent de navegador e algumas tentativas (segmentos HLS)."""
    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:  # rede instável: espera curta e tenta de novo
            last = e
            time.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


def _parse_media_playlist(
    text: str, playlist_url: str
) -> list[tuple[str, float]]:
    """Extrai (url_segmento, duração) de uma media playlist HLS."""
    segs: list[tuple[str, float]] = []
    dur = 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line[len("#EXTINF:"):].split(",")[0])
            except ValueError:
                dur = 0.0
        elif line and not line.startswith("#"):
            segs.append((urljoin(playlist_url, line), dur))
            dur = 0.0
    return segs


def _best_variant(text: str, playlist_url: str) -> str | None:
    """De uma master playlist, devolve a URL da variante de maior banda."""
    best_bw, best_url = -1, None
    pending_bw = 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            pending_bw = int(m.group(1)) if m else 0
        elif line and not line.startswith("#"):
            if pending_bw >= best_bw:
                best_bw, best_url = pending_bw, urljoin(playlist_url, line)
            pending_bw = 0
    return best_url


def _make_hls_progress():
    """Barra rich p/ HLS: %, MB baixados, decorrido e restante."""
    try:
        from rich.progress import (
            BarColumn,
            Progress,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )
    except ImportError:
        return None
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("{task.fields[mb]:.0f} MB"),
        TextColumn("decorrido"),
        TimeElapsedColumn(),
        TextColumn("restante"),
        TimeRemainingColumn(),
    )


def _download_hls_segments(
    source: dict[str, str], out: str, limit_min: float | None = None,
    workers: int = 12,
) -> bool | None:
    """Baixa um HLS pelos segmentos diretos, em paralelo, e remuxa p/ mp4.

    Contorna o throttle do YouTube em lives: a *playlist* ao vivo é servida
    em ~tempo real, mas as URLs dos segmentos baixam em velocidade plena.
    Lê a playlist, opcionalmente corta nos primeiros limit_min minutos,
    baixa os .ts concorrentemente, concatena e finaliza com ffmpeg -c copy.

    Retorna True/False (sucesso) ou None se a playlist não puder ser usada
    (cabe ao chamador cair no método antigo).
    """
    playlist_url = source["url"]
    referer = source.get("referer")
    try:
        text = _http_get(playlist_url, referer).decode("utf-8", "replace")
    except Exception as e:
        print(f"[hls] erro ao buscar playlist: {e}", file=sys.stderr)
        return None
    # master playlist → resolve uma vez para a melhor variante
    if "#EXTINF" not in text and "#EXT-X-STREAM-INF" in text:
        variant = _best_variant(text, playlist_url)
        if not variant:
            return None
        try:
            text = _http_get(variant, referer).decode("utf-8", "replace")
            playlist_url = variant
        except Exception:
            return None
    segs = _parse_media_playlist(text, playlist_url)
    if not segs:
        return None  # não é HLS por segmentos — deixa o chamador usar ffmpeg

    if limit_min:  # corta nos primeiros N minutos de vídeo
        cap, acc, kept = limit_min * 60, 0.0, []
        for u, d in segs:
            kept.append((u, d))
            acc += d
            if acc >= cap:
                break
        segs = kept

    total_secs = sum(d for _, d in segs) or float(len(segs))
    tmp = tempfile.mkdtemp(prefix="hlsdl_")

    def _fetch(job: tuple[int, str, float]) -> tuple[float, int]:
        i, u, d = job
        data = _http_get(u, referer)
        with open(os.path.join(tmp, f"{i:06d}.ts"), "wb") as f:
            f.write(data)
        return d, len(data)

    jobs = [(i, u, d) for i, (u, d) in enumerate(segs)]
    progress = _make_hls_progress()
    try:
        if progress is None:
            print(f"[hls] baixando {len(segs)} segmentos...")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_fetch, jobs))
        else:
            done_secs = 0.0
            done_bytes = 0
            with progress:
                task = progress.add_task(
                    _short(out, 30), total=total_secs, mb=0.0
                )
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = [ex.submit(_fetch, j) for j in jobs]
                    for fut in as_completed(futs):
                        d, n = fut.result()
                        done_secs += d
                        done_bytes += n
                        progress.update(
                            task, completed=done_secs, mb=done_bytes / 1e6
                        )
        # concatena em ordem (TS é byte-concatenável); libera os .ts no caminho
        ts_all = os.path.join(tmp, "all.ts")
        with open(ts_all, "wb") as o:
            for i in range(len(segs)):
                p = os.path.join(tmp, f"{i:06d}.ts")
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, o)
                os.remove(p)
        r = subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
             "-i", ts_all, "-c", "copy", out]
        )
        return r.returncode == 0
    except KeyboardInterrupt:
        print("\n[download cancelado]")
        return False
    except Exception as e:
        print(f"[hls] erro no download: {e}", file=sys.stderr)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _download(
    source: dict[str, str], out: str, limit_min: float | None = None
) -> bool:
    """Run ffmpeg directly (arg list, sem shell — imune a problemas de aspas).

    limit_min: duração máxima de vídeo a baixar, em minutos (None = tudo).
    Despacho por tipo de fonte:
      • HLS (playlist .m3u8): baixa os segmentos em paralelo — rápido e com
        corte por tempo, contornando o throttle de live do YouTube.
      • yt-dlp sem limite: yt-dlp com fragmentos concorrentes (-N).
      • resto / limite em fonte não-HLS: ffmpeg (-t finaliza ao atingir a
        duração, mesmo com a live no ar).
    """
    url = source.get("url", "")
    if source.get("seg_urls"):  # fMP4 segmentado (videosh): init + segmentos
        return _download_numbered_segments(source, out, limit_min)

    if source.get("page_url") and ".m3u8" in url:  # HLS: download por segmentos
        res = _download_hls_segments(source, out, limit_min)
        if res is not None:
            return res  # None = playlist inutilizável → cai no método antigo

    if source.get("page_url") and not limit_min:
        return _download_ytdlp(source, out)

    cmd = [FFMPEG, "-y", "-hide_banner", "-nostats"]
    ref = source.get("referer")
    if ref:
        # -headers exige CRLF real, não a sequência literal \r\n
        cmd += ["-headers", f"Referer: {ref}\r\nUser-Agent: {_UA}\r\n"]
    cmd += ["-i", source["url"], "-c", "copy", "-progress", "pipe:1"]
    if limit_min:  # -t (opção de saída) finaliza o mp4 ao atingir a duração
        cmd += ["-t", str(int(limit_min * 60))]
    cmd += [out]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print(f"ffmpeg não encontrado em {FFMPEG}", file=sys.stderr)
        return False

    # duração total vem do stderr (linha "Duration: ..."); lido em thread
    # para a barra não travar caso o stderr encha o buffer do pipe
    duration: dict[str, float] = {}
    stderr_tail: deque[str] = deque(maxlen=15)

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())
            m = re.search(rf"Duration:\s*{_TIME_RE}", line)
            if m and "total" not in duration:
                duration["total"] = _to_seconds(*m.groups())

    threading.Thread(target=_read_stderr, daemon=True).start()

    progress = _make_progress()
    if progress is None:  # rich não instalado: baixa sem barra
        print("[rich não instalado — sem barra de progresso; uv add rich]")
        proc.communicate()
        return proc.returncode == 0

    # com limite, o total é conhecido (a live não reporta Duration)
    known_total = limit_min * 60 if limit_min else None
    try:
        with progress:
            task = progress.add_task(_short(out), total=known_total)
            assert proc.stdout is not None
            for line in proc.stdout:
                if known_total is None and "total" in duration \
                        and progress.tasks[0].total is None:
                    progress.update(task, total=duration["total"])
                m = re.match(rf"out_time={_TIME_RE}", line.strip())
                if m:
                    progress.update(task, completed=_to_seconds(*m.groups()))
            proc.wait()
            final_total = known_total or duration.get("total")
            if proc.returncode == 0 and final_total is not None:
                progress.update(task, completed=final_total)
    except KeyboardInterrupt:
        proc.kill()
        print("\n[download cancelado]")
        return False

    if proc.returncode != 0:
        print("\n".join(stderr_tail), file=sys.stderr)
    return proc.returncode == 0


def _titled_filename(safe_title: str, source: dict[str, str]) -> str:
    label = source["label"]
    suffix = f"_{label}p" if label.isdigit() else f"_{label}"
    return f"{safe_title}{suffix}.mp4"


def _ask_limit_min() -> float | None:
    """Pergunta um limite de minutos de vídeo (Enter = vídeo completo)."""
    try:
        raw = input(
            "Parar após quantos minutos de vídeo? (Enter = vídeo completo): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None
    try:
        val = float(raw.replace(",", "."))
    except ValueError:
        print("Valor inválido — baixando vídeo completo.")
        return None
    return val if val > 0 else None


def _list_and_offer(items: list[tuple[dict[str, str], str]]) -> None:
    """Lista as fontes com seus comandos e oferece baixar direto pelo script."""
    for s, out in items:
        print(f"  [{s['label']}]  {out}")
        if s.get("seg_urls"):  # fMP4 segmentado: não há comando ffmpeg único
            print(f"  {len(s['seg_urls'])} segmentos fMP4 — baixe por aqui "
                  f"(as URLs são assinadas e expiram em minutos)")
        elif s.get("page_url"):  # fonte yt-dlp: mostra comando curto (URL enorme)
            print(f'  yt-dlp -N 8 -f "<={s["label"]}" -o "{out}" {s["page_url"]}')
        else:
            print(f"  {_ffmpeg_cmd(s, out)}")
        print()
    if not sys.stdin.isatty():
        return
    by_label = {s["label"]: (s, out) for s, out in items}
    try:
        choice = input(
            "Baixar agora? Digite a qualidade (ou 'all' para todas; Enter para sair): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not choice:
        return
    chosen = list(items) if choice.lower() == "all" else (
        [by_label[choice]] if choice in by_label else []
    )
    if not chosen:
        print(f"Qualidade '{choice}' não encontrada.")
        return
    limit_min = _ask_limit_min()
    for s, out in chosen:
        print(f"\n[baixando {out}...]")
        ok = _download(s, out, limit_min)
        print("[concluído]" if ok else "[falhou]")


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


def _mixdrop_extract(embed_url: str, referer: str | None = None) -> list[dict[str, str]]:
    """Extract direct video URL from a MixDrop embed page (any TLD).

    MixDrop obfuscates the stream URL in a Dean Edwards p,a,c,k,e,d
    JavaScript block.  We decode it and read MDCore.wurl.
    """
    try:
        html = fetch_page_html(embed_url, referer=referer)
    except Exception as e:
        print(f"[mixdrop] Erro ao buscar embed: {e}", file=sys.stderr)
        return []

    idx = html.find("eval(function(p,a,c,k,e,d)")
    if idx < 0:
        return []
    chunk = html[idx: idx + 10000]

    m = re.search(
        r"\}\('([^']*)',\s*\d+,\s*\d+,\s*'([^']*)'\s*\.split\('\|'\)",
        chunk,
    )
    if not m:
        return []

    packed_str = m.group(1)
    keys = m.group(2).split("|")

    def _replace(match: re.Match) -> str:
        i = int(match.group())
        return keys[i] if i < len(keys) and keys[i] else match.group()

    decoded = re.sub(r'\b\d+\b', _replace, packed_str)

    wurl = re.search(r'MDCore\.wurl\s*=\s*"([^"]+)"', decoded)
    if not wurl:
        return []

    url = wurl.group(1)
    if url.startswith("//"):
        url = "https:" + url

    domain = urlparse(embed_url).netloc
    return [{"url": url, "label": "original", "referer": f"https://{domain}/"}]


# vinovo.si redireciona para vinovo.to; a sessão e o token valem no domínio final
VINOVO_HOST = "https://vinovo.to"
VINOVO_RE = re.compile(
    r"https?://(?:www\.)?vinovo\.(?:si|to|sx|tv)/(?:e|d|f|v)/([A-Za-z0-9]+)", re.I
)


def _vinovo_extract(
    embed_url: str, referer: str | None = None
) -> tuple[list[dict[str, str]], str | None]:
    """Extrai o stream de um embed Vinovo. Retorna (sources, título).

    O player não traz a URL no HTML: ele lê <meta name="token"> e o data-base
    do <video>, e faz um POST em /api/file/url/<file_code> que devolve
    "<file_code>/<assinatura>/<expiração>".  O stream final é
    <data-base>/stream/<isso>.

    Dois detalhes que fazem a chamada falhar silenciosamente ("status":"fail"):
      • o POST exige o header Origin além do PHPSESSID da mesma sessão;
      • o token é de uso único — cada extração precisa de um GET novo do embed.
    A URL assinada expira em poucos minutos, então baixe logo após extrair.
    """
    m = VINOVO_RE.match(embed_url)
    if not m:
        return [], None
    code = m.group(1)

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def _open(url: str, data: dict[str, str] | None = None, **extra: str) -> str:
        headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9", **extra}
        body = urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers)
        with opener.open(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")

    try:
        html = _open(f"{VINOVO_HOST}/e/{code}", Referer=referer or f"{VINOVO_HOST}/")
    except Exception as e:
        print(f"[vinovo] Erro ao buscar embed: {e}", file=sys.stderr)
        return [], None

    tok = re.search(r'name="token"\s+content="([^"]+)"', html)
    base = re.search(r'data-base="([^"]+)"', html)
    if not tok or not base:
        print("[vinovo] token/data-base não encontrados no embed.", file=sys.stderr)
        return [], None

    try:
        raw = _open(
            f"{VINOVO_HOST}/api/file/url/{code}",
            {"token": tok.group(1)},
            Referer=f"{VINOVO_HOST}/e/{code}",
            Origin=VINOVO_HOST,
            Accept="*/*",
            **{"X-Requested-With": "XMLHttpRequest"},
        )
        payload = json.loads(raw)
    except Exception as e:
        print(f"[vinovo] Erro na API: {e}", file=sys.stderr)
        return [], None

    if payload.get("status") != "ok" or not payload.get("token"):
        print(
            f"[vinovo] API recusou: {payload.get('message') or payload.get('status')}",
            file=sys.stderr,
        )
        return [], None

    # título do embed: "<modelo> - <data da gravação> - VINOVO"
    title = None
    tm = re.search(r"<title>([^<]*)</title>", html)
    if tm:
        title = re.sub(r"\s*-\s*VINOVO\s*$", "", unescape(tm.group(1))).strip() or None

    source = {
        "url": f"{base.group(1)}/stream/{payload['token']}",
        "label": "original",
        "referer": f"{VINOVO_HOST}/",
    }
    return [source], title


# Nada de bloquear requisição aqui.  Interceptar rotas desativa o cache do
# Chromium e muda o timing da carga, e players com pré-roll não pedem o
# conteúdo se a cadeia de anúncio falhar — bloquear anúncio fazia o xpornium
# nunca trocar o src.  Os popunders são neutralizados fechando as abas que
# eles abrem (ctx.on("page", ...)), que é o suficiente.

# hosts de criativo de anúncio: nunca são o vídeo pedido
_AD_MEDIA_HOSTS = (
    "2mdn.net", "doubleclick", "imasdk", "subduepaler.cyou",
)

# Alguns players (vidstack/videosh) checam navigator.webdriver e simplesmente
# não carregam o vídeo num browser automatizado — o <video> fica com src vazio
# para sempre.  Mascarar os sinais óbvios é o que os faz tocar.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

_STEALTH_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
]


# dispara o play em <video> e web components, atravessando shadow DOM
_PLAY_JS = """() => {
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if ((el.tagName === 'VIDEO' || el.tagName === 'MEDIA-PLAYER') && el.play) {
        try { el.muted = true; el.play(); } catch (e) {}
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
}"""

# lê o currentSrc de todo <video>, inclusive dentro de shadow DOM.  É o sinal
# mais confiável: aponta para o conteúdo, não para o criativo do pré-roll.
_SRC_JS = """() => {
  const out = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.tagName === 'VIDEO') {
        const s = el.currentSrc || el.src || '';
        if (s) out.push(s);
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  return out;
}"""


def _browser_extract(
    embed_url: str, referer: str | None = None, timeout_s: int = 90
) -> list[dict[str, str]]:
    """Último recurso: abre o embed num Chromium e observa o pedido de mídia.

    Players modernos montam a URL em JS ofuscado (ou a recebem cifrada de uma
    API), então não há o que casar por regex no HTML.  Em vez de reverter cada
    host, deixamos o próprio player resolver e capturamos a requisição de vídeo
    — o que continua funcionando quando o host troca a ofuscação.

    Requer `uv add playwright` + `playwright install chromium`.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "[browser] playwright não instalado — "
            "rode: uv add playwright && uv run playwright install chromium",
            file=sys.stderr,
        )
        return []

    exts = (".mp4", ".m3u8", ".webm", ".mkv")
    media: list[str] = []       # requisições de mídia observadas
    found: str | None = None    # currentSrc do <video> — sinal preferido

    def _is_ad(url: str) -> bool:
        return any(h in url for h in _AD_MEDIA_HOSTS)

    def _usable(url: str) -> bool:
        return url.startswith("http") and not _is_ad(url)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_STEALTH_ARGS)
            # Só a origem, nunca a URL completa da página: esse Referer vai em
            # TODA requisição do contexto, e mandar a URL do vídeo nas chamadas
            # internas do player trava a cadeia — o src nunca deixa o pré-roll.
            ref_origin = None
            if referer:
                r = urlparse(referer)
                if r.scheme and r.netloc:
                    ref_origin = f"{r.scheme}://{r.netloc}/"

            ctx = browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 720},
                extra_http_headers={"Referer": ref_origin} if ref_origin else None,
            )
            ctx.add_init_script(_STEALTH_JS)
            page = ctx.new_page()
            # popunders abrem abas novas; fecha todas menos a principal
            ctx.on("page", lambda pg: pg is not page and pg.close())

            def on_request(req) -> None:
                url = req.url
                if req.resource_type == "media" or url.split("?")[0].lower().endswith(exts):
                    if _usable(url):
                        media.append(url)

            page.on("request", on_request)
            page.goto(embed_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)

            # Muitos players só pedem o vídeo após um clique real, e alguns
            # levam ~15s (pré-roll) para trocar o src do <video> pelo conteúdo.
            # Clicar em todos os alvos, e não parar no primeiro que existir:
            # o botão de play pode estar presente e ainda assim inerte.
            deadline = time.monotonic() + timeout_s
            rounds = 0
            while found is None and time.monotonic() < deadline:
                page.wait_for_timeout(5000)
                rounds += 1

                # play() programático só depois que o clique já teve chance:
                # num player com pré-roll IMA, forçar play() no <video> de
                # conteúdo atropela a sequência anúncio→conteúdo e o src nunca
                # é trocado.  Serve aos players em shadow DOM, que ignoram clique.
                if rounds > 2:
                    for frame in page.frames:
                        try:
                            frame.evaluate(_PLAY_JS)
                        except Exception:
                            pass  # frame trocou no meio do eval

                for sel in (".vjs-big-play-button", "video", "#video", "body"):
                    try:
                        el = page.query_selector(sel)
                        if el:
                            # curto de propósito: são 4 seletores por rodada e o
                            # orçamento total precisa caber várias rodadas
                            el.click(timeout=1200, force=True)
                    except Exception:
                        pass

                seen_srcs: list[str] = []
                for frame in page.frames:
                    try:
                        for src in frame.evaluate(_SRC_JS):
                            seen_srcs.append(src)
                            # blob: = MSE; a URL real aparece nas requisições
                            if _usable(src):
                                found = src
                                break
                    except Exception as e:
                        seen_srcs.append(f"<eval erro: {type(e).__name__}>")
                    if found:
                        break

                if os.environ.get("CARPACCIO_DEBUG"):
                    print(
                        f"[browser] rodada {rounds}: frames={len(page.frames)} "
                        f"srcs={seen_srcs} media={media}",
                        file=sys.stderr,
                    )

            browser.close()
    except Exception as e:
        print(f"[browser] Falhou: {type(e).__name__}: {e}", file=sys.stderr)
        return []

    url = found or (media[0] if media else None)
    if not url:
        return []

    origin = urlparse(embed_url)
    return [{
        "url": url,
        "label": "original",
        "referer": f"{origin.scheme}://{origin.netloc}/",
    }]


VIDEOSH_RE = re.compile(r"https?://(?:[\w-]+\.)*upns\.live/", re.I)
_SEG_RE = re.compile(r"(https://[^/]+/v\d+/[^/]+/[^/]+/)(?:seg-(\d+)|init)(-[^?]+)\?(.+)")


def _videosh_extract(
    embed_url: str, referer: str | None = None
) -> tuple[list[dict], str | None]:
    """Extrai o stream fMP4 segmentado do videosh (upns.live).

    O player é vidstack e alimenta o <video> por MSE, então o src é um
    blob: — não há URL direta para o ffmpeg.  Os segmentos vêm assim:

        <base>/seg-<N>-f1-v1-a1.woff2?k=<chave>&kx=<janela>
        <base>/init-f1-v1-a1.woff    ?k=<chave>&kx=<janela>   (moov)

    Detalhes que definem a estratégia:
      • as extensões são disfarce — é fMP4, não fonte (init usa .woff e os
        segmentos .woff2), e nada disso casa com resource_type "media";
      • a chave é GLOBAL, não por segmento: capturar uma serve para todos os
        índices, o que permite baixar em paralelo em vez de em tempo real;
      • o player só carrega se navigator.webdriver estiver mascarado.

    A numeração começa em 1; o fim é achado por busca binária (404 acima).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[videosh] playwright não instalado.", file=sys.stderr)
        return [], None

    hits: list[str] = []
    duration = 0.0
    title = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_STEALTH_ARGS)
            ctx = browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 720},
                extra_http_headers={"Referer": referer} if referer else None,
            )
            ctx.add_init_script(_STEALTH_JS)
            page = ctx.new_page()
            ctx.on("page", lambda pg: pg is not page and pg.close())
            page.on(
                "request",
                lambda r: _SEG_RE.match(r.url) and hits.append(r.url),
            )
            page.goto(embed_url, wait_until="domcontentloaded", timeout=60000)

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                page.wait_for_timeout(4000)
                for frame in page.frames:
                    try:
                        frame.evaluate(_PLAY_JS)
                    except Exception:
                        pass
                for sel in (".vjs-big-play-button", "video", "body"):
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click(timeout=1200, force=True)
                    except Exception:
                        pass
                if any("/init" in u for u in hits) and any("/seg-" in u for u in hits):
                    break

            try:
                duration = page.evaluate(
                    "() => { const v = document.querySelector('video');"
                    " return v && isFinite(v.duration) ? v.duration : 0; }"
                ) or 0.0
            except Exception:
                pass
            try:
                title = re.sub(r"\.mp4$", "", page.title()).strip() or None
            except Exception:
                pass
            browser.close()
    except Exception as e:
        print(f"[videosh] Falhou: {type(e).__name__}: {e}", file=sys.stderr)
        return [], None

    init_url = next((u for u in hits if "/init" in u), None)
    seg_url = next((u for u in hits if "/seg-" in u), None)
    if not (init_url and seg_url):
        print("[videosh] não capturei init/segmento.", file=sys.stderr)
        return [], None

    m = _SEG_RE.match(seg_url)
    if not m:
        return [], None
    base, _, suffix, query = m.groups()
    ref = f"https://{urlparse(embed_url).netloc}/"

    # fim da sequência por busca binária (a chave vale para qualquer índice)
    lo, hi = 1, 2048
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        try:
            _http_get(f"{base}seg-{mid}{suffix}?{query}", ref, retries=1)
            lo = mid
        except Exception:
            hi = mid
    print(f"[videosh] {lo} segmentos")

    source = {
        "url": init_url,
        "label": "original",
        "referer": ref,
        "seg_init": init_url,
        "seg_urls": [f"{base}seg-{i}{suffix}?{query}" for i in range(1, lo + 1)],
        "seg_duration": (duration / lo) if duration and lo else 0.0,
    }
    return [source], title


def _download_numbered_segments(
    source: dict, out: str, limit_min: float | None = None, workers: int = 12
) -> bool:
    """Baixa init + segmentos fMP4 numerados, concatena e remuxa para mp4.

    fMP4 é byte-concatenável desde que o init (moov) venha primeiro — sem ele
    os fragmentos moof/mdat não são decodificáveis.
    """
    seg_urls = list(source["seg_urls"])
    referer = source.get("referer")

    if limit_min and source.get("seg_duration"):
        keep = max(1, int((limit_min * 60) / source["seg_duration"]))
        seg_urls = seg_urls[:keep]

    tmp = tempfile.mkdtemp(prefix="fmp4dl_")
    progress = _make_hls_progress()

    def _fetch(job: tuple[int, str]) -> int:
        i, url = job
        data = _http_get(url, referer)
        with open(os.path.join(tmp, f"{i:06d}.m4s"), "wb") as f:
            f.write(data)
        return len(data)

    try:
        init_data = _http_get(source["seg_init"], referer)
        jobs = list(enumerate(seg_urls))
        if progress is None:
            print(f"[fmp4] baixando {len(jobs)} segmentos...")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_fetch, jobs))
        else:
            done_bytes = 0
            with progress:
                task = progress.add_task(_short(out, 30), total=len(jobs), mb=0.0)
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = [ex.submit(_fetch, j) for j in jobs]
                    for n, fut in enumerate(as_completed(futs), 1):
                        done_bytes += fut.result()
                        progress.update(task, completed=n, mb=done_bytes / 1e6)

        merged = os.path.join(tmp, "all.mp4")
        with open(merged, "wb") as o:
            o.write(init_data)
            for i in range(len(jobs)):
                p = os.path.join(tmp, f"{i:06d}.m4s")
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, o)
                os.remove(p)
        r = subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
             "-i", merged, "-c", "copy", out]
        )
        return r.returncode == 0
    except KeyboardInterrupt:
        print("\n[download cancelado]")
        return False
    except Exception as e:
        print(f"[fmp4] erro no download: {e}", file=sys.stderr)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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

    # YouPorn / PornHub: "videoUrl":"...","quality":"720" or "quality":0 (HLS)
    if not sources:
        seen_yp: set[str] = set()
        for m in re.finditer(r'"videoUrl"\s*:\s*"([^"]+)"', html):
            url = _json_unescape(m.group(1))
            if url in seen_yp:
                continue
            seen_yp.add(url)
            ctx = html[max(0, m.start() - 120):m.end() + 120]
            q = re.search(r'"quality"\s*:\s*"?(\d+)"?', ctx)
            label = (q.group(1) if q else "original") if not q or q.group(1) != "0" else "hls"
            sources.append({"url": url, "label": label})

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
    r"|mixdrop\.\w+"
    r"|vidmoly\.to"
    r"|voe\.sx"
    r"|upstream\.to"
    r"|vtube\.to"
    r"|speedvid\.net"
    r"|supervideo\.tv"
    r"|vinovo\.(?:si|to|sx|tv)"
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

    # last resort: any iframe src with a URL (catches unknown embed hosts).
    # aceita também src protocol-relative ("//host/embed/x"), comum nesses sites
    if not embed_url:
        m = re.search(r'<iframe[^>]+src=["\']?((?:https?:)?//[^\s"\'<>]+)["\']?', html)
        if m:
            embed_url = m.group(1)

    if embed_url and embed_url.startswith("//"):
        embed_url = "https:" + embed_url

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

        # Videosh/upns: fMP4 segmentado via MSE (ver _videosh_extract)
        if VIDEOSH_RE.match(embed_url):
            print("[tentando extração Videosh...]")
            sources, vsh_title = _videosh_extract(embed_url, referer=referer)
            if sources:
                if vsh_title:
                    safe_title = re.sub(r'[\\/*?:"<>|]', "_", vsh_title)
                return sources, safe_title

        # Vinovo: POST assinado em /api/file/url/ (ver _vinovo_extract)
        if VINOVO_RE.match(embed_url):
            print("[tentando extração Vinovo...]")
            sources, vinovo_title = _vinovo_extract(embed_url, referer=referer)
            if sources:
                # o título do embed traz modelo + data da gravação, então é único
                # por vídeo; o da página do site repete entre vídeos do mesmo perfil
                if vinovo_title:
                    safe_title = re.sub(r'[\\/*?:"<>|]', "_", vinovo_title)
                return sources, safe_title

        # MixDrop (any TLD): decode packed JS → MDCore.wurl
        if re.search(r'mixdrop\.\w+/', embed_url):
            print("[tentando extração MixDrop...]")
            sources = _mixdrop_extract(embed_url, referer=referer)
            if sources:
                return sources, safe_title

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
            embed_html = ""
        sources = extract_sources(embed_html) if embed_html else []
        if sources:
            return sources, safe_title

        # Nada no HTML: o player monta a URL em JS. Deixa o browser resolver.
        print("[abrindo o player num browser para capturar o vídeo...]")
        sources = _browser_extract(embed_url, referer=referer)
        return sources, safe_title


def extract_sources_ytdlp(url: str) -> tuple[list[dict[str, str]], str]:
    """Retorna (sources, safe_title) para qualquer URL suportada pelo yt-dlp."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-playlist", "-j", url],
            capture_output=True, text=True, check=True,
        )
        # -j emite um objeto JSON por linha; páginas com vários vídeos
        # geram várias linhas mesmo com --no-playlist
        entries = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        if not entries:
            raise YtdlpError(result.stderr or "yt-dlp não retornou JSON")
        if len(entries) > 1:
            print(f"[{len(entries)} vídeos encontrados; usando o primeiro]")
        info = entries[0]
    except FileNotFoundError:
        print("yt-dlp não encontrado. Instale com: uv add yt-dlp", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        raise YtdlpError(e.stderr)

    title = info.get("title") or info.get("id", "video")
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)

    _IMG_EXTS: frozenset[str] = frozenset({"svg", "png", "jpg", "jpeg", "gif", "webp"})

    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for fmt in info.get("formats", []):
        if fmt.get("vcodec", "none") == "none":
            continue
        if fmt.get("ext", "") in _IMG_EXTS:  # skip avatar/thumbnail images
            continue
        height = fmt.get("height")
        if not height:
            continue
        label = str(height)
        if label in seen:
            continue
        seen.add(label)
        s: dict[str, str] = {"url": fmt["url"], "label": label, "page_url": url}
        ref = (fmt.get("http_headers") or {}).get("Referer")
        if ref:
            s["referer"] = ref
        sources.append(s)

    # fallback: formato único sem lista de formats (e.g. direct stream)
    if not sources and info.get("url"):
        if info.get("ext", "") in _IMG_EXTS:
            raise YtdlpError(f"yt-dlp retornou apenas imagem ({info.get('ext')}); tentando HTML")
        height = info.get("height")
        label = str(height) if height else "original"
        ref = (info.get("http_headers") or {}).get("Referer")
        s = {"url": info["url"], "label": label, "page_url": url}
        if ref:
            s["referer"] = ref
        sources.append(s)

    if not sources:
        raise YtdlpError("nenhum formato de vídeo encontrado pelo yt-dlp; tentando HTML")

    # if yt-dlp provided no Referer in any format, use the input page URL
    if not any(s.get("referer") for s in sources):
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
        arg = sys.argv[1]
        if is_plain_url(arg):
            raw = arg  # URL passada diretamente como argumento
        else:
            raw = Path(arg).read_text(encoding="utf-8")
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
                _list_and_offer([(s, f"{s['label']}.mp4") for s in sources])
            else:
                print(f"\nEncontradas {len(sources)} qualidades para '{safe_title}':\n")
                _list_and_offer(
                    [(s, f"{safe_title}_{s['label']}.mp4") for s in sources]
                )
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
                # no external embed — try to find video URLs in page JS/JSON
                sources = extract_sources(page_html)
                if not sources:
                    print("Nenhum embed ou fonte de vídeo encontrado na página.")
                    sys.exit(1)
                safe_title = re.sub(r'[\\/*?:"<>|]', "_", page_title or "video")
            else:
                print(f"[embed detectado] {embed_url}")
                sources, safe_title = _resolve_embed(embed_url, page_title, referer=url)
        if not sources:
            print("Nenhum formato de vídeo encontrado.")
            sys.exit(1)
        print(f"\nEncontradas {len(sources)} qualidades para '{safe_title}':\n")
        _list_and_offer([(s, _titled_filename(safe_title, s)) for s in sources])
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
            _list_and_offer(
                [(s, _titled_filename(safe_title, s)) for s in sources]
            )
            return
        print("Nenhuma fonte de vídeo encontrada no HTML.")
        sys.exit(1)

    print(f"\nEncontradas {len(sources)} qualidades:\n")
    _list_and_offer(
        [(s, filename_from_url(s["url"], s["label"])) for s in sources]
    )


if __name__ == "__main__":
    main()
