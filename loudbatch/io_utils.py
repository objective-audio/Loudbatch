"""Filesystem helpers and CSV writing for loudness tools."""

from __future__ import annotations

import csv
import json
import math
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
    "integrated_lufs",
    "status",
    "error",
]

NORMALIZE_CSV_FIELDNAMES = [
    "filename",
    "status",
    "error",
    "input_lufs",
    "target_lufs",
    "gain_db",
    "sample_peak_status",
    "true_peak_status",
]

# --column で使える内部名（入力 CSV の目標列 + 出力 CSV 列）
NORMALIZE_COLUMN_KEYS = list(
    dict.fromkeys(["integrated_lufs", *NORMALIZE_CSV_FIELDNAMES])
)


def format_lufs(value: float) -> str:
    return f"{value:.1f}"


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


def csv_filename(path: Path) -> str:
    """CSV に書くファイル名（拡張子なし）。"""
    return path.stem


def csv_filename_key(name: str) -> str:
    """CSV の filename を照合キーにする。既知の音声拡張子は落とす。"""
    suffix = Path(name).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return Path(name).stem
    return name


def mapped_fieldnames(
    fieldnames: Sequence[str],
    column_map: Optional[Mapping[str, str]] = None,
) -> List[str]:
    mapping = dict(column_map or {})
    headers = [mapping.get(name, name) for name in fieldnames]
    seen: set[str] = set()
    for header in headers:
        if header in seen:
            raise SystemExit(f"複数の列が同じ CSV 列名にマップされています: {header}")
        seen.add(header)
    return headers


def parse_column_map(
    pairs: Sequence[str],
    allowed: Sequence[str],
) -> Dict[str, str]:
    """Parse repeated --column INTERNAL=NAME arguments into a rename map."""
    allowed_set = set(allowed)
    result: Dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise SystemExit(
                f"--column は 内部名=CSV列名 の形式で指定してください: {raw}"
            )
        key, name = raw.split("=", 1)
        key = key.strip()
        name = name.strip()
        if not key or not name:
            raise SystemExit(
                f"--column は 内部名=CSV列名 の形式で指定してください: {raw}"
            )
        if key not in allowed_set:
            allowed_list = ", ".join(allowed)
            raise SystemExit(f"不明な列名です: {key}（使えるのは: {allowed_list}）")
        if key in result:
            raise SystemExit(f"同じ列が複数指定されています: {key}")
        result[key] = name
    mapped_fieldnames(allowed, result)
    return result


def load_targets_csv(
    path: Path,
    *,
    column_map: Optional[Mapping[str, str]] = None,
) -> Dict[str, float]:
    """Load per-file target Integrated LUFS from a measure CSV."""
    if not path.is_file():
        raise SystemExit(f"目標 CSV が見つかりません: {path}")

    mapping = dict(column_map or {})
    filename_col = mapping.get("filename", "filename")
    lufs_col = mapping.get("integrated_lufs", "integrated_lufs")
    status_col = mapping.get("status", "status")

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        if filename_col not in fieldnames or lufs_col not in fieldnames:
            raise SystemExit(
                f"目標 CSV に {filename_col} と {lufs_col} 列が必要です"
            )

        has_status = status_col in fieldnames
        seen: set[str] = set()
        targets: Dict[str, float] = {}
        for row in reader:
            name = (row.get(filename_col) or "").strip()
            if not name:
                continue
            key = csv_filename_key(name)
            if key in seen:
                raise SystemExit(f"目標 CSV に同じファイル名が複数あります: {name}")
            seen.add(key)

            if has_status and (row.get(status_col) or "").strip() != "ok":
                continue
            raw = (row.get(lufs_col) or "").strip()
            try:
                value = float(raw)
            except ValueError:
                raise SystemExit(
                    f"目標 CSV の {lufs_col} が数値ではありません: {name}"
                )
            if not math.isfinite(value):
                raise SystemExit(f"目標 CSV の {lufs_col} が無効です: {name}")
            targets[key] = value
    return targets


def write_csv(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    fieldnames: Sequence[str] = CSV_FIELDNAMES,
    *,
    column_map: Optional[Mapping[str, str]] = None,
) -> None:
    mapping = dict(column_map or {})
    headers = mapped_fieldnames(fieldnames, mapping)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {mapping.get(key, key): row.get(key, "") for key in fieldnames}
            )


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
