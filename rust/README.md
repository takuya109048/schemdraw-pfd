# schemdraw-pfd（Rust 版）

Python 版 `schemdraw-pfd` の配置・配線・描画を Rust に移植したもの。**速く動き、1つの実行ファイルで配布できる**ことが目的。同じ JSON を読み、**配置・配線・警告・描く図形が座標まで Python 版と同じ**になる（`tests/snapshots` のスナップショットで確認する）。

## 使い方

```bash
cd rust
cargo build --release              # target/release/pfd ができる
./target/release/pfd ../examples/solvent_recovery.json -o out.svg
./target/release/pfd ../examples/solvent_recovery.json -o out.xlsx   # Excel の図形で
./target/release/pfd ../examples/solvent_recovery.json -o out.png    # 画像
./target/release/pfd ../examples/solvent_recovery.json -o out.pdf    # PDF（ベクトル）
```

オプションは Python 版の `pfd` と同じ（`--no-labels`、`--no-frame`、`--fontsize`、`--layout`、`-j/--jobs`、`--revision`、`--highlight`、`--since`、`--freeze`、`--list-elements`、`--describe`）。`-j` はプロセス数ではなくスレッド数。

```bash
# 改訂 R1 までの図を、R1 だけ色付きで
./target/release/pfd SPEC.json -o out.svg --revision R1 --highlight latest
# いまの図を JSON に凍結して書き出す（以後の改訂はこの図に描き足される）
./target/release/pfd SPEC.json --freeze frozen.json
```

凍結したファイルは Python 版と互換で、どちらで凍結してもどちらでも読み戻せる（内容の指紋が同じになる）。

`--snapshot PATH` を付けると、配置・配線・描く図形を Python 版のスナップショットと同じ形の JSON で書き出す。

## Python 版との違い（今の範囲）

| 項目 | 状態 |
|---|---|
| JSON の読み込み・検査（エラーメッセージも同じ） | 対応 |
| 自動配置・段の自動決定・段への折り返し・配線・圧縮・並列系列 | 対応（座標まで一致） |
| 出力 | SVG・PNG・PDF・xlsx |
| 改訂（`revisions`：撤去・追加・変更・取替）・凍結（`frozen`, `--freeze`） | 対応（座標・色まで一致、凍結ファイルは Python 版と相互に読める） |

SVG は SchemDraw（matplotlib）を通さず直接書くので、ファイルの中身は Python 版と違う。描く図形の位置・大きさ・線種は同じ。

PNG と PDF は、その SVG を [resvg](https://github.com/linebender/resvg)（PNG）と [svg2pdf](https://github.com/typst/svg2pdf)（PDF）で変換して作る。どちらも Rust だけで動くので、matplotlib もほかのソフトも要らない。Python 版と同じく 72 dpi（1 ポイント = 1 画素）で、PDF は線も文字もベクトルのまま（フォントは埋め込む）。文字は、Python 版と同じ日本語フォントの候補（Yu Gothic, Meiryo, MS Gothic, Noto Sans CJK JP, IPAexGothic）のうち入っている最初のもので描く（どれもなければ IPAGothic など）。matplotlib とは文字の描き方（字形の位置合わせ）が違うので、画素単位では Python 版の PNG と同じにならない。

xlsx（Excel の図形として編集できるブック）は Python 版と同じものを書く。小さな例と作例 312 件のうち 311 件で、ブックの全部品がバイト単位で Python 版と同じだった（`examples/loading_station.xlsx` とも同じ）。違いが出るのはテキストボックスの高さだけで、これは Python 版が matplotlib でフォントの行の高さを測っているのを、Rust 版は DejaVu Sans の比率（0.9677 em、1/64 pt 刻み）で近似しているため。図形の位置には影響しない。

## 速さ

作例のビルド時間（段の探索を含む）。Python 版は `docs/internals.md` の値（起動時間を除く）、Rust 版はこの環境（4コア）で測った値（起動を含む）:

| 作例 | Python 1プロセス | Python 4コア | Rust 1スレッド | Rust 4スレッド |
|---|---|---|---|---|
| `feed_heating` | 0.2 秒 | — | 0.01 秒 | 0.01 秒 |
| `loading_station` | 1.4 秒 | 0.8 秒 | 0.30 秒 | 0.16 秒 |
| `cooling_water` | 2.5 秒 | 1.1 秒 | 0.65 秒 | 0.29 秒 |
| `solvent_recovery` | 9 秒 | 3.7 秒 | 0.99 秒 | 0.43 秒 |
| `btx_fractionation` | 10 秒 | 4.7 秒 | 1.24 秒 | 0.58 秒 |

配線探索は Python 版と同じ順位付け（推定コスト → ここまでのコスト → 積んだ順）の A*。探索の状態は密な配列で持ち、スレッドごとに使い回す。

長引く探索（800 歩を超えたもの）は、Python 版と同じく、目的地から逆向きに「各状態からあと何回曲がれば着けるか」を数え（曲がり 1 回を 1 段とする 0-1 幅優先探索。始点の段を越えたら打ち切る）、

- 始点から着けなければ、そこで探索をやめる
- 着けば、曲がるたびに 1 段下りる経路を 1 本たどってそのコストを出し、以後は「ここまでのコスト ＋ 目的地までの距離 ＋ あと要る曲がり × 曲がりコスト」がそれを超える状態を積まない

最安の経路に乗る状態は落とさないので、見つかる経路は変わらない（全スナップショットで確認）。これで 1 スレッドの作例のビルドが約 1.6 倍速くなった。Python 版は数え上げをビットボードで行うので 50 歩で始めるが、状態ごとに数える Rust 版では 800 歩からが最も速かった。

## テスト

```bash
cargo test --release
```

- `tests/snapshots.rs`: `tests/cases/` の小さな例すべて（改訂・凍結の例を含む 306 件）と `examples/` の作例が、`tests/snapshots/` のスナップショットと一致する（数値は 1e-6 まで）。`PFD_CASE=<部分文字列>` で絞り込める
- `tests/revisions.rs`: 凍結した図が読み戻せる、凍結後の改訂は凍結した図に描き足される、元が変わった凍結は作り直して警告する、すべての例が凍結できる、など
- `tests/xlsx.rs`: 作例のブックが `examples/loading_station.xlsx`（Python 版が書いたもの）と部品ごとに同じ、直線ごとに Excel の線が1本、改訂の色
- `tests/raster.rs`: PNG が 72 dpi の図の大きさ、PDF が図の大きさ（ポイント）の1ページ
- `tests/invalid.rs`: `tests/cases/invalid/` の各例（48 件）が、Python 版のテスト（`tests/test_cases.py` の `INVALID`）と同じ種類・メッセージのエラーになる

GitHub Actions（`.github/workflows/ci.yml`）が、プッシュと PR ごとに Python 版のテスト（`ruff`・`pyrefly`・`pytest`）と Rust 版のテスト（`cargo fmt --check`・`clippy`・`cargo test`）を回す。

Python 版のスナップショットを更新したら（`PFD_UPDATE_SNAPSHOTS=1 pytest ...`）、Rust 版もそれに合わせる。

## 構成 (`src/`)

Python 版のモジュールと1対1に対応する: `geometry.rs`、`elements.rs`（全エレメント）、`style.rs`、`netlist.rs`、`expand.rs`、`flow.rs`、`levels.rs`、`symmetry.rs`、`layout.rs`、`router.rs`、`model.rs`、`revise.rs`、`freeze.rs`、`render.rs`、`xlsx.rs`。ほかに、SVG を書く `svg.rs`、PNG / PDF に変換する `raster.rs`、スレッドで並べて作る `pool.rs`、Python の値の扱い（`int()`・`str()`・`round()`・`//` など）を真似る `pyval.rs`、`json.dumps` と同じ文字列を書く `pyjson.rs`（凍結の指紋用）、matplotlib の色名の表 `colors.rs`、エラーの `error.rs`、コマンドの `main.rs`。

座標を一致させるため、Python 版の処理の順序（辞書の順、安定ソート、同点の扱い、浮動小数点の計算順、`round` の偶数丸め、負の数の `//`）をそのまま守っている。
