"""Once a day, ask Claude for an easy, nutritious three-dish menu (serves two) and save it to docs/data/recipes.json.

Run from the repository root:  python scripts/recipes.py
Needs ANTHROPIC_API_KEY; without it the script does nothing.
"""

import json
import os
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
OUT_PATH = ROOT / "docs" / "data" / "recipes.json"
JST = timezone(timedelta(hours=9))

ROLES = ["主菜", "副菜", "汁物"]
TAGS = ["10分以内", "レンジだけ", "火を使わない", "作り置き", "たんぱく質", "野菜たっぷり", "鉄分", "カルシウム", "食物繊維", "低カロリー"]

DISH = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ROLES},
        "name": {"type": "string", "description": "料理名"},
        "catch": {"type": "string", "description": "ひとことの紹介（30字以内）"},
        "minutes": {"type": "integer", "description": "調理時間（分）"},
        "kcal": {"type": "integer", "description": "1人分の目安カロリー"},
        "protein_g": {"type": "integer", "description": "1人分の目安たんぱく質（g）"},
        "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "amount": {"type": "string"}},
                "required": ["name", "amount"],
                "additionalProperties": False,
            },
        },
        "steps": {"type": "array", "items": {"type": "string"}},
        "nutrition_note": {"type": "string", "description": "どんな栄養がとれて体にどう良いか（2文）"},
        "tip": {"type": "string", "description": "失敗しないコツや時短のポイント（1文）"},
    },
    "required": ["role", "name", "catch", "minutes", "kcal", "protein_g", "tags", "ingredients", "steps", "nutrition_note", "tip"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "theme": {"type": "string", "description": "今日の献立のテーマ（20字以内）"},
        "dishes": {"type": "array", "items": DISH},
    },
    "required": ["theme", "dishes"],
    "additionalProperties": False,
}


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set: skipping recipes")
        return
    settings = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("recipes", {})
    now = datetime.now(JST)
    if now.hour < settings.get("hour_jst", 6):
        return
    today = now.strftime("%Y-%m-%d")
    data = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {"days": []}
    if any(d["date"] == today for d in data["days"]):
        return

    recent = [dish["name"] for d in data["days"][:14] for dish in d["dishes"]]
    prompt = (
        f"今日は{now.month}月{now.day}日です。2人分の夕食の献立を、主菜・副菜・汁物の3品で考えてください。\n\n"
        "条件:\n"
        "- 簡単であること。どの料理も調理時間20分以内、材料は調味料を除いて6つ以内。特別な道具は使わない\n"
        "- 栄養があること。3品合わせて、たんぱく質・野菜・海藻やきのこ・大豆製品がそろうようにする\n"
        "- 今の季節に手に入りやすく安い、旬の食材を使う。日本の一般的なスーパーで買えるものだけ\n"
        "- 材料の分量は2人分で、「大さじ1」「1/2個」「150g」のように具体的に書く\n"
        "- 手順は3〜5ステップ。料理に慣れていない人でも分かる言葉で\n"
        "- カロリーとたんぱく質は1人分の目安。tags は当てはまるものだけ選ぶ\n"
        f"- 最近出した料理とは別のものにする: {json.dumps(recent, ensure_ascii=False)}\n"
    )

    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=settings.get("model", "claude-opus-5-5"),
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        print(f"recipes: API error ({e.status_code}): {e.message}", file=sys.stderr)
        return
    except anthropic.APIConnectionError as e:
        print(f"recipes: connection error: {e}", file=sys.stderr)
        return
    if response.stop_reason != "end_turn":
        print(f"recipes: stopped ({response.stop_reason})", file=sys.stderr)
        return
    menu = json.loads(next(b.text for b in response.content if b.type == "text"))
    menu["dishes"].sort(key=lambda d: ROLES.index(d["role"]))
    for i, dish in enumerate(menu["dishes"]):
        dish["id"] = f"{today}-{i}"

    data["days"].insert(0, {"date": today, "theme": menu["theme"], "dishes": menu["dishes"]})
    data["days"] = data["days"][: settings.get("keep_days", 60)]
    data["generated_at"] = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"recipes: {today} {menu['theme']} — " + " / ".join(d["name"] for d in menu["dishes"]))


if __name__ == "__main__":
    main()
