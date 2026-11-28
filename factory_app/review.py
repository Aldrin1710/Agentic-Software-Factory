from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agents import reviewer_agent
from .gates import require_plan_approved, require_verification_passed
from .orchestrator import AgentRunnerFn, _load_repo_context, run_adk_agent
from .state import RUNS_DIR, get_run_dir, transition_state
from .tools import edit_product_file, get_git_diff, write_product_file, write_run_evidence

DiffProviderFn = Callable[[], dict[str, Any]]

REVIEW_CONTEXT_DOCS = (
    "docs/REQUIREMENTS.md",
    "docs/SPEC.md",
    "docs/ARCHITECTURE.md",
)
MAX_DIFF_CHARS_FOR_REVIEW = 12_000
FORBIDDEN_REVIEWER_TOOLS = {
    write_product_file.__name__,
    edit_product_file.__name__,
    write_run_evidence.__name__,
}


def _assert_reviewer_is_read_only() -> None:
    """Enforce least privilege: ReviewerAgent must remain strictly read-only."""
    tool_names = {getattr(tool, "__name__", str(tool)) for tool in reviewer_agent.tools}
    forbidden = sorted(tool_names & FORBIDDEN_REVIEWER_TOOLS)
    if forbidden:
        raise RuntimeError(
            f"ReviewerAgent must be read-only; forbidden tools detected: {forbidden}"
        )


def parse_review_findings(report: str) -> dict[str, Any]:
    """Count severity-tagged findings in a review report deterministically."""
    must_fix = len(re.findall(r"(?im)(?:^|\[\s*)MUST\s+FIX\b", report))
    should_fix = len(re.findall(r"(?im)(?:^|\[\s*)SHOULD\s+FIX\b", report))
    optional = len(re.findall(r"(?im)(?:^|\[\s*)OPTIONAL\b", report))
    return {
        "must_fix": must_fix,
        "should_fix": should_fix,
        "optional": optional,
        "total": must_fix + should_fix + optional,
        "has_must_fix": must_fix > 0,
    }


def build_independent_review_prompt(
    change_id: str,
    cr_text: str,
    repo_context: str,
    diff_text: str,
    verification_text: str,
    *,
    is_rereview: bool = False,
) -> str:
    """Build an isolated review prompt using only primary artifacts (never Coder claims)."""
    bounded_diff = diff_text.strip() or "(no uncommitted git diff in campusflow/ or tests/)"
    if len(bounded_diff) > MAX_DIFF_CHARS_FOR_REVIEW:
        bounded_diff = bounded_diff[:MAX_DIFF_CHARS_FOR_REVIEW] + "\n...[TRUNCATED]..."

    mode_label = "RE-REVIEW" if is_rereview else "FIRST-PASS INDEPENDENT REVIEW"
    return (
        f"Perform a {mode_label} for Change Request {change_id} in strict read-only mode.\n"
        f"You are independent from the Coder: do NOT assume any unverified claims and inspect "
        f"only primary evidence (requirements, spec, architecture, diff, tests, and deterministic "
        f"verification output). Only call `read_repo_file` if you need to inspect a specific "
        f"file not included below.\n\n"
        f"Check explicitly:\n"
        f"1. Compliance with Change Request acceptance criteria and docs/REQUIREMENTS.md.\n"
        f"2. Architecture boundaries (API -> Service -> Repository; business rules in Service).\n"
        f"3. Real APIs/dependencies (flag any invented or non-existent API/method).\n"
        f"4. Test quality (flag regressions, missing negative/boundary tests, or weakened "
        f"tests).\n\n"
        f"Format every finding starting with `[MUST FIX]`, `[SHOULD FIX]`, or `[OPTIONAL]` "
        f"followed by exact file/evidence citations. If none exist, write `NO FINDINGS`.\n\n"
        f"Change Request ({change_id}):\n{cr_text}\n\n"
        f"Repository Reference Docs:\n{repo_context}\n\n"
        f"Git Diff (bounded):\n{bounded_diff}\n\n"
        f"Deterministic Verification Evidence:\n{verification_text}"
    )


async def run_independent_review(
    change_id: str,
    *,
    runs_dir: Path = RUNS_DIR,
    agent_runner: AgentRunnerFn = run_adk_agent,
    diff_provider: DiffProviderFn = get_git_diff,
    verification_filename: str = "05-verification.txt",
    output_filename: str = "06-review-report.md",
    transition_to_human_review: bool = True,
    is_rereview: bool = False,
) -> dict[str, Any]:
    """Run read-only ReviewerAgent in an isolated session after deterministic checks pass."""
    _assert_reviewer_is_read_only()

    normalized = change_id.strip().upper()
    require_plan_approved(normalized, runs_dir=runs_dir)
    verification_path = require_verification_passed(
        normalized,
        runs_dir=runs_dir,
        report_filename=verification_filename,
    )

    run_dir = get_run_dir(normalized, runs_dir=runs_dir)
    cr_path = run_dir / "00-change-request.md"
    cr_text = cr_path.read_text(encoding="utf-8") if cr_path.is_file() else ""
    verification_text = verification_path.read_text(encoding="utf-8")
    repo_context = _load_repo_context(REVIEW_CONTEXT_DOCS)

    diff_info = diff_provider()
    diff_text = diff_info.get("diff", "") if isinstance(diff_info, dict) else ""

    prompt = build_independent_review_prompt(
        normalized,
        cr_text,
        repo_context,
        diff_text,
        verification_text,
        is_rereview=is_rereview,
    )

    # Fresh isolated session (initial_state=None) — zero shared context with CoderAgent.
    review_report = await agent_runner(reviewer_agent, prompt, None)
    report_path = run_dir / output_filename
    report_path.write_text(review_report.strip() + "\n", encoding="utf-8")

    findings = parse_review_findings(review_report)
    review_verdict = "CHANGES_REQUESTED" if findings["has_must_fix"] else "PASS"
    next_status = "AWAITING_HUMAN_REVIEW" if transition_to_human_review else "INDEPENDENT_REVIEW"

    state = transition_state(
        normalized,
        next_status,
        extra_updates={"review": review_verdict},
        runs_dir=runs_dir,
    )

    return {
        "change_id": normalized,
        "status": state["status"],
        "review": review_verdict,
        "review_report": str(report_path),
        "findings": findings,
    }
