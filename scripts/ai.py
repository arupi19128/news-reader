"""Claude API steps: label every article, pick the day's stories, and write explanations for the picks."""

import json
import sys

import anthropic

LABEL_BATCH = 40
EXPLAIN_MAX_CONTINUATIONS = 5

_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _create(model, effort, messages, **kwargs):
    return client().beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort, **kwargs.pop("output_config", {})},
        messages=messages,
        **kwargs,
    )


def _json_call(model, effort, prompt, schema):
    """One structured-output request. Returns the parsed object, or None on failure."""
    try:
        response = _create(model, effort, [{"role": "user", "content": prompt}],
                           output_config={"format": {"type": "json_schema", "schema": schema}})
    except anthropic.RateLimitError as e:
        print(f"  rate-limited: {e}", file=sys.stderr)
        return None
    except anthropic.APIStatusError as e:
        print(f"  API error ({e.status_code}): {e.message}", file=sys.stderr)
        return None
    except anthropic.APIConnectionError as e:
        print(f"  connection error: {e}", file=sys.stderr)
        return None
    if response.stop_reason != "end_turn":
        print(f"  stopped: {response.stop_reason}", file=sys.stderr)
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


# ---- 1. Labels: Japanese headline + category for every article -----------

def label(articles, categories, model):
    """Set title_ja (English articles only) and category on each article."""
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "title_ja": {"type": "string"},
                "category": {"type": "string", "enum": categories},
            },
            "required": ["id", "title_ja", "category"],
            "additionalProperties": False,
        }}},
        "required": ["items"],
        "additionalProperties": False,
    }
    done = 0
    for start in range(0, len(articles), LABEL_BATCH):
        batch = articles[start:start + LABEL_BATCH]
        payload = [{"id": a["id"], "title": a["title"], "summary": a["summary"][:160]} for a in batch]
        prompt = (
            "ニュース記事の一覧です。各記事について次の2つを返してください。\n"
            "- title_ja: 日本語の見出し。元が日本語ならそのまま、英語なら日本の新聞の見出しのように自然に訳す。"
            "人名・地名は日本の報道で一般的な表記にする。\n"
            f"- category: 次のどれか1つ → {'、'.join(categories)}\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        result = _json_call(model, "low", prompt, schema)
        if result is None:
            continue
        by_id = {a["id"]: a for a in batch}
        for row in result["items"]:
            target = by_id.get(row["id"])
            if not target:
                continue
            if target["lang"] != "ja":
                target["title_ja"] = row["title_ja"].strip()
            target["category"] = row["category"]
            done += 1
    print(f"labelled {done} articles")


# ---- 2. Picks: choose the stories worth explaining ------------------------

def pick(candidates, already_picked_titles, count, model):
    """Return up to `count` article ids, most worth explaining first."""
    schema = {
        "type": "object",
        "properties": {"picks": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["id", "reason"],
            "additionalProperties": False,
        }}},
        "required": ["picks"],
        "additionalProperties": False,
    }
    payload = [{
        "id": a["id"],
        "title": a.get("title_ja") or a["title"],
        "outlet": a["outlet"],
        "category": a.get("category", ""),
        "tv": "報道あり" if isinstance(a.get("tv_match"), dict) else "未報道",
        "summary": a["summary"][:120],
    } for a in candidates]
    prompt = (
        "あなたは、日本のテレビニュースでは伝わりにくい世界の動きを読者に届ける編集者です。\n"
        f"候補の記事から、解説記事にする価値が高いものを最大{count}本選んでください。\n\n"
        "選ぶ基準:\n"
        "- 社会・経済・暮らし・国際情勢への影響が大きい\n"
        "- tv が「未報道」の記事を優先する（日本のテレビ局サイトで見当たらない話題）\n"
        "- 芸能ゴシップ、スポーツの試合結果、商品のセール情報、広告的な記事は選ばない\n"
        "- 同じ出来事を扱う記事は1本だけにし、分野が偏らないようにする\n"
        "- 下の「選定済み」と同じ出来事は選ばない\n\n"
        f"選定済み: {json.dumps(already_picked_titles, ensure_ascii=False)}\n\n"
        f"候補: {json.dumps(payload, ensure_ascii=False)}"
    )
    result = _json_call(model, "medium", prompt, schema)
    if result is None:
        return []
    valid = {a["id"] for a in candidates}
    return [p["id"] for p in result["picks"] if p["id"] in valid][:count]


# ---- 3. Explanation: an easy-to-read article for each pick ---------------

SAVE_TOOL = {
    "name": "save_article",
    "description": "書き上げた解説記事を保存する。調べ終えたら必ず1回だけ呼ぶ。",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "日本語の見出し（40字以内）"},
            "lead": {"type": "string", "description": "何が起きたかを3〜4文で。5W1Hが分かるように"},
            "background": {"type": "string", "description": "なぜ起きたのか。経緯や前提を中学生にも分かる言葉で"},
            "impact": {"type": "string", "description": "日本や私たちの暮らしにどう関わるか"},
            "outlook": {"type": "string", "description": "今後の注目点。推測は推測と分かる書き方で"},
            "terms": {
                "type": "array",
                "description": "難しい言葉の説明（0〜4個）",
                "items": {
                    "type": "object",
                    "properties": {"term": {"type": "string"}, "meaning": {"type": "string"}},
                    "required": ["term", "meaning"],
                    "additionalProperties": False,
                },
            },
            "sources": {
                "type": "array",
                "description": "実際に読んで確かめた情報源",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                    "required": ["title", "url"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["headline", "lead", "background", "impact", "outlook", "terms", "sources"],
        "additionalProperties": False,
    },
}

WEB_TOOLS = [
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 3},
]


def explain(article, model, use_web=True):
    """Return a dict of explanation fields, or None if Claude could not write one."""
    prompt = (
        "次のニュースについて、中学生にも分かるやさしい日本語で解説記事を書いてください。\n\n"
        f"見出し: {article['title']}\n"
        f"日本語見出し: {article.get('title_ja', '')}\n"
        f"媒体: {article['outlet']}\n"
        f"配信日時: {article['published']}\n"
        f"概要: {article['summary']}\n"
        f"URL: {article['link']}\n\n"
        + (
            "まず web_fetch で元記事を読んでください。読めなければ web_search で同じ出来事を報じる記事を探してください。"
            "事実は複数の信頼できる媒体で確かめ、偏りのある媒体や確認できない話は使わないでください。\n"
            if use_web else
            "上の情報だけを使い、書かれていないことは推測で補わないでください。\n"
        )
        + "書き終えたら save_article ツールで保存してください。事実と意見をはっきり分け、断定できないことは断定しないでください。"
    )
    tools = (WEB_TOOLS if use_web else []) + [SAVE_TOOL]
    messages = [{"role": "user", "content": prompt}]
    for _ in range(EXPLAIN_MAX_CONTINUATIONS + 1):
        try:
            response = _create(model, "medium", messages, tools=tools)
        except anthropic.RateLimitError as e:
            print(f"  rate-limited: {e}", file=sys.stderr)
            return None
        except anthropic.BadRequestError as e:
            if use_web:  # web tools may be disabled for the organization; write from the feed instead
                print(f"  web tools unavailable ({e.message}); retrying without them", file=sys.stderr)
                return explain(article, model, use_web=False)
            print(f"  API error (400): {e.message}", file=sys.stderr)
            return None
        except anthropic.APIStatusError as e:
            print(f"  API error ({e.status_code}): {e.message}", file=sys.stderr)
            return None
        except anthropic.APIConnectionError as e:
            print(f"  connection error: {e}", file=sys.stderr)
            return None

        for block in response.content:
            if block.type == "tool_use" and block.name == "save_article":
                return block.input
        if response.stop_reason == "pause_turn":
            # Resend the turn so far; the server resumes from the trailing server-tool block.
            if messages[-1]["role"] == "assistant":
                messages[-1] = {"role": "assistant", "content": list(messages[-1]["content"]) + list(response.content)}
            else:
                messages = messages + [{"role": "assistant", "content": response.content}]
            continue
        if response.stop_reason == "end_turn":
            # It finished without saving; ask once more to save.
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": "save_article ツールで記事を保存してください。"},
            ]
            continue
        print(f"  explanation stopped: {response.stop_reason}", file=sys.stderr)
        return None
    return None
