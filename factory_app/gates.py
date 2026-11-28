from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .state import RUNS_DIR, VALID_STATUSES, get_run_dir, load_state, transition_state

VALID_GATES = {"requirements", "plan", "review", "release"}
VALID_DECISIONS = {"APPROVED", "MODIFIED", "REJECTED", "DEFERRED"}
GATE_OWNERS = {
    "requirements": "Giuseph Alexis Chan Torres (Product Owner)",
    "plan": "Aldrin Enrique Novelo Góngora (Tech Lead)",
    "review": "Erick Ricardo Vega Nolasco (Human Reviewer)",
    "release": "Aldrin Enrique Novelo Góngora (Release Manager)",
}

GATE_EVIDENCE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "requirements": ("human-requirements-decision.json",),
    "plan": ("03-human-plan-decision.json", "human-plan-decision.json"),
    "review": ("08-human-review-decisions.json", "human-review-decision.json"),
    "release": ("12-release-decision.json", "human-release-decision.json"),
}

GATE_STATUS_TRANSITIONS: dict[tuple[str, str], str] = {
    ("requirements", "APPROVED"): "PLANNING",
    ("requirements", "REJECTED"): "PLAN_REJECTED",
    ("plan", "APPROVED"): "IMPLEMENTING",
    ("plan", "REJECTED"): "PLAN_REJECTED",
    ("review", "APPROVED"): "EVIDENCE_PACKAGING",
    ("review", "REJECTED"): "FIX_IN_PROGRESS",
    ("review", "MODIFIED"): "FIX_IN_PROGRESS",
    ("release", "APPROVED"): "READY_FOR_HUMAN_MERGE",
    ("release", "REJECTED"): "RELEASE_BLOCKED",
}


class GateBlockedError(RuntimeError):
    """Raised when an execution step attempts to bypass a required human gate."""


def get_gate_decision_path(
    change_id: str, gate: str, *, runs_dir: Path = RUNS_DIR
) -> Path:
    normalized_gate = gate.strip().lower()
    candidates = GATE_EVIDENCE_CANDIDATES.get(normalized_gate)
    if not candidates:
        raise ValueError(f"Invalid gate: {gate!r}; expected one of {sorted(VALID_GATES)}")
    run_dir = get_run_dir(change_id, runs_dir=runs_dir)
    return run_dir / candidates[0]


def find_gate_decision_file(
    change_id: str, gate: str, *, runs_dir: Path = RUNS_DIR
) -> Path | None:
    run_dir = get_run_dir(change_id, runs_dir=runs_dir)
    normalized_gate = gate.strip().lower()
    candidates = GATE_EVIDENCE_CANDIDATES.get(
        normalized_gate, (f"human-{normalized_gate}-decision.json",)
    )
    for filename in candidates:
        candidate = run_dir / filename
        if candidate.is_file():
            return candidate
    return None


def record_gate_decision(
    change_id: str,
    gate: str,
    decision: str,
    reason: str,
    *,
    runs_dir: Path = RUNS_DIR,
    recorded_by: str | None = None,
) -> tuple[dict, Path]:
    """Record an explicit human gate decision and update factory-state.json."""
    normalized_id = change_id.strip().upper()
    normalized_gate = gate.strip().lower()
    normalized_decision = decision.strip().upper()
    cleaned_reason = reason.strip()

    if normalized_gate not in VALID_GATES:
        raise ValueError(f"Invalid gate: {sorted(VALID_GATES)}")
    if normalized_decision not in VALID_DECISIONS:
        raise ValueError(f"Invalid decision: {sorted(VALID_DECISIONS)}")
    if not cleaned_reason:
        raise ValueError("Human decision requires a reason")

    state = load_state(normalized_id, runs_dir=runs_dir)

    if normalized_gate == "plan":
        if (
            state.get("status") == "CLARIFICATION_REQUIRED"
            or state.get("human_gates", {}).get("requirements") == "CLARIFICATION_REQUIRED"
        ):
            raise GateBlockedError(
                f"Cannot record plan gate decision for {normalized_id} while "
                f"requirements clarification is still required."
            )
        plan_path = get_run_dir(normalized_id, runs_dir=runs_dir) / "02-plan.md"
        if not plan_path.is_file():
            raise GateBlockedError(
                f"Cannot record plan gate decision for {normalized_id}: "
                f"missing plan artifact {plan_path.name}."
            )

    current_status = state.get("status", "INTAKE_PENDING")
    next_status = GATE_STATUS_TRANSITIONS.get(
        (normalized_gate, normalized_decision), current_status
    )
    if next_status not in VALID_STATUSES:
        raise ValueError(f"Invalid factory status: {next_status!r}")

    path = get_gate_decision_path(normalized_id, normalized_gate, runs_dir=runs_dir)
    transition_state(
        normalized_id,
        next_status,
        gate_updates={normalized_gate: normalized_decision},
        runs_dir=runs_dir,
    )

    evidence = {
        "change_id": normalized_id,
        "gate": normalized_gate,
        "decision": normalized_decision,
        "reason": cleaned_reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "recorded_by": recorded_by or GATE_OWNERS[normalized_gate],
    }
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return evidence, path


def require_human_gate(
    change_id: str,
    gate: str,
    *,
    allowed_decisions: set[str] | None = None,
    runs_dir: Path = RUNS_DIR,
) -> dict:
    """Verify both factory-state.json and recorded decision evidence before proceeding."""
    allowed = allowed_decisions or {"APPROVED"}
    normalized_id = change_id.strip().upper()
    normalized_gate = gate.strip().lower()
    state = load_state(normalized_id, runs_dir=runs_dir)
    gate_status = state.get("human_gates", {}).get(normalized_gate)
    if gate_status not in allowed:
        raise GateBlockedError(
            f"Human gate {normalized_gate!r} for {normalized_id} is {gate_status!r}; "
            f"expected one of {sorted(allowed)}."
        )

    evidence_path = find_gate_decision_file(normalized_id, normalized_gate, runs_dir=runs_dir)
    if evidence_path is None:
        raise GateBlockedError(
            f"Missing human decision evidence file for gate {normalized_gate!r} in "
            f"{get_run_dir(normalized_id, runs_dir=runs_dir)}."
        )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence_change_id = evidence.get("change_id")
    if evidence_change_id is not None and str(evidence_change_id).strip().upper() != normalized_id:
        raise GateBlockedError(
            f"Recorded change_id in {evidence_path.name} is {evidence_change_id!r}; "
            f"expected {normalized_id!r}."
        )
    evidence_gate = evidence.get("gate")
    if evidence_gate is not None and str(evidence_gate).strip().lower() != normalized_gate:
        raise GateBlockedError(
            f"Recorded gate in {evidence_path.name} is {evidence_gate!r}; "
            f"expected {normalized_gate!r}."
        )
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
    normalized_id = change_id.strip().upper()
    state = load_state(normalized_id, runs_dir=runs_dir)
    if (
        state.get("status") == "CLARIFICATION_REQUIRED"
        or state.get("human_gates", {}).get("requirements") == "CLARIFICATION_REQUIRED"
    ):
        raise GateBlockedError(
            f"Change Request {normalized_id} requires Product Owner clarification before "
            f"planning or implementation can proceed."
        )
    return require_human_gate(
        normalized_id, "plan", allowed_decisions={"APPROVED"}, runs_dir=runs_dir
    )


def require_verification_passed(
    change_id: str,
    *,
    runs_dir: Path = RUNS_DIR,
    report_filename: str = "05-verification.txt",
) -> Path:
    """Block downstream review/release unless deterministic verification passed with evidence."""
    normalized_id = change_id.strip().upper()
    state = load_state(normalized_id, runs_dir=runs_dir)
    if state.get("status") == "VERIFICATION_FAILED" or state.get("verification") != "PASS":
        raise GateBlockedError(
            f"Deterministic verification has not passed for {normalized_id} "
            f"(status={state.get('status')!r}, verification={state.get('verification')!r})."
        )

    report_path = get_run_dir(normalized_id, runs_dir=runs_dir) / report_filename
    if not report_path.is_file():
        raise GateBlockedError(
            f"Missing deterministic verification artifact {report_filename!r} for {normalized_id}."
        )

    content = report_path.read_text(encoding="utf-8")
    if "OVERALL: FAIL" in content or "OVERALL: PASS" not in content:
        raise GateBlockedError(
            f"Verification artifact {report_filename!r} for {normalized_id} "
            f"does not confirm deterministic OVERALL: PASS."
        )

    return report_path
