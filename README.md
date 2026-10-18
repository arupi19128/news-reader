# ニュースレーダー

テレビのニュースでは扱われにくい話題を見つけるための、自分専用のニュースサイトです。

- 海外メディアやテーマ別（通信キャリア・ネットワークなど）の情報源から、1時間ごとに記事を集めます。
- 国内テレビ局（NHK・TBS・日テレ・フジ・テレ朝・テレ東）のニュースサイトの見出しと照らし合わせます。同じ話題が見当たらない記事に「TV未報道」の印を付けます。
- 画面上でトピック（キーワードの組み合わせ）を追加して、絞り込めます。

「TV未報道」は、テレビ局のウェブ記事に似た見出しがないことを示します。実際の放送内容と完全に一致するわけではありません。見出しの文字の重なりで判定する推定です。

## しくみ

| ファイル | 役割 |
|---|---|
| `config.toml` | 情報源（RSS / Googleニュース検索）とトピックの設定 |
| `scripts/fetch_news.py` | 記事を集めて照合し、`docs/data/news.json` に保存 |
| `.github/workflows/update-news.yml` | 毎時の自動実行と GitHub Pages への公開 |
| `docs/index.html` | 閲覧ページ |

## はじめ方

1. このリポジトリを GitHub に置きます。
2. **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にします。
3. （任意）英語記事に日本語訳と要約を付ける場合は、**Settings → Secrets and variables → Actions** で `ANTHROPIC_API_KEY` を登録します。これがないと英語記事はテレビ報道と照合されず、「TV照合なし」と表示されます。
4. **Actions → Update news → Run workflow** で初回を実行します。
5. 数分後、`https://<ユーザー名>.github.io/<リポジトリ名>/` で見られます。以後は毎時自動で更新されます。

## カスタマイズ

- **情報源を足す**：`config.toml` に `[[feeds]]` を追加します。`url` に RSS の URL を書くか、`google_news` に Googleニュースの検索式を書きます。`kind` は画面の分類名になります。
- **トピックを足す**：`config.toml` の `[[topics]]` に追加するか、画面の「＋ トピックを追加」から追加します。画面から追加したトピックは、そのブラウザだけに保存されます。
- **判定の厳しさ**：`tv_match_threshold` を下げると「TV報道あり」と判定されやすくなります。

## 手元で動かす

```sh
pip install anthropic   # 翻訳を使わないなら不要
python scripts/fetch_news.py
python -m http.server -d docs 8000   # http://localhost:8000
```
