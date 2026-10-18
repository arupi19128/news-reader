"""Collect news feeds, flag stories that TV news has not covered, and write docs/data/news.json.

Run from the repository root:  python scripts/fetch_news.py
Set ANTHROPIC_API_KEY to label articles (Japanese headline, category) and to pick and explain
the day's stories with Claude; without it the script only collects and matches.
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
PICKS_PATH = ROOT / "docs" / "data" / "picks.json"
USER_AGENT = "Mozilla/5.0 (compatible; news-radar/1.0; +https://github.com/)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
_IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)
_IMG_TAG = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
_OG_IMAGE = re.compile(r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)[\"'][^>]*>", re.I)
_CONTENT_ATTR = re.compile(r"content=[\"']([^\"']+)[\"']", re.I)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


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


def feed_urls(feed):
    """URLs to try in order: the feed's own RSS first, then its Google News search as a fallback."""
    urls = [feed["url"]] if "url" in feed else []
    if "google_news" in feed:
        urls.append(google_news_url(feed))
    return urls


def google_news_url(feed):
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


def find_image(node, *html_parts):
    """Best image URL for a feed item: media:thumbnail/content, an image enclosure, or the first <img> in its HTML."""
    for tag in ("media:thumbnail", "media:content", "enclosure", "atom:link"):
        for el in node.findall(tag, NS):
            url = el.get("url") or el.get("href") or ""
            kind = el.get("type", "") + el.get("medium", "")
            if tag == "atom:link" and el.get("rel") != "enclosure":
                continue
            if url.startswith("http") and ("image" in kind or _IMG_EXT.search(url) or tag == "media:thumbnail"):
                return url
    for part in html_parts:
        m = _IMG_TAG.search(part or "")
        if m and m.group(1).startswith("http"):
            return html.unescape(m.group(1))
    return ""


def fetch_og_image(url):
    """The page's og:image (or twitter:image), read from the first 300 KB of its HTML."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=10) as res:
        head = res.read(300_000).decode(res.headers.get_content_charset() or "utf-8", "replace")
    for tag in _OG_IMAGE.findall(head):
        m = _CONTENT_ATTR.search(tag)
        if m:
            img = html.unescape(m.group(1))
            return urllib.parse.urljoin(url, img)
    return ""


def parse_feed(raw, feed):
    """Return a list of {title, link, summary, published, source} from RSS 2.0, RSS 1.0 or Atom."""
    # expat only handles UTF-8/16 and Latin-1; re-encode feeds such as Shift_JIS ones.
    head = raw[:200].decode("ascii", "ignore")
    declared = re.search(r'encoding=["\']([\w.-]+)["\']', head)
    if declared and declared.group(1).lower().replace("_", "-") not in ("utf-8", "utf8"):
        text = raw.decode(declared.group(1), "replace")
        raw = re.sub(r'encoding=["\'][\w.-]+["\']', 'encoding="utf-8"', text, count=1).encode("utf-8")
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
                "image": find_image(n, child_text(n, "description"), child_text(n, "content:encoded")),
            })
    elif root.tag.endswith("RDF"):
        for n in root.findall("rss1:item", NS):
            items.append({
                "title": child_text(n, "rss1:title"),
                "link": child_text(n, "rss1:link"),
                "summary": child_text(n, "rss1:description"),
                "published": child_text(n, "dc:date"),
                "source": "",
                "image": find_image(n, child_text(n, "rss1:description"), child_text(n, "content:encoded")),
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
                "image": find_image(n, child_text(n, "atom:content", "atom:summary")),
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
            "image": it["image"],
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


# ---- Thumbnails ------------------------------------------------------------

def add_page_images(articles, limit):
    """Fill in missing thumbnails from each article page's og:image, newest first, at most `limit` pages per run."""
    from concurrent.futures import ThreadPoolExecutor

    todo = [a for a in articles if not a.get("image") and not a.get("image_checked")
            and "news.google.com" not in a["link"]]
    todo.sort(key=lambda a: a["published"], reverse=True)
    todo = todo[:limit]

    def one(a):
        try:
            return a, fetch_og_image(a["link"])
        except Exception:
            return a, ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = 0
        for a, img in pool.map(one, todo):
            a["image_checked"] = True
            if img:
                a["image"] = img
                found += 1
    print(f"page images: {found} found in {len(todo)} pages")


# ---- Featured stories (no AI) ---------------------------------------------

_WORD = re.compile(r"[a-z][a-z'-]{2,}")
_STOP = set("the and for with from that this into over after about amid says said will have has are was were its their they his her you not but more than new how why what who when".split())


def _story_key(a):
    if a["lang"] == "en" and not a.get("title_ja"):
        text = (a["title"] + " " + a["summary"]).lower()
        return "en", {w for w in _WORD.findall(text) if w not in _STOP}
    return "ja", bigrams(a.get("title_ja") or a["title"])


def _same_story(k1, k2):
    (l1, s1), (l2, s2) = k1, k2
    if l1 != l2 or not s1 or not s2:
        return False
    overlap = len(s1 & s2) / min(len(s1), len(s2))
    return overlap >= (0.4 if l1 == "en" else 0.45)


def feature(articles, now, settings):
    """Pick the day's headline stories without AI: stories carried by more outlets, missing from TV, and recent.

    Returns [{"id", "related", "outlets"}] best first; "related" are other articles on the same story.
    """
    skip = [w.lower() for w in settings.get("feature_skip_words", [])]
    recent = [a for a in articles
              if parse_iso(a["published"]) >= now - timedelta(hours=24)
              and not any(w in (a["title"] + a["summary"]).lower() for w in skip)]
    recent.sort(key=lambda a: a["published"], reverse=True)

    stories = []  # each: {"items": [...], "key": story_key of the first item}
    for a in recent:
        key = _story_key(a)
        for st in stories:
            if _same_story(key, st["key"]):
                st["items"].append(a)
                break
        else:
            stories.append({"key": key, "items": [a]})

    def score(st):
        items = st["items"]
        outlets = len({a["outlet"] for a in items})
        uncovered = all(a.get("tv_match") is None for a in items)
        age_h = (now - parse_iso(items[0]["published"])).total_seconds() / 3600
        readable = any(a["lang"] == "ja" or a.get("title_ja") for a in items)
        return outlets * 3 + (4 if uncovered else 0) + max(0, 24 - age_h) / 8 + (2 if readable else 0)

    stories.sort(key=score, reverse=True)
    count = settings.get("featured_count", 9)
    per_kind = settings.get("featured_per_kind", 5)
    picked, kinds, picked_keys = [], {}, []
    for st in stories:
        items = st["items"]
        # Prefer a Japanese article as the face of the story.
        lead = next((a for a in items if a["lang"] == "ja" or a.get("title_ja")), items[0])
        if kinds.get(lead["kind"], 0) >= per_kind:
            continue
        # Skip near-duplicates of a story already picked (same event, worded differently).
        key = _story_key(lead)
        if any(k[0] == key[0] and key[1] and k[1] and len(key[1] & k[1]) / min(len(key[1]), len(k[1])) >= 0.3
               for k in picked_keys):
            continue
        picked_keys.append(key)
        kinds[lead["kind"]] = kinds.get(lead["kind"], 0) + 1
        picked.append({
            "id": lead["id"],
            "related": [a["id"] for a in items if a is not lead][:5],
            "outlets": len({a["outlet"] for a in items}),
        })
        if len(picked) >= count:
            break
    return picked


# ---- Daily picks ----------------------------------------------------------

JST = timezone(timedelta(hours=9))


def current_round(now, hours):
    """The latest pick round (e.g. "2026-10-18-12") that has started today in JST, or None."""
    jst = now.astimezone(JST)
    started = [h for h in hours if h <= jst.hour]
    return f"{jst:%Y-%m-%d}-{max(started):02d}" if started else None


def update_picks(picks_data, articles, settings, now, round_id):
    """Pick this round's stories (if round_id is set) and write any missing explanations."""
    import ai

    model = settings.get("model", "claude-opus-5-5")
    picked_ids = {p["id"] for p in picks_data["picks"]}
    if round_id:
        recent = now - timedelta(hours=24)
        candidates = [a for a in articles if a["id"] not in picked_ids and parse_iso(a["published"]) >= recent]
        # Stories missing from TV come first, newest first within each group.
        candidates.sort(key=lambda a: (a.get("tv_match") is None, a["published"]), reverse=True)
        candidates = candidates[:150]
        recent_titles = [(p.get("detail") or {}).get("headline") or p.get("title_ja") or p["title"]
                         for p in picks_data["picks"] if parse_iso(p["picked_at"]) >= now - timedelta(days=3)]
        chosen = ai.pick(candidates, recent_titles, settings.get("picks_per_round", 3), model) if candidates else []
        by_id = {a["id"]: a for a in candidates}
        for art_id in chosen:
            entry = {k: v for k, v in by_id[art_id].items()}
            entry |= {"picked_at": iso(now), "round": round_id, "detail": None}
            picks_data["picks"].append(entry)
        picks_data["rounds_done"] = (picks_data["rounds_done"] + [round_id])[-60:]
        print(f"round {round_id}: picked {len(chosen)}")

    pending = [p for p in picks_data["picks"] if not p.get("detail")]
    for p in pending[:settings.get("explain_max_per_run", 6)]:
        print(f"  explaining: {p.get('title_ja') or p['title']}")
        p["detail"] = ai.explain(p, model)

    keep = now - timedelta(days=settings.get("picks_keep_days", 30))
    picks_data["picks"] = [p for p in picks_data["picks"] if parse_iso(p["picked_at"]) >= keep]


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
        entries, error = None, None
        for url in feed_urls(feed):
            try:
                entries = parse_feed(fetch(url), feed)
                break
            except Exception as e:  # one broken feed must not stop the others; try the fallback URL
                error = e
                print(f"[retry] {feed['name']} {url[:60]}: {e}", file=sys.stderr)
        if entries is None:
            statuses.append({"name": feed["name"], "kind": feed["kind"], "ok": False, "error": str(error)[:200]})
            continue
        statuses.append({"name": feed["name"], "kind": feed["kind"], "ok": True, "count": len(entries)})
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
            if e["image"]:
                art["image"] = e["image"]
            art.setdefault("published", iso(published))
            art.setdefault("first_seen", iso(started))
            collected[art_id] = art
        print(f"[ok] {feed['name']}: {len(entries)}")

    # Keep earlier articles that dropped out of the feeds but are still within keep_days.
    for art_id, art in previous.items():
        collected.setdefault(art_id, art)
    cutoff = started - keep
    items = [a for a in collected.values()
             if parse_iso(a["published"]) >= cutoff]

    excludes = {f["name"]: f["exclude"] for f in config.get("feeds", []) if f.get("exclude")}
    items = [a for a in items
             if not any(w in a["title"] + a["summary"] for w in excludes.get(a["feed"], []))]

    tv = [a for a in items if a["kind"] == "tv"]
    articles = [a for a in items if a["kind"] != "tv"]
    add_page_images(articles, settings.get("og_image_max_per_run", 80))

    picks_data = {"rounds_done": [], "picks": []}
    if PICKS_PATH.exists():
        picks_data = json.loads(PICKS_PATH.read_text(encoding="utf-8"))

    # Claude runs only at the pick hours (JST); other runs just collect and match, using no tokens.
    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
    round_id = current_round(started, settings.get("pick_hours_jst", [7, 16, 22]))
    if round_id in picks_data["rounds_done"]:
        round_id = None
    if not use_ai:
        print("ANTHROPIC_API_KEY not set: skipping labels, picks and explanations")
    elif round_id:
        import ai
        recent = started - timedelta(hours=24)
        pending = [a for a in articles if "category" not in a and parse_iso(a["published"]) >= recent]
        pending.sort(key=lambda a: a["published"], reverse=True)
        if pending:
            ai.label(pending[:settings.get("label_max_per_round", 200)], settings["categories"],
                     settings.get("model", "claude-opus-5-5"))
    else:
        print("not a pick round: Claude is not called this run")

    for t in tv:
        t["_grams"] = bigrams(t["title"])
    for a in articles:
        a["tv_match"] = match_tv(a, tv, threshold, window)
    for t in tv:
        del t["_grams"]

    if use_ai and round_id:
        update_picks(picks_data, articles, settings, started, round_id)
    picked_ids = {p["id"] for p in picks_data["picks"]}
    for a in articles:
        a["picked"] = a["id"] in picked_ids

    articles.sort(key=lambda a: a["published"], reverse=True)
    tv.sort(key=lambda a: a["published"], reverse=True)
    data = {
        "generated_at": iso(started),
        "settings": {"keep_days": keep.days, "tv_match_threshold": threshold,
                     "categories": settings.get("categories", [])},
        "topics": config.get("topics", []),
        "feeds": statuses,
        "featured": feature(articles, started, settings),
        "articles": articles,
        "tv": tv,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    picks_data["picks"].sort(key=lambda p: p["picked_at"], reverse=True)
    picks_data["generated_at"] = iso(started)
    PICKS_PATH.write_text(json.dumps(picks_data, ensure_ascii=False, indent=1), encoding="utf-8")
    uncovered = sum(1 for a in articles if a["tv_match"] is None)
    print(f"wrote {len(articles)} articles ({uncovered} not seen on TV) and {len(tv)} TV headlines")


if __name__ == "__main__":
    main()
