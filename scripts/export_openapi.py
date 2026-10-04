"""Write the API's OpenAPI spec to docs/openapi.json (run: make openapi).

The committed file lets anyone read the API, or import it into Postman or Insomnia, without
running the service. tests/test_openapi.py fails if it falls out of date.
"""

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "docs" / "openapi.json"


def build_spec() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql://unused",
        jwt_secret="unused",
        admin_api_key="unused",
        log_level="WARNING",
    )
    return create_app(settings).openapi()


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    SPEC_PATH.write_text(render(build_spec()))
    print(f"wrote {SPEC_PATH.relative_to(ROOT)}")
