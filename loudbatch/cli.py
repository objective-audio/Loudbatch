"""Command-line interface for loudness measure / normalize."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__
from .io_utils import CSV_FIELDNAMES, NORMALIZE_CSV_FIELDNAMES, parse_column_map, require_ffmpeg
from .measure import measure_directory
from .normalize import normalize_directory


def _column_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--column",
        action="append",
        default=[],
        metavar="INTERNAL=NAME",
        dest="columns",
        help="CSV の列名を置き換える（繰り返し可）。例: --column filename=ファイル名",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loudbatch",
        description="フォルダ内音声の Integrated ラウドネス計測と正規化（ffmpeg）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    column_parent = _column_parent()

    measure_p = sub.add_parser(
        "measure",
        parents=[column_parent],
        help="Integrated ラウドネスを計測して CSV に書き出す",
    )
    measure_p.add_argument("input_dir", type=Path, help="音声ファイルが入ったフォルダ")
    measure_p.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="CSV を書き出す出力フォルダ（入力とは別フォルダ）",
    )
    measure_p.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="サブフォルダも再帰的に走査する",
    )

    normalize_p = sub.add_parser(
        "normalize",
        parents=[column_parent],
        help="CSV の目標 LUFS に正規化して別フォルダへ書き出す",
    )
    normalize_p.add_argument("input_dir", type=Path, help="音声ファイルが入ったフォルダ")
    normalize_p.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="正規化後ファイルの出力フォルダ",
    )
    normalize_p.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="ファイルごとの目標 LUFS が入った CSV（measure の出力）",
    )
    normalize_p.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="サブフォルダも再帰的に走査する",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    require_ffmpeg()

    if args.command == "measure":
        input_dir = args.input_dir.expanduser().resolve()
        output_dir = args.output.expanduser().resolve()
        if output_dir == input_dir:
            parser.error("出力フォルダは入力フォルダと別にしてください（元ファイルを保持します）")
        output_csv = output_dir / "loudbatch.csv"
        column_map = parse_column_map(args.columns, CSV_FIELDNAMES)
        measure_directory(
            input_dir,
            output_csv,
            recursive=args.recursive,
            column_map=column_map,
        )
        return 0

    if args.command == "normalize":
        input_dir = args.input_dir.expanduser().resolve()
        output_dir = args.output.expanduser().resolve()
        if output_dir == input_dir:
            parser.error("出力フォルダは入力フォルダと別にしてください（元ファイルを保持します）")
        csv_path = args.csv.expanduser().resolve()
        column_map = parse_column_map(args.columns, NORMALIZE_CSV_FIELDNAMES)
        normalize_directory(
            input_dir,
            output_dir,
            csv_path=csv_path,
            recursive=args.recursive,
            column_map=column_map,
        )
        return 0

    parser.error(f"不明なコマンド: {args.command}")
    return 2
