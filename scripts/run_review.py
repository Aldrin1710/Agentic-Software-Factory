from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_app.gates import GateBlockedError  # noqa: E402
from factory_app.review import run_independent_review  # noqa: E402
from factory_app.state import RUNS_DIR  # noqa: E402


def main(
    argv: list[str] | None = None,
    *,
    runs_dir: Path | None = None,
    agent_runner=None,
    diff_provider=None,
) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or len(args) > 2:
        raise SystemExit("Usage: python scripts/run_review.py CR-001 [--rereview]")

    change_id = args[0].strip().upper()
    is_rereview = False
    if len(args) == 2:
        if args[1] != "--rereview":
            raise SystemExit(f"Unknown flag: {args[1]!r}. Supported flag: --rereview")
        is_rereview = True

    kwargs: dict = {
        "runs_dir": runs_dir or RUNS_DIR,
        "verification_filename": "10-reverification.txt" if is_rereview else "05-verification.txt",
        "output_filename": "11-rereview.md" if is_rereview else "06-review-report.md",
        "is_rereview": is_rereview,
    }
    if agent_runner is not None:
        kwargs["agent_runner"] = agent_runner
    if diff_provider is not None:
        kwargs["diff_provider"] = diff_provider

    try:
        result = asyncio.run(run_independent_review(change_id, **kwargs))
    except GateBlockedError as exc:
        raise SystemExit(f"BLOCKED BY GATE: {exc}") from exc
    except FileNotFoundError as exc:
        raise SystemExit(f"Run not found: {exc}") from exc

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
