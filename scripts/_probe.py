"""Temporary: find how the HIS kessan page loads its plan data."""
import json, re, sys
sys.path.insert(0, "scripts")
from wedding import fetch
out = {}
st, final, ct, cs, body = fetch("https://www.his-wedding.com/fair/kessan/")
t = body.decode(cs or "utf-8", "replace")
out["inline"] = [m[:1500] for m in re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", t, re.S) if re.search(r"json|insertHtml|dataFolder|ajax", m)]
out["guam_html"] = [t[max(0, m.start() - 600):m.end() + 900] for m in re.finditer(r"GUAM WEDDING", t)][:1]
st, final, ct, cs, body = fetch("https://www.his-wedding.com/fair/kessan/js/template_fair_index.js")
j = body.decode(cs or "utf-8", "replace")
out["template_json_ctx"] = [j[max(0, m.start() - 900):m.end() + 600] for m in re.finditer(r"\.json|getJSON|\$\.ajax", j)][:3]
open("docs/data/_probe.json", "w").write(json.dumps(out, ensure_ascii=False, indent=1))
