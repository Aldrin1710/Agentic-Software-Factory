# Architecture

## 1. Product

```text
FastAPI adapter
      |
      v
CampusFlowService        <- business rules
      |
      v
SQLiteCampusFlowRepository
      |
      v
SQLite
```

Rules: API does not implement business policy; Repository does not decide business policy; Service coordinates behavior.

### Data Stores

- **SQLite** (`campusflow.db` in runtime, `:memory:` in tests): tables `events` (`id`, `title`, `capacity`, `owner_id`) and `registrations` (`id`, `event_id`, `user_id`). No external database or migration service.
- **Run Evidence (`runs/<CR-ID>/`)**: filesystem JSON/Markdown/text artifacts (`factory-state.json`, `factory-run.json`, reports, and human gate decisions).

## 2. Factory

```text
Change Request -> Intake -> Planner -> HUMAN GATE -> Coder -> Verification
                                                   -> Reviewer + Security
                                                   -> HUMAN REVIEW
                                                   -> Fix / Re-review
                                                   -> HUMAN RELEASE
                                                   -> PR Evidence
```

The Reviewer must inspect requirements, architecture, changed code, tests, verification evidence and actual APIs/dependencies. It must not treat the Coder's own claims as proof.

### Agent Contracts Matrix

Each agent operates under least privilege with explicitly scoped context, tools, outputs, and stop conditions:

| Agent | Responsibility | Input Context | Tools & Permissions | Output (`output_key` / File) | Stop Conditions |
|---|---|---|---|---|---|
| **IntakeAgent** | Classifies a Change Request, extracts affected requirements/AC, identifies ambiguities/conflicts, and recommends `PROCEED` or `HUMAN_CLARIFICATION_REQUIRED`. | Active `change_requests/CR-*.md`, `docs/REQUIREMENTS.md`, `docs/SPEC.md`, `docs/REPO_MAP.md`. | **Read-only**: `list_repo_files`, `read_repo_file`. | `intake_report` (`01-intake-report.md`) | Requirements conflict or are ambiguous (e.g., `CR-005`); missing CR details; cannot map to requirements without inventing policy. |
| **PlannerAgent** | Produces an implementation plan covering scope, affected files, architecture constraints, security checks, and test/verification plan without writing code. | `intake_report`, `docs/ARCHITECTURE.md`, `docs/SPEC.md`, target `campusflow/` and `tests/` files. | **Read-only**: `list_repo_files`, `read_repo_file`. | `implementation_plan` (`02-plan.md`) | Ambiguity remains in `intake_report`; human clarification required; architectural constraint conflict. |
| **CoderAgent** | Implements strictly the human-approved plan inside the product and product-test directories without altering requirements or weakening tests. | Human-approved `02-plan.md`, `03-human-plan-decision.json`, `docs/ARCHITECTURE.md`, `campusflow/`, `tests/product/`. | **Starter**: `READ_TOOLS` only. **Target (F2)**: `bounded-write` restricted exclusively to `campusflow/` and `tests/product/` (never `docs/`, `change_requests/`, `factory_app/`, `scripts/`, or `.env`). | `coder_report` (`04-coder-report.md` + git diff) | Human `plan` gate is not `APPROVED`; implementation requires changing requirements/spec; required dependency/API does not exist. |
| **TestAnalystAgent** | Inspects Change Request, requirements, diff, and tests; triggers deterministic baseline verification and flags missing negative/boundary/regression coverage or weakened assertions. | Change Request, requirements/AC, changed code/diff, `tests/product/`, deterministic tool output. | **Read-only + deterministic verification**: `list_repo_files`, `read_repo_file`, `run_baseline_verification`. | `test_analysis` (`05-verification.txt`, test gap report) | Deterministic checks (`pytest` / `ruff`) fail; tests appear weakened or mask defects; insufficient evidence to claim pass. |
| **ReviewerAgent** | Performs an independent first-pass review of requirements, architecture adherence, changed code, tests, verification output, and actual APIs/dependencies. Must not share Coder's context or trust Coder claims as proof. | Change Request, `docs/REQUIREMENTS.md`, `docs/SPEC.md`, `docs/ARCHITECTURE.md`, diff, `tests/`, `05-verification.txt`. | **Read-only** during review: `list_repo_files`, `read_repo_file`. | `review_report` (`06-review-report.md`) | Architecture violation (`API -> Service -> Repository`); invented/missing API or dependency; unverified claims; `MUST FIX` regressions. |
| **SecurityAgent** | Performs an independent security review of input handling, injection risks, secrets exposure, dependency risks, least-privilege compliance, and prompt-injection exposure. | Change Request, diff, `campusflow/`, `factory_app/`, deterministic scanner outputs. | **Read-only** during review: `list_repo_files`, `read_repo_file` (+ Semgrep/SCA/secret scanners in F4). | `security_report` (`07-security-report.md`) | Exposed secrets or PII; unsafe tool permissions or path traversal; injection/auth bypass risk; scanner failure. |
| **EvidenceAgent** | Consolidates already-produced artifacts, verification logs, review/security findings, and human decisions into traceability and release-readiness evidence without making the final release decision. | Existing artifacts in `runs/<CR-ID>/` (`00` through `11`). | **Read + bounded evidence write**: `list_repo_files`, `read_repo_file`, `write_run_evidence` (restricted to `runs/<CR-ID>/`). | `evidence_summary` (`traceability.md`, `factory-run.json`) | Missing required artifacts or verification logs; unresolved `MUST FIX` findings; cannot fabricate missing evidence. |

### Factory State Machine

Every Change Request run tracked in `runs/<CR-ID>/factory-state.json` follows an explicit, auditable state machine. Transitions across human gates require a recorded human decision; deterministic verification failures block progression to review.

```text
[Change Request]
       |
       v
INTAKE_PENDING ----(ambiguous CR)----> CLARIFICATION_REQUIRED --(PO: REJECTED)--> PLAN_REJECTED
       |                                         |
   (clear CR)                             (PO: APPROVED)
       |                                         |
       +-------------------+---------------------+
                           |
                           v
                       PLANNING --(PlannerAgent)--> AWAITING_PLAN_APPROVAL
                                                       |              |
                                                (TL: APPROVED)  (TL: REJECTED)
                                                       |              |
                                                       v              v
                                                 IMPLEMENTING    PLAN_REJECTED
                                                   ^      |
                                                   | (CoderAgent)
                                                   |      |
                                                   |      v
                                                   | VERIFICATION_RUNNING
                                                   | (pytest, ruff, TestAnalyst)
                                                   |      |              |
                                                   |   (PASS)         (FAIL)
                                                   |      |              |
                                                   |      |              v
                                                   +------)---- VERIFICATION_FAILED
                                                          |
                                                          v
                                                 INDEPENDENT_REVIEW
                                                 (Reviewer + Security)
                                                          |
                                                          v
                                                AWAITING_HUMAN_REVIEW <-----------+
                                                   |                  |           |
                                            (HR: APPROVED)      (HR: MUST FIX)    |
                                                   |                  |           |
                                                   |                  v           |
                                                   |           FIX_IN_PROGRESS ---+
                                                   |       (Coder -> re-verify -> re-review)
                                                   v
                                            EVIDENCE_PACKAGING
                                              (EvidenceAgent)
                                                   |
                                                   v
                                           AWAITING_RELEASE_GATE
                                              |              |
                                       (RM: APPROVED)  (RM: REJECTED)
                                              |              |
                                              v              v
                                   READY_FOR_HUMAN_MERGE  RELEASE_BLOCKED
```

| State | Owner / Actor | Valid Next States | Exit Condition / Evidence |
|---|---|---|---|
| `INTAKE_PENDING` | `IntakeAgent` | `CLARIFICATION_REQUIRED`, `PLANNING` | `01-intake-report.md` produced with `PROCEED` or `HUMAN_CLARIFICATION_REQUIRED`. |
| `CLARIFICATION_REQUIRED` | Product Owner | `PLANNING`, `PLAN_REJECTED` | `human-requirements-decision.json` recorded via `scripts/approve_gate.py`. |
| `PLANNING` | `PlannerAgent` | `AWAITING_PLAN_APPROVAL`, `CLARIFICATION_REQUIRED` | `02-plan.md` generated (read-only; no code edits). |
| `AWAITING_PLAN_APPROVAL` | Tech Lead | `IMPLEMENTING`, `PLAN_REJECTED` | `03-human-plan-decision.json` (`APPROVED` required to unlock `CoderAgent`). |
| `IMPLEMENTING` | `CoderAgent` | `VERIFICATION_RUNNING` | `04-coder-report.md` + scoped changes inside `campusflow/` and `tests/product/`. |
| `VERIFICATION_RUNNING` | Deterministic Tools (`pytest`, `ruff`) + `TestAnalystAgent` | `INDEPENDENT_REVIEW`, `VERIFICATION_FAILED` | `05-verification.txt` recorded; failures deterministically block LLM review. |
| `VERIFICATION_FAILED` | `CoderAgent` / Tech Lead | `IMPLEMENTING`, `PLAN_REJECTED` | Failure diagnosed and fixed without weakening tests or altering requirements. |
| `INDEPENDENT_REVIEW` | `ReviewerAgent`, `SecurityAgent` | `AWAITING_HUMAN_REVIEW` | `06-review-report.md`, `07-security-report.md` (read-only, isolated from Coder context). |
| `AWAITING_HUMAN_REVIEW` | Human Reviewer | `FIX_IN_PROGRESS`, `EVIDENCE_PACKAGING` | `08-human-review-decisions.md` triaging `MUST FIX` / `SHOULD FIX` / `OPTIONAL`. |
| `FIX_IN_PROGRESS` | `CoderAgent` -> Verification -> `ReviewerAgent` | `AWAITING_HUMAN_REVIEW` | `09-fix-report.md`, `10-reverification.txt`, `11-rereview.md`. |
| `EVIDENCE_PACKAGING` | `EvidenceAgent` | `AWAITING_RELEASE_GATE` | `traceability.md` and `factory-run.json` consolidated without fabricating evidence. |
| `AWAITING_RELEASE_GATE` | Release Manager | `READY_FOR_HUMAN_MERGE`, `RELEASE_BLOCKED` | `12-release-decision.json` recorded by human Release Manager. |
| `READY_FOR_HUMAN_MERGE` | Human (Final State) | *(Terminal)* | Maximum automated state; merge is always performed manually by a human. |
