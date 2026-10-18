# ニュースレーダー

テレビのニュースでは扱われにくい話題を見つけるための、自分専用のニュースサイトです。

- 海外メディアやテーマ別（通信キャリア・ネットワークなど）の情報源から、1時間ごとに記事を集めます。
- 国内テレビ局（NHK・TBS・日テレ・フジ・テレ朝・テレ東）のニュースサイトの見出しと照らし合わせます。同じ話題が見当たらない記事に「TV未報道」の印を付けます。
- トップには「今日の注目」として約9本だけを出します（AIなし・無料）。同じ話題を報じる媒体が多いもの、テレビで見当たらないもの、新しいものほど上に来ます。本数は `config.toml` の `featured_count` で変えられます。
- 画面上でトピック（キーワードの組み合わせ）を追加して、絞り込めます。
- 日本時間の 7時・16時・22時に、AI（Claude）が記事に日本語の見出しとカテゴリを付けます。あわせて1回7本（1日約21本）を厳選し、「要約・なぜ起きたか・暮らしへの影響・今後の注目点・用語」の解説を書きます。それ以外の毎時の実行は記事集めとテレビ照合だけで、API の料金はかかりません。

「TV未報道」は、テレビ局のウェブ記事に似た見出しがないことを示します。実際の放送内容と完全に一致するわけではありません。見出しの文字の重なりで判定する推定です。

## しくみ

| ファイル | 役割 |
|---|---|
| `config.toml` | 情報源（RSS / Googleニュース検索）とトピックの設定 |
| `scripts/fetch_news.py` | 記事を集めて照合し、`docs/data/news.json` に保存 |
| `docs/rakuten-config.js` | 楽天ウェブサービスのアプリIDとアクセスキー（レシピのタブで使用。閲覧者のブラウザが楽天レシピAPIを直接呼び出します） |
| `scripts/ai.py` | Claude による日本語化・分類・厳選・解説（結果は `docs/data/picks.json`） |
| `.github/workflows/update-news.yml` | 毎時の自動実行と GitHub Pages への公開 |
| `docs/index.html` | 閲覧ページ |

## はじめ方

1. このリポジトリを GitHub に置きます。
2. **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にします。
3. **Settings → Secrets and variables → Actions → New repository secret** で、名前 `ANTHROPIC_API_KEY`、値に Claude の API キーを登録します。これがないと AI の厳選・解説は作られず、英語記事も「TV照合なし」のままです。解説の事実確認には Claude の Web 検索・Web 取得ツールを使います。Claude Console の組織設定でこれらが無効の場合は、RSS の概要だけをもとに書きます。
4. **Actions → Update news → Run workflow** で初回を実行します。
5. 数分後、`https://<ユーザー名>.github.io/<リポジトリ名>/` で見られます。以後は毎時自動で更新されます。

## カスタマイズ

- **情報源を足す**：`config.toml` に `[[feeds]]` を追加します。`url` に RSS の URL を書くか、`google_news` に Googleニュースの検索式を書きます。`kind` は画面の分類名になります。
- **トピックを足す**：`config.toml` の `[[topics]]` に追加するか、画面の「＋ トピックを追加」から追加します。画面から追加したトピックは、そのブラウザだけに保存されます。
- **AI の時刻・本数・モデル**：`config.toml` の `pick_hours_jst`・`picks_per_round`・`model` で変えられます。
- **判定の厳しさ**：`tv_match_threshold` を下げると「TV報道あり」と判定されやすくなります。

## 手元で動かす

```sh
pip install anthropic   # 翻訳を使わないなら不要
python scripts/fetch_news.py
python -m http.server -d docs 8000   # http://localhost:8000
```
