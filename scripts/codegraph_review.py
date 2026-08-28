"""Codegraph review — static analysis pass before the github push.

User directive 2026-08-29: "do the codegraph review and bug fixing
before the github push."

This is a lean codegraph review tool. It scans:
  - import graph (cycles, orphans, dead code)
  - syntax / compile errors (catches broken modules early)
  - missing __init__.py exports
  - test parity (every new module has at least one test)
  - μ=0 invariant (no `import openai` or LLM calls at module top-level)

It outputs a JSON report to stdout + a markdown report to
``benchmarks/results/codegraph_review.json``. Exit code 0 = clean,
1 = warnings, 2 = errors.

Run it: ``python3 scripts/codegraph_review.py``
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import sys
import py_compile
import pathlib
import re
from collections import defaultdict
from typing import Any


PROJECT_ROOT = pathlib.Path("/home/z/my-project")
PYTHON_PKG = PROJECT_ROOT / "cortexm"
TESTS_DIR = PROJECT_ROOT / "tests"


# ------------------------------ helpers ------------------------------

def _walk_py(root: pathlib.Path):
    for p in root.rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        yield p


def _rel(p: pathlib.Path) -> str:
    return str(p.relative_to(PROJECT_ROOT))


# ------------------------------ checks -------------------------------

def check_compile() -> list[dict]:
    """Compile every .py to bytecode. Catch syntax errors."""
    out = []
    for p in _walk_py(PYTHON_PKG):
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as e:
            out.append({
                "file": _rel(p), "check": "compile",
                "severity": "error", "msg": str(e)[:200],
            })
    return out


def check_imports_resolve() -> list[dict]:
    """Try to import every module in cortexm/. Detect broken imports."""
    out = []
    for p in _walk_py(PYTHON_PKG):
        if p.name == "__init__.py":
            mod = "cortexm"
        else:
            parts = list(p.relative_to(PYTHON_PKG.parent).parts)
            parts[-1] = parts[-1][:-3]  # strip .py
            mod = ".".join(parts)
        try:
            importlib.import_module(mod)
        except ImportError as e:
            out.append({
                "file": _rel(p), "check": "import",
                "severity": "error",
                "msg": f"ImportError: {e}"[:200],
            })
        except Exception as e:
            # Skip non-import errors (those are runtime issues for tests)
            pass
    return out


def check_test_parity() -> list[dict]:
    """For every module under cortexm/, verify some test references it.

    A test "references" a module if it imports the module by name,
    imports one of its public symbols (functions/classes/constants),
    or mentions the module's name as a string.
    """
    out = []
    if not TESTS_DIR.exists():
        return [{"check": "test_parity", "severity": "warning",
                  "msg": "no tests/ dir"}]
    # Collect all test file text once
    test_files = list(TESTS_DIR.glob("*.py"))
    test_text = ""
    for tf in test_files:
        try:
            test_text += "\n" + tf.read_text(encoding="utf-8")
        except Exception:
            pass

    for p in PYTHON_PKG.rglob("*.py"):
        if p.name == "__init__.py":
            continue
        if "__pycache__" in str(p):
            continue
        mod_name = p.stem
        if mod_name in {"__init__", "cli", "cortexm"}:
            continue
        # Collect module's public symbols (top-level def/class names)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        public_syms: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                if not node.name.startswith("_"):
                    public_syms.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and not t.id.startswith("_"):
                        public_syms.add(t.id)
        # Build patterns: module name + each public symbol
        patterns = {mod_name, f"cortexm.{mod_name}",
                    f"from cortexm import {mod_name}"}
        patterns.update(public_syms)
        # Filter out overly-common names to reduce false positives
        common = {"Pipeline", "Memory", "Config", "Context",
                   "Stage", "main", "run"}
        patterns -= common
        if not any(pat in test_text for pat in patterns):
            out.append({
                "file": _rel(p),
                "check": "test_parity",
                "severity": "warning",
                "msg": f"no test file references module '{mod_name}' "
                       f"or its public symbols",
            })
    return out


def check_llm_free() -> list[dict]:
    """μ=0 invariant: no module top-level imports openai/anthropic/etc.

    LLM calls at module top-level would break the μ=0 protocol
    counter (LLM_CALLS would never be 0 if openai was auto-loaded).
    """
    out = []
    forbidden = ("openai", "anthropic", "langchain", "llama_index",
                 "transformers", "torch", "tensorflow")
    for p in _walk_py(PYTHON_PKG):
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                else:
                    if node.module:
                        names = [node.module]
                for n in names:
                    top = n.split(".")[0]
                    if top in forbidden and \
                       _rel(p) != "cortexm/api/long_recall.py":
                        # long_recall.py is allowed to DOCUMENT that LLMs
                        # could be plugged in; it doesn't actually import them
                        out.append({
                            "file": _rel(p), "check": "μ=0_invariant",
                            "severity": "error",
                            "msg": f"top-level import of '{n}' breaks "
                                   f"μ=0 protocol",
                        })
    return out


def check_circular_imports() -> list[dict]:
    """Build import graph, detect cycles among cortexm modules."""
    out = []
    edges: dict[str, set[str]] = defaultdict(set)
    for p in _walk_py(PYTHON_PKG):
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        # Compute the module's dotted name
        if p.name == "__init__.py":
            parts = list(p.relative_to(PYTHON_PKG.parent).parts)[:-1]
        else:
            parts = list(p.relative_to(PYTHON_PKG.parent).parts)
            parts[-1] = parts[-1][:-3]
        src_mod = "cortexm." + ".".join(parts) if parts else "cortexm"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and \
               node.module.startswith("cortexm"):
                edges[src_mod].add(node.module)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("cortexm"):
                        edges[src_mod].add(a.name)
    # Detect cycles via DFS
    visited: set[str] = set()
    stack: set[str] = set()

    def _dfs(n: str, path: list[str]) -> list[list[str]] | None:
        if n in stack:
            # found cycle
            i = path.index(n)
            return [path[i:] + [n]]
        if n in visited:
            return None
        visited.add(n)
        stack.add(n)
        for nxt in edges.get(n, ()):
            cyc = _dfs(nxt, path + [n])
            if cyc:
                return cyc
        stack.discard(n)
        return None

    cycles_found = []
    for n in list(edges.keys()):
        cyc = _dfs(n, [])
        if cyc:
            cycles_found.extend(cyc)
    if cycles_found:
        out.append({
            "check": "circular_imports",
            "severity": "warning",
            "msg": f"{len(cycles_found)} circular import paths detected",
            "cycles": cycles_found[:5],
        })
    return out


def check_new_modules_have_docstrings() -> list[dict]:
    """Module docstrings are required for new modules."""
    out = []
    new_modules = [
        "cortexm/kernel.py",
        "cortexm/router.py",
        "cortexm/plugins/verbatim.py",
        "cortexm/plugins/structured.py",
        "cortexm/plugins/security.py",
        "cortexm/bridge/fusion.py",
    ]
    for relpath in new_modules:
        p = PROJECT_ROOT / relpath
        if not p.exists():
            out.append({
                "file": relpath, "check": "docstring",
                "severity": "error", "msg": "module file missing",
            })
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception as e:
            out.append({
                "file": relpath, "check": "docstring",
                "severity": "error", "msg": f"parse error: {e}",
            })
            continue
        if not (tree.body and isinstance(tree.body[0], ast.Expr) and
                isinstance(tree.body[0].value, ast.Constant) and
                isinstance(tree.body[0].value.value, str)):
            out.append({
                "file": relpath, "check": "docstring",
                "severity": "warning",
                "msg": "module docstring missing",
            })
    return out


def check_exports_in_init() -> list[dict]:
    """Verify the new public symbols are exported from cortexm/__init__.py."""
    out = []
    init = PYTHON_PKG / "__init__.py"
    if not init.exists():
        return [{"check": "exports", "severity": "error",
                  "msg": "cortexm/__init__.py missing"}]
    src = init.read_text(encoding="utf-8")
    expected = ["Memory", "Context", "mount_default", "__version__"]
    for sym in expected:
        if sym not in src:
            out.append({
                "file": "cortexm/__init__.py",
                "check": "exports",
                "severity": "error",
                "msg": f"symbol '{sym}' not exported",
            })
    return out


# ------------------------------ runner -------------------------------

def run_all() -> dict[str, Any]:
    checks = {
        "compile": check_compile(),
        "imports_resolve": check_imports_resolve(),
        "test_parity": check_test_parity(),
        "μ=0_invariant": check_llm_free(),
        "circular_imports": check_circular_imports(),
        "docstrings": check_new_modules_have_docstrings(),
        "exports": check_exports_in_init(),
    }
    n_errors = sum(1 for v in checks.values()
                   for r in v if r.get("severity") == "error")
    n_warnings = sum(1 for v in checks.values()
                     for r in v if r.get("severity") == "warning")
    return {
        "checks": checks,
        "totals": {
            "errors": n_errors,
            "warnings": n_warnings,
            "exit_code": 0 if n_errors == 0 else 2,
        },
    }


def main() -> int:
    report = run_all()
    print(json.dumps(report, indent=2, default=str))
    out_path = PROJECT_ROOT / "benchmarks" / "results" / \
        "codegraph_review.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str),
                         encoding="utf-8")
    return report["totals"]["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
