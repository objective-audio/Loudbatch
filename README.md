# Loudbatch

フォルダ内の音声ファイルについて、ITU-R BS.1770 の **Integrated ラウドネス（LUFS）** を計測して CSV に書き出す CLI と、CSV で指定した目標 LUFS へ正規化して別フォルダへ書き出す CLI です。計測は [ffmpeg](https://ffmpeg.org/) の `ebur128`、正規化は計測結果に基づく `volume` ゲインを使います。

## 前提

- Python 3.9+
- ffmpeg（PATH 上で実行できること）

ffmpeg が入っているかは次で確認できます。

```bash
ffmpeg -version
```

見つからない場合は、macOS では Homebrew 経由で入れます。Homebrew 自体が未導入なら、[Homebrew 公式サイト](https://brew.sh/ja/)の手順に従ってインストールしてください。その後:

```bash
brew install ffmpeg
```

外部 Python パッケージは不要です。

## 使い方

リポジトリ直下で実行します。

### 計測 → CSV

```bash
python -m loudbatch measure /path/to/audio_dir -o /path/to/out_dir
python -m loudbatch measure /path/to/audio_dir -r -o /path/to/out_dir
python -m loudbatch measure /path/to/audio_dir -o /path/to/out_dir \
  --column filename=ファイル名 --column integrated_lufs=LUFS
```

`-o` に指定した別フォルダへ `loudbatch.csv` を書き出します（入力フォルダには書きません）。

`--column 内部名=CSV列名` でヘッダ名を置き換えられます（繰り返し可。未指定の列は下表のまま）。

CSV 列:

| 列 | 内容 |
| --- | --- |
| `filename` | 拡張子なしのファイル名 |
| `integrated_lufs` | Integrated ラウドネス（LUFS、小数点以下1桁） |
| `status` | `ok` / `error` |
| `error` | 失敗時のメッセージ |

0.4 秒以下のファイルは、計測時だけ末尾に無音を足して 0.5 秒にしてから Integrated を取ります（書き出す音声の長さは変えません）。

### 正規化（別フォルダへ書き出し）

`measure` で得た CSV を `--csv` に渡し、同じ（拡張子なしの）ファイル名の音声をその `integrated_lufs` に揃えます。CSV の値を編集して別の目標を指定することもできます。

```bash
python -m loudbatch normalize /path/to/audio_dir -o /path/to/out_dir --csv /path/to/loudbatch.csv
python -m loudbatch normalize /path/to/audio_dir -o /path/to/out_dir --csv /path/to/loudbatch.csv -r
python -m loudbatch normalize /path/to/audio_dir -o /path/to/out_dir --csv /path/to/targets.csv \
  --column filename=ファイル名 --column integrated_lufs=目標LUFS
```

| 引数 | デフォルト | 意味 |
| --- | --- | --- |
| `--csv` | （必須） | ファイルごとの目標 Integrated（LUFS）。`filename` と `integrated_lufs` 列が必要（`--column` で別名可） |
| `-r` / `--recursive` | off | サブフォルダも対象 |
| `--column` | なし | CSV 列名の置き換え（繰り返し可）。入力 CSV の目標列は `integrated_lufs`、結果 CSV の実測列は `input_lufs`（別名はそれぞれ別指定） |

照合は拡張子なしのファイル名です（既存 CSV に `.wav` などが付いていても読み込めます）。CSV に目標が無いファイルは書き出さず `status=error` になります。元ファイルは変更しません。相対パス構造を保ったまま出力フォルダへ書き出します。ピーク制限は行わないため、ブースト時は 0 dBFS を超えてクリップし得ます。

正規化結果は出力フォルダの `loudbatch_normalize.csv` に記録します。ゲイン適用後の予測ピークが 0 を超える場合もファイルは書き出し、コンソールに警告を出し CSV のフラグで判別できます。

| 列 | 内容 |
| --- | --- |
| `filename` | 拡張子なしのファイル名 |
| `status` | `ok` / `error` |
| `error` | 失敗時のメッセージ |
| `input_lufs` | 入力の Integrated（LUFS、小数点以下1桁） |
| `target_lufs` | CSV から採用した目標 Integrated（LUFS、小数点以下1桁） |
| `gain_db` | 適用ゲイン（dB、小数点以下1桁・切り捨て） |
| `sample_peak_status` | サンプルピークが 0 dBFS 超なら `over`、計測不能なら `unknown`（未超過は空） |
| `true_peak_status` | True Peak が 0 dBTP 超なら `over`、計測不能なら `unknown`（未超過は空） |

## 対応拡張子

リニア PCM のみ: `.wav` `.aiff` `.aif`

`.flac` `.mp3` `.m4a` `.aac` `.ogg` `.opus` などの圧縮形式は発見時に `status=error` になります。WAV/AIFF でも非 PCM コーデックは同様にエラーです。

## テスト

リポジトリ直下で実行します。一時ファイルは `workspace/test/` に書き出します（gitignore 済み）。Push / Pull Request 時にも GitHub Actions で同じコマンドが走ります。

```bash
python -m unittest discover -s tests -v
```

## 補足

- 0.4 秒以下のファイルは、計測時だけ末尾に無音を足して 0.5 秒にしてから Integrated を取ります。無音が混ざるため、ワンショット単体より静かめの値になります。正規化の書き出しは元の長さのままです。
- 正規化は ebur128 で Integrated を計測し、CSV の目標との差を `volume` ゲインで適用します（ピーク制限なし。整数 PCM ではクリップし得ます）。
- ゲイン適用後にサンプルピーク / True Peak が 0 を超える場合は警告のみ（書き出しは継続）。`loudbatch_normalize.csv` の `sample_peak_status` / `true_peak_status` で確認できます。ピークを測定できなかった場合は `unknown` になります。
- リニア PCM は、元のコーデック（ビット深度など）・サンプルレート・チャンネル数をできるだけ継承します。
