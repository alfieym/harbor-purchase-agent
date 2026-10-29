"""Export the FastAPI OpenAPI schema for frontend type generation.

Usage:
    uv run --directory backend python scripts/export_openapi.py [output]
"""

import json
import sys
from pathlib import Path

from harbor.core.config import Settings
from harbor.main import create_app

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "frontend" / "openapi.json"


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    settings = Settings(_env_file=None, app_env="test")
    schema = create_app(settings).openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
