from __future__ import annotations

import json

from app.monitor import load_config, poll_all

results = poll_all(load_config())
print(json.dumps(results, indent=2))
raise SystemExit(1 if any(hsm["problems"] for hsm in results) else 0)
