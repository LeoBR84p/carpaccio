# Carpaccio

Download `.ts` video segments in parallel, merge them into a single video file, and optionally re-encode existing videos for smaller file sizes.

## Requirements

- Python 3.12+
- ffmpeg with NVENC support — recommended: [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) `win64-gpl` build
- NVIDIA GPU (GTX/RTX series)
- Python dependencies: `uv sync`

---

## video_downloader.py

Download an M3U8 playlist or a list of `.ts` segment URLs and merge them into a single video.

```bash
uv run video_downloader.py --m3u8 "https://cdnapisec.kaltura.com/p/2444871/sp/244487100/playManifest/entryId/1_5umuv9sa/protocol/https/format/applehttp/flavorIds/1_vt4s4dz6,1_gx5leptn,1_4utaqzqi,1_io1c0ubx,1_b0imtgys/a.m3u8?uiConfId=46596453&playSessionId=05849001-94eb-dea8-360e-3829dd3c18af:bd4742c2-4412-7369-f255-e3533a7d9d14&referrer=aHR0cHM6Ly9jbGFzcy5tYnguYWNhZGVteS9jbGFzc192Mj90PWY1NGNkMjJkMDNiNTkzMWY0Yjg4NDE1NGMxMDhmMDhl&clientTag=html5:v7.54" --output aula.mp4
```

### Downloader Usage

```bash
# From an M3U8 playlist
uv run video_downloader.py --m3u8 https://example.com/playlist.m3u8 --output video.mp4

# With sharpening (GPU re-encode, improves text/slide clarity)
uv run video_downloader.py --m3u8 https://example.com/playlist.m3u8 --output video.mp4 --sharpen

# From a URL list file
uv run video_downloader.py --urls urls.txt --output video.mp4

# With subtitles
uv run video_downloader.py --m3u8 https://example.com/playlist.m3u8 --output video.mp4 --subtitles https://example.com/subs.vtt

# Keep individual .ts segment files after merging
uv run video_downloader.py --m3u8 https://example.com/playlist.m3u8 --output video.mp4 --keep-segments

# More parallel download workers (default: 8)
uv run video_downloader.py --m3u8 https://example.com/playlist.m3u8 --output video.mp4 --workers 16
```

### Options

| Flag                  | Short | Default        | Description                             |
| --------------------- | ----- | -------------- | --------------------------------------- |
| `--urls FILE`         |       | -              | Text file with one segment URL per line |
| `--m3u8 URL_OR_FILE`  |       | -              | M3U8 playlist URL or local file         |
| `--output FILE`       | `-o`  | required       | Output video file (e.g. `video.mp4`)    |
| `--subtitles URL`     | `-s`  | -              | Subtitle URL to download                |
| `--subtitle-out FILE` |       | `<output>.vtt` | Subtitle output filename                |
| `--workers N`         | `-w`  | `8`            | Number of parallel download workers     |
| `--keep-segments`     |       | false          | Preserve `.ts` segment files            |
| `--sharpen`           |       | false          | GPU re-encode with unsharp filter       |

`--urls` and `--m3u8` are mutually exclusive; exactly one is required.

### How it works

1. Segment URLs are sorted in natural order (`seg2.ts` before `seg10.ts`).
2. Segments are downloaded in parallel with automatic retries (up to 3 attempts, exponential back-off).
3. A quality report is shown (resolution, codec, FPS, bitrate, tier) before downloading.
4. If `--sharpen` is recommended and not set, the script asks whether to enable it.
5. ffmpeg merges the segments. Without `--sharpen`: fast remux (`-c copy`). With `--sharpen`: GPU re-encode via `h264_nvenc` + `unsharp` filter.
6. If the output file already exists, the script asks to overwrite or auto-rename.

---

## reencode.py

Re-encode existing video files for smaller file size using GPU acceleration.

### Reencoder Usage

```bash
uv run reencode.py
```

A file picker dialog opens for selecting one or more videos. After selection:

1. **Mode selection** - choose the encode profile
2. **Analysis** - ffprobe reads each file's metadata
3. **Size estimation** - a 30-second sample is encoded to accurately predict the output size
4. **Summary table** - shows current vs estimated size and reduction % per file
5. **Optional preview** - encode 1 minute from the middle of the first file, open automatically, then confirm or cancel
6. **Encode** - parallel GPU encode with live progress bars

### Encode modes

| Mode | Codec      | Settings                        | Output | Use case                            |
| ---- | ---------- | ------------------------------- | ------ | ----------------------------------- |
| 1    | H.264      | h264_nvenc, CQ 26, p6           | `.mp4` | General size reduction              |
| 2    | AV1        | av1_nvenc, CQ 38, p7            | `.mkv` | Maximum compression                 |
| 3    | AV1 capped | av1_nvenc, CQ 32, maxrate 900k  | `.mkv` | Guaranteed reduction, quality floor |

All modes use full GPU pipeline (decode + encode on GPU) with 2 parallel workers.

Output files are saved alongside the originals with a suffix: `_cq26`, `_av1`, or `_av1cap`.

---

## extract_video.py

Extract video URLs from HTML copied from a video page and generate ready-to-run ffmpeg download commands.

Useful when a page serves a direct `.mp4` (not HLS segments) with signed CDN URLs embedded in the HTML source.

### Usage

```bash
# Read HTML from a file
python extract_video.py page.html

# Read HTML from stdin (paste, then press Enter + Ctrl+Z + Enter on Windows)
python extract_video.py -

# Read HTML from clipboard (requires: pip install pyperclip)
python extract_video.py
```

### Example output

```text
Encontradas 3 qualidades:

  [240]  0hkh1xwa6j80qjcsiigdk_240p.mp4
  .\ffmpeg\bin\ffmpeg.exe -i "https://cdn2.xpto.com/..." -c copy 0hkh1xwa6j80qjcsiigdk_240p.mp4

  [720]  0hkh1xwa6j80qjcsiigdk_720p.mp4
  .\ffmpeg\bin\ffmpeg.exe -i "https://cdn2.xpto.com/..." -c copy 0hkh1xwa6j80qjcsiigdk_720p.mp4

  [original]  0hkh1xwa6j80qjcsiigdk_source.mp4
  .\ffmpeg\bin\ffmpeg.exe -i "https://cdn2.xpto.com/..." -c copy 0hkh1xwa6j80qjcsiigdk_source.mp4
```

Copy the desired ffmpeg command and run it directly. The script handles `&amp;` HTML entity decoding automatically.

---

## join_videos.py

Join multiple MP4 files into one without re-encoding — codec, resolution, bitrate, and all stream settings are preserved exactly.

### Joiner Usage

```bash
python join_videos.py <number_of_videos>
```

File picker dialogs open in sequence — one per video, in the order they will be joined — followed by a "Save as" dialog for the output file. The output filename defaults to `<first_video>_joined.mp4`.

```bash
python join_videos.py 2   # join 2 videos
python join_videos.py 4   # join 4 videos
```

If the chosen output file already exists, the script asks for confirmation before overwriting.

### How the joiner works

1. A list file with the selected paths is written to a temp file.
2. ffmpeg reads it via the concat demuxer with `-c copy` (no re-encoding).
3. The temp file is deleted automatically after the process finishes.
