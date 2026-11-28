from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import test_analyst_agent
from .gates import require_plan_approved
from .orchestrator import AgentRunnerFn, run_adk_agent
from .state import RUNS_DIR, get_run_dir, transition_state
from .tools import get_git_diff, run_baseline_verification

VerifierFn = Callable[[], dict[str, Any]]
DiffProviderFn = Callable[[], dict[str, Any]]

MAX_DIFF_CHARS_FOR_PROMPT = 10_000


def format_verification_report(
    change_id: str,
    verification_result: dict[str, Any],
    *,
    label: str = "INITIAL VERIFICATION",
) -> str:
    """Render deterministic verification command outputs into an auditable text artifact."""
    normalized = change_id.strip().upper()
    passed = bool(verification_result.get("ok", False))
    verdict = "PASS" if passed else "FAIL"
    timestamp = datetime.now(timezone.utc).isoformat()

    lines = [
        f"=== DETERMINISTIC VERIFICATION ({label}) ===",
        f"CHANGE_ID: {normalized}",
        f"TIMESTAMP_UTC: {timestamp}",
        f"OVERALL: {verdict}",
        "",
    ]

    for index, entry in enumerate(verification_result.get("results", []), start=1):
        command = entry.get("command", "<unknown>")
        returncode = entry.get("returncode", -1)
        stdout = (entry.get("stdout") or "").rstrip()
        stderr = (entry.get("stderr") or "").rstrip()
        step_status = "PASS" if returncode == 0 else "FAIL"
        lines.append(f"--- CHECK {index}: {step_status} (exit={returncode}) ---")
        lines.append(f"$ {command}")
        if stdout:
            lines.append("STDOUT:")
            lines.append(stdout)
        if stderr:
            lines.append("STDERR:")
            lines.append(stderr)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_test_analyst_prompt(
    change_id: str,
    cr_text: str,
    deterministic_report: str,
    diff_text: str,
) -> str:
    bounded_diff = diff_text.strip() or "(no uncommitted git diff in campusflow/ or tests/)"
    if len(bounded_diff) > MAX_DIFF_CHARS_FOR_PROMPT:
        bounded_diff = bounded_diff[:MAX_DIFF_CHARS_FOR_PROMPT] + "\n...[TRUNCATED]..."
    return (
        f"Analyze test coverage and test quality for Change Request {change_id}. "
        f"Deterministic verification ALREADY ran and its exact output is provided below; "
        f"do NOT re-run `run_baseline_verification` and only call `read_repo_file` if you "
        f"genuinely need another file. Keep your report concise.\n\n"
        f"Report:\n"
        f"1. Missing negative, boundary, or regression test cases.\n"
        f"2. Any weakened assertions or false-confidence tests.\n"
        f"3. Exact file references for any gaps found.\n\n"
        f"Change Request ({change_id}):\n{cr_text}\n\n"
        f"Git Diff (bounded):\n{bounded_diff}\n\n"
        f"Deterministic Verification Output:\n{deterministic_report}"
    )


async def run_verification(
    change_id: str,
    *,
    runs_dir: Path = RUNS_DIR,
    verifier: VerifierFn = run_baseline_verification,
    agent_runner: AgentRunnerFn = run_adk_agent,
    diff_provider: DiffProviderFn = get_git_diff,
    run_test_analyst: bool = True,
    output_filename: str = "05-verification.txt",
) -> dict[str, Any]:
    """Run blocking deterministic verification (pytest + ruff) and optional TestAnalystAgent.

    Deterministic failures immediately block the workflow (status=VERIFICATION_FAILED) and skip
    LLM execution to avoid unnecessary Gemini API token usage.
    """
    normalized = change_id.strip().upper()
    require_plan_approved(normalized, runs_dir=runs_dir)

    run_dir = get_run_dir(normalized, runs_dir=runs_dir)
    cr_path = run_dir / "00-change-request.md"
    cr_text = cr_path.read_text(encoding="utf-8") if cr_path.is_file() else ""

    transition_state(normalized, "VERIFICATION_RUNNING", runs_dir=runs_dir)

    verification_result = verifier()
    passed = bool(verification_result.get("ok", False))
    report_text = format_verification_report(
        normalized,
        verification_result,
        label="INITIAL VERIFICATION",
    )

    report_path = run_dir / output_filename
    report_path.write_text(report_text, encoding="utf-8")

    if not passed:
        state = transition_state(
            normalized,
            "VERIFICATION_FAILED",
            extra_updates={"verification": "FAIL"},
            runs_dir=runs_dir,
        )
        return {
            "change_id": normalized,
            "ok": False,
            "status": state["status"],
            "verification": "FAIL",
            "verification_report": str(report_path),
            "test_analysis": None,
            "results": verification_result.get("results", []),
        }

    test_analysis: str | None = None
    if run_test_analyst:
        diff_info = diff_provider()
        diff_text = diff_info.get("diff", "") if isinstance(diff_info, dict) else ""
        prompt = _build_test_analyst_prompt(normalized, cr_text, report_text, diff_text)
        test_analysis = await agent_runner(test_analyst_agent, prompt, None)
        advisory_block = (
            "\n=== TEST ANALYST REPORT (ADVISORY — NOT VERIFICATION) ===\n"
            f"{test_analysis.strip()}\n"
        )
        report_path.write_text(report_text + advisory_block, encoding="utf-8")

    state = transition_state(
        normalized,
        "INDEPENDENT_REVIEW",
        extra_updates={"verification": "PASS"},
        runs_dir=runs_dir,
    )
    return {
        "change_id": normalized,
        "ok": True,
        "status": state["status"],
        "verification": "PASS",
        "verification_report": str(report_path),
        "test_analysis": test_analysis,
        "results": verification_result.get("results", []),
    }
