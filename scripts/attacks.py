"""Fetch worldwide attack statistics from Cloudflare Radar and save them to docs/data/attacks.json.

Run from the repository root:  python scripts/attacks.py
Needs CLOUDFLARE_API_TOKEN (a free token with Radar read access); without it the script does nothing.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "data" / "attacks.json"
API = "https://api.cloudflare.com/client/v4/radar"

# name -> (path, params). Each one is optional: a failure is logged and the rest still save.
QUERIES = {
    "pairs": ("attacks/layer7/top/attacks", {"limit": 40, "dateRange": "1d"}),
    "l7_origin": ("attacks/layer7/top/locations/origin", {"limit": 10, "dateRange": "1d"}),
    "l7_target": ("attacks/layer7/top/locations/target", {"limit": 10, "dateRange": "1d"}),
    "l3_origin": ("attacks/layer3/top/locations/origin", {"limit": 10, "dateRange": "1d"}),
    "l3_target": ("attacks/layer3/top/locations/target", {"limit": 10, "dateRange": "1d"}),
    "mitigation": ("attacks/layer7/summary/mitigation_product", {"dateRange": "1d"}),
    "l3_vector": ("attacks/layer3/summary/vector", {"dateRange": "1d"}),
    "l7_series": ("attacks/layer7/timeseries", {"dateRange": "1d", "aggInterval": "1h"}),
}


def get(path, params):
    url = f"{API}/{path}?{urllib.parse.urlencode({**params, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}",
        "User-Agent": "news-radar/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as res:
        body = json.loads(res.read())
    if not body.get("success", True):
        raise RuntimeError(body.get("errors"))
    return body["result"]


def main():
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        print("CLOUDFLARE_API_TOKEN not set: skipping attack statistics")
        return
    data, errors = {}, {}
    for name, (path, params) in QUERIES.items():
        try:
            data[name] = get(path, params)
        except Exception as e:  # keep whatever else worked
            errors[name] = str(e)[:300]
            print(f"attacks: {name} failed: {e}", file=sys.stderr)
    if not data:
        print("attacks: nothing fetched, keeping the previous file", file=sys.stderr)
        return
    data["errors"] = errors
    data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"attacks: saved {len(data) - 2} datasets ({len(errors)} failed)")


if __name__ == "__main__":
    main()
