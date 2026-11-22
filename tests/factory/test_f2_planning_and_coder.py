from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from factory_app import agents, tools
from factory_app.gates import GateBlockedError, require_plan_approved
from factory_app.orchestrator import parse_intake_recommendation, run_coder, run_planning
from factory_app.state import init_run, load_state, transition_state


def test_parse_intake_recommendation_defaults_safely():
    assert parse_intake_recommendation("Summary\nRECOMMENDATION: PROCEED") == "PROCEED"
    assert (
        parse_intake_recommendation("Ambiguous\nRECOMMENDATION: HUMAN_CLARIFICATION_REQUIRED")
        == "HUMAN_CLARIFICATION_REQUIRED"
    )
    assert parse_intake_recommendation("No explicit verdict") == "HUMAN_CLARIFICATION_REQUIRED"


def test_planning_stops_on_ambiguous_change_request(tmp_path: Path):
    called_agents: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        called_agents.append(agent.name)
        return "Ambiguous priority rules.\nRECOMMENDATION: HUMAN_CLARIFICATION_REQUIRED"

    result = asyncio.run(run_planning("CR-005", runs_dir=tmp_path, agent_runner=fake_runner))

    assert called_agents == ["IntakeAgent"]
    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["plan_report"] is None
    assert (tmp_path / "CR-005" / "01-intake-report.md").is_file()
    assert not (tmp_path / "CR-005" / "02-plan.md").exists()
    state = load_state("CR-005", runs_dir=tmp_path)
    assert state["status"] == "CLARIFICATION_REQUIRED"
    assert state["human_gates"]["requirements"] == "CLARIFICATION_REQUIRED"


def test_planning_produces_intake_and_plan_and_waits_for_human_gate(tmp_path: Path):
    called_agents: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        called_agents.append(agent.name)
        if agent.name == "IntakeAgent":
            return "Clear waitlist CR.\nRECOMMENDATION: PROCEED"
        assert initial_state and "intake_report" in initial_state
        return "# Implementation Plan\n1. Update campusflow/service.py\n2. Add tests."

    result = asyncio.run(run_planning("CR-001", runs_dir=tmp_path, agent_runner=fake_runner))

    assert called_agents == ["IntakeAgent", "PlannerAgent"]
    assert result["status"] == "AWAITING_PLAN_APPROVAL"
    assert (tmp_path / "CR-001" / "01-intake-report.md").is_file()
    assert (tmp_path / "CR-001" / "02-plan.md").is_file()
    state = load_state("CR-001", runs_dir=tmp_path)
    assert state["status"] == "AWAITING_PLAN_APPROVAL"
    assert state["human_gates"]["plan"] == "PENDING"


def test_coder_write_tools_enforce_strict_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tools, "REPO_ROOT", tmp_path.resolve())

    # Allowed paths
    res_prod = tools.write_product_file("campusflow/sample.py", "VALUE = 1\n")
    assert res_prod["ok"] is True
    assert (tmp_path / "campusflow" / "sample.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    res_edit = tools.edit_product_file("campusflow/sample.py", "VALUE = 1", "VALUE = 2")
    assert res_edit["ok"] is True
    assert (tmp_path / "campusflow" / "sample.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    res_test = tools.write_product_file("tests/product/test_sample.py", "def test_ok(): pass\n")
    assert res_test["ok"] is True

    # Forbidden paths
    blocked_paths = [
        "docs/REQUIREMENTS.md",
        "docs/SPEC.md",
        "change_requests/CR-001-waitlist.md",
        "factory_app/agents.py",
        "scripts/verify_repo.py",
        "tests/factory/test_contracts.py",
        "tests/eval/eval_config.yaml",
        ".env",
        "../escape.py",
    ]
    for forbidden in blocked_paths:
        res = tools.write_product_file(forbidden, "hacked")
        assert res["ok"] is False, f"Expected write to be blocked for {forbidden}"
        res_ed = tools.edit_product_file(forbidden, "a", "b")
        assert res_ed["ok"] is False, f"Expected edit to be blocked for {forbidden}"


def test_only_coder_agent_has_product_write_tools():
    write_tool_names = {tools.write_product_file.__name__, tools.edit_product_file.__name__}
    coder_tools = {t.__name__ for t in agents.coder_agent.tools}
    assert write_tool_names.issubset(coder_tools)

    readonly_agents = [
        agents.intake_agent,
        agents.planner_agent,
        agents.test_analyst_agent,
        agents.reviewer_agent,
        agents.security_agent,
        agents.evidence_agent,
    ]
    for agent in readonly_agents:
        agent_tool_names = {t.__name__ for t in agent.tools}
        assert not (agent_tool_names & write_tool_names), (
            f"{agent.name} must not have product write tools"
        )


def test_coder_blocked_until_human_plan_gate_approved(tmp_path: Path):
    run_dir = init_run("CR-001", runs_dir=tmp_path)
    (run_dir / "02-plan.md").write_text("# Plan\nUpdate service.", encoding="utf-8")

    # PENDING -> blocked
    with pytest.raises(GateBlockedError):
        require_plan_approved("CR-001", runs_dir=tmp_path)

    async def fake_coder(agent, prompt, initial_state):
        return "# Coder Report\nUpdated campusflow/service.py"

    with pytest.raises(GateBlockedError):
        asyncio.run(run_coder("CR-001", runs_dir=tmp_path, agent_runner=fake_coder))

    # APPROVED in state without decision JSON -> still blocked
    transition_state(
        "CR-001",
        "AWAITING_PLAN_APPROVAL",
        gate_updates={"plan": "APPROVED"},
        runs_dir=tmp_path,
    )
    with pytest.raises(GateBlockedError):
        asyncio.run(run_coder("CR-001", runs_dir=tmp_path, agent_runner=fake_coder))

    # APPROVED in state + recorded decision JSON -> allowed
    decision = {
        "change_id": "CR-001",
        "gate": "plan",
        "decision": "APPROVED",
        "reason": "Architecture verified by Tech Lead",
        "recorded_by": "Aldrin Enrique Novelo Góngora (Tech Lead)",
    }
    (run_dir / "03-human-plan-decision.json").write_text(json.dumps(decision), encoding="utf-8")

    result = asyncio.run(run_coder("CR-001", runs_dir=tmp_path, agent_runner=fake_coder))
    assert result["status"] == "VERIFICATION_RUNNING"
    assert (run_dir / "04-coder-report.md").is_file()
    assert load_state("CR-001", runs_dir=tmp_path)["status"] == "VERIFICATION_RUNNING"
