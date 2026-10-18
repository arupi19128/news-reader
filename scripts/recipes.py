"""Once a day, collect popular easy/healthy recipes from the Rakuten Recipe API and save them to docs/data/recipes.json.

Run from the repository root:  python scripts/recipes.py
Needs RAKUTEN_APP_ID (a free Rakuten Web Service application ID); without it the script does nothing.
"""

import json
import os
import re
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
OUT_PATH = ROOT / "docs" / "data" / "recipes.json"
JST = timezone(timedelta(hours=9))
API = "https://app.rakuten.co.jp/services/api/Recipe"
ROLES = ["主菜", "副菜", "汁物"]


def call(endpoint, **params):
    params = {"format": "json", "formatVersion": 2, "applicationId": os.environ["RAKUTEN_APP_ID"], **params}
    url = f"{API}/{endpoint}/20170426?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "news-radar/1.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.loads(res.read())
    time.sleep(1.1)  # the API allows about one request per second
    return data


def category_ids(wanted):
    """Map each wanted name to a category id the ranking API accepts ("large", "large-medium" or "large-medium-small").

    An exact name match wins; otherwise the first category whose name contains the word, larger categories first.
    """
    result = call("CategoryList", categoryType="")["result"]
    cats = []  # (name, id) from large to small
    for c in result.get("large", []):
        cats.append((c["categoryName"], str(c["categoryId"])))
    medium_path = {}
    for c in result.get("medium", []):
        path = f'{c["parentCategoryId"]}-{c["categoryId"]}'
        medium_path[str(c["categoryId"])] = path
        cats.append((c["categoryName"], path))
    for c in result.get("small", []):
        parent = medium_path.get(str(c["parentCategoryId"]))
        if parent:
            cats.append((c["categoryName"], f'{parent}-{c["categoryId"]}'))
    found = {}
    for word in wanted:
        hit = next(((n, i) for n, i in cats if n == word), None) or next(((n, i) for n, i in cats if word in n), None)
        if hit:
            found[word] = hit[1]
        else:
            print(f"recipes: no category for {word!r}", file=sys.stderr)
    return found


def minutes(indication):
    """"約10分" → 10, "5分以内" → 5, "1時間以上" → 60, unknown → None."""
    if not indication:
        return None
    if "時間" in indication:
        m = re.search(r"(\d+)", indication)
        return 60 * int(m.group(1)) if m else 60
    m = re.search(r"(\d+)", indication)
    return int(m.group(1)) if m else None


def main():
    if not os.environ.get("RAKUTEN_APP_ID"):
        print("RAKUTEN_APP_ID not set: skipping recipes")
        return
    settings = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("recipes", {})
    now = datetime.now(JST)
    if now.hour < settings.get("hour_jst", 6):
        return
    today = now.strftime("%Y-%m-%d")
    data = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {"days": []}
    if any(d["date"] == today for d in data["days"]):
        return

    roles = settings.get("roles", {})  # role -> category names
    general = settings.get("categories", [])
    wanted = list(dict.fromkeys(general + [c for names in roles.values() for c in names]))
    try:
        ids = category_ids(wanted)
    except Exception as e:
        print(f"recipes: category list failed: {e}", file=sys.stderr)
        return

    recipes, rankings = {}, {}
    for name, cid in ids.items():
        if cid not in rankings:  # two words can point at the same category; fetch it once
            try:
                rankings[cid] = call("CategoryRanking", categoryId=cid).get("result", [])
            except Exception as e:
                print(f"recipes: ranking failed for {name}: {e}", file=sys.stderr)
                rankings[cid] = []
        items = rankings[cid]
        role = next((r for r, names in roles.items() if name in names), None)
        for it in items:
            rid = str(it["recipeId"])
            r = recipes.setdefault(rid, {
                "id": rid,
                "name": it.get("recipeTitle", ""),
                "catch": it.get("recipeDescription", ""),
                "url": it.get("recipeUrl", ""),
                "image": it.get("foodImageUrl") or it.get("mediumImageUrl") or "",
                "materials": it.get("recipeMaterial", []),
                "indication": it.get("recipeIndication", ""),
                "minutes": minutes(it.get("recipeIndication", "")),
                "cost": it.get("recipeCost", ""),
                "author": it.get("nickname", ""),
                "role": None,
                "categories": [],
            })
            r["categories"].append(name)
            if role and not r["role"]:
                r["role"] = role
        print(f"recipes: {name}: {len(items)}")
    if not recipes:
        print("recipes: nothing collected", file=sys.stderr)
        return

    # Today's menu: one main, one side and one soup, quickest first, not shown in the last two weeks.
    shown = {d.get("menu", {}).get(r) for d in data["days"][:14] for r in ROLES}
    menu = {}
    for role in ROLES:
        options = [r for r in recipes.values() if r["role"] == role and r["id"] not in shown]
        options.sort(key=lambda r: (r["minutes"] or 99, -len(r["categories"])))
        if options:
            menu[role] = options[0]["id"]

    data["days"].insert(0, {"date": today, "menu": menu, "recipes": list(recipes.values())})
    data["days"] = data["days"][: settings.get("keep_days", 30)]
    data["generated_at"] = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"recipes: {today}: {len(recipes)} recipes, menu {menu}")


if __name__ == "__main__":
    main()
