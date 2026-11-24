from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CR_DIR = REPO_ROOT / "change_requests"
RUNS_DIR = REPO_ROOT / "runs"

VALID_STATUSES = {
    "INTAKE_PENDING",
    "CLARIFICATION_REQUIRED",
    "PLANNING",
    "AWAITING_PLAN_APPROVAL",
    "PLAN_REJECTED",
    "IMPLEMENTING",
    "VERIFICATION_RUNNING",
    "VERIFICATION_FAILED",
    "INDEPENDENT_REVIEW",
    "AWAITING_HUMAN_REVIEW",
    "FIX_IN_PROGRESS",
    "EVIDENCE_PACKAGING",
    "AWAITING_RELEASE_GATE",
    "RELEASE_BLOCKED",
    "READY_FOR_HUMAN_MERGE",
}


def find_change_request(change_id: str, cr_dir: Path = CR_DIR) -> Path:
    normalized = change_id.strip().upper()
    matches = sorted(cr_dir.glob(f"{normalized}-*.md"))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"Expected one Change Request for {normalized}; found {len(matches)}"
        )
    return matches[0]


def get_run_dir(change_id: str, runs_dir: Path = RUNS_DIR) -> Path:
    normalized = change_id.strip().upper()
    if not normalized.startswith("CR-"):
        raise ValueError("change_id must start with 'CR-'")
    return runs_dir / normalized


def init_run(change_id: str, *, runs_dir: Path = RUNS_DIR, cr_dir: Path = CR_DIR) -> Path:
    """Initialize runs/<CR-ID>/ idempotently with 00-change-request.md and factory-state.json."""
    normalized = change_id.strip().upper()
    run_dir = get_run_dir(normalized, runs_dir=runs_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    cr_dest = run_dir / "00-change-request.md"
    if not cr_dest.exists():
        source = find_change_request(normalized, cr_dir=cr_dir)
        shutil.copy2(source, cr_dest)

    state_path = run_dir / "factory-state.json"
    if not state_path.exists():
        state = {
            "change_id": normalized,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "INTAKE_PENDING",
            "human_gates": {
                "requirements": "PENDING",
                "plan": "PENDING",
                "review": "PENDING",
                "release": "PENDING",
            },
            "verification": "NOT_RUN",
            "review": "NOT_RUN",
            "security": "NOT_RUN",
            "iterations": 0,
        }
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    return run_dir


def load_state(change_id: str, *, runs_dir: Path = RUNS_DIR) -> dict:
    run_dir = get_run_dir(change_id, runs_dir=runs_dir)
    state_path = run_dir / "factory-state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"Run state not found for {change_id}: {state_path}")
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(change_id: str, state: dict, *, runs_dir: Path = RUNS_DIR) -> Path:
    run_dir = get_run_dir(change_id, runs_dir=runs_dir)
    state_path = run_dir / "factory-state.json"
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state_path


def transition_state(
    change_id: str,
    new_status: str,
    *,
    gate_updates: dict[str, str] | None = None,
    extra_updates: dict | None = None,
    runs_dir: Path = RUNS_DIR,
) -> dict:
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid factory status: {new_status!r}")
    state = load_state(change_id, runs_dir=runs_dir)
    state["status"] = new_status
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    if gate_updates:
        state.setdefault("human_gates", {}).update(gate_updates)
    if extra_updates:
        state.update(extra_updates)
    save_state(change_id, state, runs_dir=runs_dir)
    return state
