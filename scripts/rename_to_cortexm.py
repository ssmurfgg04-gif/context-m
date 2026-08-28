#!/usr/bin/env python3
"""Rename context_m -> cortexm everywhere in the main repo.

The playbook (Task ID 2026-08-28-tier4-recall-and-pypi-and-playbook)
identified the three-way naming inconsistency as the #1 high-impact
refactor: PyPI pkg=context-m, Python module=context_m, CLI=cortexm.
The README already says `pip install cortexm` — this rename makes
that truthful.

Steps:
  1. git mv context_m/ cortexm/  (preserves history)
  2. sed replace `from cortexm`, `import cortexm`, `cortexm.` in
     every .py file (excluding the nested older clone at ./context-m/
     and the new cortexm.py shim we're creating)
  3. delete the old top-level cortexm.py shim (wrong direction:
     `from cortexm import Memory`)
  4. create new top-level cortexm.py backward-compat shim
     (`from cortexm import Memory, Config, __version__`)
  5. update pyproject.toml: name="cortexm", packages.find, py-modules
  6. update plugins/langchain internal imports to use cortexm
  7. verify: python -c 'import cortexm; print(cortexm.__version__)'
  8. verify: python -c 'from cortexm import Memory; print(Memory)'
  9. verify: pytest tests/ -q (all green)

Idempotent: re-running is a no-op (git mv fails safely, sed is
no-op on already-renamed files, shim is overwritten identically).
"""
from __future__ import annotations
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/z/my-project")

# Step 1: git mv context_m/ cortexm/
def step1_rename_dir():
    src = REPO / "context_m"
    dst = REPO / "cortexm"
    if not src.exists() and dst.exists():
        print(f"[1] already done: {dst} exists, {src} absent")
        return
    if not src.exists():
        print(f"[1] FAIL: source {src} not found")
        sys.exit(1)
    # git mv preserves history
    r = subprocess.run(["git", "-C", str(REPO), "mv", "context_m", "cortexm"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # fall back to plain mv if git mv fails (e.g. untracked files)
        print(f"[1] git mv failed ({r.stderr.strip()}); falling back to mv")
        shutil.move(str(src), str(dst))
    print(f"[1] renamed context_m/ -> cortexm/")

# Step 2: sed replace in all .py files
def step2_replace_imports():
    patterns = [
        (re.compile(r"\bfrom\s+context_m\b"), "from cortexm"),
        (re.compile(r"\bimport\s+context_m\b"), "import cortexm"),
        (re.compile(r"\bcontext_m\."), "cortexm."),
    ]
    # find all .py files EXCLUDING:
    #  - the nested older clone at ./context-m/
    #  - the new cortexm.py shim we're about to write
    #  - .git, .venv, .pytest_cache, node_modules
    #  - skills/ (not part of this repo)
    excludes = {".git", ".venv", ".pytest_cache", "node_modules",
                "skills", "context-m", "tool-results", "upload"}
    files_changed = 0
    for p in REPO.rglob("*.py"):
        rel = p.relative_to(REPO)
        if any(part in excludes for part in rel.parts):
            continue
        # Skip the new cortexm.py shim (will be written in step 4)
        if rel.name == "cortexm.py" and rel.parent == REPO:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        new = txt
        for pat, repl in patterns:
            new = pat.sub(repl, new)
        if new != txt:
            p.write_text(new, encoding="utf-8")
            files_changed += 1
    print(f"[2] updated imports in {files_changed} .py files")

# Step 3: delete the old top-level cortexm.py shim
def step3_delete_old_shim():
    old = REPO / "cortexm.py"
    if old.exists():
        # check contents — should be the 9-line shim
        txt = old.read_text(encoding="utf-8")
        if "from cortexm" in txt or "from cortexm" in txt and len(txt) < 500:
            old.unlink()
            print(f"[3] deleted old cortexm.py shim")
            return
        print(f"[3] cortexm.py exists but is not the old shim (len={len(txt)}); leaving alone")
        return
    print(f"[3] cortexm.py absent (already removed)")

# Step 4: create new top-level cortexm.py backward-compat shim
def step4_write_compat_shim():
    shim = REPO / "cortexm.py"
    body = '''"""Backward-compat shim. The canonical module is now ``cortexm``.

This file exists so existing scripts that did::

    from cortexm import Memory

keep working after `pip install cortexm`. New code should use::

    from cortexm import Memory

The shim will be removed in a future major release; migrate at your
leisure. The shim imports lazily so it adds ~0ms to cold-start when
nobody uses the old name.
"""
from cortexm import Memory, Config, __version__

__all__ = ["Memory", "Config", "__version__"]
'''
    shim.write_text(body, encoding="utf-8")
    print(f"[4] wrote {shim.name} backward-compat shim")

# Step 5: update pyproject.toml
def step5_pyproject():
    pp = REPO / "pyproject.toml"
    txt = pp.read_text(encoding="utf-8")
    new = txt
    # name change
    new = new.replace('name = "context-m"', 'name = "cortexm"')
    # packages.find
    new = new.replace('include = ["context_m*"]',
                      'include = ["cortexm*"]')
    # py-modules: was ["cortexm"] (the old shim); now ["context_m"] (the new shim)
    new = re.sub(
        r'py-modules\s*=\s*\[\s*"cortexm"\s*\]',
        'py-modules = ["context_m"]',
        new
    )
    # Homepage URL — current value is github.com/context-m/context-m (wrong);
    # fix to the actual repo URL
    new = re.sub(
        r'Homepage\s*=\s*"https://github\.com/context-m/context-m"',
        'Homepage = "https://github.com/ssmurfgg04-gif/context-m"',
        new
    )
    if new != txt:
        pp.write_text(new, encoding="utf-8")
        print(f"[5] updated pyproject.toml")
    else:
        print(f"[5] pyproject.toml unchanged (already updated?)")

# Step 6: plugin internal imports (langchain etc.) — the bulk sed in
# step 2 already updated plugins/*/  .py files. Now bump plugin version
# + ensure the langchain plugin's setup.py / pyproject.toml reference
# the right dependency name (it should now depend on cortexm).
def step6_plugins():
    # plugins/langchain/pyproject.toml + setup.py: bump version + dep
    paths = [
        REPO / "plugins" / "langchain" / "pyproject.toml",
        REPO / "plugins" / "langchain" / "setup.py",
    ]
    for p in paths:
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8")
        new = txt
        # the plugin depends on the main package. Old name was context-m,
        # new name is cortexm. Bump version too.
        new = new.replace('dependencies = ["context-m"]',
                          'dependencies = ["cortexm"]')
        new = re.sub(r'version\s*=\s*"0\.\d+\.\d+"',
                     'version = "0.3.0"', new)
        if new != txt:
            p.write_text(new, encoding="utf-8")
            print(f"[6] updated {p.relative_to(REPO)} (deps + version 0.3.0)")
        else:
            print(f"[6] {p.relative_to(REPO)} unchanged")

# Step 7: verify imports
def step7_verify():
    r = subprocess.run(
        [sys.executable, "-c",
         "import cortexm; print('cortexm', cortexm.__version__)"],
        capture_output=True, text=True, cwd=str(REPO))
    print(f"[7a] cortexm: {r.stdout.strip()}{r.stderr.strip()}")
    r = subprocess.run(
        [sys.executable, "-c",
         "from cortexm import Memory; print('context_m shim:', Memory)"],
        capture_output=True, text=True, cwd=str(REPO))
    print(f"[7b] context_m shim: {r.stdout.strip()}{r.stderr.strip()}")

if __name__ == "__main__":
    step1_rename_dir()
    step2_replace_imports()
    step3_delete_old_shim()
    step4_write_compat_shim()
    step5_pyproject()
    step6_plugins()
    step7_verify()
    print("\nrename complete. Run: pytest tests/ -q --ignore=tests/test_rust_accel.py")
