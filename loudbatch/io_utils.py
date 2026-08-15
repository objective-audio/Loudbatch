"""Filesystem helpers and CSV writing for loudness tools."""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

PCM_EXTENSIONS = {
    ".wav",
    ".aiff",
    ".aif",
}

REJECTED_AUDIO_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
}

AUDIO_EXTENSIONS = PCM_EXTENSIONS | REJECTED_AUDIO_EXTENSIONS

CSV_FIELDNAMES = [
    "filename",
    "path",
    "integrated_lufs",
    "lra",
    "true_peak_db",
    "status",
    "error",
]

NORMALIZE_CSV_FIELDNAMES = [
    "filename",
    "path",
    "output",
    "status",
    "error",
    "integrated_lufs",
    "gain_db",
    "sample_peak_db",
    "true_peak_db",
    "sample_peak_over",
    "true_peak_over",
]


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise SystemExit(
            "ffmpeg が見つかりません。Homebrew なら `brew install ffmpeg` を実行してください。"
        )
    return path


def require_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise SystemExit(
            "ffprobe が見つかりません。Homebrew なら `brew install ffmpeg` を実行してください。"
        )
    return path


def probe_audio_stream(path: Path) -> Optional[Dict[str, Any]]:
    """Return the first audio stream metadata from ffprobe, or None on failure."""
    ffprobe = require_ffprobe()
    result = subprocess.run(
        [
            ffprobe,
            "-hide_banner",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-select_streams",
            "a:0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = payload.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    return stream if isinstance(stream, dict) else None


def validate_linear_pcm(path: Path) -> Optional[str]:
    """Return an error message if the file is not linear PCM; otherwise None."""
    ext = path.suffix.lower()
    if ext not in PCM_EXTENSIONS:
        return f"リニアPCM以外の形式はサポートしていません ({ext})"

    stream = probe_audio_stream(path)
    if not stream:
        return "音声ストリームを取得できませんでした"

    codec_name = str(stream.get("codec_name") or "")
    if not codec_name.startswith("pcm_"):
        label = codec_name or "unknown"
        return f"リニアPCM以外のコーデックはサポートしていません ({label})"
    return None


def iter_audio_files(directory: Path, recursive: bool = False) -> List[Path]:
    if not directory.is_dir():
        raise SystemExit(f"入力ディレクトリが存在しません: {directory}")

    if recursive:
        candidates = directory.rglob("*")
    else:
        candidates = directory.iterdir()

    files = [
        p.resolve()
        for p in candidates
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    return sorted(files, key=lambda p: str(p).lower())


def relative_under(root: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return Path(path.name)


def write_csv(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    fieldnames: Sequence[str] = CSV_FIELDNAMES,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def run_ffmpeg(args: Sequence[str], *, check: bool = False) -> subprocess.CompletedProcess:
    ffmpeg = require_ffmpeg()
    return subprocess.run(
        [ffmpeg, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def print_summary(label: str, ok: int, failed: int, skipped: int = 0) -> None:
    parts = [f"{label}: 成功 {ok}", f"失敗 {failed}"]
    if skipped:
        parts.append(f"スキップ {skipped}")
    print(", ".join(parts))
