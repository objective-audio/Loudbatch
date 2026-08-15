"""Loudness normalization via ebur128 measure + linear volume gain."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .io_utils import (
    iter_audio_files,
    print_summary,
    probe_audio_stream,
    relative_under,
    run_ffmpeg,
    validate_linear_pcm,
)
from .measure import measure_file

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
    """Choose PCM encoder args based on source extension and stream format."""
    ext = src.suffix.lower()
    probed = stream if stream is not None else probe_audio_stream(src)
    if probed:
        return ["-c:a", pcm_codec_from_stream(ext, probed)]
    return ["-c:a", _with_pcm_endian("pcm_s24", _pcm_endian_for_ext(ext))]


def output_encode_args(src: Path) -> List[str]:
    """Encoder args plus sample-rate/channel preservation."""
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


def normalize_file(
    src: Path,
    dst: Path,
    *,
    target_i: float,
) -> Tuple[bool, str]:
    pcm_error = validate_linear_pcm(src)
    if pcm_error:
        return False, pcm_error

    measured = measure_file(src)
    if measured["status"] != "ok":
        return False, str(measured.get("error") or "計測に失敗しました")

    try:
        integrated = float(measured["integrated_lufs"])  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, "Integrated ラウドネスを取得できませんでした"

    if not math.isfinite(integrated):
        return False, "Integrated ラウドネスが無効です（無音など）"

    # ebur128 absolute gate floor; pure silence typically reports -70.0 LUFS
    if integrated <= -70.0:
        return False, "Integrated ラウドネスが無効です（無音など）"

    gain_db = target_i - integrated

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    result = run_ffmpeg(
        [
            "-hide_banner",
            "-nostats",
            "-y",
            "-i",
            str(src),
            "-af",
            f"volume={gain_db}dB",
            *output_encode_args(src),
            str(dst),
        ]
    )
    if result.returncode != 0 or not dst.is_file():
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()[-500:]
        return False, detail
    return True, ""


def normalize_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    target_i: float = -23.0,
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
