from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_app.gates import (  # noqa: E402
    GATE_OWNERS,
    VALID_DECISIONS,
    VALID_GATES,
    GateBlockedError,
    record_gate_decision,
)
from factory_app.state import RUNS_DIR  # noqa: E402

__all__ = ["GATE_OWNERS", "VALID_DECISIONS", "VALID_GATES", "main"]


def main(argv: list[str] | None = None, *, runs_dir: Path | None = None) -> Path:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 4:
        raise SystemExit('Usage: python scripts/approve_gate.py CR-001 plan APPROVED "reason"')
    change_id = args[0].strip().upper()
    gate = args[1].strip().lower()
    decision = args[2].strip().upper()
    reason = " ".join(args[3:]).strip()
    if gate not in VALID_GATES:
        raise SystemExit(f"Invalid gate: {sorted(VALID_GATES)}")
    if decision not in VALID_DECISIONS:
        raise SystemExit(f"Invalid decision: {sorted(VALID_DECISIONS)}")
    if not reason:
        raise SystemExit("Human decision requires a reason")

    target_runs_dir = runs_dir or RUNS_DIR
    try:
        _, path = record_gate_decision(
            change_id,
            gate,
            decision,
            reason,
            runs_dir=target_runs_dir,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Run not found: {change_id}") from exc
    except (ValueError, GateBlockedError) as exc:
        raise SystemExit(f"BLOCKED BY HUMAN GATE: {exc}") from exc

    try:
        display_path = path.relative_to(ROOT)
    except ValueError:
        display_path = path
    print(f"Recorded {gate}: {decision} -> {display_path}")
    return path


if __name__ == "__main__":
    main()
