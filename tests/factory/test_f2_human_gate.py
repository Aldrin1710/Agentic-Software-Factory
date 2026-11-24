from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from factory_app.gates import (
    GATE_OWNERS,
    VALID_DECISIONS,
    GateBlockedError,
    record_gate_decision,
    require_plan_approved,
)
from factory_app.orchestrator import run_coder, run_planning
from factory_app.state import VALID_STATUSES, init_run, load_state, save_state
from scripts import approve_gate

EXPECTED_STATE_KEYS = {
    "change_id",
    "created_at",
    "updated_at",
    "status",
    "human_gates",
    "verification",
    "review",
    "security",
    "iterations",
}


def test_cr001_planning_waits_for_human_gate_and_approve_gate_records_03_decision(
    tmp_path: Path,
):
    planning_agents: list[str] = []

    async def fake_planning_runner(agent, prompt, initial_state):
        planning_agents.append(agent.name)
        if agent.name == "IntakeAgent":
            return "Clear waitlist requirements.\nRECOMMENDATION: PROCEED"
        return "# Plan\n1. Update campusflow/service.py\n2. Add waitlist tests."

    plan_result = asyncio.run(
        run_planning("CR-001", runs_dir=tmp_path, agent_runner=fake_planning_runner)
    )

    run_dir = tmp_path / "CR-001"
    decision_path = run_dir / "03-human-plan-decision.json"
    assert planning_agents == ["IntakeAgent", "PlannerAgent"]
    assert plan_result["status"] == "AWAITING_PLAN_APPROVAL"
    assert "CR-001" in (run_dir / "00-change-request.md").read_text(encoding="utf-8")
    assert (run_dir / "01-intake-report.md").is_file()
    assert (run_dir / "02-plan.md").is_file()
    assert not decision_path.exists()

    state_before = load_state("CR-001", runs_dir=tmp_path)
    assert state_before["status"] == "AWAITING_PLAN_APPROVAL"
    assert state_before["human_gates"]["plan"] == "PENDING"

    coder_calls: list[str] = []

    async def fake_coder_runner(agent, prompt, initial_state):
        coder_calls.append(agent.name)
        return "# Coder Report\nImplemented waitlist in campusflow/service.py."

    # Coder must remain blocked until explicit human approval is recorded.
    with pytest.raises(GateBlockedError):
        asyncio.run(run_coder("CR-001", runs_dir=tmp_path, agent_runner=fake_coder_runner))
    assert coder_calls == []

    # Record explicit approval through scripts/approve_gate.py.
    recorded_path = approve_gate.main(
        ["CR-001", "plan", "APPROVED", "Plan validado por Tech Lead"],
        runs_dir=tmp_path,
    )
    assert recorded_path == decision_path
    assert decision_path.is_file()
    assert not (run_dir / "human-plan-decision.json").exists()

    evidence = json.loads(decision_path.read_text(encoding="utf-8"))
    assert evidence["change_id"] == "CR-001"
    assert evidence["gate"] == "plan"
    assert evidence["decision"] == "APPROVED"
    assert evidence["reason"] == "Plan validado por Tech Lead"
    assert evidence["recorded_by"] == GATE_OWNERS["plan"]
    assert evidence["recorded_at"]

    state_after = load_state("CR-001", runs_dir=tmp_path)
    assert EXPECTED_STATE_KEYS.issubset(state_after.keys())
    assert state_after["status"] == "IMPLEMENTING"
    assert state_after["status"] in VALID_STATUSES
    assert state_after["human_gates"]["plan"] == "APPROVED"

    verified = require_plan_approved("CR-001", runs_dir=tmp_path)
    assert verified["decision"] == "APPROVED"

    # run_coder must work seamlessly after approve_gate transitioned status to IMPLEMENTING.
    coder_result = asyncio.run(
        run_coder("CR-001", runs_dir=tmp_path, agent_runner=fake_coder_runner)
    )
    assert coder_calls == ["CoderAgent"]
    assert coder_result["status"] == "VERIFICATION_RUNNING"
    assert (run_dir / "04-coder-report.md").is_file()
    assert load_state("CR-001", runs_dir=tmp_path)["status"] == "VERIFICATION_RUNNING"


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [
        ("REJECTED", "PLAN_REJECTED"),
        ("MODIFIED", "AWAITING_PLAN_APPROVAL"),
        ("DEFERRED", "AWAITING_PLAN_APPROVAL"),
    ],
)
def test_cr001_non_approved_plan_decisions_record_03_file_and_block_coder(
    tmp_path: Path, decision: str, expected_status: str
):
    async def fake_planning_runner(agent, prompt, initial_state):
        if agent.name == "IntakeAgent":
            return "Clear waitlist requirements.\nRECOMMENDATION: PROCEED"
        return "# Plan\n1. Update campusflow/service.py."

    asyncio.run(run_planning("CR-001", runs_dir=tmp_path, agent_runner=fake_planning_runner))

    approve_gate.main(
        ["CR-001", "plan", decision, f"Tech Lead marked plan as {decision}"],
        runs_dir=tmp_path,
    )

    decision_path = tmp_path / "CR-001" / "03-human-plan-decision.json"
    assert decision_path.is_file()
    evidence = json.loads(decision_path.read_text(encoding="utf-8"))
    assert evidence["decision"] == decision

    state = load_state("CR-001", runs_dir=tmp_path)
    assert state["status"] == expected_status
    assert state["status"] in VALID_STATUSES
    assert state["human_gates"]["plan"] == decision

    coder_calls: list[str] = []

    async def fake_coder_runner(agent, prompt, initial_state):
        coder_calls.append(agent.name)
        return "# Coder Report"

    with pytest.raises(GateBlockedError):
        require_plan_approved("CR-001", runs_dir=tmp_path)
    with pytest.raises(GateBlockedError):
        asyncio.run(run_coder("CR-001", runs_dir=tmp_path, agent_runner=fake_coder_runner))
    assert coder_calls == []


def test_cr005_ambiguous_stops_planning_and_blocks_coder_and_plan_approval(tmp_path: Path):
    planning_agents: list[str] = []

    async def fake_ambiguous_runner(agent, prompt, initial_state):
        planning_agents.append(agent.name)
        return (
            "Priority criteria and popularity threshold are undefined.\n"
            "RECOMMENDATION: HUMAN_CLARIFICATION_REQUIRED"
        )

    result = asyncio.run(
        run_planning("CR-005", runs_dir=tmp_path, agent_runner=fake_ambiguous_runner)
    )

    run_dir = tmp_path / "CR-005"
    assert planning_agents == ["IntakeAgent"]
    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["plan_report"] is None
    assert "CR-005" in (run_dir / "00-change-request.md").read_text(encoding="utf-8")
    assert (run_dir / "01-intake-report.md").is_file()
    assert not (run_dir / "02-plan.md").exists()
    assert not (run_dir / "03-human-plan-decision.json").exists()

    state = load_state("CR-005", runs_dir=tmp_path)
    assert state["status"] == "CLARIFICATION_REQUIRED"
    assert state["human_gates"]["requirements"] == "CLARIFICATION_REQUIRED"
    assert state["human_gates"]["plan"] == "PENDING"

    coder_calls: list[str] = []

    async def fake_coder_runner(agent, prompt, initial_state):
        coder_calls.append(agent.name)
        return "# Coder Report"

    with pytest.raises(GateBlockedError):
        require_plan_approved("CR-005", runs_dir=tmp_path)
    with pytest.raises(GateBlockedError):
        asyncio.run(run_coder("CR-005", runs_dir=tmp_path, agent_runner=fake_coder_runner))
    assert coder_calls == []

    # Attempting to approve the plan gate while CR-005 requires clarification must fail.
    with pytest.raises(SystemExit, match="BLOCKED BY HUMAN GATE"):
        approve_gate.main(
            ["CR-005", "plan", "APPROVED", "Attempting to bypass ambiguity"],
            runs_dir=tmp_path,
        )
    assert not (run_dir / "03-human-plan-decision.json").exists()
    assert load_state("CR-005", runs_dir=tmp_path)["human_gates"]["plan"] == "PENDING"

    # Defense-in-depth: even if state/decision were manually forged, CLARIFICATION_REQUIRED blocks.
    forged_state = load_state("CR-005", runs_dir=tmp_path)
    forged_state["human_gates"]["plan"] = "APPROVED"
    save_state("CR-005", forged_state, runs_dir=tmp_path)
    (run_dir / "03-human-plan-decision.json").write_text(
        json.dumps(
            {
                "change_id": "CR-005",
                "gate": "plan",
                "decision": "APPROVED",
                "reason": "Forged approval",
                "recorded_by": GATE_OWNERS["plan"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GateBlockedError, match="clarification"):
        require_plan_approved("CR-005", runs_dir=tmp_path)


@pytest.mark.parametrize("decision", sorted(VALID_DECISIONS))
def test_plan_gate_requires_plan_artifact_for_any_decision(tmp_path: Path, decision: str):
    run_dir = init_run("CR-001", runs_dir=tmp_path)
    assert not (run_dir / "02-plan.md").exists()

    with pytest.raises(GateBlockedError, match="02-plan.md"):
        record_gate_decision(
            "CR-001",
            "plan",
            decision,
            "Decision without generated plan",
            runs_dir=tmp_path,
        )

    assert not (run_dir / "03-human-plan-decision.json").exists()
    state = load_state("CR-001", runs_dir=tmp_path)
    assert state["status"] == "INTAKE_PENDING"
    assert state["human_gates"]["plan"] == "PENDING"


def test_approve_gate_cli_validation_and_evidence_integrity(tmp_path: Path):
    with pytest.raises(SystemExit, match="Usage:"):
        approve_gate.main(["CR-001", "plan", "APPROVED"], runs_dir=tmp_path)

    with pytest.raises(SystemExit, match="Invalid gate"):
        approve_gate.main(["CR-001", "unknown", "APPROVED", "reason"], runs_dir=tmp_path)

    with pytest.raises(SystemExit, match="Invalid decision"):
        approve_gate.main(["CR-001", "plan", "MAYBE", "reason"], runs_dir=tmp_path)

    with pytest.raises(SystemExit, match="requires a reason"):
        approve_gate.main(["CR-001", "plan", "APPROVED", "   "], runs_dir=tmp_path)

    with pytest.raises(SystemExit, match="Run not found"):
        approve_gate.main(["CR-001", "plan", "APPROVED", "reason"], runs_dir=tmp_path)

    # Verify mismatched change_id / gate inside 03-human-plan-decision.json is rejected.
    run_dir = init_run("CR-001", runs_dir=tmp_path)
    (run_dir / "02-plan.md").write_text("# Plan\nUpdate service.", encoding="utf-8")
    approve_gate.main(["CR-001", "plan", "APPROVED", "Valid reason"], runs_dir=tmp_path)

    decision_path = run_dir / "03-human-plan-decision.json"
    tampered = json.loads(decision_path.read_text(encoding="utf-8"))
    tampered["change_id"] = "CR-002"
    decision_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(GateBlockedError, match="change_id"):
        require_plan_approved("CR-001", runs_dir=tmp_path)
