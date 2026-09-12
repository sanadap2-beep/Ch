"""Configuration ↔ documentation drift guards.

These are cheap tests that catch the mistakes which are invisible at runtime:
a typo'd key in ``.env.example`` silently does nothing, a new setting nobody
documented silently keeps its default in production, and a real API key committed
by accident is a security incident.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

FIELD_NAMES = {name.upper() for name in Settings.model_fields}


def _keys_in(text: str, *, include_commented: bool = True) -> set[str]:
    """Every ``KEY=`` occurrence, optionally including commented-out examples."""
    pattern = r"^\s*(?:#\s*)?([A-Za-z0-9_]+)\s*=" if include_commented else r"^\s*([A-Za-z0-9_]+)\s*="
    return {m.group(1).upper() for line in text.splitlines() if (m := re.match(pattern, line))}


# ═══════════════════════════════════════════════════════════════════════════
#  .env.example ↔ Settings
# ═══════════════════════════════════════════════════════════════════════════
def test_every_env_example_key_maps_to_a_real_setting() -> None:
    """A typo here would be read by nobody — the operator thinks they configured it."""
    unknown = sorted(_keys_in(ENV_EXAMPLE) - FIELD_NAMES)
    assert not unknown, f".env.example documents settings that do not exist: {unknown}"


def test_every_setting_is_documented_in_env_example() -> None:
    """New knobs must be discoverable; commented-out lines count as documented."""
    undocumented = sorted(FIELD_NAMES - _keys_in(ENV_EXAMPLE))
    assert not undocumented, f"settings missing from .env.example: {undocumented}"


def test_active_env_example_values_are_not_real_secrets() -> None:
    """Copying .env.example must never ship something that looks like a live credential."""
    risky = {
        "BOT_TOKEN": re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{30,}$"),
        "NANOGPT_API_KEY": re.compile(r"^(sk-|ngpt-)[A-Za-z0-9]{16,}$"),
        "S3_SECRET_ACCESS_KEY": re.compile(r"^[A-Za-z0-9/+=]{30,}$"),
        "DATABASE_URL": re.compile(r"^postgresql(\+\w+)?://[^:]+:(?!coach|postgres|changeme)[^@]{8,}@"),
    }
    offenders: list[str] = []
    for line in ENV_EXAMPLE.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)     # uncommented only
        if not match:
            continue
        key, value = match.group(1).upper(), match.group(2).strip().strip('"').strip("'")
        if key in risky and value and risky[key].match(value):
            offenders.append(f"{key}={value[:12]}…")
    assert not offenders, f".env.example contains real-looking credentials: {offenders}"


def test_secret_settings_default_to_empty_not_to_a_value() -> None:
    """A missing credential must fail loudly, not silently authenticate as someone else."""
    defaults = Settings.model_fields
    for name in ("bot_token", "nanogpt_api_key"):
        default = defaults[name].default
        assert default in ("", None), f"{name} should default to empty, got {default!r}"


@pytest.mark.parametrize("field", ["webhook_url", "webhook_path", "webhook_secret",
                                   "webhook_host", "webhook_port"])
def test_webhook_settings_exist_and_are_documented(field: str) -> None:
    """Webhook mode is a documented deployment path — its knobs must survive refactors."""
    assert field in Settings.model_fields
    assert field.upper() in _keys_in(ENV_EXAMPLE)


@pytest.mark.parametrize("field", ["s3_bucket", "s3_region", "s3_endpoint_url",
                                   "s3_access_key_id", "s3_secret_access_key"])
def test_s3_settings_exist_and_are_documented(field: str) -> None:
    assert field in Settings.model_fields
    assert field.upper() in _keys_in(ENV_EXAMPLE)


# ═══════════════════════════════════════════════════════════════════════════
#  deployment artifacts stay coherent with the code
# ═══════════════════════════════════════════════════════════════════════════
def test_docker_compose_points_the_bot_at_the_db_service() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"db", "bot"} <= set(services)

    env = services["bot"].get("environment", {})
    database_url = env.get("DATABASE_URL", "")
    assert database_url.startswith("postgresql+asyncpg://"), database_url
    assert "@db:" in database_url, "the bot must reach postgres by service name, not localhost"
    assert env.get("DB_AUTO_CREATE") == "false", "migrations own the schema in production"
    assert services["bot"]["depends_on"]["db"]["condition"] == "service_healthy"


def test_dockerfile_installs_requirements_and_runs_the_app() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.txt" in dockerfile
    # CMD may be shell-form or exec-form; both must launch the app module
    assert re.search(r'CMD\s+(\[.*"app\.main".*\]|.*python\s+-m\s+app\.main)', dockerfile)
    assert "fonts-dejavu-core" in dockerfile, "Arabic charts/PDFs need a font in the image"
    assert re.search(r"^USER\s+\S+", dockerfile, re.MULTILINE), "the image should not run as root"


def test_ci_runs_both_lint_and_the_test_suite() -> None:
    """CI must not silently lose the checks this project depends on."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["quality"]["steps"]
    commands = " ".join(str(step.get("run", "")) for step in steps)

    assert "ruff check" in commands
    assert "pytest" in commands
    assert "alembic upgrade head" in commands
    assert "run.sh --check" in commands
    assert "python-version" in yaml.safe_load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )["jobs"]["quality"]["steps"][1]["with"]


def test_ci_does_not_need_secrets_to_run() -> None:
    """The suite is offline by design — CI must not depend on a live bot or API key."""
    workflow_text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "secrets." not in workflow_text, "CI should run on forks without credentials"


def test_documented_entrypoints_exist() -> None:
    """README/DEPLOYMENT reference these files; a rename must not orphan the docs."""
    for relative in ("scripts/setup.sh", "scripts/run.sh", "scripts/backup_db.sh",
                     "scripts/docker-entrypoint.sh", "alembic.ini", "requirements.txt",
                     "requirements-dev.txt", "docs/DEPLOYMENT.md", "docs/ADMIN_GUIDE.md",
                     "docs/ARCHITECTURE.md", "assets/fonts/README.md"):
        assert (ROOT / relative).exists(), f"missing documented file: {relative}"

    for script in ("scripts/setup.sh", "scripts/run.sh", "scripts/backup_db.sh",
                   "scripts/docker-entrypoint.sh"):
        path = ROOT / script
        assert path.stat().st_mode & 0o111, f"{script} is not executable"


# ═══════════════════════════════════════════════════════════════════════════
#  env parsing: the documented formats must actually load
# ═══════════════════════════════════════════════════════════════════════════
def _active_env_pairs(text: str) -> dict[str, str]:
    """Uncommented ``KEY=VALUE`` pairs, in file order."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        if m := re.match(r"^([A-Za-z0-9_]+)=(.*)$", line):
            pairs[m.group(1)] = m.group(2).strip()
    return pairs


def test_env_example_loads_as_a_real_env_file(tmp_path: Path) -> None:
    """`cp .env.example .env`, fill in the secrets, and the app must boot.

    Loaded as an env *file* — the path a real deploy takes — so inline comments
    are stripped by python-dotenv exactly as they are in production.

    This is the guard that would have caught the comma-separated list fields:
    pydantic-settings JSON-decodes a complex field's env value before validators
    run, so the documented ``ADMIN_IDS=111111111,222222222`` raised SettingsError
    and a single ``ADMIN_IDS=123`` crashed with "TypeError: 'int' object is not
    iterable" — an error that never mentions the offending variable.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_EXAMPLE, encoding="utf-8")

    settings = Settings(_env_file=env_file)  # must not raise

    assert settings.admin_ids == [111111111, 222222222]
    assert all(isinstance(i, int) for i in settings.admin_ids)
    # inline comments must not leak into the parsed values
    assert settings.protect_content is False
    assert settings.db_auto_create is True
    assert settings.support_username == "@your_support"
    assert "#" not in settings.bot_token


def test_env_example_pairs_are_all_known_settings() -> None:
    """The uncommented lines a user actually inherits must map to real fields."""
    unknown = {
        key for key in _active_env_pairs(ENV_EXAMPLE)
        if key.upper() not in FIELD_NAMES
    }
    assert not unknown, f".env.example sets keys that do nothing: {sorted(unknown)}"


@pytest.mark.parametrize(("env_var", "field", "value", "expected"), [
    ("ADMIN_IDS", "admin_ids", "123456789", [123456789]),
    ("ADMIN_IDS", "admin_ids", "111111111,222222222", [111111111, 222222222]),
    ("ADMIN_IDS", "admin_ids", " 1 , 2 ", [1, 2]),
    ("ADMIN_IDS", "admin_ids", "[1,2]", [1, 2]),
    ("ADMIN_IDS", "admin_ids", "", []),
    ("STREAK_MILESTONES", "streak_milestones", "7", [7]),
    ("STREAK_MILESTONES", "streak_milestones", "3,7,14", [3, 7, 14]),
    ("FORCED_CHANNELS", "forced_channels", "@chan,-100123", ["@chan", "-100123"]),
    ("FALLBACK_COACH", "fallback_coach", "a/b,c/d", ["a/b", "c/d"]),
    ("DEFAULT_PRICING", "default_pricing", "1.5,4.5", [1.5, 4.5]),
    ("MODEL_ROUTING", "model_routing", '{"consult":["x/y"]}', {"consult": ["x/y"]}),
    ("MODEL_PRICING", "model_pricing", '{"x/y":[1.0,2.0]}', {"x/y": [1.0, 2.0]}),
])
def test_complex_env_values_parse(
    monkeypatch: pytest.MonkeyPatch, env_var: str, field: str, value: str, expected: object
) -> None:
    """Both the comma-separated and the JSON spelling of every list/dict setting."""
    monkeypatch.setenv(env_var, value)
    assert getattr(Settings(_env_file=None), field) == expected


def test_every_complex_setting_has_a_no_decode_validator() -> None:
    """A NoDecode field without a validator rejects any env string outright."""
    from typing import get_origin

    import pydantic_settings

    complex_types = (list, dict, set)
    for name, field in Settings.model_fields.items():
        # pydantic unpacks Annotated: the inner type stays on .annotation and the
        # markers move to .metadata.
        if get_origin(field.annotation) not in complex_types:
            continue

        assert pydantic_settings.NoDecode in field.metadata, (
            f"{name} is a complex field without NoDecode: pydantic-settings will "
            f"JSON-decode its env value before validators run, breaking the "
            f"comma-separated form documented in .env.example"
        )

        validated = {
            v
            for decorator in Settings.__pydantic_decorators__.field_validators.values()
            for v in decorator.info.fields
        }
        assert name in validated, (
            f"{name} is NoDecode but has no mode='before' validator, so the raw "
            f"env string reaches pydantic and fails list/dict validation"
        )
