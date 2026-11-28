from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from .agents import coder_agent, intake_agent, planner_agent
from .gates import GATE_OWNERS, record_gate_decision, require_plan_approved
from .state import CR_DIR, REPO_ROOT, RUNS_DIR, get_run_dir, init_run, load_state, transition_state

AgentRunnerFn = Callable[[Any, str, dict[str, Any] | None], Awaitable[str]]
GatePromptFn = Callable[[str, str, str], tuple[str, str]]

PAUSE_GATE_MAP: dict[str, tuple[str, str]] = {
    "CLARIFICATION_REQUIRED": ("requirements", "01-intake-report.md"),
    "AWAITING_PLAN_APPROVAL": ("plan", "02-plan.md"),
    "AWAITING_HUMAN_REVIEW": ("review", "06-review-report.md"),
    "AWAITING_RELEASE_GATE": ("release", "traceability.md"),
}

TERMINAL_OR_BLOCKED_STATUSES = {
    "PLAN_REJECTED",
    "VERIFICATION_FAILED",
    "RELEASE_BLOCKED",
    "READY_FOR_HUMAN_MERGE",
}

INTAKE_CONTEXT_DOCS = ("docs/REQUIREMENTS.md", "docs/SPEC.md", "docs/REPO_MAP.md")
PLANNER_CONTEXT_DOCS = (
    "docs/REQUIREMENTS.md",
    "docs/SPEC.md",
    "docs/ARCHITECTURE.md",
    "docs/REPO_MAP.md",
)
CODER_CONTEXT_FILES = (
    "campusflow/domain.py",
    "campusflow/errors.py",
    "campusflow/repository.py",
    "campusflow/service.py",
    "campusflow/api.py",
    "tests/product/test_service.py",
)


def _load_repo_context(relative_paths: tuple[str, ...]) -> str:
    """Pre-load small reference docs to minimize redundant tool-call round trips."""
    sections: list[str] = []
    for rel in relative_paths:
        path = REPO_ROOT / rel
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            sections.append(f"--- {rel} ---\n{content}")
    return "\n\n".join(sections)


def parse_intake_recommendation(report: str) -> str:
    """Deterministically extract PROCEED vs HUMAN_CLARIFICATION_REQUIRED from intake report."""
    upper = report.upper()
    explicit = re.findall(
        r"RECOMMENDATION\s*:\s*(HUMAN_CLARIFICATION_REQUIRED|PROCEED)",
        upper,
    )
    if explicit:
        return explicit[-1]
    if "HUMAN_CLARIFICATION_REQUIRED" in upper:
        return "HUMAN_CLARIFICATION_REQUIRED"
    if "PROCEED" in upper:
        return "PROCEED"
    return "HUMAN_CLARIFICATION_REQUIRED"


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect Gemini API rate-limit / quota exhaustion errors eligible for backoff retry."""
    message = f"{type(exc).__name__}: {exc}".lower()
    markers = ("429", "resource_exhausted", "rate limit", "too many requests", "quota")
    return any(marker in message for marker in markers)


async def _run_adk_agent_once(
    agent: Any,
    prompt: str,
    initial_state: dict[str, Any] | None = None,
) -> str:
    app_name = "campusflow_factory"
    user_id = "factory_orchestrator"
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        state=dict(initial_state or {}),
    )
    message = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])

    chunks: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=message,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                text = getattr(part, "text", None)
                if text:
                    chunks.append(text)

    updated = await runner.session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session.id,
    )
    output_key = getattr(agent, "output_key", None)
    if updated and output_key and updated.state.get(output_key):
        return str(updated.state[output_key]).strip()
    return "\n".join(chunks).strip()


def _extract_retry_delay(exc: BaseException, fallback: float) -> float:
    """Honor Gemini API retryDelay hint (e.g. 'retryDelay': '35s') when present."""
    if fallback == 0.0:
        return 0.0
    text = str(exc)
    match = re.search(r"retry\s+in\s+(\d+(?:\.\d+)?)s", text, re.IGNORECASE) or re.search(
        r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", text, re.IGNORECASE
    )
    if match:
        return min(65.0, float(match.group(1)) + 1.0)
    return fallback


async def run_adk_agent(
    agent: Any,
    prompt: str,
    initial_state: dict[str, Any] | None = None,
) -> str:
    """Run a single Google ADK LlmAgent in an isolated session with 429 backoff retry."""
    max_attempts = max(1, int(os.getenv("GEMINI_RETRY_ATTEMPTS", "3")))
    base_delay = max(0.0, float(os.getenv("GEMINI_RETRY_BASE_DELAY", "2.0")))

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await _run_adk_agent_once(agent, prompt, initial_state)
        except Exception as exc:
            if not _is_rate_limit_error(exc) or attempt + 1 >= max_attempts:
                raise
            last_exc = exc
            delay = _extract_retry_delay(exc, base_delay * (2**attempt))
            await asyncio.sleep(delay)

    raise RuntimeError("Agent execution failed after retries") from last_exc


async def run_planning(
    change_id: str,
    *,
    runs_dir: Path = RUNS_DIR,
    cr_dir: Path = CR_DIR,
    agent_runner: AgentRunnerFn = run_adk_agent,
) -> dict[str, Any]:
    """Run Change Request -> Intake -> (stop on ambiguity) -> Planner -> Human Plan Gate."""
    normalized = change_id.strip().upper()
    run_dir = init_run(normalized, runs_dir=runs_dir, cr_dir=cr_dir)
    cr_text = (run_dir / "00-change-request.md").read_text(encoding="utf-8")

    intake_context = _load_repo_context(INTAKE_CONTEXT_DOCS)
    intake_prompt = (
        f"Analyze Change Request {normalized} and produce the intake report. "
        f"Do not design the implementation and do not modify files. "
        f"The core repository docs are already included below; only use read tools if you "
        f"genuinely need another file.\n\n"
        f"Change Request ({normalized}):\n{cr_text}\n\n"
        f"Repository Context:\n{intake_context}"
    )
    intake_report = await agent_runner(intake_agent, intake_prompt, None)
    intake_path = run_dir / "01-intake-report.md"
    intake_path.write_text(intake_report + "\n", encoding="utf-8")

    recommendation = parse_intake_recommendation(intake_report)
    if recommendation != "PROCEED":
        state = transition_state(
            normalized,
            "CLARIFICATION_REQUIRED",
            gate_updates={"requirements": "CLARIFICATION_REQUIRED"},
            runs_dir=runs_dir,
        )
        return {
            "change_id": normalized,
            "status": state["status"],
            "recommendation": recommendation,
            "intake_report": str(intake_path),
            "plan_report": None,
        }

    transition_state(
        normalized,
        "PLANNING",
        gate_updates={"requirements": "APPROVED"},
        runs_dir=runs_dir,
    )

    planner_context = _load_repo_context(PLANNER_CONTEXT_DOCS)
    planner_prompt = (
        f"Create a scoped implementation plan for Change Request {normalized} using the "
        f"intake report and repository docs below. Do not write code. "
        f"Only call read_repo_file if you need to inspect a specific Python file in "
        f"campusflow/ or tests/.\n\n"
        f"Change Request ({normalized}):\n{cr_text}\n\n"
        f"Repository Context:\n{planner_context}"
    )
    plan_report = await agent_runner(
        planner_agent,
        planner_prompt,
        {"intake_report": intake_report},
    )
    plan_path = run_dir / "02-plan.md"
    plan_path.write_text(plan_report + "\n", encoding="utf-8")

    if "STOP: HUMAN_CLARIFICATION_REQUIRED" in plan_report.upper():
        state = transition_state(
            normalized,
            "CLARIFICATION_REQUIRED",
            gate_updates={"requirements": "CLARIFICATION_REQUIRED"},
            runs_dir=runs_dir,
        )
        return {
            "change_id": normalized,
            "status": state["status"],
            "recommendation": "HUMAN_CLARIFICATION_REQUIRED",
            "intake_report": str(intake_path),
            "plan_report": str(plan_path),
        }

    state = transition_state(
        normalized,
        "AWAITING_PLAN_APPROVAL",
        runs_dir=runs_dir,
    )
    return {
        "change_id": normalized,
        "status": state["status"],
        "recommendation": recommendation,
        "intake_report": str(intake_path),
        "plan_report": str(plan_path),
    }


async def run_coder(
    change_id: str,
    *,
    runs_dir: Path = RUNS_DIR,
    agent_runner: AgentRunnerFn = run_adk_agent,
) -> dict[str, Any]:
    """Run CoderAgent strictly after verifying Human Plan Gate approval."""
    normalized = change_id.strip().upper()
    decision = require_plan_approved(normalized, runs_dir=runs_dir)

    run_dir = runs_dir / normalized
    plan_path = run_dir / "02-plan.md"
    cr_path = run_dir / "00-change-request.md"
    if not plan_path.is_file():
        raise FileNotFoundError(f"Approved plan not found: {plan_path}")

    cr_text = cr_path.read_text(encoding="utf-8")
    plan_text = plan_path.read_text(encoding="utf-8")

    transition_state(normalized, "IMPLEMENTING", runs_dir=runs_dir)

    coder_context = _load_repo_context(CODER_CONTEXT_FILES)
    coder_prompt = (
        f"Implement the human-approved plan for {normalized}. "
        f"Use bounded tools only under campusflow/ and tests/product/. "
        f"Never modify docs/REQUIREMENTS.md or docs/SPEC.md and never weaken tests. "
        f"The current product and test files are already provided below; do NOT call "
        f"`list_repo_files` or `read_repo_file` for files already shown here.\n\n"
        f"Human plan approval: {decision.get('decision')} by {decision.get('recorded_by')} "
        f"({decision.get('reason')})\n\n"
        f"Change Request:\n{cr_text}\n\n"
        f"Approved Plan:\n{plan_text}\n\n"
        f"Current Product & Test Files:\n{coder_context}"
    )
    coder_report = await agent_runner(coder_agent, coder_prompt, None)
    coder_path = run_dir / "04-coder-report.md"
    coder_path.write_text(coder_report + "\n", encoding="utf-8")

    state = transition_state(normalized, "VERIFICATION_RUNNING", runs_dir=runs_dir)
    return {
        "change_id": normalized,
        "status": state["status"],
        "coder_report": str(coder_path),
        "plan_decision": decision,
    }


def default_console_gate_prompt(change_id: str, gate: str, artifact_rel: str) -> tuple[str, str]:
    """Prompt the human gate owner interactively in the terminal."""
    owner = GATE_OWNERS.get(gate, "Human Owner")
    print(f"\n=== HUMAN GATE: {gate.upper()} ({change_id}) ===")
    print(f"Owner   : {owner}")
    print(f"Review  : {artifact_rel}")
    decision = (
        input("Decision [APPROVED / MODIFIED / REJECTED / DEFERRED / PAUSE]: ").strip().upper()
    )
    if not decision or decision == "PAUSE":
        return "PAUSE", ""
    reason = input("Reason (required): ").strip()
    return decision, reason


async def run_factory(
    change_id: str,
    *,
    runs_dir: Path = RUNS_DIR,
    cr_dir: Path = CR_DIR,
    agent_runner: AgentRunnerFn = run_adk_agent,
    verifier: Callable[[], dict[str, Any]] | None = None,
    diff_provider: Callable[[], dict[str, Any]] | None = None,
    run_test_analyst: bool = True,
    interactive: bool = False,
    prompt_fn: GatePromptFn = default_console_gate_prompt,
    max_steps: int = 10,
) -> dict[str, Any]:
    """Master pause/resume orchestrator driven by runs/<CR-ID>/factory-state.json."""
    from .review import run_independent_review
    from .tools import get_git_diff, run_baseline_verification
    from .verification import run_verification

    active_verifier = verifier or run_baseline_verification
    active_diff_provider = diff_provider or get_git_diff

    normalized = change_id.strip().upper()
    run_dir = get_run_dir(normalized, runs_dir=runs_dir)
    if not (run_dir / "factory-state.json").is_file():
        init_run(normalized, runs_dir=runs_dir, cr_dir=cr_dir)

    executed_steps: list[str] = []

    for _ in range(max_steps):
        state = load_state(normalized, runs_dir=runs_dir)
        status = state.get("status", "INTAKE_PENDING")

        if status in TERMINAL_OR_BLOCKED_STATUSES:
            return {
                "change_id": normalized,
                "status": status,
                "paused": False,
                "executed_steps": executed_steps,
                "message": f"Factory stopped at status {status}.",
            }

        if status in PAUSE_GATE_MAP:
            gate, artifact = PAUSE_GATE_MAP[status]
            artifact_path = str(run_dir / artifact)
            if not interactive:
                return {
                    "change_id": normalized,
                    "status": status,
                    "paused": True,
                    "waiting_gate": gate,
                    "gate_owner": GATE_OWNERS.get(gate, "Human Owner"),
                    "review_artifact": artifact_path,
                    "executed_steps": executed_steps,
                    "next_action": (
                        f'python scripts/approve_gate.py {normalized} {gate} APPROVED "<reason>"'
                    ),
                }

            decision, reason = prompt_fn(normalized, gate, artifact_path)
            if decision == "PAUSE":
                return {
                    "change_id": normalized,
                    "status": status,
                    "paused": True,
                    "waiting_gate": gate,
                    "gate_owner": GATE_OWNERS.get(gate, "Human Owner"),
                    "review_artifact": artifact_path,
                    "executed_steps": executed_steps,
                    "next_action": (
                        f'python scripts/approve_gate.py {normalized} {gate} APPROVED "<reason>"'
                    ),
                }

            record_gate_decision(
                normalized,
                gate,
                decision,
                reason,
                runs_dir=runs_dir,
            )
            executed_steps.append(f"human_gate:{gate}:{decision}")
            continue

        if status in {"INTAKE_PENDING", "PLANNING"}:
            await run_planning(
                normalized,
                runs_dir=runs_dir,
                cr_dir=cr_dir,
                agent_runner=agent_runner,
            )
            executed_steps.append("planning")
            continue

        if status == "IMPLEMENTING":
            await run_coder(
                normalized,
                runs_dir=runs_dir,
                agent_runner=agent_runner,
            )
            executed_steps.append("coder")
            continue

        if status == "VERIFICATION_RUNNING":
            await run_verification(
                normalized,
                runs_dir=runs_dir,
                verifier=active_verifier,
                agent_runner=agent_runner,
                diff_provider=active_diff_provider,
                run_test_analyst=run_test_analyst,
            )
            executed_steps.append("verification")
            continue

        if status == "INDEPENDENT_REVIEW":
            await run_independent_review(
                normalized,
                runs_dir=runs_dir,
                agent_runner=agent_runner,
                diff_provider=active_diff_provider,
            )
            executed_steps.append("independent_review")
            continue

        return {
            "change_id": normalized,
            "status": status,
            "paused": True,
            "executed_steps": executed_steps,
            "message": f"Stage {status!r} reached; awaiting downstream handler.",
        }

    final_state = load_state(normalized, runs_dir=runs_dir)
    return {
        "change_id": normalized,
        "status": final_state.get("status"),
        "paused": True,
        "executed_steps": executed_steps,
        "message": f"Stopped after reaching max_steps={max_steps}.",
    }
