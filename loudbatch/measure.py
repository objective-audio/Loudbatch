"""Integrated loudness measurement using ffmpeg ebur128."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from .io_utils import iter_audio_files, print_summary, run_ffmpeg, validate_linear_pcm, write_csv

# Final summary lines look like:
#   I:         -23.0 LUFS
#   Peak:       -1.02 dBFS   (when peak=true; True Peak may appear as True peak)
_RE_INTEGRATED = re.compile(r"^\s*I:\s*([+-]?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)
_RE_TRUE_PEAK = re.compile(
    r"^\s*(?:True peak|Peak):\s*([+-]?\d+(?:\.\d+)?)\s*dB(?:FS|TP)?",
    re.MULTILINE | re.IGNORECASE,
)


def parse_ebur128(stderr: str) -> Dict[str, Optional[float]]:
    """Parse Integrated / True Peak from ebur128 stderr summary."""
    # Prefer the last Summary block if present (ebur128 prints ongoing + final).
    summary = stderr
    marker = "Summary:"
    if marker in stderr:
        summary = stderr.rsplit(marker, 1)[-1]

    def _first(pattern: re.Pattern[str]) -> Optional[float]:
        matches = pattern.findall(summary)
        if not matches:
            return None
        return float(matches[-1])

    return {
        "integrated_lufs": _first(_RE_INTEGRATED),
        "true_peak_db": _first(_RE_TRUE_PEAK),
    }


def measure_file(path: Path) -> Dict[str, object]:
    row: Dict[str, object] = {
        "filename": path.name,
        "integrated_lufs": "",
        "true_peak_db": "",
        "status": "error",
        "error": "",
    }
    pcm_error = validate_linear_pcm(path)
    if pcm_error:
        row["error"] = pcm_error
        return row

    try:
        result = run_ffmpeg(
            [
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-af",
                "ebur128=peak=true",
                "-f",
                "null",
                "-",
            ]
        )
    except OSError as exc:
        row["error"] = str(exc)
        return row

    parsed = parse_ebur128(result.stderr or "")
    if parsed["integrated_lufs"] is None:
        detail = (result.stderr or result.stdout or "").strip()
        tail = detail[-500:] if detail else f"exit {result.returncode}"
        row["error"] = f"ebur128 の結果を解析できませんでした: {tail}"
        return row

    row["integrated_lufs"] = parsed["integrated_lufs"]
    row["true_peak_db"] = "" if parsed["true_peak_db"] is None else parsed["true_peak_db"]
    row["status"] = "ok"
    return row


def measure_directory(
    input_dir: Path,
    output_csv: Path,
    *,
    recursive: bool = False,
) -> List[Dict[str, object]]:
    files = iter_audio_files(input_dir, recursive=recursive)
    if not files:
        print(f"音声ファイルが見つかりません: {input_dir}")
        write_csv(output_csv, [])
        print_summary("measure", 0, 0)
        return []

    rows: List[Dict[str, object]] = []
    ok = 0
    failed = 0
    for path in files:
        print(f"計測中: {path.name}")
        row = measure_file(path)
        rows.append(row)
        if row["status"] == "ok":
            ok += 1
            print(f"  I={row['integrated_lufs']} LUFS")
        else:
            failed += 1
            print(f"  失敗: {row['error']}")

    write_csv(output_csv, rows)
    print(f"CSV 書き出し: {output_csv}")
    print_summary("measure", ok, failed)
    return rows
