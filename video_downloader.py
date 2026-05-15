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
import os
import re
import sys
import tempfile
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from tqdm import tqdm
except ImportError:
    print("Missing dependencies. Run: pip install requests tqdm")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def natural_sort_key(text: str):
    """Sort strings with embedded numbers in human order (seg2 < seg10)."""
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


# ---------------------------------------------------------------------------
# M3U8 parsing
# ---------------------------------------------------------------------------

def parse_m3u8(source: str, session: requests.Session) -> tuple[list[str], str | None]:
    """
    Parse an M3U8 playlist from a URL or local file.
    Returns (segment_urls, subtitle_url_or_none).
    """
    is_url = source.startswith("http://") or source.startswith("https://")
    base = source.rsplit("/", 1)[0] + "/" if is_url else ""

    if is_url:
        content = session.get(source, timeout=30).text
    else:
        content = Path(source).read_text()

    # Handle master playlist (selects first media playlist)
    if "#EXT-X-STREAM-INF" in content:
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                media_url = line if line.startswith("http") else urljoin(source, line)
                return parse_m3u8(media_url, session)

    segments: list[str] = []
    subtitle_url: str | None = None

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            # Look for subtitle URI inside EXT-X-MEDIA tags
            if "TYPE=SUBTITLES" in line or "TYPE=CLOSED-CAPTIONS" in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    uri = m.group(1)
                    subtitle_url = uri if uri.startswith("http") else urljoin(source, uri)
            continue
        url = line if line.startswith("http") else (urljoin(source, line) if is_url else line)
        segments.append(url)

    return segments, subtitle_url


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

    with tqdm(total=total, desc="Downloading segments", unit="seg") as bar:
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
                    print(f"\n[ERROR] Segment {idx}: {exc}", file=sys.stderr)
                    sys.exit(1)
                bar.update(1)

    return [results[i] for i in sorted(results)]


def merge_segments(segment_files: list[Path], output: Path) -> None:
    """Concatenate .ts files and remux to the desired container via ffmpeg."""
    concat_list = output.parent / "concat_list.txt"
    with open(concat_list, "w") as f:
        for seg in segment_files:
            f.write(f"file '{seg.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output),
    ]
    print(f"\nMerging {len(segment_files)} segments → {output}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    concat_list.unlink(missing_ok=True)

    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError("ffmpeg merge failed")
    print(f"Video saved: {output}")


def download_subtitle(url: str, dest: Path, session: requests.Session) -> None:
    print(f"Downloading subtitles from {url}")
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    print(f"Subtitles saved: {dest}")


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
    return p


def main():
    args = build_parser().parse_args()
    session = requests.Session()
    output = Path(args.output)

    # -- Collect segment URLs --------------------------------------------------
    subtitle_url_from_m3u8: str | None = None

    if args.urls:
        raw = Path(args.urls).read_text().splitlines()
        urls = sort_urls([u.strip() for u in raw if u.strip()])
        print(f"Loaded {len(urls)} URLs from {args.urls}")
    else:
        print(f"Parsing M3U8: {args.m3u8}")
        urls, subtitle_url_from_m3u8 = parse_m3u8(args.m3u8, session)
        print(f"Found {len(urls)} segments")

    if not urls:
        print("No segment URLs found. Aborting.", file=sys.stderr)
        sys.exit(1)

    # -- Download & merge ------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="ts_segments_") as tmp:
        work_dir = Path(tmp)
        segment_files = download_segments(urls, work_dir, workers=args.workers)
        merge_segments(segment_files, output)

        if args.keep_segments:
            kept_dir = output.parent / (output.stem + "_segments")
            kept_dir.mkdir(exist_ok=True)
            for seg in segment_files:
                seg.rename(kept_dir / seg.name)
            print(f"Segments kept in: {kept_dir}")

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
