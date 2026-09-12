"""Dependency audit: every third-party import must be a declared requirement.

A module that is only present *transitively* (numpy via matplotlib, aiohttp via
aiogram) works on the author's machine and breaks on the next fresh install when
the parent library drops or renames it. This walks the AST of ``app/`` and
``tests/`` and checks each import against ``requirements*.txt``.
"""
from __future__ import annotations

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STDLIB = set(sys.stdlib_module_names)
FIRST_PARTY = {"app", "tests", "migrations", "scripts", "alembic"}

# Import names that intentionally need no distribution (local/conditional).
IGNORED = {"__future__"}


def _python_files() -> list[Path]:
    files = [p for p in (ROOT / "app").rglob("*.py") if "__pycache__" not in str(p)]
    files += [p for p in (ROOT / "tests").rglob("*.py") if "__pycache__" not in str(p)]
    env = ROOT / "migrations" / "env.py"
    if env.exists():
        files.append(env)
    return files


def _imported_modules() -> dict[str, set[str]]:
    """top-level import name → the files that use it."""
    used: dict[str, set[str]] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in STDLIB or name in FIRST_PARTY or name in IGNORED:
                    continue
                used.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return used


def _declared_distributions() -> set[str]:
    declared: set[str] = set()
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            package = re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0].strip()
            if package:
                declared.add(re.sub(r"[-_.]+", "-", package).lower())
    return declared


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_requirements_files_parse_and_pin_something() -> None:
    declared = _declared_distributions()
    assert "aiogram" in declared and "sqlalchemy" in declared
    assert "pytest" in declared and "ruff" in declared

    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for critical in ("aiogram", "SQLAlchemy", "asyncpg", "alembic", "httpx", "APScheduler",
                     "matplotlib", "reportlab", "pillow", "boto3",
                     "arabic-reshaper", "python-bidi", "aiohttp", "numpy"):
        pattern = rf"^{re.escape(critical)}(\[[^\]]*\])?\s*[=<>~!]"
        assert re.search(pattern, runtime, re.MULTILINE | re.IGNORECASE), (
            f"{critical} is not pinned in requirements.txt"
        )


def test_every_third_party_import_is_declared() -> None:
    """Catches both typos and reliance on transitive dependencies."""
    mapping = packages_distributions()
    declared = _declared_distributions()

    undeclared: list[str] = []
    for module, files in sorted(_imported_modules().items()):
        distributions = mapping.get(module)
        if not distributions:
            # Not installed at all — either a typo or a genuinely missing dependency.
            undeclared.append(f"{module} (imported by {sorted(files)[0]}) — not installed")
            continue
        if not any(_normalise(dist) in declared for dist in distributions):
            undeclared.append(
                f"{module} → {'/'.join(distributions)} (imported by {sorted(files)[0]}) "
                f"— only available transitively"
            )

    assert not undeclared, "undeclared dependencies:\n  " + "\n  ".join(undeclared)


def test_no_requirement_is_unused() -> None:
    """The reverse direction: a pinned package nothing imports is dead weight.

    Packages used only through configuration or as plugins are listed explicitly.
    """
    indirect = {
        "aiosqlite",          # SQLAlchemy driver, selected by DATABASE_URL
        "asyncpg",            # SQLAlchemy driver, selected by DATABASE_URL
        "alembic",            # CLI + migrations/env.py
        "greenlet",           # required by SQLAlchemy asyncio
        "pydantic-settings",  # imported as pydantic_settings — covered below
        "python-bidi",        # imported as `bidi`
        "arabic-reshaper",    # imported as `arabic_reshaper`
        "fonttools",          # matplotlib font handling
        "pytest-asyncio",     # pytest plugin
        "ruff",               # linter CLI
        "pyyaml",             # imported as `yaml`
        "pillow",             # imported as `PIL`
        "sqlalchemy",
        "apscheduler",
        "reportlab",
        "matplotlib",
        "boto3",
        "aiohttp",
        "numpy",
        "aiogram",
        "httpx",
        "pydantic",
        "pytest",
    }
    declared = _declared_distributions()
    mapping = packages_distributions()

    imported_dists = set()
    for module in _imported_modules():
        for dist in mapping.get(module, []):
            imported_dists.add(_normalise(dist))

    unused = sorted(
        pkg for pkg in declared
        if pkg not in imported_dists and pkg not in {_normalise(p) for p in indirect}
    )
    assert not unused, f"pinned but never imported (add to `indirect` or remove): {unused}"


def test_dev_only_packages_are_not_in_runtime_requirements() -> None:
    """Shipping pytest/ruff to production would be pointless attack surface."""
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for dev_only in ("pytest", "ruff", "PyYAML"):
        assert not re.search(rf"^{dev_only}\s*[=<>~!]", runtime, re.MULTILINE), (
            f"{dev_only} belongs in requirements-dev.txt, not requirements.txt"
        )
