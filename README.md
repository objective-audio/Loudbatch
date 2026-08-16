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
```

`-o` に指定した別フォルダへ `loudbatch.csv` を書き出します（入力フォルダには書きません）。

CSV 列:

| 列 | 内容 |
| --- | --- |
| `filename` | ファイル名 |
| `integrated_lufs` | Integrated ラウドネス（LUFS、小数点以下1桁） |
| `status` | `ok` / `error` |
| `error` | 失敗時のメッセージ |

### 正規化（別フォルダへ書き出し）

`measure` で得た CSV を `--csv` に渡し、同じファイル名の音声をその `integrated_lufs` に揃えます。CSV の値を編集して別の目標を指定することもできます。

```bash
python -m loudbatch normalize /path/to/audio_dir -o /path/to/out_dir --csv /path/to/loudbatch.csv
python -m loudbatch normalize /path/to/audio_dir -o /path/to/out_dir --csv /path/to/loudbatch.csv -r
```

| 引数 | デフォルト | 意味 |
| --- | --- | --- |
| `--csv` | （必須） | ファイルごとの目標 Integrated（LUFS）。`filename` と `integrated_lufs` 列が必要 |
| `-r` / `--recursive` | off | サブフォルダも対象 |

照合はファイル名です。CSV に目標が無いファイルは書き出さず `status=error` になります。元ファイルは変更しません。相対パス構造を保ったまま出力フォルダへ書き出します。ピーク制限は行わないため、ブースト時は 0 dBFS を超えてクリップし得ます。

正規化結果は出力フォルダの `loudbatch_normalize.csv` に記録します。ゲイン適用後の予測ピークが 0 を超える場合もファイルは書き出し、コンソールに警告を出し CSV のフラグで判別できます。

| 列 | 内容 |
| --- | --- |
| `filename` | ファイル名 |
| `status` | `ok` / `error` |
| `error` | 失敗時のメッセージ |
| `integrated_lufs` | 入力の Integrated（LUFS、小数点以下1桁） |
| `target_lufs` | CSV から採用した目標 Integrated（LUFS、小数点以下1桁） |
| `gain_db` | 適用ゲイン（dB） |
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

- 正規化は ebur128 で Integrated を計測し、CSV の目標との差を `volume` ゲインで適用します（ピーク制限なし。整数 PCM ではクリップし得ます）。
- ゲイン適用後にサンプルピーク / True Peak が 0 を超える場合は警告のみ（書き出しは継続）。`loudbatch_normalize.csv` の `sample_peak_status` / `true_peak_status` で確認できます。ピークを測定できなかった場合は `unknown` になります。
- リニア PCM は、元のコーデック（ビット深度など）・サンプルレート・チャンネル数をできるだけ継承します。
