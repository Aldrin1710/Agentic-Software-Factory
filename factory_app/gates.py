from __future__ import annotations

import json
from pathlib import Path

from .state import RUNS_DIR, get_run_dir, load_state

GATE_EVIDENCE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "requirements": ("human-requirements-decision.json",),
    "plan": ("03-human-plan-decision.json", "human-plan-decision.json"),
    "review": ("08-human-review-decisions.json", "human-review-decision.json"),
    "release": ("12-release-decision.json", "human-release-decision.json"),
}


class GateBlockedError(RuntimeError):
    """Raised when an execution step attempts to bypass a required human gate."""


def find_gate_decision_file(
    change_id: str, gate: str, *, runs_dir: Path = RUNS_DIR
) -> Path | None:
    run_dir = get_run_dir(change_id, runs_dir=runs_dir)
    candidates = GATE_EVIDENCE_CANDIDATES.get(gate, (f"human-{gate}-decision.json",))
    for filename in candidates:
        candidate = run_dir / filename
        if candidate.is_file():
            return candidate
    return None


def require_human_gate(
    change_id: str,
    gate: str,
    *,
    allowed_decisions: set[str] | None = None,
    runs_dir: Path = RUNS_DIR,
) -> dict:
    """Verify both factory-state.json and recorded decision evidence before proceeding."""
    allowed = allowed_decisions or {"APPROVED"}
    state = load_state(change_id, runs_dir=runs_dir)
    gate_status = state.get("human_gates", {}).get(gate)
    if gate_status not in allowed:
        raise GateBlockedError(
            f"Human gate {gate!r} for {change_id.upper()} is {gate_status!r}; "
            f"expected one of {sorted(allowed)}."
        )

    evidence_path = find_gate_decision_file(change_id, gate, runs_dir=runs_dir)
    if evidence_path is None:
        raise GateBlockedError(
            f"Missing human decision evidence file for gate {gate!r} in "
            f"{get_run_dir(change_id, runs_dir=runs_dir)}."
        )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("decision") not in allowed:
        raise GateBlockedError(
            f"Recorded decision in {evidence_path.name} is {evidence.get('decision')!r}; "
            f"expected one of {sorted(allowed)}."
        )
    if not str(evidence.get("reason", "")).strip():
        raise GateBlockedError(f"Human decision in {evidence_path.name} is missing a reason.")

    return evidence


def require_plan_approved(change_id: str, *, runs_dir: Path = RUNS_DIR) -> dict:
    """Block CoderAgent execution unless the Tech Lead approved the plan."""
    return require_human_gate(change_id, "plan", allowed_decisions={"APPROVED"}, runs_dir=runs_dir)
