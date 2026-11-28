from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_app.gates import GateBlockedError  # noqa: E402
from factory_app.state import RUNS_DIR  # noqa: E402
from factory_app.verification import run_verification  # noqa: E402


def main(
    argv: list[str] | None = None,
    *,
    runs_dir: Path | None = None,
    verifier=None,
    agent_runner=None,
) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or len(args) > 2:
        raise SystemExit(
            "Usage: python scripts/run_verification.py CR-001 [--deterministic-only]"
        )

    change_id = args[0].strip().upper()
    deterministic_only = False
    if len(args) == 2:
        if args[1] != "--deterministic-only":
            raise SystemExit(
                f"Unknown flag: {args[1]!r}. Supported flag: --deterministic-only"
            )
        deterministic_only = True

    kwargs: dict = {
        "runs_dir": runs_dir or RUNS_DIR,
        "run_test_analyst": not deterministic_only,
    }
    if verifier is not None:
        kwargs["verifier"] = verifier
    if agent_runner is not None:
        kwargs["agent_runner"] = agent_runner

    try:
        result = asyncio.run(run_verification(change_id, **kwargs))
    except GateBlockedError as exc:
        raise SystemExit(f"BLOCKED BY HUMAN GATE: {exc}") from exc
    except FileNotFoundError as exc:
        raise SystemExit(f"Run not found: {exc}") from exc

    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
