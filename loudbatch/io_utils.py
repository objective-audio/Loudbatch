"""Filesystem helpers and CSV writing for loudness tools."""

from __future__ import annotations

import csv
import shutil
import subprocess
from pathlib import Path
from typing import List, Mapping, Sequence

AUDIO_EXTENSIONS = {
    ".wav",
    ".flac",
    ".aiff",
    ".aif",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
}

CSV_FIELDNAMES = [
    "filename",
    "path",
    "integrated_lufs",
    "lra",
    "true_peak_db",
    "status",
    "error",
]


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise SystemExit(
            "ffmpeg が見つかりません。Homebrew なら `brew install ffmpeg` を実行してください。"
        )
    return path


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


def write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_FIELDNAMES})


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
