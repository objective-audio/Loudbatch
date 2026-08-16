"""Integration tests for measure CSV and normalize using sine-wave fixtures."""

from __future__ import annotations

import csv
import math
import shutil
import subprocess
import unittest
from pathlib import Path

from loudbatch.io_utils import (
    CSV_FIELDNAMES,
    NORMALIZE_COLUMN_KEYS,
    NORMALIZE_CSV_FIELDNAMES,
    csv_filename,
    csv_filename_key,
    duration_from_stream,
    load_targets_csv,
    mapped_fieldnames,
    parse_column_map,
    probe_audio_stream,
    require_ffmpeg,
    validate_linear_pcm,
    write_csv,
)
from loudbatch.measure import ebur128_filter, measure_directory, measure_file
from loudbatch.normalize import _peak_status, normalize_directory, pcm_codec_from_stream

REPO_ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = REPO_ROOT / "workspace" / "test"
TARGET_I = -23.0
TOLERANCE_LU = 1.5


def _ffmpeg_available() -> bool:
    try:
        require_ffmpeg()
    except SystemExit:
        return False
    return True


def _clean_work_root() -> None:
    if WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)


def _generate_sine(
    dst: Path,
    *,
    volume_db: float,
    duration: float = 3.0,
    codec: str = "pcm_s16le",
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = require_ffmpeg()
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=1000:duration={duration}",
            "-af",
            f"volume={volume_db}dB",
            "-c:a",
            codec,
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not dst.is_file():
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()[-500:]
        raise RuntimeError(f"sine WAV の生成に失敗しました: {detail}")


class PcmCodecFromStreamTests(unittest.TestCase):
    def test_prefers_codec_name_with_container_endian(self) -> None:
        self.assertEqual(
            pcm_codec_from_stream(".wav", {"codec_name": "pcm_s16le"}),
            "pcm_s16le",
        )
        self.assertEqual(
            pcm_codec_from_stream(".aiff", {"codec_name": "pcm_s16le"}),
            "pcm_s16be",
        )
        self.assertEqual(
            pcm_codec_from_stream(".wav", {"codec_name": "pcm_s24be"}),
            "pcm_s24le",
        )

    def test_maps_sample_fmt_and_24bit_s32(self) -> None:
        self.assertEqual(
            pcm_codec_from_stream(".wav", {"sample_fmt": "s16"}),
            "pcm_s16le",
        )
        self.assertEqual(
            pcm_codec_from_stream(
                ".wav",
                {"sample_fmt": "s32", "bits_per_raw_sample": "24"},
            ),
            "pcm_s24le",
        )
        self.assertEqual(
            pcm_codec_from_stream(".aif", {"sample_fmt": "flt"}),
            "pcm_f32be",
        )
        self.assertEqual(
            pcm_codec_from_stream(".wav", {}),
            "pcm_s24le",
        )


class ValidateLinearPcmTests(unittest.TestCase):
    def test_rejected_extension_message(self) -> None:
        err = validate_linear_pcm(Path("sample.mp3"))
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn(".mp3", err)
        self.assertIn("リニアPCM以外の形式", err)


def _generate_silence(dst: Path, *, duration: float = 3.0) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = require_ffmpeg()
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono",
            "-t",
            str(duration),
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not dst.is_file():
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()[-500:]
        raise RuntimeError(f"無音 WAV の生成に失敗しました: {detail}")


def _write_targets_csv(path: Path, targets: dict[str, float]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(
        path,
        [
            {
                "filename": name,
                "integrated_lufs": value,
                "status": "ok",
                "error": "",
            }
            for name, value in targets.items()
        ],
    )
    return path


def _rows_by_filename(
    csv_path: Path,
    fieldnames: list[str] | None = None,
    *,
    filename_key: str = "filename",
) -> dict[str, dict[str, str]]:
    expected = list(fieldnames) if fieldnames is not None else list(CSV_FIELDNAMES)
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == expected
        return {row[filename_key]: row for row in reader}


class CsvFilenameHelperTests(unittest.TestCase):
    def test_csv_filename_uses_stem(self) -> None:
        self.assertEqual(csv_filename(Path("loud.wav")), "loud")
        self.assertEqual(csv_filename(Path("tone.mp3")), "tone")
        self.assertEqual(csv_filename(Path("foo.bar.aiff")), "foo.bar")

    def test_csv_filename_key_strips_known_audio_extensions(self) -> None:
        self.assertEqual(csv_filename_key("loud.wav"), "loud")
        self.assertEqual(csv_filename_key("loud"), "loud")
        self.assertEqual(csv_filename_key("tone.mp3"), "tone")
        self.assertEqual(csv_filename_key("notes.txt"), "notes.txt")


class LoadTargetsCsvTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.csv_path = WORK_ROOT / "targets.csv"

    def tearDown(self) -> None:
        _clean_work_root()

    def test_loads_ok_rows_and_skips_errors(self) -> None:
        write_csv(
            self.csv_path,
            [
                {
                    "filename": "keep.wav",
                    "integrated_lufs": -18.5,
                    "status": "ok",
                    "error": "",
                },
                {
                    "filename": "bad.wav",
                    "integrated_lufs": "",
                    "status": "error",
                    "error": "計測に失敗しました",
                },
            ],
        )
        targets = load_targets_csv(self.csv_path)
        self.assertEqual(targets, {"keep": -18.5})

    def test_rejects_duplicate_filenames(self) -> None:
        write_csv(
            self.csv_path,
            [
                {
                    "filename": "dup.wav",
                    "integrated_lufs": -23.0,
                    "status": "ok",
                    "error": "",
                },
                {
                    "filename": "dup.wav",
                    "integrated_lufs": -18.0,
                    "status": "ok",
                    "error": "",
                },
            ],
        )
        with self.assertRaises(SystemExit) as ctx:
            load_targets_csv(self.csv_path)
        self.assertIn("同じファイル名", str(ctx.exception))

    def test_loads_stem_without_extension(self) -> None:
        write_csv(
            self.csv_path,
            [
                {
                    "filename": "keep",
                    "integrated_lufs": -18.5,
                    "status": "ok",
                    "error": "",
                }
            ],
        )
        targets = load_targets_csv(self.csv_path)
        self.assertEqual(targets, {"keep": -18.5})

    def test_rejects_duplicate_stems_with_different_extensions(self) -> None:
        write_csv(
            self.csv_path,
            [
                {
                    "filename": "dup.wav",
                    "integrated_lufs": -23.0,
                    "status": "ok",
                    "error": "",
                },
                {
                    "filename": "dup.aiff",
                    "integrated_lufs": -18.0,
                    "status": "ok",
                    "error": "",
                },
            ],
        )
        with self.assertRaises(SystemExit) as ctx:
            load_targets_csv(self.csv_path)
        self.assertIn("同じファイル名", str(ctx.exception))

    def test_missing_file(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            load_targets_csv(self.csv_path)
        self.assertIn("見つかりません", str(ctx.exception))

    def test_reads_remapped_column_names(self) -> None:
        write_csv(
            self.csv_path,
            [
                {
                    "filename": "keep.wav",
                    "integrated_lufs": -18.5,
                    "status": "ok",
                    "error": "",
                }
            ],
            column_map={"filename": "ファイル名", "integrated_lufs": "目標LUFS"},
        )
        targets = load_targets_csv(
            self.csv_path,
            column_map={"filename": "ファイル名", "integrated_lufs": "目標LUFS"},
        )
        self.assertEqual(targets, {"keep": -18.5})

    def test_accepts_rows_when_status_column_is_absent(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["filename", "integrated_lufs"])
            writer.writeheader()
            writer.writerow({"filename": "keep.wav", "integrated_lufs": "-23.0"})
        targets = load_targets_csv(self.csv_path)
        self.assertEqual(targets, {"keep": -23.0})

    def test_skips_non_ok_when_status_column_is_remapped(self) -> None:
        write_csv(
            self.csv_path,
            [
                {
                    "filename": "keep.wav",
                    "integrated_lufs": -18.5,
                    "status": "ok",
                    "error": "",
                },
                {
                    "filename": "bad.wav",
                    "integrated_lufs": -12.0,
                    "status": "error",
                    "error": "計測に失敗しました",
                },
            ],
            column_map={"status": "状態"},
        )
        targets = load_targets_csv(self.csv_path, column_map={"status": "状態"})
        self.assertEqual(targets, {"keep": -18.5})


class ParseColumnMapTests(unittest.TestCase):
    def test_parses_pairs_and_strips_whitespace(self) -> None:
        mapping = parse_column_map(
            ["filename=ファイル名", " integrated_lufs = LUFS "],
            CSV_FIELDNAMES,
        )
        self.assertEqual(
            mapping,
            {"filename": "ファイル名", "integrated_lufs": "LUFS"},
        )

    def test_empty_pairs_returns_empty_map(self) -> None:
        self.assertEqual(parse_column_map([], CSV_FIELDNAMES), {})

    def test_rejects_missing_equals(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            parse_column_map(["filename"], CSV_FIELDNAMES)
        self.assertIn("内部名=CSV列名", str(ctx.exception))

    def test_rejects_unknown_internal_name(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            parse_column_map(["target_lufs=目標"], CSV_FIELDNAMES)
        self.assertIn("不明な列名", str(ctx.exception))

    def test_rejects_duplicate_internal_name(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            parse_column_map(
                ["filename=A", "filename=B"],
                CSV_FIELDNAMES,
            )
        self.assertIn("同じ列が複数", str(ctx.exception))

    def test_rejects_duplicate_csv_names(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            parse_column_map(
                ["filename=同じ", "status=同じ"],
                CSV_FIELDNAMES,
            )
        self.assertIn("同じ CSV 列名", str(ctx.exception))

    def test_rejects_collision_with_unmapped_name(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            parse_column_map(["filename=status"], CSV_FIELDNAMES)
        self.assertIn("同じ CSV 列名", str(ctx.exception))

    def test_normalize_keys_accept_input_and_target_columns(self) -> None:
        mapping = parse_column_map(
            ["integrated_lufs=目標LUFS", "input_lufs=入力LUFS"],
            NORMALIZE_COLUMN_KEYS,
        )
        self.assertEqual(
            mapping,
            {"integrated_lufs": "目標LUFS", "input_lufs": "入力LUFS"},
        )


class WriteCsvColumnMapTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.csv_path = WORK_ROOT / "mapped.csv"

    def tearDown(self) -> None:
        _clean_work_root()

    def test_writes_mapped_headers_from_internal_keys(self) -> None:
        column_map = {"filename": "ファイル名", "integrated_lufs": "LUFS"}
        write_csv(
            self.csv_path,
            [
                {
                    "filename": "a",
                    "integrated_lufs": -23.0,
                    "status": "ok",
                    "error": "",
                }
            ],
            column_map=column_map,
        )
        expected = mapped_fieldnames(CSV_FIELDNAMES, column_map)
        with self.csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            self.assertEqual(reader.fieldnames, expected)
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ファイル名"], "a")
        self.assertEqual(rows[0]["LUFS"], "-23.0")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertNotIn("filename", rows[0])



@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class MeasureNormalizeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "input"
        self.measure_out = WORK_ROOT / "measure_out"
        self.normalize_out = WORK_ROOT / "normalize_out"
        self.remeasure_out = WORK_ROOT / "remeasure_out"
        self.loud_wav = self.input_dir / "loud.wav"
        self.quiet_wav = self.input_dir / "quiet.wav"

        _generate_sine(self.loud_wav, volume_db=6.0)
        _generate_sine(self.quiet_wav, volume_db=-36.0)

    def tearDown(self) -> None:
        _clean_work_root()

    def test_measure_csv_and_normalize_to_target(self) -> None:
        measure_csv = self.measure_out / "loudbatch.csv"
        rows = measure_directory(self.input_dir, measure_csv)
        self.assertTrue(measure_csv.is_file())
        self.assertEqual(len(rows), 2)

        by_name = _rows_by_filename(measure_csv)
        self.assertEqual(set(by_name), {"loud", "quiet"})

        loud = by_name["loud"]
        quiet = by_name["quiet"]
        self.assertEqual(loud["status"], "ok")
        self.assertEqual(quiet["status"], "ok")

        self.assertRegex(loud["integrated_lufs"], r"^-?\d+\.\d$")
        self.assertRegex(quiet["integrated_lufs"], r"^-?\d+\.\d$")
        loud_i = float(loud["integrated_lufs"])
        quiet_i = float(quiet["integrated_lufs"])
        self.assertGreater(loud_i, TARGET_I, msg=f"loud I={loud_i}")
        self.assertLess(quiet_i, TARGET_I, msg=f"quiet I={quiet_i}")

        normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=_write_targets_csv(
                WORK_ROOT / "targets.csv",
                {"loud": TARGET_I, "quiet": TARGET_I},
            ),
        )
        self.assertTrue((self.normalize_out / "loud.wav").is_file())
        self.assertTrue((self.normalize_out / "quiet.wav").is_file())
        normalize_csv = self.normalize_out / "loudbatch_normalize.csv"
        self.assertTrue(normalize_csv.is_file())
        norm_by_name = _rows_by_filename(
            normalize_csv,
            fieldnames=list(NORMALIZE_CSV_FIELDNAMES),
        )
        self.assertEqual(norm_by_name["loud"]["sample_peak_status"], "")
        self.assertEqual(norm_by_name["loud"]["true_peak_status"], "")
        self.assertRegex(norm_by_name["loud"]["input_lufs"], r"^-?\d+\.\d$")
        self.assertNotEqual(float(norm_by_name["loud"]["input_lufs"]), TARGET_I)
        self.assertEqual(float(norm_by_name["loud"]["target_lufs"]), TARGET_I)

        remeasure_csv = self.remeasure_out / "loudbatch.csv"
        rem_rows = measure_directory(self.normalize_out, remeasure_csv)
        self.assertEqual(len(rem_rows), 2)
        rem_by_name = _rows_by_filename(remeasure_csv)

        for name in ("loud", "quiet"):
            row = rem_by_name[name]
            self.assertEqual(row["status"], "ok", msg=row.get("error", ""))
            integrated = float(row["integrated_lufs"])
            self.assertLessEqual(
                abs(integrated - TARGET_I),
                TOLERANCE_LU,
                msg=f"{name} I={integrated} (target {TARGET_I} ± {TOLERANCE_LU})",
            )


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class ColumnMapDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "input"
        self.measure_out = WORK_ROOT / "measure_out"
        self.normalize_out = WORK_ROOT / "normalize_out"
        _generate_sine(self.input_dir / "tone.wav", volume_db=-6.0)

    def tearDown(self) -> None:
        _clean_work_root()

    def test_measure_and_normalize_use_column_map_headers(self) -> None:
        measure_map = {"filename": "ファイル名", "integrated_lufs": "LUFS"}
        measure_csv = self.measure_out / "loudbatch.csv"
        measure_directory(
            self.input_dir,
            measure_csv,
            column_map=measure_map,
        )
        measure_headers = mapped_fieldnames(CSV_FIELDNAMES, measure_map)
        by_name = _rows_by_filename(
            measure_csv,
            fieldnames=measure_headers,
            filename_key="ファイル名",
        )
        self.assertIn("tone", by_name)
        self.assertEqual(by_name["tone"]["status"], "ok")
        self.assertRegex(by_name["tone"]["LUFS"], r"^-?\d+\.\d$")

        normalize_map = {
            "filename": "ファイル名",
            "integrated_lufs": "LUFS",
            "input_lufs": "入力LUFS",
            "target_lufs": "目標LUFS",
        }
        targets_csv = WORK_ROOT / "targets.csv"
        write_csv(
            targets_csv,
            [
                {
                    "filename": "tone",
                    "integrated_lufs": TARGET_I,
                    "status": "ok",
                    "error": "",
                }
            ],
            column_map={"filename": "ファイル名", "integrated_lufs": "LUFS"},
        )
        normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=targets_csv,
            column_map=normalize_map,
        )
        self.assertTrue((self.normalize_out / "tone.wav").is_file())
        normalize_csv = self.normalize_out / "loudbatch_normalize.csv"
        norm_by_name = _rows_by_filename(
            normalize_csv,
            fieldnames=mapped_fieldnames(NORMALIZE_CSV_FIELDNAMES, normalize_map),
            filename_key="ファイル名",
        )
        row = norm_by_name["tone"]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(float(row["目標LUFS"]), TARGET_I)
        self.assertRegex(row["入力LUFS"], r"^-?\d+\.\d$")
        self.assertNotIn("LUFS", row)


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class NormalizeToMeasureCsvTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.reference_dir = WORK_ROOT / "reference"
        self.input_dir = WORK_ROOT / "processed"
        self.measure_out = WORK_ROOT / "reference_measure_out"
        self.normalize_out = WORK_ROOT / "match_normalize_out"
        self.remeasure_out = WORK_ROOT / "match_remeasure_out"
        _generate_sine(self.reference_dir / "loud.wav", volume_db=6.0)
        _generate_sine(self.reference_dir / "quiet.wav", volume_db=-36.0)
        _generate_sine(self.input_dir / "loud.wav", volume_db=-12.0)
        _generate_sine(self.input_dir / "quiet.wav", volume_db=-6.0)

    def tearDown(self) -> None:
        _clean_work_root()

    def test_normalize_matches_measured_reference_values(self) -> None:
        measure_csv = self.measure_out / "loudbatch.csv"
        measure_directory(self.reference_dir, measure_csv)
        ref_by_name = _rows_by_filename(measure_csv)

        normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=measure_csv,
        )
        self.assertTrue((self.normalize_out / "loud.wav").is_file())
        self.assertTrue((self.normalize_out / "quiet.wav").is_file())

        normalize_csv = self.normalize_out / "loudbatch_normalize.csv"
        norm_by_name = _rows_by_filename(
            normalize_csv,
            fieldnames=list(NORMALIZE_CSV_FIELDNAMES),
        )
        self.assertEqual(
            float(norm_by_name["loud"]["target_lufs"]),
            float(ref_by_name["loud"]["integrated_lufs"]),
        )
        self.assertEqual(
            float(norm_by_name["quiet"]["target_lufs"]),
            float(ref_by_name["quiet"]["integrated_lufs"]),
        )

        rem_csv = self.remeasure_out / "loudbatch.csv"
        measure_directory(self.normalize_out, rem_csv)
        rem_by_name = _rows_by_filename(rem_csv)
        for name in ("loud", "quiet"):
            target = float(ref_by_name[name]["integrated_lufs"])
            integrated = float(rem_by_name[name]["integrated_lufs"])
            self.assertLessEqual(
                abs(integrated - target),
                TOLERANCE_LU,
                msg=f"{name} I={integrated} (target {target} ± {TOLERANCE_LU})",
            )


class MissingCsvTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "missing_target_input"
        self.normalize_out = WORK_ROOT / "missing_target_out"
        self.input_dir.mkdir(parents=True)
        (self.input_dir / "tone.wav").write_bytes(b"not a wav file")

    def tearDown(self) -> None:
        _clean_work_root()

    def test_normalize_errors_when_filename_missing_from_csv(self) -> None:
        csv_path = _write_targets_csv(
            WORK_ROOT / "other.csv",
            {"other": TARGET_I},
        )
        rows = normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=csv_path,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["filename"], "tone")
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("目標値", str(rows[0]["error"]))
        self.assertFalse((self.normalize_out / "tone.wav").exists())


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class PreservePcmFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "pcm_input"
        self.normalize_out = WORK_ROOT / "pcm_normalize_out"

    def tearDown(self) -> None:
        _clean_work_root()

    def test_normalize_preserves_wav_pcm_codecs(self) -> None:
        cases = {
            "s16.wav": "pcm_s16le",
            "s24.wav": "pcm_s24le",
            "f32.wav": "pcm_f32le",
        }
        for name, codec in cases.items():
            _generate_sine(self.input_dir / name, volume_db=-6.0, codec=codec)

        normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=_write_targets_csv(
                WORK_ROOT / "pcm_targets.csv",
                {csv_filename(Path(name)): TARGET_I for name in cases},
            ),
        )

        for name, codec in cases.items():
            src = probe_audio_stream(self.input_dir / name)
            dst = probe_audio_stream(self.normalize_out / name)
            self.assertIsNotNone(src)
            self.assertIsNotNone(dst)
            assert src is not None and dst is not None
            self.assertEqual(src.get("codec_name"), codec)
            self.assertEqual(dst.get("codec_name"), codec)
            self.assertEqual(dst.get("sample_fmt"), src.get("sample_fmt"))
            self.assertEqual(dst.get("sample_rate"), src.get("sample_rate"))
            self.assertEqual(dst.get("channels"), src.get("channels"))


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class CompressedFormatRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "compressed_input"
        self.measure_out = WORK_ROOT / "compressed_measure_out"
        self.normalize_out = WORK_ROOT / "compressed_normalize_out"
        self.mp3_path = self.input_dir / "tone.mp3"
        _generate_sine(self.mp3_path, volume_db=-6.0, codec="libmp3lame")

    def tearDown(self) -> None:
        _clean_work_root()

    def test_measure_and_normalize_reject_mp3(self) -> None:
        measure_csv = self.measure_out / "loudbatch.csv"
        rows = measure_directory(self.input_dir, measure_csv)
        self.assertEqual(len(rows), 1)
        by_name = _rows_by_filename(measure_csv)
        row = by_name["tone"]
        self.assertEqual(row["status"], "error")
        self.assertIn(".mp3", row["error"])
        self.assertEqual(row["integrated_lufs"], "")

        norm_rows = normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=_write_targets_csv(
                WORK_ROOT / "mp3_targets.csv",
                {"tone": TARGET_I},
            ),
        )
        self.assertEqual(len(norm_rows), 1)
        norm = norm_rows[0]
        self.assertEqual(norm["filename"], "tone")
        self.assertEqual(norm["status"], "error")
        self.assertIn(".mp3", str(norm["error"]))
        self.assertFalse((self.normalize_out / "tone.mp3").exists())


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class MeasureNormalizeFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "fail_input"
        self.measure_out = WORK_ROOT / "fail_measure_out"
        self.normalize_out = WORK_ROOT / "fail_normalize_out"
        self.bad_wav = self.input_dir / "bad.wav"

        self.input_dir.mkdir(parents=True)
        self.bad_wav.write_bytes(b"not a wav file")

    def tearDown(self) -> None:
        _clean_work_root()

    def test_measure_and_normalize_fail_on_corrupt_wav(self) -> None:
        measure_csv = self.measure_out / "loudbatch.csv"
        rows = measure_directory(self.input_dir, measure_csv)
        self.assertTrue(measure_csv.is_file())
        self.assertEqual(len(rows), 1)

        by_name = _rows_by_filename(measure_csv)
        bad = by_name["bad"]
        self.assertEqual(bad["status"], "error")
        self.assertTrue(bad["error"])
        self.assertEqual(bad["integrated_lufs"], "")

        norm_rows = normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=_write_targets_csv(
                WORK_ROOT / "bad_targets.csv",
                {"bad": TARGET_I},
            ),
        )
        self.assertEqual(len(norm_rows), 1)
        norm = norm_rows[0]
        self.assertEqual(norm["filename"], "bad")
        self.assertEqual(norm["status"], "error")
        self.assertTrue(norm["error"])
        self.assertFalse((self.normalize_out / "bad.wav").exists())


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class SilenceNormalizeFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "silence_input"
        self.normalize_out = WORK_ROOT / "silence_normalize_out"
        self.silence_wav = self.input_dir / "silence.wav"
        _generate_silence(self.silence_wav)

    def tearDown(self) -> None:
        _clean_work_root()

    def test_normalize_fails_on_silence(self) -> None:
        norm_rows = normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=_write_targets_csv(
                WORK_ROOT / "silence_targets.csv",
                {"silence": TARGET_I},
            ),
        )
        self.assertEqual(len(norm_rows), 1)
        norm = norm_rows[0]
        self.assertEqual(norm["filename"], "silence")
        self.assertEqual(norm["status"], "error")
        self.assertTrue(norm["error"])
        self.assertFalse((self.normalize_out / "silence.wav").exists())


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class PeakOverCsvTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "peak_input"
        self.normalize_out = WORK_ROOT / "peak_normalize_out"
        self.quiet_wav = self.input_dir / "quiet.wav"
        _generate_sine(self.quiet_wav, volume_db=-36.0)

    def tearDown(self) -> None:
        _clean_work_root()

    def test_boost_sets_peak_over_flags_but_still_writes(self) -> None:
        # Large boost so predicted peaks exceed 0 while still writing output.
        boost_target = 0.0
        rows = normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=_write_targets_csv(
                WORK_ROOT / "peak_targets.csv",
                {"quiet": boost_target},
            ),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertTrue((self.normalize_out / "quiet.wav").is_file())

        normalize_csv = self.normalize_out / "loudbatch_normalize.csv"
        self.assertTrue(normalize_csv.is_file())
        by_name = _rows_by_filename(
            normalize_csv,
            fieldnames=list(NORMALIZE_CSV_FIELDNAMES),
        )
        row = by_name["quiet"]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["sample_peak_status"], "over")
        self.assertEqual(row["true_peak_status"], "over")


class DurationFromStreamTests(unittest.TestCase):
    def test_prefers_duration_field(self) -> None:
        self.assertEqual(duration_from_stream({"duration": "0.286417"}), 0.286417)

    def test_falls_back_to_samples_over_rate(self) -> None:
        self.assertAlmostEqual(
            duration_from_stream({"nb_samples": "13748", "sample_rate": "48000"}),
            13748 / 48000,
        )

    def test_returns_none_when_missing(self) -> None:
        self.assertIsNone(duration_from_stream({}))
        self.assertIsNone(duration_from_stream({"duration": "N/A"}))


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg が PATH 上にありません")
class ShortFilePadMeasureTests(unittest.TestCase):
    def setUp(self) -> None:
        _clean_work_root()
        self.input_dir = WORK_ROOT / "short_input"
        self.measure_out = WORK_ROOT / "short_measure_out"
        self.normalize_out = WORK_ROOT / "short_normalize_out"
        self.short_wav = self.input_dir / "click.wav"
        _generate_sine(self.short_wav, volume_db=-6.0, duration=0.2)

    def tearDown(self) -> None:
        _clean_work_root()

    def test_measure_and_normalize_short_sine(self) -> None:
        self.assertIn("apad=whole_dur=0.5", ebur128_filter(self.short_wav))
        long_wav = WORK_ROOT / "long.wav"
        _generate_sine(long_wav, volume_db=-6.0, duration=3.0)
        self.assertEqual(ebur128_filter(long_wav), "ebur128=peak=true")

        measure_csv = self.measure_out / "loudbatch.csv"
        rows = measure_directory(self.input_dir, measure_csv)
        self.assertEqual(len(rows), 1)
        by_name = _rows_by_filename(measure_csv)
        row = by_name["click"]
        self.assertEqual(row["status"], "ok")
        self.assertRegex(row["integrated_lufs"], r"^-?\d+\.\d$")
        integrated = float(row["integrated_lufs"])
        self.assertGreater(integrated, -70.0)
        self.assertTrue(math.isfinite(integrated))

        measured = measure_file(self.short_wav)
        self.assertEqual(measured["status"], "ok")

        normalize_directory(
            self.input_dir,
            self.normalize_out,
            csv_path=_write_targets_csv(
                WORK_ROOT / "short_targets.csv",
                {"click": TARGET_I},
            ),
        )
        dst = self.normalize_out / "click.wav"
        self.assertTrue(dst.is_file())
        src_stream = probe_audio_stream(self.short_wav)
        dst_stream = probe_audio_stream(dst)
        self.assertIsNotNone(src_stream)
        self.assertIsNotNone(dst_stream)
        assert src_stream is not None and dst_stream is not None
        self.assertAlmostEqual(
            float(src_stream["duration"]),
            0.2,
            places=2,
        )
        self.assertAlmostEqual(
            float(dst_stream["duration"]),
            float(src_stream["duration"]),
            places=2,
        )

        rem = measure_file(dst)
        self.assertEqual(rem["status"], "ok")
        self.assertLessEqual(
            abs(float(str(rem["integrated_lufs"])) - TARGET_I),
            TOLERANCE_LU,
            msg=f"short I={rem['integrated_lufs']} (target {TARGET_I} ± {TOLERANCE_LU})",
        )


class PeakStatusHelperTests(unittest.TestCase):
    def test_unmeasured_over_and_clear(self) -> None:
        self.assertEqual(_peak_status(None), "unknown")
        self.assertEqual(_peak_status(float("nan")), "unknown")
        self.assertEqual(_peak_status(0.1), "over")
        self.assertEqual(_peak_status(0.0), "")
        self.assertEqual(_peak_status(-1.0), "")


if __name__ == "__main__":
    unittest.main()
