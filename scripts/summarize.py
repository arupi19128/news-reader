"""Write a three-line Japanese summary for each of today's featured stories, using GitHub Models' free tier.

Run from the repository root after fetch_news.py:  python scripts/summarize.py
Needs GITHUB_TOKEN with the `models: read` permission (set in the workflow); without it the script does nothing.
Summaries are kept in docs/data/summaries.json, so each story is summarized only once.
"""

import html
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
NEWS_PATH = ROOT / "docs" / "data" / "news.json"
OUT_PATH = ROOT / "docs" / "data" / "summaries.json"
API = "https://models.github.ai/inference/chat/completions"
USER_AGENT = "Mozilla/5.0 (compatible; news-radar/1.0)"
JST = timezone(timedelta(hours=9))

BODY_MAX_CHARS = 5000
_SCRIPT = re.compile(r"<(script|style|noscript|nav|header|footer|aside|form)\b.*?</\1>", re.S | re.I)
_PARA = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


class RateLimited(Exception):
    pass


def page_text(url):
    """The article's paragraphs as plain text, or "" when the page can't be read (Google News links, paywalls, errors)."""
    if "news.google.com" in url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read(1_500_000).decode(res.headers.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""
    raw = _SCRIPT.sub(" ", raw)
    paras = [html.unescape(_TAG.sub("", p)).strip() for p in _PARA.findall(raw)]
    # Body paragraphs are sentences; menus, captions and bylines are short.
    paras = [re.sub(r"\s+", " ", p) for p in paras if len(p) >= 40]
    return "\n".join(paras)[:BODY_MAX_CHARS]


def source_material(story, by_id):
    """Text to summarize: the first readable article body among the story's articles, plus every outlet's headline."""
    items = [by_id[i] for i in [story["id"], *story.get("related", [])] if i in by_id]
    body, body_from = "", None
    for a in items:
        body = page_text(a["link"])
        if len(body) >= 300:
            body_from = a
            break
    lines = [f"- {a['outlet']}：{a.get('title_ja') or a['title']}" + (f"（{a['summary'][:200]}）" if a.get("summary") else "")
             for a in items]
    text = "同じ出来事を報じた見出しと概要:\n" + "\n".join(lines)
    if body_from:
        text += f"\n\n{body_from['outlet']} の記事本文:\n{body}"
    return text, ("本文" if body_from else "見出しと概要")


PROMPT = (
    "次のニュースを、日本語で3行に要約してください。\n"
    "- what: 何が起きたか（誰が・いつ・何を）\n"
    "- why: なぜ起きたのか、背景\n"
    "- impact: 何が変わるか、私たちや社会への影響\n"
    "各行は60字以内。中学生にも分かる言葉で、自分の言葉で書き直し、記事の文をそのまま写さない。"
    "材料に書かれていないことは推測で補わず、分からない行は「記事からは分かりません」と書く。\n"
    'JSON で {"what": "...", "why": "...", "impact": "..."} の形だけを返す。\n\n'
)


def ask(model, material, token):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT + material}],
        "response_format": {"type": "json_object"},
        "max_tokens": 600,
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            data = json.loads(res.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(e.read()[:300])
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}")
    text = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    out = json.loads(m.group(0) if m else text)
    lines = [str(out.get(k, "")).strip() for k in ("what", "why", "impact")]
    if not all(lines):
        raise RuntimeError(f"incomplete summary: {text[:200]}")
    return lines


def main(ask=ask):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN not set: skipping summaries")
        return
    settings = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("summaries", {})
    models = settings.get("models", ["openai/gpt-4.1-mini"])
    per_run = settings.get("max_per_run", 9)
    per_day = settings.get("max_per_day", 60)

    news = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    by_id = {a["id"]: a for a in news["articles"]}
    store = {"items": {}, "usage": {}}
    if OUT_PATH.exists():
        store = json.loads(OUT_PATH.read_text(encoding="utf-8"))

    now = datetime.now(timezone.utc)
    today = now.astimezone(JST).strftime("%Y-%m-%d")
    if store["usage"].get("date") != today:
        store["usage"] = {"date": today, "count": 0}

    # A summary belongs to a story; reuse it when a related article of an earlier story becomes the lead.
    done = set(store["items"])
    for sid, s in store["items"].items():
        done.update(s.get("articles", []))
    todo = [st for st in news.get("featured", []) if st["id"] not in done]

    made = 0
    for st in todo:
        if made >= per_run or store["usage"]["count"] >= per_day:
            print(f"summaries: limit reached ({made} this run, {store['usage']['count']} today)")
            break
        material, basis = source_material(st, by_id)
        lines, limited = None, 0
        for model in models:
            try:
                lines = ask(model, material, token)
                break
            except RateLimited as e:
                limited += 1
                print(f"summaries: rate-limited on {model}: {e}", file=sys.stderr)
            except Exception as e:  # unknown model, bad JSON, network: try the next model
                print(f"summaries: {model} failed: {e}", file=sys.stderr)
        store["usage"]["count"] += 1
        if limited == len(models):
            print("summaries: every model is rate-limited; trying again next run", file=sys.stderr)
            break
        if not lines:
            continue
        store["items"][st["id"]] = {
            "lines": lines, "basis": basis, "model": model,
            "articles": [st["id"], *st.get("related", [])],
            "made_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        made += 1

    # Forget summaries of stories that are no longer in the news data.
    store["items"] = {k: v for k, v in store["items"].items() if any(i in by_id for i in v["articles"])}
    store["generated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"summaries: {made} new, {len(store['items'])} kept, {store['usage']['count']} requests today")


if __name__ == "__main__":
    main()
