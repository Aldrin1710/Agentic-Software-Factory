from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_app.orchestrator import run_planning  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/run_planning.py CR-001")
    change_id = sys.argv[1].strip().upper()
    result = asyncio.run(run_planning(change_id))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
