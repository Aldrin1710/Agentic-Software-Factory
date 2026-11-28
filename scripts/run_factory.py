from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_app.gates import GateBlockedError  # noqa: E402
from factory_app.orchestrator import run_factory  # noqa: E402
from factory_app.state import RUNS_DIR  # noqa: E402


def main(
    argv: list[str] | None = None,
    *,
    runs_dir: Path | None = None,
    agent_runner=None,
    verifier=None,
    diff_provider=None,
    prompt_fn=None,
) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        raise SystemExit(
            "Usage: python scripts/run_factory.py CR-001 [--interactive] [--deterministic-only]"
        )

    change_id = args[0].strip().upper()
    flags = set(args[1:])
    supported = {"--interactive", "--deterministic-only"}
    unknown = sorted(flags - supported)
    if unknown:
        raise SystemExit(f"Unknown flags: {unknown}. Supported: {sorted(supported)}")

    kwargs: dict = {
        "runs_dir": runs_dir or RUNS_DIR,
        "interactive": "--interactive" in flags,
        "run_test_analyst": "--deterministic-only" not in flags,
    }
    if agent_runner is not None:
        kwargs["agent_runner"] = agent_runner
    if verifier is not None:
        kwargs["verifier"] = verifier
    if diff_provider is not None:
        kwargs["diff_provider"] = diff_provider
    if prompt_fn is not None:
        kwargs["prompt_fn"] = prompt_fn

    try:
        result = asyncio.run(run_factory(change_id, **kwargs))
    except (GateBlockedError, ValueError, FileNotFoundError) as exc:
        raise SystemExit(f"FACTORY STOPPED: {exc}") from exc

    print(json.dumps(result, indent=2))
    return 1 if result.get("status") == "VERIFICATION_FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
