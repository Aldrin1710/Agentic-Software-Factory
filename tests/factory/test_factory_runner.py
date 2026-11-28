from __future__ import annotations

import asyncio
from pathlib import Path

from factory_app.gates import record_gate_decision
from factory_app.orchestrator import run_factory
from factory_app.state import load_state
from scripts import run_factory as run_factory_script


def _passing_verifier() -> dict:
    return {
        "ok": True,
        "results": [
            {"command": "python -m pytest -q", "returncode": 0, "stdout": "10 passed", "stderr": ""}
        ],
    }


def _failing_verifier() -> dict:
    return {
        "ok": False,
        "results": [
            {"command": "python -m pytest -q", "returncode": 1, "stdout": "1 failed", "stderr": ""}
        ],
    }


def test_factory_pauses_on_ambiguous_cr_without_calling_planner_or_coder(tmp_path: Path):
    called: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        called.append(agent.name)
        return "Priority is undefined.\nRECOMMENDATION: HUMAN_CLARIFICATION_REQUIRED"

    result = asyncio.run(
        run_factory(
            "CR-005",
            runs_dir=tmp_path,
            agent_runner=fake_runner,
            verifier=_passing_verifier,
        )
    )

    assert called == ["IntakeAgent"]
    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["paused"] is True
    assert result["waiting_gate"] == "requirements"


def test_factory_pause_and_resume_through_independent_review(tmp_path: Path):
    called: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        called.append(agent.name)
        if agent.name == "IntakeAgent":
            return "Clear CR.\nRECOMMENDATION: PROCEED"
        if agent.name == "PlannerAgent":
            return "# Plan\nUpdate service.py and tests."
        if agent.name == "CoderAgent":
            return "# Coder Report\nUpdated service.py."
        if agent.name == "TestAnalystAgent":
            return "Coverage looks solid."
        if agent.name == "ReviewerAgent":
            return "# Review\nNO FINDINGS"
        raise AssertionError(f"Unexpected agent: {agent.name}")

    res1 = asyncio.run(
        run_factory(
            "CR-001",
            runs_dir=tmp_path,
            agent_runner=fake_runner,
            verifier=_passing_verifier,
            diff_provider=lambda: {"ok": True, "diff": "diff --git a/campusflow/service.py"},
        )
    )
    assert called == ["IntakeAgent", "PlannerAgent"]
    assert res1["status"] == "AWAITING_PLAN_APPROVAL"
    assert res1["paused"] is True
    assert res1["waiting_gate"] == "plan"

    record_gate_decision(
        "CR-001",
        "plan",
        "APPROVED",
        "Approved by Tech Lead",
        runs_dir=tmp_path,
    )

    res2 = asyncio.run(
        run_factory(
            "CR-001",
            runs_dir=tmp_path,
            agent_runner=fake_runner,
            verifier=_passing_verifier,
            diff_provider=lambda: {"ok": True, "diff": "diff --git a/campusflow/service.py"},
        )
    )
    assert called == [
        "IntakeAgent",
        "PlannerAgent",
        "CoderAgent",
        "TestAnalystAgent",
        "ReviewerAgent",
    ]
    assert res2["status"] == "AWAITING_HUMAN_REVIEW"
    assert res2["paused"] is True
    assert res2["waiting_gate"] == "review"
    assert (tmp_path / "CR-001" / "06-review-report.md").is_file()


def test_factory_blocks_review_when_deterministic_verification_fails(tmp_path: Path):
    called: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        called.append(agent.name)
        if agent.name == "IntakeAgent":
            return "RECOMMENDATION: PROCEED"
        if agent.name == "PlannerAgent":
            return "# Plan\nUpdate service."
        if agent.name == "CoderAgent":
            return "# Coder Report"
        raise AssertionError(f"Should not run {agent.name} after verification failure")

    asyncio.run(run_factory("CR-001", runs_dir=tmp_path, agent_runner=fake_runner))
    record_gate_decision("CR-001", "plan", "APPROVED", "OK", runs_dir=tmp_path)

    res = asyncio.run(
        run_factory(
            "CR-001",
            runs_dir=tmp_path,
            agent_runner=fake_runner,
            verifier=_failing_verifier,
        )
    )
    assert res["status"] == "VERIFICATION_FAILED"
    assert called == ["IntakeAgent", "PlannerAgent", "CoderAgent"]
    assert not (tmp_path / "CR-001" / "06-review-report.md").exists()


def test_factory_interactive_mode_approves_plan_inline_and_continues(tmp_path: Path):
    prompts_seen: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        if agent.name == "IntakeAgent":
            return "RECOMMENDATION: PROCEED"
        if agent.name == "PlannerAgent":
            return "# Plan\nUpdate service."
        if agent.name == "CoderAgent":
            return "# Coder Report"
        if agent.name == "TestAnalystAgent":
            return "No test gaps."
        if agent.name == "ReviewerAgent":
            return "# Review\nNO FINDINGS"
        return ""

    def fake_prompt(change_id: str, gate: str, artifact: str) -> tuple[str, str]:
        prompts_seen.append(gate)
        if gate == "plan":
            return "APPROVED", "Tech Lead approved inline"
        return "PAUSE", ""

    code = run_factory_script.main(
        ["CR-001", "--interactive"],
        runs_dir=tmp_path,
        agent_runner=fake_runner,
        verifier=_passing_verifier,
        diff_provider=lambda: {"ok": True, "diff": ""},
        prompt_fn=fake_prompt,
    )

    assert code == 0
    assert prompts_seen == ["plan", "review"]
    assert (tmp_path / "CR-001" / "03-human-plan-decision.json").is_file()
    assert (tmp_path / "CR-001" / "06-review-report.md").is_file()
    assert load_state("CR-001", runs_dir=tmp_path)["status"] == "AWAITING_HUMAN_REVIEW"
