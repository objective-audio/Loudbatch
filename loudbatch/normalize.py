"""Two-pass loudness normalization using ffmpeg loudnorm."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .io_utils import (
    iter_audio_files,
    print_summary,
    probe_audio_stream,
    relative_under,
    run_ffmpeg,
)

_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)

_PCM_BASE_FROM_SAMPLE_FMT = {
    "u8": "pcm_u8",
    "u8p": "pcm_u8",
    "s16": "pcm_s16",
    "s16p": "pcm_s16",
    "s24": "pcm_s24",
    "s32": "pcm_s32",
    "s32p": "pcm_s32",
    "flt": "pcm_f32",
    "fltp": "pcm_f32",
    "dbl": "pcm_f64",
    "dblp": "pcm_f64",
}


def parse_loudnorm_json(stderr: str) -> Dict[str, str]:
    """Extract the loudnorm measurement JSON object from ffmpeg stderr."""
    matches = _JSON_OBJECT_RE.findall(stderr or "")
    for blob in reversed(matches):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if "input_i" in data and "input_tp" in data:
            return {k: str(v) for k, v in data.items()}
    raise ValueError("loudnorm 計測 JSON が見つかりませんでした")


def _pcm_endian_for_ext(ext: str) -> str:
    if ext in {".aiff", ".aif"}:
        return "be"
    return "le"


def _with_pcm_endian(codec: str, endian: str) -> str:
    if codec == "pcm_u8":
        return codec
    if codec.endswith("le") or codec.endswith("be"):
        return codec[:-2] + endian
    return f"{codec}{endian}"


def pcm_codec_from_stream(ext: str, stream: Mapping[str, Any]) -> str:
    """Pick a PCM encoder that matches the source stream as closely as possible."""
    endian = _pcm_endian_for_ext(ext)
    codec_name = str(stream.get("codec_name") or "")
    if codec_name.startswith("pcm_"):
        return _with_pcm_endian(codec_name, endian)

    sample_fmt = str(stream.get("sample_fmt") or "")
    bits_raw = stream.get("bits_per_raw_sample")
    bits: Optional[int] = None
    if bits_raw not in (None, "", "0", 0):
        try:
            bits = int(bits_raw)
        except (TypeError, ValueError):
            bits = None

    if sample_fmt in {"s32", "s32p"} and bits == 24:
        return _with_pcm_endian("pcm_s24", endian)

    base = _PCM_BASE_FROM_SAMPLE_FMT.get(sample_fmt)
    if base:
        return _with_pcm_endian(base, endian)

    return _with_pcm_endian("pcm_s24", endian)


def output_codec_args(src: Path, stream: Optional[Mapping[str, Any]] = None) -> List[str]:
    """Choose encoder args based on source extension (and PCM format when applicable)."""
    ext = src.suffix.lower()
    if ext == ".wav" or ext in {".aiff", ".aif"}:
        probed = stream if stream is not None else probe_audio_stream(src)
        if probed:
            return ["-c:a", pcm_codec_from_stream(ext, probed)]
        return ["-c:a", _with_pcm_endian("pcm_s24", _pcm_endian_for_ext(ext))]
    if ext == ".flac":
        return ["-c:a", "flac"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-q:a", "0"]
    if ext in {".m4a", ".aac"}:
        return ["-c:a", "aac", "-b:a", "256k"]
    if ext == ".ogg":
        return ["-c:a", "libvorbis", "-q:a", "6"]
    if ext == ".opus":
        return ["-c:a", "libopus", "-b:a", "192k"]
    return ["-c:a", "pcm_s24le"]


def output_encode_args(src: Path) -> List[str]:
    """Encoder args plus sample-rate/channel restoration after loudnorm."""
    stream = probe_audio_stream(src)
    args = output_codec_args(src, stream)
    if not stream:
        return args
    sample_rate = stream.get("sample_rate")
    if sample_rate not in (None, ""):
        args.extend(["-ar", str(sample_rate)])
    channels = stream.get("channels")
    if channels not in (None, ""):
        args.extend(["-ac", str(channels)])
    return args


def _loudnorm_filter(
    target_i: float,
    target_tp: float,
    target_lra: float,
    measured: Optional[Dict[str, str]] = None,
) -> str:
    parts = [
        f"I={target_i}",
        f"TP={target_tp}",
        f"LRA={target_lra}",
    ]
    if measured is None:
        parts.append("print_format=json")
    else:
        parts.extend(
            [
                f"measured_I={measured['input_i']}",
                f"measured_TP={measured['input_tp']}",
                f"measured_LRA={measured['input_lra']}",
                f"measured_thresh={measured['input_thresh']}",
                f"offset={measured['target_offset']}",
                "linear=true",
                "print_format=summary",
            ]
        )
    return "loudnorm=" + ":".join(parts)


def normalize_file(
    src: Path,
    dst: Path,
    *,
    target_i: float,
    target_tp: float,
    target_lra: float,
) -> Tuple[bool, str]:
    # Pass 1: measure
    pass1 = run_ffmpeg(
        [
            "-hide_banner",
            "-nostats",
            "-i",
            str(src),
            "-af",
            _loudnorm_filter(target_i, target_tp, target_lra),
            "-f",
            "null",
            "-",
        ]
    )
    try:
        measured = parse_loudnorm_json(pass1.stderr or "")
    except ValueError as exc:
        detail = (pass1.stderr or pass1.stdout or str(exc)).strip()[-500:]
        return False, detail

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    # Pass 2: apply linear normalization
    pass2 = run_ffmpeg(
        [
            "-hide_banner",
            "-nostats",
            "-y",
            "-i",
            str(src),
            "-af",
            _loudnorm_filter(target_i, target_tp, target_lra, measured),
            *output_encode_args(src),
            str(dst),
        ]
    )
    if pass2.returncode != 0 or not dst.is_file():
        detail = (pass2.stderr or pass2.stdout or f"exit {pass2.returncode}").strip()[-500:]
        return False, detail
    return True, ""


def normalize_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    target_i: float = -23.0,
    target_tp: float = -1.0,
    target_lra: float = 7.0,
    recursive: bool = False,
) -> List[Dict[str, object]]:
    files = iter_audio_files(input_dir, recursive=recursive)
    if not files:
        print(f"音声ファイルが見つかりません: {input_dir}")
        print_summary("normalize", 0, 0)
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, object]] = []
    ok = 0
    failed = 0

    for src in files:
        rel = relative_under(input_dir, src)
        dst = (output_dir / rel).resolve()
        print(f"正規化中: {src.name} → {dst}")
        success, error = normalize_file(
            src,
            dst,
            target_i=target_i,
            target_tp=target_tp,
            target_lra=target_lra,
        )
        row: Dict[str, object] = {
            "filename": src.name,
            "path": str(src),
            "output": str(dst),
            "status": "ok" if success else "error",
            "error": error,
        }
        rows.append(row)
        if success:
            ok += 1
            print("  完了")
        else:
            failed += 1
            print(f"  失敗: {error}")

    print_summary("normalize", ok, failed)
    return rows
