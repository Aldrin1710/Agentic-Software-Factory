from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from factory_app import agents, review, tools
from factory_app.gates import GateBlockedError, record_gate_decision
from factory_app.state import init_run, load_state, transition_state
from scripts import run_review as run_review_script


def _prepare_verified_run(
    tmp_path: Path,
    change_id: str = "CR-001",
    *,
    verification_ok: bool = True,
    verification_filename: str = "05-verification.txt",
) -> Path:
    run_dir = init_run(change_id, runs_dir=tmp_path)
    (run_dir / "02-plan.md").write_text("# Plan\nUpdate service.py and tests.\n", encoding="utf-8")
    record_gate_decision(
        change_id,
        "plan",
        "APPROVED",
        "Plan approved by Tech Lead",
        runs_dir=tmp_path,
    )
    verdict = "PASS" if verification_ok else "FAIL"
    (run_dir / verification_filename).write_text(
        f"=== DETERMINISTIC VERIFICATION ===\nCHANGE_ID: {change_id}\nOVERALL: {verdict}\n",
        encoding="utf-8",
    )
    transition_state(
        change_id,
        "INDEPENDENT_REVIEW" if verification_ok else "VERIFICATION_FAILED",
        extra_updates={"verification": verdict},
        runs_dir=tmp_path,
    )
    return run_dir


def test_parse_review_findings_counts_severities():
    report = (
        "# Independent Review\n"
        "[MUST FIX] Missing duplicate check in campusflow/service.py:27\n"
        "[SHOULD FIX] Add edge case test for empty user_id\n"
        "[OPTIONAL] Rename local variable\n"
    )
    parsed = review.parse_review_findings(report)
    assert parsed == {
        "must_fix": 1,
        "should_fix": 1,
        "optional": 1,
        "total": 3,
        "has_must_fix": True,
    }

    clean = review.parse_review_findings("# Independent Review\nNO FINDINGS\n")
    assert clean["total"] == 0
    assert clean["has_must_fix"] is False


def test_reviewer_agent_is_strictly_read_only():
    forbidden = {
        tools.write_product_file.__name__,
        tools.edit_product_file.__name__,
        tools.write_run_evidence.__name__,
    }
    reviewer_tools = {t.__name__ for t in agents.reviewer_agent.tools}
    assert not (reviewer_tools & forbidden)


def test_independent_review_blocked_when_verification_failed_or_missing(tmp_path: Path):
    _prepare_verified_run(tmp_path, "CR-001", verification_ok=False)

    async def should_not_run(agent, prompt, initial_state):
        raise AssertionError("ReviewerAgent must not run when verification failed")

    with pytest.raises(GateBlockedError):
        asyncio.run(
            review.run_independent_review(
                "CR-001",
                runs_dir=tmp_path,
                agent_runner=should_not_run,
            )
        )


def test_independent_review_isolates_context_from_coder_report(tmp_path: Path):
    run_dir = _prepare_verified_run(tmp_path, "CR-001", verification_ok=True)
    # Plant a Coder report with bogus self-approval claims; Reviewer must NOT receive it.
    (run_dir / "04-coder-report.md").write_text(
        "CODER_SELF_CLAIM_DO_NOT_LEAK_TO_REVIEWER: everything is 100% perfect!\n",
        encoding="utf-8",
    )

    captured: dict = {}

    async def fake_runner(agent, prompt, initial_state):
        captured["agent_name"] = agent.name
        captured["prompt"] = prompt
        captured["initial_state"] = initial_state
        return (
            "# Independent Review (CR-001)\n"
            "[MUST FIX] Waitlist FIFO order not enforced in campusflow/service.py.\n"
        )

    result = asyncio.run(
        review.run_independent_review(
            "CR-001",
            runs_dir=tmp_path,
            agent_runner=fake_runner,
            diff_provider=lambda: {"ok": True, "diff": "diff --git a/campusflow/service.py"},
        )
    )

    assert captured["agent_name"] == "ReviewerAgent"
    assert captured["initial_state"] is None
    assert "CODER_SELF_CLAIM_DO_NOT_LEAK_TO_REVIEWER" not in captured["prompt"]
    assert "diff --git a/campusflow/service.py" in captured["prompt"]
    assert "OVERALL: PASS" in captured["prompt"]

    assert result["status"] == "AWAITING_HUMAN_REVIEW"
    assert result["review"] == "CHANGES_REQUESTED"
    assert result["findings"]["must_fix"] == 1
    assert (run_dir / "06-review-report.md").is_file()

    state = load_state("CR-001", runs_dir=tmp_path)
    assert state["status"] == "AWAITING_HUMAN_REVIEW"
    assert state["review"] == "CHANGES_REQUESTED"


def test_independent_review_passes_cleanly_with_no_must_fix(tmp_path: Path):
    run_dir = _prepare_verified_run(tmp_path, "CR-003", verification_ok=True)

    async def fake_runner(agent, prompt, initial_state):
        return "# Independent Review (CR-003)\nNO FINDINGS\n"

    result = asyncio.run(
        review.run_independent_review(
            "CR-003",
            runs_dir=tmp_path,
            agent_runner=fake_runner,
            diff_provider=lambda: {"ok": True, "diff": ""},
        )
    )

    assert result["status"] == "AWAITING_HUMAN_REVIEW"
    assert result["review"] == "PASS"
    assert result["findings"]["has_must_fix"] is False
    assert (run_dir / "06-review-report.md").is_file()


def test_run_review_cli_supports_rereview_flag(tmp_path: Path):
    run_dir = _prepare_verified_run(
        tmp_path,
        "CR-001",
        verification_ok=True,
        verification_filename="10-reverification.txt",
    )

    async def fake_runner(agent, prompt, initial_state):
        assert "RE-REVIEW" in prompt
        return "# Re-Review (CR-001)\nNO FINDINGS\n"

    code = run_review_script.main(
        ["CR-001", "--rereview"],
        runs_dir=tmp_path,
        agent_runner=fake_runner,
        diff_provider=lambda: {"ok": True, "diff": ""},
    )
    assert code == 0
    assert (run_dir / "11-rereview.md").is_file()
