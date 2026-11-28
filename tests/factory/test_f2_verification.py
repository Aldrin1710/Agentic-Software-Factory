from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory_app import agents, orchestrator, tools
from factory_app.gates import GateBlockedError, record_gate_decision, require_verification_passed
from factory_app.state import init_run, load_state, save_state
from factory_app.verification import format_verification_report, run_verification
from scripts import run_verification as run_verification_cli


def _prepare_approved_run(tmp_path: Path, change_id: str = "CR-001") -> Path:
    run_dir = init_run(change_id, runs_dir=tmp_path)
    (run_dir / "01-intake-report.md").write_text(
        "Clear requirements.\nRECOMMENDATION: PROCEED\n",
        encoding="utf-8",
    )
    (run_dir / "02-plan.md").write_text(
        "# Plan\n1. Update campusflow/service.py\n2. Add deterministic tests.\n",
        encoding="utf-8",
    )
    record_gate_decision(
        change_id,
        "plan",
        "APPROVED",
        "Plan validated by Tech Lead",
        runs_dir=tmp_path,
    )
    return run_dir


def _passing_verifier() -> dict:
    return {
        "ok": True,
        "results": [
            {
                "command": "python -m pytest -q",
                "returncode": 0,
                "stdout": "26 passed, 1 xfailed in 1.20s\n",
                "stderr": "",
            },
            {
                "command": "python -m ruff check campusflow tests factory_app scripts",
                "returncode": 0,
                "stdout": "All checks passed!\n",
                "stderr": "",
            },
        ],
    }


def _failing_verifier() -> dict:
    return {
        "ok": False,
        "results": [
            {
                "command": "python -m pytest -q",
                "returncode": 1,
                "stdout": "FAILED tests/product/test_service.py::test_waitlist_fifo\n",
                "stderr": "",
            },
            {
                "command": "python -m ruff check campusflow tests factory_app scripts",
                "returncode": 0,
                "stdout": "All checks passed!\n",
                "stderr": "",
            },
        ],
    }


def test_verification_pass_writes_05_report_runs_test_analyst_and_unlocks_review(tmp_path: Path):
    run_dir = _prepare_approved_run(tmp_path, "CR-001")
    called_agents: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        called_agents.append(agent.name)
        assert "OVERALL: PASS" in prompt
        assert "CR-001" in prompt
        return "- Add boundary test for cancellation when waitlist is empty."

    result = asyncio.run(
        run_verification(
            "CR-001",
            runs_dir=tmp_path,
            verifier=_passing_verifier,
            agent_runner=fake_runner,
            diff_provider=lambda: {"ok": True, "diff": "+def waitlist(): pass", "truncated": False},
        )
    )

    assert result["ok"] is True
    assert result["status"] == "INDEPENDENT_REVIEW"
    assert result["verification"] == "PASS"
    assert called_agents == ["TestAnalystAgent"]

    report_path = run_dir / "05-verification.txt"
    assert report_path.is_file()
    report_text = report_path.read_text(encoding="utf-8")
    assert "OVERALL: PASS" in report_text
    assert "26 passed" in report_text
    assert "TEST ANALYST REPORT" in report_text
    assert "waitlist is empty" in report_text

    state = load_state("CR-001", runs_dir=tmp_path)
    assert state["status"] == "INDEPENDENT_REVIEW"
    assert state["verification"] == "PASS"

    assert require_verification_passed("CR-001", runs_dir=tmp_path) == report_path


def test_verification_fail_blocks_flow_and_skips_llm_to_save_tokens(tmp_path: Path):
    run_dir = _prepare_approved_run(tmp_path, "CR-001")
    called_agents: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        called_agents.append(agent.name)
        return "Should not be invoked when deterministic checks fail."

    result = asyncio.run(
        run_verification(
            "CR-001",
            runs_dir=tmp_path,
            verifier=_failing_verifier,
            agent_runner=fake_runner,
        )
    )

    assert result["ok"] is False
    assert result["status"] == "VERIFICATION_FAILED"
    assert result["verification"] == "FAIL"
    assert result["test_analysis"] is None
    assert called_agents == []

    report_path = run_dir / "05-verification.txt"
    assert report_path.is_file()
    report_text = report_path.read_text(encoding="utf-8")
    assert "OVERALL: FAIL" in report_text
    assert "test_waitlist_fifo" in report_text

    state = load_state("CR-001", runs_dir=tmp_path)
    assert state["status"] == "VERIFICATION_FAILED"
    assert state["verification"] == "FAIL"

    with pytest.raises(GateBlockedError, match="verification has not passed"):
        require_verification_passed("CR-001", runs_dir=tmp_path)


def test_verification_deterministic_only_uses_zero_llm_calls(tmp_path: Path):
    _prepare_approved_run(tmp_path, "CR-003")
    called_agents: list[str] = []

    async def fake_runner(agent, prompt, initial_state):
        called_agents.append(agent.name)
        return "Should not run."

    result = asyncio.run(
        run_verification(
            "CR-003",
            runs_dir=tmp_path,
            verifier=_passing_verifier,
            agent_runner=fake_runner,
            run_test_analyst=False,
        )
    )

    assert result["ok"] is True
    assert result["test_analysis"] is None
    assert called_agents == []
    assert load_state("CR-003", runs_dir=tmp_path)["status"] == "INDEPENDENT_REVIEW"


def test_verification_blocked_without_approved_plan(tmp_path: Path):
    init_run("CR-001", runs_dir=tmp_path)
    verifier_called = False

    def spy_verifier() -> dict:
        nonlocal verifier_called
        verifier_called = True
        return _passing_verifier()

    with pytest.raises(GateBlockedError):
        asyncio.run(
            run_verification(
                "CR-001",
                runs_dir=tmp_path,
                verifier=spy_verifier,
                run_test_analyst=False,
            )
        )
    assert verifier_called is False


def test_require_verification_passed_enforces_state_and_evidence_integrity(tmp_path: Path):
    run_dir = _prepare_approved_run(tmp_path, "CR-001")

    # Forged PASS in state without 05-verification.txt must still be blocked.
    state = load_state("CR-001", runs_dir=tmp_path)
    state["verification"] = "PASS"
    state["status"] = "INDEPENDENT_REVIEW"
    save_state("CR-001", state, runs_dir=tmp_path)
    with pytest.raises(GateBlockedError, match="Missing deterministic verification artifact"):
        require_verification_passed("CR-001", runs_dir=tmp_path)

    # Forged PASS in state with FAIL content in 05-verification.txt must be blocked.
    (run_dir / "05-verification.txt").write_text(
        format_verification_report("CR-001", _failing_verifier()),
        encoding="utf-8",
    )
    with pytest.raises(GateBlockedError, match="OVERALL: PASS"):
        require_verification_passed("CR-001", runs_dir=tmp_path)


def test_run_baseline_verification_uses_active_python_executable(monkeypatch):
    seen_commands: list[list[str]] = []

    def fake_subprocess_run(command, **kwargs):
        seen_commands.append(command)
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(tools.subprocess, "run", fake_subprocess_run)
    result = tools.run_baseline_verification()

    assert result["ok"] is True
    assert len(seen_commands) == 2
    assert seen_commands[0][:3] == [sys.executable, "-m", "pytest"]
    assert seen_commands[1][:4] == [sys.executable, "-m", "ruff", "check"]


def test_test_analyst_agent_has_readonly_and_verification_tools_only():
    tool_names = {tool.__name__ for tool in agents.test_analyst_agent.tools}
    assert "list_repo_files" in tool_names
    assert "read_repo_file" in tool_names
    assert "run_baseline_verification" in tool_names
    assert "write_product_file" not in tool_names
    assert "edit_product_file" not in tool_names


def test_run_verification_cli_exits_nonzero_on_failure_and_zero_on_pass(tmp_path: Path, capsys):
    _prepare_approved_run(tmp_path, "CR-001")

    fail_code = run_verification_cli.main(
        ["CR-001", "--deterministic-only"],
        runs_dir=tmp_path,
        verifier=_failing_verifier,
    )
    assert fail_code == 1
    fail_payload = json.loads(capsys.readouterr().out)
    assert fail_payload["status"] == "VERIFICATION_FAILED"

    pass_code = run_verification_cli.main(
        ["CR-001", "--deterministic-only"],
        runs_dir=tmp_path,
        verifier=_passing_verifier,
    )
    assert pass_code == 0
    pass_payload = json.loads(capsys.readouterr().out)
    assert pass_payload["status"] == "INDEPENDENT_REVIEW"


def test_run_adk_agent_retries_on_rate_limit_429(monkeypatch):
    attempts = {"count": 0}

    async def flaky_once(agent, prompt, initial_state):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
        return "recovered response"

    monkeypatch.setenv("GEMINI_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("GEMINI_RETRY_BASE_DELAY", "0")
    monkeypatch.setattr(orchestrator, "_run_adk_agent_once", flaky_once)

    output = asyncio.run(orchestrator.run_adk_agent(agents.test_analyst_agent, "prompt", None))
    assert output == "recovered response"
    assert attempts["count"] == 3
