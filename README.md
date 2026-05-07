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
