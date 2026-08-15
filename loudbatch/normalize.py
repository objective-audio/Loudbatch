"""Loudness normalization via ebur128 measure + linear volume gain."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .io_utils import (
    NORMALIZE_CSV_FIELDNAMES,
    iter_audio_files,
    print_summary,
    probe_audio_stream,
    relative_under,
    run_ffmpeg,
    validate_linear_pcm,
    write_csv,
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

_RE_PEAK_LEVEL_DB = re.compile(
    r"Peak level dB:\s*([+-]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


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


def measure_sample_peak_db(path: Path) -> Optional[float]:
    """Return overall sample peak level in dBFS via ffmpeg astats."""
    result = run_ffmpeg(
        [
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "astats=measure_perchannel=0",
            "-f",
            "null",
            "-",
        ]
    )
    matches = _RE_PEAK_LEVEL_DB.findall(result.stderr or "")
    if not matches:
        return None
    return float(matches[-1])


def _peak_over_flag(peak_db: Optional[float]) -> str:
    if peak_db is None or not math.isfinite(peak_db):
        return ""
    return "yes" if peak_db > 0.0 else "no"


def _empty_normalize_row(src: Path, dst: Path) -> Dict[str, object]:
    return {
        "filename": src.name,
        "path": str(src),
        "output": str(dst),
        "status": "error",
        "error": "",
        "integrated_lufs": "",
        "gain_db": "",
        "sample_peak_db": "",
        "true_peak_db": "",
        "sample_peak_over": "",
        "true_peak_over": "",
    }


def normalize_file(
    src: Path,
    dst: Path,
    *,
    target_i: float,
) -> Dict[str, object]:
    row = _empty_normalize_row(src, dst)

    pcm_error = validate_linear_pcm(src)
    if pcm_error:
        row["error"] = pcm_error
        return row

    measured = measure_file(src)
    if measured["status"] != "ok":
        row["error"] = str(measured.get("error") or "計測に失敗しました")
        return row

    try:
        integrated = float(measured["integrated_lufs"])  # type: ignore[arg-type]
    except (TypeError, ValueError):
        row["error"] = "Integrated ラウドネスを取得できませんでした"
        return row

    if not math.isfinite(integrated):
        row["error"] = "Integrated ラウドネスが無効です（無音など）"
        return row

    # ebur128 absolute gate floor; pure silence typically reports -70.0 LUFS
    if integrated <= -70.0:
        row["error"] = "Integrated ラウドネスが無効です（無音など）"
        return row

    gain_db = target_i - integrated
    row["integrated_lufs"] = integrated
    row["gain_db"] = gain_db

    sample_peak_in = measure_sample_peak_db(src)
    true_peak_in: Optional[float] = None
    raw_tp = measured.get("true_peak_db", "")
    if raw_tp not in ("", None):
        try:
            true_peak_in = float(raw_tp)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            true_peak_in = None

    sample_peak_after = (
        sample_peak_in + gain_db if sample_peak_in is not None else None
    )
    true_peak_after = true_peak_in + gain_db if true_peak_in is not None else None

    row["sample_peak_db"] = "" if sample_peak_after is None else sample_peak_after
    row["true_peak_db"] = "" if true_peak_after is None else true_peak_after
    row["sample_peak_over"] = _peak_over_flag(sample_peak_after)
    row["true_peak_over"] = _peak_over_flag(true_peak_after)

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
        row["error"] = detail
        return row

    row["status"] = "ok"
    return row


def normalize_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    target_i: float = -23.0,
    recursive: bool = False,
) -> List[Dict[str, object]]:
    output_csv = output_dir / "loudbatch_normalize.csv"
    files = iter_audio_files(input_dir, recursive=recursive)
    if not files:
        print(f"音声ファイルが見つかりません: {input_dir}")
        write_csv(output_csv, [], fieldnames=NORMALIZE_CSV_FIELDNAMES)
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
        row = normalize_file(
            src,
            dst,
            target_i=target_i,
        )
        rows.append(row)
        if row["status"] == "ok":
            ok += 1
            print("  完了")
            if row.get("sample_peak_over") == "yes":
                print(
                    f"  警告: サンプルピークが 0 dBFS を超えます"
                    f" ({row.get('sample_peak_db')} dBFS)"
                )
            if row.get("true_peak_over") == "yes":
                print(
                    f"  警告: True Peak が 0 dBTP を超えます"
                    f" ({row.get('true_peak_db')} dBTP)"
                )
        else:
            failed += 1
            print(f"  失敗: {row['error']}")

    write_csv(output_csv, rows, fieldnames=NORMALIZE_CSV_FIELDNAMES)
    print(f"CSV 書き出し: {output_csv}")
    print_summary("normalize", ok, failed)
    return rows
