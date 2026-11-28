from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
READABLE_ROOTS = {"docs", "change_requests", "campusflow", "tests", "runs"}
BINARY_EXTENSIONS = {".db", ".png", ".jpg", ".jpeg", ".zip", ".pdf", ".docx"}


def _safe_repo_path(relative_path: str) -> Path:
    candidate = (REPO_ROOT / relative_path).resolve()
    if REPO_ROOT not in candidate.parents and candidate != REPO_ROOT:
        raise ValueError("path escapes repository")
    return candidate


def _is_allowed_product_write(rel: Path) -> bool:
    """Allow writes strictly under campusflow/ or tests/product/."""
    if not rel.parts:
        return False
    if any(part.startswith(".") for part in rel.parts):
        return False
    if rel.suffix.lower() in BINARY_EXTENSIONS:
        return False
    if rel.parts[0] == "campusflow":
        return len(rel.parts) >= 2
    if rel.parts[0] == "tests":
        return len(rel.parts) >= 3 and rel.parts[1] == "product"
    return False


def list_repo_files(prefix: str = "") -> dict:
    """List files under an allowlisted read-only area."""
    prefix = prefix.strip().strip("/")
    if prefix:
        top = prefix.split("/", 1)[0]
        if top not in READABLE_ROOTS:
            return {"ok": False, "error": f"read not allowed under {top!r}"}
        root = _safe_repo_path(prefix)
    else:
        root = REPO_ROOT
    if not root.exists():
        return {"ok": False, "error": "path does not exist"}
    items = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        if not prefix and rel.parts[0] not in READABLE_ROOTS:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        items.append(str(rel))
    return {"ok": True, "files": items[:250]}


def read_repo_file(relative_path: str) -> dict:
    """Read UTF-8 text from an allowlisted repository area."""
    path = _safe_repo_path(relative_path)
    rel = path.relative_to(REPO_ROOT)
    if not rel.parts or rel.parts[0] not in READABLE_ROOTS:
        return {"ok": False, "error": "read not allowed for this path"}
    if not path.is_file():
        return {"ok": False, "error": "file does not exist"}
    if path.suffix.lower() in {".db", ".png", ".jpg", ".jpeg", ".zip", ".pdf"}:
        return {"ok": False, "error": "binary file not supported"}
    text = path.read_text(encoding="utf-8")
    if len(text) > 40_000:
        text = text[:40_000] + "\n...[TRUNCATED]..."
    return {"ok": True, "path": str(rel), "content": text}


def run_baseline_verification() -> dict:
    """Run an allowlisted deterministic baseline. No arbitrary shell input."""
    commands = [
        [sys.executable, "-m", "pytest", "-q"],
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "campusflow",
            "tests",
            "factory_app",
            "scripts",
        ],
    ]
    results = []
    all_ok = True
    for command in commands:
        completed = subprocess.run(
            command, cwd=REPO_ROOT, text=True, capture_output=True, timeout=120, check=False
        )
        all_ok = all_ok and completed.returncode == 0
        results.append({
            "command": " ".join(command),
            "returncode": completed.returncode,
            "stdout": completed.stdout[-6000:],
            "stderr": completed.stderr[-3000:],
        })
    return {"ok": all_ok, "results": results}


def get_git_diff(
    paths: tuple[str, ...] = ("campusflow", "tests"),
    *,
    max_chars: int = 12_000,
) -> dict:
    """Return a bounded read-only git diff for allowlisted repo paths to save LLM tokens."""
    safe_paths = [p for p in paths if p in READABLE_ROOTS]
    cmd = ["git", "diff", "HEAD", "--", *safe_paths] if safe_paths else ["git", "diff", "HEAD"]
    completed = subprocess.run(
        cmd, cwd=REPO_ROOT, text=True, capture_output=True, timeout=30, check=False
    )
    if completed.returncode != 0:
        fallback = ["git", "diff", "--", *safe_paths] if safe_paths else ["git", "diff"]
        completed = subprocess.run(
            fallback, cwd=REPO_ROOT, text=True, capture_output=True, timeout=30, check=False
        )
    diff_text = completed.stdout or ""
    truncated = False
    if len(diff_text) > max_chars:
        diff_text = diff_text[:max_chars] + "\n...[TRUNCATED FOR TOKEN BUDGET]..."
        truncated = True
    return {
        "ok": completed.returncode == 0,
        "diff": diff_text,
        "truncated": truncated,
        "stderr": (completed.stderr or "")[-1000:],
    }


def write_run_evidence(change_id: str, filename: str, content: str) -> dict:
    """Write only evidence files under runs/<CR-ID>/; never product code."""
    if not change_id.startswith("CR-"):
        return {"ok": False, "error": "change_id must start with CR-"}
    if "/" in filename or "\\" in filename or not filename.endswith((".md", ".json", ".txt")):
        return {"ok": False, "error": "invalid evidence filename"}
    folder = REPO_ROOT / "runs" / change_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path.relative_to(REPO_ROOT))}


def write_product_file(relative_path: str, content: str) -> dict:
    """Bounded write tool for CoderAgent: strictly limited to campusflow/ and tests/product/."""
    try:
        path = _safe_repo_path(relative_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    rel = path.relative_to(REPO_ROOT)
    if not _is_allowed_product_write(rel):
        return {
            "ok": False,
            "error": (
                f"write not allowed for {str(rel)!r}; "
                "CoderAgent may only edit files under 'campusflow/' or 'tests/product/'"
            ),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(rel), "bytes_written": len(content.encode("utf-8"))}


def edit_product_file(relative_path: str, old_text: str, new_text: str) -> dict:
    """Bounded edit tool for CoderAgent: replaces an exact unique snippet in product/test code."""
    if not old_text:
        return {"ok": False, "error": "old_text must be non-empty"}
    try:
        path = _safe_repo_path(relative_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    rel = path.relative_to(REPO_ROOT)
    if not _is_allowed_product_write(rel):
        return {
            "ok": False,
            "error": (
                f"edit not allowed for {str(rel)!r}; "
                "CoderAgent may only edit files under 'campusflow/' or 'tests/product/'"
            ),
        }
    if not path.is_file():
        return {"ok": False, "error": f"file does not exist: {str(rel)!r}"}
    original = path.read_text(encoding="utf-8")
    occurrences = original.count(old_text)
    if occurrences == 0:
        return {"ok": False, "error": "old_text not found in file"}
    if occurrences > 1:
        return {
            "ok": False,
            "error": f"old_text matched {occurrences} times; provide more surrounding context",
        }
    updated = original.replace(old_text, new_text, 1)
    path.write_text(updated, encoding="utf-8")
    return {"ok": True, "path": str(rel)}
