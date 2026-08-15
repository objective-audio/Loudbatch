# Loudbatch

フォルダ内の音声ファイルについて、ITU-R BS.1770 の **Integrated ラウドネス（LUFS）** を計測して CSV に書き出す CLI と、目標 LUFS へ正規化して別フォルダへ書き出す CLI です。計測は [ffmpeg](https://ffmpeg.org/) の `ebur128`、正規化は計測結果に基づく `volume` ゲインを使います。

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
| `path` | 絶対パス |
| `integrated_lufs` | Integrated ラウドネス（LUFS） |
| `lra` | Loudness Range（LU） |
| `true_peak_db` | True Peak（dB） |
| `status` | `ok` / `error` |
| `error` | 失敗時のメッセージ |

### 正規化（別フォルダへ書き出し）

```bash
python -m loudbatch normalize /path/to/audio_dir -o /path/to/out_dir
python -m loudbatch normalize /path/to/audio_dir -o /path/to/out_dir -t -23
```

| 引数 | デフォルト | 意味 |
| --- | --- | --- |
| `-t` / `--target` | `-23` | 目標 Integrated（LUFS） |
| `-r` / `--recursive` | off | サブフォルダも対象 |

元ファイルは変更しません。相対パス構造を保ったまま出力フォルダへ書き出します。ピーク制限は行わないため、ブースト時は 0 dBFS を超えてクリップし得ます。

## 対応拡張子

リニア PCM のみ: `.wav` `.aiff` `.aif`

`.flac` `.mp3` `.m4a` `.aac` `.ogg` `.opus` などの圧縮形式は発見時に `status=error` になります。WAV/AIFF でも非 PCM コーデックは同様にエラーです。

## テスト

リポジトリ直下で実行します。一時ファイルは `workspace/test/` に書き出します（gitignore 済み）。Push / Pull Request 時にも GitHub Actions で同じコマンドが走ります。

```bash
python -m unittest discover -s tests -v
```

## 補足

- 正規化は ebur128 で Integrated を計測し、目標との差を `volume` ゲインで適用します（ピーク制限なし。整数 PCM ではクリップし得ます）。
- リニア PCM は、元のコーデック（ビット深度など）・サンプルレート・チャンネル数をできるだけ継承します。
