"""Cross-platform developer tasks mirroring the Makefile targets.

``make`` is not available in every Windows shell, so each target also runs as
``python tasks.py <target>`` with only the standard library:

    run      start the dashboard on http://127.0.0.1:8080/
    test     run the full regression suite
    check    compile Python, syntax-check both JS files, then run the tests
    harvest  run a seven-day dataset harvest
    clean    remove __pycache__ directories and stray .pyc files
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON_SOURCES = ("backend", "run.py", "tests")
JS_FILES = ("frontend/preview.js", "frontend/app.js")
#: Never descend into environments or VCS folders while cleaning byte-code.
SKIP_DIRS = frozenset({".git", ".venv", "venv", "env", "node_modules"})


def _run(*command: str) -> None:
    """Run one command from the repository root, failing the task on error."""
    print("$ " + " ".join(command), flush=True)
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"command not found: {command[0]} ({exc})") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from None


def task_run() -> None:
    """Start the dashboard."""
    _run(sys.executable, "run.py")


def task_test() -> None:
    """Run the full regression suite."""
    _run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")


def task_check() -> None:
    """Compile Python, syntax-check the frontend, then run the tests."""
    _run(sys.executable, "-m", "compileall", "-q", *PYTHON_SOURCES)
    for js_file in JS_FILES:
        _run("node", "--check", js_file)
    task_test()


def task_harvest() -> None:
    """Run a seven-day dataset harvest."""
    _run(
        sys.executable, "run.py", "--harvest", "--days", "7", "--type", "dataset"
    )


def task_clean() -> None:
    """Delete byte-code caches only; no tracked file is ever touched."""
    removed = 0
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        if "__pycache__" in dirs:
            shutil.rmtree(Path(current) / "__pycache__", ignore_errors=True)
            dirs.remove("__pycache__")
            removed += 1
        for name in files:
            if name.endswith(".pyc"):
                (Path(current) / name).unlink(missing_ok=True)
    print(f"Removed {removed} __pycache__ director{'y' if removed == 1 else 'ies'}.")


TASKS = {
    "run": task_run,
    "test": task_test,
    "check": task_check,
    "harvest": task_harvest,
    "clean": task_clean,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", choices=sorted(TASKS))
    args = parser.parse_args(argv)
    TASKS[args.task]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
