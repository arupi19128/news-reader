"""Temporary: look at the HIS kessan plan JSON."""
import json, sys
sys.path.insert(0, "scripts")
from wedding import fetch
st, final, ct, cs, body = fetch("https://www.his-wedding.com/fair/kessan/json/kessan.json")
data = json.loads(body.decode("utf-8-sig", "replace"))
out = {"type": str(type(data)), "keys": list(data)[:200] if isinstance(data, dict) else None,
       "guam": {k: v for k, v in data.items() if "guam" in k} if isinstance(data, dict) else None}
open("docs/data/_probe.json", "w").write(json.dumps(out, ensure_ascii=False, indent=1))
