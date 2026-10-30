"""Generate the checked-in API schema without opening or modifying a vault."""
import json
from pathlib import Path

from devos.web import create_app

root = Path(__file__).resolve().parents[1]
schema = create_app(root / ".devos" / "schema-only.sqlite3", run_worker=False).openapi()
(root / "web" / "openapi.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
print("Generated web/openapi.json")
