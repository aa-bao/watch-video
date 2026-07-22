#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path.

Also fetches subtitles (manual first, then auto-generated) in VTT format so
transcribe.py can parse them without needing Whisper.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def _find_ffmpeg() -> str:
    """Locate ffmpeg, including the one packaged by imageio-ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    TMP = os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    for ext in ("", ".exe"):
        candidate = Path(TMP) / f"ffmpeg{ext}"
        if candidate.exists() and candidate.is_file():
            os.environ.setdefault("PATH", TMP)
            return str(candidate)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    return ""


def _ffmpeg_env() -> dict[str, str]:
    """Return an env dict with ffmpeg's directory on PATH."""
    env = dict(os.environ)
    ff = _find_ffmpeg()
    if ff:
        d = str(Path(ff).parent)
        env["PATH"] = d + os.pathsep + env.get("PATH", "")
    return env


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}


def _yt_dlp_cmd() -> list[str]:
    """Return the command to invoke yt-dlp — prefer the CLI, fall back to python -m."""
    if shutil.which("yt-dlp") is not None:
        return ["yt-dlp"]
    # Check if yt-dlp is available as a Python module (common on Windows
    # where pip may not install the Scripts entry point).
    try:
        import yt_dlp
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        raise SystemExit(
            "yt-dlp is not installed. Install with:\n"
            "  pip install yt-dlp"
        )


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def resolve_local(path: str) -> dict:
    p = Path(path).expanduser().resolve()

    # On Windows / Git Bash, filenames with non-ASCII characters (e.g. Chinese)
    # get garbled when passed through sys.argv. If the resolved path doesn't
    # exist, try matching by parent directory listing.
    if not p.exists():
        parent = p.parent
        if parent.exists():
            needle = p.name
            for entry in os.listdir(str(parent)):
                # Compare by suffix + rough size/prefix heuristics
                if entry == needle:
                    p = parent / entry
                    break
                # Fuzzy: same extension, similar-length base name
                if entry.lower().endswith(p.suffix.lower()) and len(entry) == len(needle):
                    p = parent / entry
                    print(f"[watch] fuzzy-matched garbled filename: '{needle}' → '{entry}'", file=sys.stderr)
                    break
            else:
                raise SystemExit(
                    f"File not found: {p}\n"
                    f"(If the filename contains non-ASCII characters, try renaming it to ASCII "
                    f"and re-run — Git Bash on Windows garbles Unicode filenames in subprocess args.)"
                )
        else:
            raise SystemExit(f"File not found: {p}")

    if p.suffix.lower() not in VIDEO_EXTS:
        print(
            f"[watch] warning: {p.suffix} is not a known video extension, proceeding anyway",
            file=sys.stderr,
        )

    # On Windows / Git Bash, filenames with non-ASCII characters (e.g. Chinese)
    # get garbled when passed to subprocess (ffprobe/ffmpeg). Copy to a safe
    # ASCII-only name in the same directory.
    if not p.name.isascii():
        safe = p.parent / f"temp_video{p.suffix}"
        if not safe.exists():
            print(f"[watch] copying '{p.name}' → '{safe.name}' (ASCII-safe name)", file=sys.stderr)
            import shutil as _shutil
            _shutil.copy2(str(p), str(safe))
        p = safe

    return {
        "video_path": str(p),
        "subtitle_path": None,
        "info": {"title": p.name, "url": str(p)},
        "downloaded": False,
    }


def _pick_subtitle(out_dir: Path) -> Path | None:
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    preferred = [
        c for c in candidates
        if any(marker in c.name for marker in (".en.", ".en-US.", ".en-GB.", ".en-orig."))
    ]
    return preferred[0] if preferred else candidates[0]


def _pick_video(out_dir: Path) -> Path | None:
    # Merged output: video.mp4
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return candidate
    # Unmerged Bilibili-style output: video.f100024.mp4
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        for p in out_dir.glob("video.f*"):
            if p.suffix.lower() in VIDEO_EXTS:
                return p
    return None


def fetch_captions(url: str, out_dir: Path) -> dict:
    """Fetch metadata and best available VTT captions without downloading video."""
    _yt_dlp_cmd()  # validate availability

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    cmd = [*_yt_dlp_cmd(),
        "--skip-download",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
        "--",
        url,
    ]
    subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)
    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)
    return {
        "video_path": None,
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": False,
    }


def _read_info(info_path: Path, url: str) -> dict:
    info: dict = {}
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {
                "title": raw.get("title"),
                "uploader": raw.get("uploader") or raw.get("channel"),
                "duration": raw.get("duration"),
                "url": raw.get("webpage_url") or url,
            }
        except Exception as exc:
            print(f"[watch] info.json parse failed: {exc}", file=sys.stderr)
            info = {"url": url}
    return info


def download_url(
    url: str,
    out_dir: Path,
    audio_only: bool = False,
) -> dict:
    _yt_dlp_cmd()  # validate availability

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")

    fmt = "ba/bestaudio" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    cmd = [*_yt_dlp_cmd(),
        "-N", "8",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
        "--",
        url,
    ]

    # yt-dlp may exit non-zero if a subtitle variant fails (e.g. 429) even when
    # the video itself downloaded fine. Treat "video file present" as success.
    env = _ffmpeg_env()
    result = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, env=env)
    video = _pick_video(out_dir)
    if video is None:
        raise SystemExit(
            f"yt-dlp did not produce a video file in {out_dir} (exit {result.returncode})"
        )

    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)

    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def download(
    source: str,
    out_dir: Path,
    audio_only: bool = False,
) -> dict:
    if is_url(source):
        return download_url(source, out_dir, audio_only=audio_only)
    return resolve_local(source)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
