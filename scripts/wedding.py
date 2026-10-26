"""Find wedding fairs and campaigns for the chosen chapels on the official / travel-agency sites.

Run from the repository root:  python scripts/wedding.py
Settings live in config.toml under [wedding]. Results go to docs/data/wedding.json.
Runs at most once every `interval_hours`, or right away when the [wedding] settings change.
When WEDDING_NOTIFY_FILE is set and new campaign pages turned up, writes a Markdown notice there
(the workflow turns it into a GitHub issue, which reaches the owner by email).
"""

import hashlib
import html
import io
import json
import os
import re
import sys
import tomllib
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
OUT_PATH = ROOT / "docs" / "data" / "wedding.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
JST = timezone(timedelta(hours=9))

CAMPAIGN_WORDS = re.compile(r"キャンペーン|フェア|セール|特典|OFF|オフ|割引|円引|プレゼント|限定|campaign|fair|cp", re.I)
_DROP = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"</?(p|div|li|tr|td|th|h[1-6]|br|section|article|dt|dd|table|ul|ol)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)

# 2027年7月1日 / 2027.7.1 / 2027/7/1, optionally followed by a weekday like （木）
_DATE = r"(?:(\d{4})\s*[年./／]\s*)?(\d{1,2})\s*[月./／]\s*(\d{1,2})\s*日?(?:\s*[（(][^）)]{1,3}[）)])?"
_RANGE = re.compile(_DATE + r"\s*[~～〜\-－ー―から]+\s*" + _DATE)
_UNTIL = re.compile(_DATE + r"\s*(?:\d{1,2}:\d{2}\s*)?まで")
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.I)
CEREMONY_LABEL = re.compile(r"挙式|ご結婚式|wedding date", re.I)
APPLY_LABEL = re.compile(r"申込|申し込み|お申込|予約|成約|受付|販売")


def fetch(url, limit=3_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*",
                                               "Accept-Language": "ja,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=25) as res:
        body = res.read(limit)
        return res.status, res.geturl(), res.headers.get_content_type(), res.headers.get_content_charset(), body


def html_text(raw):
    raw = _DROP.sub(" ", raw)
    raw = _BLOCK.sub("\n", raw)
    text = html.unescape(_TAG.sub(" ", raw))
    lines = [re.sub(r"[ \t　]+", " ", l).strip() for l in text.splitlines()]
    return "\n".join(l for l in lines if l)


def pdf_text(body):
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(body))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:20])
    except Exception:
        return ""


def to_date(y, m, d, fallback_year):
    try:
        return date(int(y) if y else fallback_year, int(m), int(d))
    except ValueError:
        return None


def _kind(text, pos):
    # The label is on the same line just before the date, or alone on the line above it.
    lines = text[max(0, pos - 60):pos].split("\n")
    label = lines[-1] if lines[-1].strip() or len(lines) < 2 else lines[-2]
    return "apply" if APPLY_LABEL.search(label) else "ceremony" if CEREMONY_LABEL.search(label) else "other"


def find_periods(text, today):
    """Every date range ("7月1日〜12月31日") and deadline ("12月30日まで", start None) in the text.

    kind is ceremony / apply / other, judged by the words just before the date.
    """
    out, spans = [], []
    for m in _RANGE.finditer(text):
        y1, m1, d1, y2, m2, d2 = m.groups()
        start = to_date(y1, m1, d1, int(y2) if y2 else today.year)
        end = start and to_date(y2, m2, d2, start.year)
        if not end:
            continue
        if end < start:  # 2026年12月1日〜3月31日 crosses the new year
            end = end.replace(year=end.year + 1)
        spans.append(m.span())
        snippet = re.sub(r"\s+", " ", text[max(0, m.start() - 30):m.end() + 5]).strip()
        out.append({"kind": _kind(text, m.start()), "start": start.isoformat(), "end": end.isoformat(), "text": snippet[:120]})
    for m in _UNTIL.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        y, mo, d = m.groups()
        end = to_date(y, mo, d, today.year)
        if not end:
            continue
        snippet = re.sub(r"\s+", " ", text[max(0, m.start() - 30):m.end() + 5]).strip()
        out.append({"kind": _kind(text, m.start()), "start": None, "end": end.isoformat(), "text": snippet[:120]})
    # Drop exact duplicates, keep order.
    seen, uniq = set(), []
    for p in out:
        k = (p["kind"], p["start"], p["end"])
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq[:12]


def highlights(text):
    """Short lines that state the deal: discounts, presents, limits."""
    picks = []
    for line in text.split("\n"):
        line = line.strip()
        if re.search(r"無料相談|はこちら|予約する|お問い合わせ", line) and not re.search(r"OFF|円", line, re.I):
            continue  # the "book a free consultation" buttons on every page
        if 6 <= len(line) <= 90 and re.search(r"OFF|オフ|円引|割引|%|％|プレゼント|無料|限定|特典", line, re.I):
            if line not in picks:
                picks.append(line)
        if len(picks) >= 6:
            break
    return picks


def overlaps(periods, target):
    """True if a ceremony period overlaps the target months, False if all ceremony periods miss it, None if unknown."""
    t0, t1 = date.fromisoformat(target[0]), date.fromisoformat(target[1])
    cer = [p for p in periods if p["kind"] == "ceremony"] or [p for p in periods if p["kind"] == "other"]
    if not cer:
        return None
    return any((p["start"] is None or date.fromisoformat(p["start"]) <= t1) and date.fromisoformat(p["end"]) >= t0
               for p in cer)


_JSON_FILES = re.compile(r"jsonFiles\s*:\s*\[\s*['\"]([^'\"]+)['\"]")
_DATA_FOLDER = re.compile(r"dataFolder\s*:\s*['\"]([^'\"]*)['\"]")


_MONTHS = re.compile(r"(\d{4})年(\d{1,2})月\s*[~～〜\-－]\s*(?:(\d{4})年)?(\d{1,2})月(?!\s*\d)")


def month_range(text):
    """'2026年10月～2027年9月' -> '2026年10月1日～2027年9月30日' (plans often give whole months)."""
    def full(m):
        y1, m1, y2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3) or m.group(1)), int(m.group(4))
        last = (date(y2 + (m2 == 12), m2 % 12 + 1, 1) - timedelta(days=1)).day
        return f"{y1}年{m1}月1日～{y2}年{m2}月{last}日"
    return _MONTHS.sub(full, text)


def fair_plans(page_url, page_title, raw, area):
    """HIS sale pages fill in their plans from a JSON file; read it and return one pseudo-page per plan in the area."""
    files, folder = _JSON_FILES.search(raw), _DATA_FOLDER.search(raw)
    if not files:
        return {}
    url = urllib.parse.urljoin(page_url, (folder.group(1) if folder else "") + files.group(1) + ".json")
    try:
        data = json.loads(fetch(url)[4].decode("utf-8-sig", "replace"))
    except Exception as e:
        print(f"wedding: {url}: {e}", file=sys.stderr)
        return {}
    plans = {}
    for key, entries in data.items():
        if not key.startswith("avanti--") or not isinstance(entries, list):
            continue
        for p in entries:
            if not isinstance(p, dict) or p.get("disp") != "○" or not area.search(p.get("area_name", "") + key):
                continue
            name = re.sub(r"<[^>]+>", "", p.get("title_plan", "")).strip()
            deal = f"【{p['limit']}組限定】" if p.get("limit") else ""
            if p.get("discount"):
                deal += f"{p.get('yaku', '')}{p['discount']}万円引き"
            price = f"おふたりで {p['listprice_01']}円{p.get('pricemax', '')}" if p.get("listprice_01") else ""
            text = "\n".join(x for x in [
                name,
                f"対象挙式期間：{p['kikan_dtl']}" if p.get("kikan_dtl")
                else f"対象挙式期間：{month_range(p['kikan'])}" if p.get("kikan") else "",
                f"お申込期間：{p['kikan_application']}" if p.get("kikan_application") else "",
                deal, price, p.get("prm", ""),
                html.unescape(re.sub(r"<[^>]+>", " ", p.get("pkg_01_dtl", ""))),
            ] if x)
            code = p.get("code") if p.get("code") not in (None, "", "なし") else f"{key}-{p.get('no', '')}"
            plans[f"{page_url}#{code}"] = {"title": f"{page_title}：{name}", "text": text, "plan": True}
    return plans


def crawl_source(src, venues, area, max_pages):
    """Fetch the seed pages and the campaign-looking links on them (one level deep, same site)."""
    pages, probe = {}, []
    queue = list(src["seeds"])
    seen = set()
    hosts = {urllib.parse.urlparse(u).netloc for u in src["seeds"]}
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            status, final, ctype, charset, body = fetch(url)
        except Exception as e:
            probe.append({"url": url, "error": str(e)[:160]})
            continue
        if ctype == "application/pdf" or final.lower().endswith(".pdf"):
            text, title, links = pdf_text(body), Path(urllib.parse.urlparse(final).path).name, []
        else:
            if not charset:
                meta = _META_CHARSET.search(body[:4000])
                charset = meta.group(1).decode() if meta else "utf-8"
            try:
                raw = body.decode(charset, "replace")
            except LookupError:
                raw = body.decode("utf-8", "replace")
            text = html_text(raw)
            t = _H1.search(raw) or _TITLE.search(raw)
            title = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", t.group(1)))).strip() if t else final
            links = []
            for href, label in _LINK.findall(raw):
                link = urllib.parse.urljoin(final, html.unescape(href)).split("#")[0]
                label = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", label))).strip()
                if urllib.parse.urlparse(link).netloc not in hosts:
                    continue
                if any(re.search(p, link) for p in src.get("follow", [])) or (
                        CAMPAIGN_WORDS.search(label + " " + link) and (area.search(label + link) or venues.search(label + link))):
                    links.append(link)
        final = final.replace(":443/", "/", 1)
        entry = {"url": final, "status": status, "type": ctype, "chars": len(text), "title": title[:80],
                 "links": len(links), "head": re.sub(r"\s+", " ", text[:300])}
        probe.append(entry)
        pages[final] = {"title": title, "text": text}
        if ctype != "application/pdf" and not final.lower().endswith(".pdf"):
            plans = fair_plans(final, title, raw, area)
            if plans:
                entry["plans"] = len(plans)
                pages.update(plans)
        if url in src["seeds"]:
            queue.extend(l for l in dict.fromkeys(links) if l not in seen)
    return pages, probe


def notice(fresh, target, config):
    """Markdown body for the notification issue, target-period campaigns first."""
    def period(it, kind, label):
        p = next((p for p in it["periods"] if p["kind"] == kind), None)
        if not p:
            return ""
        return f"- {label}：{p['start'] + '〜' if p['start'] else ''}{p['end']}{'' if p['start'] else ' まで'}\n"

    fresh = sorted(fresh, key=lambda it: {True: 0, None: 1, False: 2}[it["target_match"]])
    lines = []
    for it in fresh:
        mark = {True: "🎯 **2027年7〜12月の挙式が対象**", None: "対象の挙式期間は公式ページで確認", False: "ほかの時期の挙式が対象"}[it["target_match"]]
        lines.append(f"### [{it['title']}]({it['url']})\n")
        lines.append(f"{it['source']} ／ {'・'.join(it['venues'])} ／ {mark}\n\n")
        lines.append(period(it, "ceremony", "対象の挙式") + period(it, "apply", "申込期間"))
        lines += [f"- {h}\n" for h in it["highlights"][:3]]
        lines.append("\n")
    owner = config.get("notify_mention", "")
    return ("".join(lines)
            + f"\n一覧：{config.get('page_url', '')}\n\n"
            + "期間や金額はページの文字から自動で読み取ったものです。申し込む前に公式ページで確認してください。\n"
            + (f"\n{owner}\n" if owner else ""))


def main():
    config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("wedding", {})
    if not config.get("sources"):
        print("wedding: no sources configured")
        return
    # Re-check right away when the settings or this script change.
    config_hash = hashlib.sha1(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
                               + Path(__file__).read_bytes()).hexdigest()[:12]
    now = datetime.now(timezone.utc)
    today = now.astimezone(JST).date()

    old = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {}
    last = old.get("generated_at")
    if (old.get("config_hash") == config_hash and last
            and now - datetime.fromisoformat(last.replace("Z", "+00:00")) < timedelta(hours=config.get("interval_hours", 20))):
        print("wedding: checked recently; skipping")
        return

    venues = {v["name"]: re.compile(v["match"], re.I) for v in config["venues"]}
    any_venue = re.compile("|".join(v["match"] for v in config["venues"]), re.I)
    area = re.compile(config.get("area_match", "グアム|GUAM"), re.I)
    target = config["target"]
    previous = {it["url"]: it for it in old.get("items", [])}

    items, probes = [], []
    for src in config["sources"]:
        pages, probe = crawl_source(src, any_venue, area, config.get("max_pages_per_source", 30))
        probes.append({"source": src["name"], "pages": probe})
        # Menus and footers repeat on every page of a site and name every chapel; drop lines found on most pages.
        real = [page for page in pages.values() if not page.get("plan")]
        if len(real) >= 3:
            counts = {}
            for page in real:
                for line in set(page["text"].split("\n")):
                    counts[line] = counts.get(line, 0) + 1
            common = {line for line, n in counts.items() if n >= max(2, len(real) // 2)}
            for page in real:
                page["text"] = "\n".join(l for l in page["text"].split("\n") if l not in common)
        for url, page in pages.items():
            text = page["text"]
            # A PDF's file name often carries the chapel (crystal_guam_2609.pdf).
            hit = [name for name, rx in venues.items() if rx.search(text) or rx.search(url)
                   or (name == "ジュエル・バイ・ザ・シー" and re.search(r"jewel", url, re.I))
                   or (name == "クリスタルチャペル" and re.search(r"crystal", url, re.I))]
            in_area = bool(area.search(text) or area.search(url))
            if not (hit or in_area) or not CAMPAIGN_WORDS.search(page["title"] + text[:4000]):
                continue
            periods = find_periods(text, today)
            prev = previous.get(url, {})
            items.append({
                "url": url,
                "source": src["name"],
                "title": page["title"],
                "venues": hit or ["グアム全体"],
                "periods": periods,
                "highlights": highlights(text),
                "target_match": overlaps(periods, target),
                "first_seen": prev.get("first_seen", now.strftime("%Y-%m-%dT%H:%M:%SZ")),
                "last_seen": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })

    # Keep items that vanished from the sites for a while, marked as gone, so a campaign that ended is still visible.
    seen_now = {it["url"] for it in items}
    for url, it in previous.items():
        if url not in seen_now and datetime.fromisoformat(it["last_seen"].replace("Z", "+00:00")) > now - timedelta(days=30):
            items.append({**it, "gone": True})

    data = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # When checking started; pages already there on the first check are not marked NEW.
        "since": old.get("since", now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        "config_hash": config_hash,
        "target": target,
        "venues": [v["name"] for v in config["venues"]],
        "sources": [{"name": s["name"], "url": s["seeds"][0]} for s in config["sources"]],
        "items": items,
        "probe": probes,
    }
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # Notify about pages never seen before (not on the very first check, when everything is new).
    # Also re-announce a known page once it turns out to cover the target months.
    fresh = [it for it in items if not it.get("gone") and (
        it["url"] not in previous or (it["target_match"] is True and previous[it["url"]].get("target_match") is not True))]
    notify_path = os.environ.get("WEDDING_NOTIFY_FILE")
    if fresh and previous and notify_path:
        Path(notify_path).write_text(notice(fresh, target, config), encoding="utf-8")
        print(f"wedding: {len(fresh)} new campaign pages; notice written")
    print(f"wedding: {len(items)} campaign pages; " + ", ".join(f"{p['source']} {len(p['pages'])} pages" for p in probes))


if __name__ == "__main__":
    main()
