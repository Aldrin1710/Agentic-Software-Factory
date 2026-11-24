from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_app.gates import GateBlockedError  # noqa: E402
from factory_app.orchestrator import run_coder  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/run_coder.py CR-001")
    change_id = sys.argv[1].strip().upper()
    try:
        result = asyncio.run(run_coder(change_id))
    except GateBlockedError as exc:
        raise SystemExit(f"BLOCKED BY HUMAN GATE: {exc}") from exc
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
