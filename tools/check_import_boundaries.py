"""tools/check_import_boundaries.py

Run this locally / in CI to enforce Phase-1 architecture boundaries.

Goal:
  - All AI calls must go through core.ai_gateway.request_text.
  - Commands should not import utils.ai_client.generate_text or utils.backpressure helpers directly.

Usage:
  python -m tools.check_import_boundaries
"""

from __future__ import annotations

import ast
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Files that are allowed to reference forbidden symbols (the boundary modules themselves).
ALLOWLIST = {
    REPO_ROOT / "core" / "ai_gateway.py",
    REPO_ROOT / "utils" / "ai_client.py",
    REPO_ROOT / "utils" / "backpressure.py",
}

FORBIDDEN_BACKPRESSURE_NAMES = {"is_open", "ai_slot", "trip"}
FORBIDDEN_AICLIENT_NAMES = {"generate_text"}


def _should_scan(path: pathlib.Path) -> bool:
    if path in ALLOWLIST:
        return False
    if path.name.startswith("."):
        return False
    if "venv" in path.parts or ".venv" in path.parts:
        return False
    return path.suffix == ".py"


def main() -> int:
    offenders: list[tuple[pathlib.Path, int, str]] = []

    for py in REPO_ROOT.rglob("*.py"):
        if not _should_scan(py):
            continue

        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        # Collect imported names for better messages
        for node in ast.walk(tree):
            # from utils.ai_client import generate_text
            if isinstance(node, ast.ImportFrom) and node.module == "utils.ai_client":
                for alias in node.names:
                    if alias.name in FORBIDDEN_AICLIENT_NAMES:
                        offenders.append(
                            (py.relative_to(REPO_ROOT), node.lineno, f"from utils.ai_client import {alias.name}")
                        )

            # from utils.backpressure import is_open / ai_slot / trip
            if isinstance(node, ast.ImportFrom) and node.module == "utils.backpressure":
                for alias in node.names:
                    if alias.name in FORBIDDEN_BACKPRESSURE_NAMES:
                        offenders.append(
                            (py.relative_to(REPO_ROOT), node.lineno, f"from utils.backpressure import {alias.name}")
                        )

            # Direct call to generate_text(...)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "generate_text":
                offenders.append((py.relative_to(REPO_ROOT), node.lineno, "generate_text(...)"))

    if offenders:
        print("\n❌ Import boundary violations found:\n")
        for p, ln, src in offenders:
            print(f"- {p}:{ln}: {src}")
        print("\nFix: route AI calls through core.ai_gateway.request_text and remove direct backpressure imports.")
        return 1

    print("✅ Import boundaries look good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


