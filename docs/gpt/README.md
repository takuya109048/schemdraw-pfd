# ChatGPT のカスタム GPT で使う

単一ファイル版 `pfd.py` を ChatGPT のカスタム GPT にアップロードし、会話から PFD / P&ID を描かせるための材料。

| ファイル | 内容 |
|---|---|
| [system_prompt.md](system_prompt.md) | GPT の「指示」欄に貼るシステムプロンプト（5000 文字以内） |
| [chat_examples.md](chat_examples.md) | GPT に送るチャットの例文3パターン（かんたん / 詳細な蒸留工程 / 独自機器と予備機） |
| [example_distillation.json](example_distillation.json) | 例文パターン2に対応する仕様の例 |
| [example_compressor.json](example_compressor.json) | 例文パターン3に対応する仕様の例（圧縮機を `custom_elements` で定義） |

## 設定のしかた

1. `python tools/build_single.py` で `dist/pfd.py` を作る
2. GPT の「知識」に `pfd.py` をアップロードする。日本語フォントの .ttf（IPAexGothic など）も一緒に入れておく。ChatGPT の実行環境には日本語フォントが無いことが多く、無いと日本語が □ に化ける
3. 「機能」で「コードインタープリターとデータ分析」をオンにする
4. 「指示」に `system_prompt.md` の中身を貼る

`pfd.py` は Python 3.11 でも動くことを確かめている（ChatGPT の実行環境向け）。

プロンプトの文字数と、2つの JSON が描けることは `tests/test_gpt_docs.py` で確かめる。
