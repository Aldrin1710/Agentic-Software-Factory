# Repository Map

Navigation guide for agents and contributors: entry points, responsibilities, change paths, and protected boundaries.

## Entry Points

### Product (`campusflow/`)
- `campusflow/api.py` — FastAPI HTTP adapter (routes and HTTP error mapping only; no business policy)
- `campusflow/service.py` — `CampusFlowService` (all product business rules)
- `campusflow/repository.py` — `SQLiteCampusFlowRepository` (SQLite persistence only)
- `campusflow/domain.py` & `campusflow/errors.py` — entities (`Event`, `Registration`) and domain exceptions

### Factory (`factory_app/` & `scripts/`)
- `factory_app/agent.py` — ADK root export (`root_agent` and `app`)
- `factory_app/workflows.py` — ADK workflow pipelines (`PlanningPipeline`, `IndependentReviewPipeline`)
- `factory_app/agents.py` & `factory_app/prompts.py` — specialized agent definitions and role contracts
- `factory_app/tools.py` — bounded deterministic tools (read-only repo access, verification, evidence writing)
- `scripts/verify_repo.py` — standard deterministic verification (`pytest` + `ruff`)
- `scripts/start_run.py` & `scripts/approve_gate.py` — run initialization and auditable human gate decisions

## Tests

- `tests/product/` — product behavior, negative cases, and regression tests (`test_service.py`, `test_known_gaps.py`)
- `tests/factory/` — repository contract and factory guardrail tests (`test_contracts.py`)
- `tests/eval/` — agent evaluation config (`eval_config.yaml`) and prompt datasets (`datasets/`)

## Change Guide

### If implementing a product Change Request (`change_requests/CR-*.md`)
1. Read `docs/REQUIREMENTS.md`, `docs/SPEC.md`, `docs/ARCHITECTURE.md`, and the active `change_requests/CR-*.md`.
2. Implement business rules in `campusflow/service.py`.
3. Update `campusflow/domain.py`, `campusflow/errors.py`, or `campusflow/repository.py` only if needed by the rule.
4. Update `campusflow/api.py` only to expose routes or map exceptions (keep business policy out of the API layer).
5. Add deterministic tests in `tests/product/` (never weaken or delete existing assertions).
6. Run `uv run python scripts/verify_repo.py`.

### If extending the Agentic Factory (`factory_app/` / `scripts/`)
1. Check `AGENTS.md` and `docs/ARCHITECTURE.md` (roles, agent contracts, and state machine).
2. Keep prompts in `factory_app/prompts.py`, deterministic/bounded tools in `factory_app/tools.py`, and agent wiring in `factory_app/agents.py` / `workflows.py`.
3. Add guardrail tests in `tests/factory/` and eval cases in `tests/eval/datasets/`.
4. Run `uv run python scripts/verify_repo.py`.

## Do Not Edit Directly

- `docs/REQUIREMENTS.md` and `docs/SPEC.md` — source of truth; never modify to make an implementation pass.
- `change_requests/CR-*.md` — run inputs; treat as read-only during execution.
- `.env` — local secrets (`GEMINI_API_KEY`); never commit, log, or expose.
- `runs/<CR-ID>/` — generated auditable evidence; produced only by factory runs and human gate scripts.
- `.venv/`, `*.db`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/` — generated local artifacts.
