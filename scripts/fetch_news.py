"""Collect news feeds, flag stories that TV news has not covered, and write docs/data/news.json.

Run from the repository root:  python scripts/fetch_news.py
Set ANTHROPIC_API_KEY to add Japanese titles and summaries to English articles.
"""

import email.utils
import hashlib
import html
import json
import os
import re
import sys
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
OUT_PATH = ROOT / "docs" / "data" / "news.json"
USER_AGENT = "Mozilla/5.0 (compatible; news-radar/1.0; +https://github.com/)"
TRANSLATE_BATCH = 25
TRANSLATE_MAX_PER_RUN = 150

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def clean_text(text, limit=280):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def feed_url(feed):
    if "url" in feed:
        return feed["url"]
    lang = feed.get("lang", "ja")
    params = {"q": feed["google_news"] + " when:3d"}
    if lang == "en":
        params.update(hl="en-US", gl="US", ceid="US:en")
    else:
        params.update(hl="ja", gl="JP", ceid="JP:ja")
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


def child_text(el, *paths):
    for path in paths:
        found = el.find(path, NS)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def parse_feed(raw, feed):
    """Return a list of {title, link, summary, published, source} from RSS 2.0, RSS 1.0 or Atom."""
    root = ET.fromstring(raw)
    items = []
    if root.tag == "rss":
        nodes = root.findall("./channel/item")
        for n in nodes:
            items.append({
                "title": child_text(n, "title"),
                "link": child_text(n, "link"),
                "summary": child_text(n, "description"),
                "published": child_text(n, "pubDate", "dc:date"),
                "source": child_text(n, "source"),
            })
    elif root.tag.endswith("RDF"):
        for n in root.findall("rss1:item", NS):
            items.append({
                "title": child_text(n, "rss1:title"),
                "link": child_text(n, "rss1:link"),
                "summary": child_text(n, "rss1:description"),
                "published": child_text(n, "dc:date"),
                "source": "",
            })
    elif root.tag.endswith("feed"):
        for n in root.findall("atom:entry", NS):
            link_el = n.find("atom:link[@rel='alternate']", NS)
            if link_el is None:
                link_el = n.find("atom:link", NS)
            items.append({
                "title": child_text(n, "atom:title"),
                "link": link_el.get("href", "") if link_el is not None else "",
                "summary": child_text(n, "atom:summary", "atom:content"),
                "published": child_text(n, "atom:published", "atom:updated"),
                "source": "",
            })

    out = []
    for it in items[: feed.get("max_items", 10_000)]:
        title = clean_text(it["title"], 300)
        if not title or not it["link"]:
            continue
        source = it["source"]
        # Google News titles end with " - <outlet>"; keep the outlet separately.
        if "google_news" in feed and source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        out.append({
            "title": title,
            "link": it["link"].strip(),
            "summary": clean_text(it["summary"]),
            "published": parse_date(it["published"]),
            "outlet": source or feed["name"],
        })
    return out


# ---- TV coverage matching -------------------------------------------------

_KEEP = re.compile(r"[぀-ヿ㐀-鿿A-Za-z0-9ー]")
_HIRAGANA = re.compile(r"^[぀-ゟ]+$")


def bigrams(text):
    """Character bigrams that carry meaning (drops punctuation and hiragana-only particles)."""
    chars = "".join(c for c in text.lower() if _KEEP.match(c))
    grams = {chars[i:i + 2] for i in range(len(chars) - 1)}
    return {g for g in grams if not _HIRAGANA.match(g)}


def match_tv(article, tv_items, threshold, window):
    """Return the best-matching TV headline if the article looks covered, else None."""
    text = article.get("title_ja") or article["title"]
    if article.get("lang") == "en" and not article.get("title_ja"):
        return "unknown"
    grams = bigrams(text)
    if len(grams) < 4:
        return None
    published = datetime.fromisoformat(article["published"].replace("Z", "+00:00"))
    best, best_score = None, 0.0
    for tv in tv_items:
        tv_time = datetime.fromisoformat(tv["published"].replace("Z", "+00:00"))
        if abs(tv_time - published) > window:
            continue
        score = len(grams & tv["_grams"]) / len(grams)
        if score > best_score:
            best, best_score = tv, score
    if best and best_score >= threshold:
        return {"title": best["title"], "outlet": best["outlet"], "link": best["link"],
                "score": round(best_score, 2)}
    return None


# ---- Translation (optional) ----------------------------------------------

def translate(articles, model):
    """Add title_ja and summary_ja to English articles using the Claude API."""
    import anthropic

    client = anthropic.Anthropic()
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title_ja": {"type": "string"},
                        "summary_ja": {"type": "string"},
                    },
                    "required": ["id", "title_ja", "summary_ja"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    done = 0
    for start in range(0, len(articles), TRANSLATE_BATCH):
        batch = articles[start:start + TRANSLATE_BATCH]
        payload = [{"id": a["id"], "title": a["title"], "summary": a["summary"]} for a in batch]
        prompt = (
            "以下は海外ニュースの見出しと概要です。各項目について、日本の読者向けに"
            "自然な日本語の見出し(title_ja)と、何が起きたかを1〜2文で説明する要約(summary_ja)を作ってください。"
            "人名・地名は日本の報道で一般的な表記にしてください。概要が空なら見出しから分かる範囲で書いてください。\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            response = client.beta.messages.create(
                model=model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.RateLimitError as e:
            print(f"  translation rate-limited, stopping for this run: {e}", file=sys.stderr)
            break
        except anthropic.APIStatusError as e:
            print(f"  translation failed ({e.status_code}): {e.message}", file=sys.stderr)
            continue
        except anthropic.APIConnectionError as e:
            print(f"  translation connection error: {e}", file=sys.stderr)
            continue
        if response.stop_reason != "end_turn":
            print(f"  translation stopped: {response.stop_reason}", file=sys.stderr)
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        by_id = {a["id"]: a for a in batch}
        for row in json.loads(text)["items"]:
            target = by_id.get(row["id"])
            if target:
                target["title_ja"] = row["title_ja"].strip()
                target["summary_ja"] = row["summary_ja"].strip()
                done += 1
    print(f"translated {done} articles")


# ---- Main -----------------------------------------------------------------

def main():
    config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    settings = config.get("settings", {})
    keep = timedelta(days=settings.get("keep_days", 7))
    window = timedelta(hours=settings.get("tv_match_window_hours", 72))
    threshold = settings.get("tv_match_threshold", 0.32)
    per_feed = settings.get("max_items_per_feed", 40)
    started = now_utc()

    previous = {}
    if OUT_PATH.exists():
        old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        for a in old.get("articles", []) + old.get("tv", []):
            previous[a["id"]] = a

    collected = {}
    statuses = []
    for feed in config.get("feeds", []):
        feed = {**feed, "max_items": per_feed}
        url = feed_url(feed)
        try:
            entries = parse_feed(fetch(url), feed)
            statuses.append({"name": feed["name"], "kind": feed["kind"], "ok": True, "count": len(entries)})
        except Exception as e:  # one broken feed must not stop the others
            print(f"[skip] {feed['name']}: {e}", file=sys.stderr)
            statuses.append({"name": feed["name"], "kind": feed["kind"], "ok": False, "error": str(e)[:200]})
            continue
        for e in entries:
            art_id = hashlib.sha1(e["link"].encode()).hexdigest()[:16]
            if art_id in collected:  # the same article from an earlier feed wins
                continue
            published = e["published"] or started
            art = previous.get(art_id, {}) | {
                "id": art_id,
                "title": e["title"],
                "link": e["link"],
                "summary": e["summary"],
                "outlet": e["outlet"],
                "feed": feed["name"],
                "kind": feed["kind"],
                "lang": feed.get("lang", "ja"),
            }
            art.setdefault("published", iso(published))
            art.setdefault("first_seen", iso(started))
            collected[art_id] = art
        print(f"[ok] {feed['name']}: {len(entries)}")

    # Keep earlier articles that dropped out of the feeds but are still within keep_days.
    for art_id, art in previous.items():
        collected.setdefault(art_id, art)
    cutoff = started - keep
    items = [a for a in collected.values()
             if datetime.fromisoformat(a["published"].replace("Z", "+00:00")) >= cutoff]

    tv = [a for a in items if a["kind"] == "tv"]
    articles = [a for a in items if a["kind"] != "tv"]

    if os.environ.get("ANTHROPIC_API_KEY"):
        pending = [a for a in articles if a["lang"] == "en" and "title_ja" not in a]
        pending.sort(key=lambda a: a["published"], reverse=True)
        if pending:
            translate(pending[:TRANSLATE_MAX_PER_RUN], settings.get("model", "claude-opus-5-5"))
    else:
        print("ANTHROPIC_API_KEY not set: English articles stay untranslated")

    for t in tv:
        t["_grams"] = bigrams(t["title"])
    for a in articles:
        a["tv_match"] = match_tv(a, tv, threshold, window)
    for t in tv:
        del t["_grams"]

    articles.sort(key=lambda a: a["published"], reverse=True)
    tv.sort(key=lambda a: a["published"], reverse=True)
    data = {
        "generated_at": iso(started),
        "settings": {"keep_days": keep.days, "tv_match_threshold": threshold},
        "topics": config.get("topics", []),
        "feeds": statuses,
        "articles": articles,
        "tv": tv,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    uncovered = sum(1 for a in articles if a["tv_match"] is None)
    print(f"wrote {len(articles)} articles ({uncovered} not seen on TV) and {len(tv)} TV headlines")


if __name__ == "__main__":
    main()
