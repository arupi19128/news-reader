"""Temporary: fetch HIS kessan JS files and record where they load their data from."""
import json, re, sys, urllib.request
sys.path.insert(0, "scripts")
from wedding import fetch
out = {}
for u in ["https://www.his-wedding.com/fair/kessan/js/search.js", "https://www.his-wedding.com/fair/kessan/js/template_fair_index.js",
          "https://www.his-wedding.com/fair/kessan/js/top.js", "https://www.his-wedding.com/static/js/ajax.js"]:
    try:
        st, final, ct, cs, body = fetch(u)
        t = body.decode(cs or "utf-8", "replace")
        hits = sorted(set(re.findall(r"[\"'`]([^\"'`\s]*(?:\.json|\.csv|\.xml|api|data|/fair/)[^\"'`\s]*)[\"'`]", t)))[:80]
        out[u] = {"status": st, "len": len(t), "hits": hits, "head": t[:2500]}
    except Exception as e:
        out[u] = {"error": str(e)}
open("docs/data/_probe.json", "w").write(json.dumps(out, ensure_ascii=False, indent=1))
