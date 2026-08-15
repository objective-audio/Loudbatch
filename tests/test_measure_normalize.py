"""Integration tests for measure CSV and normalize using sine-wave fixtures."""

from __future__ import annotations

import csv
import shutil
import subprocess
import unittest
from pathlib import Path

from loudbatch.io_utils import CSV_FIELDNAMES, probe_audio_stream, require_ffmpeg
from loudbatch.measure import measure_directory
from loudbatch.normalize import normalize_directory, pcm_codec_from_stream

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


def _rows_by_filename(csv_path: Path) -> dict[str, dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == list(CSV_FIELDNAMES)
        return {row["filename"]: row for row in reader}


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
        self.assertEqual(set(by_name), {"loud.wav", "quiet.wav"})

        loud = by_name["loud.wav"]
        quiet = by_name["quiet.wav"]
        self.assertEqual(loud["status"], "ok")
        self.assertEqual(quiet["status"], "ok")

        loud_i = float(loud["integrated_lufs"])
        quiet_i = float(quiet["integrated_lufs"])
        self.assertGreater(loud_i, TARGET_I, msg=f"loud I={loud_i}")
        self.assertLess(quiet_i, TARGET_I, msg=f"quiet I={quiet_i}")

        normalize_directory(
            self.input_dir,
            self.normalize_out,
            target_i=TARGET_I,
            target_tp=-1.0,
            target_lra=7.0,
        )
        self.assertTrue((self.normalize_out / "loud.wav").is_file())
        self.assertTrue((self.normalize_out / "quiet.wav").is_file())

        remeasure_csv = self.remeasure_out / "loudbatch.csv"
        rem_rows = measure_directory(self.normalize_out, remeasure_csv)
        self.assertEqual(len(rem_rows), 2)
        rem_by_name = _rows_by_filename(remeasure_csv)

        for name in ("loud.wav", "quiet.wav"):
            row = rem_by_name[name]
            self.assertEqual(row["status"], "ok", msg=row.get("error", ""))
            integrated = float(row["integrated_lufs"])
            self.assertLessEqual(
                abs(integrated - TARGET_I),
                TOLERANCE_LU,
                msg=f"{name} I={integrated} (target {TARGET_I} ± {TOLERANCE_LU})",
            )


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
            target_i=TARGET_I,
            target_tp=-1.0,
            target_lra=7.0,
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
        bad = by_name["bad.wav"]
        self.assertEqual(bad["status"], "error")
        self.assertTrue(bad["error"])
        self.assertEqual(bad["integrated_lufs"], "")

        norm_rows = normalize_directory(
            self.input_dir,
            self.normalize_out,
            target_i=TARGET_I,
            target_tp=-1.0,
            target_lra=7.0,
        )
        self.assertEqual(len(norm_rows), 1)
        norm = norm_rows[0]
        self.assertEqual(norm["filename"], "bad.wav")
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
            target_i=TARGET_I,
            target_tp=-1.0,
            target_lra=7.0,
        )
        self.assertEqual(len(norm_rows), 1)
        norm = norm_rows[0]
        self.assertEqual(norm["filename"], "silence.wav")
        self.assertEqual(norm["status"], "error")
        self.assertTrue(norm["error"])
        self.assertFalse((self.normalize_out / "silence.wav").exists())


if __name__ == "__main__":
    unittest.main()
