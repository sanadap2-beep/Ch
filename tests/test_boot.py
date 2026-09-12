"""Boot-path tests: ``app/main.py`` bootstrap, shutdown and webhook mode.

``bootstrap()``/``shutdown()`` are what run on every deploy, and webhook mode is a
documented production option — neither had ever been executed. The webhook test
starts the real aiohttp server and posts a signed update through it.
"""
from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app import main as app_main
from app.config import settings
from app.state import state as runtime
from tests.telegram_mock import MockedSession, make_bot, text_update


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest_asyncio.fixture
async def mocked_create_bot(monkeypatch: pytest.MonkeyPatch, session):
    """``bootstrap()`` builds its own Bot — give it one with the mocked transport."""
    bot, api = make_bot()

    def factory(token: str | None = None):
        return bot

    monkeypatch.setattr(app_main, "create_bot", factory)
    yield SimpleNamespace(bot=bot, api=api)
    runtime.bot = None
    runtime.started_at = None


# ═══════════════════════════════════════════════════════════════════════════
#  bootstrap / shutdown
# ═══════════════════════════════════════════════════════════════════════════
async def test_bootstrap_creates_bot_commands_and_dispatcher(mocked_create_bot) -> None:
    bot, dispatcher = await app_main.bootstrap()

    assert bot is mocked_create_bot.bot
    assert runtime.bot is bot
    assert runtime.bot_username == "ai_coach_bot"          # from the mocked getMe
    assert runtime.started_at is not None

    # the dispatcher is fully wired
    assert dispatcher.sub_routers
    total = sum(len(r.message.handlers) + len(r.callback_query.handlers)
                for r in dispatcher.sub_routers)
    assert total > 100

    # bot identity/commands were registered in both languages
    api = mocked_create_bot.api
    assert api.by_name("getMe")
    assert len(api.by_name("setMyCommands")) >= 3           # ar, en and the default scope
    assert api.by_name("setMyName") and api.by_name("setMyDescription")

    await bot.session.close()


async def test_bootstrap_fails_with_an_actionable_message_without_a_token(
    monkeypatch: pytest.MonkeyPatch, session
) -> None:
    monkeypatch.setattr(settings, "bot_token", "", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        await app_main.bootstrap()
    message = str(excinfo.value)
    assert "BOT_TOKEN" in message and ".env" in message


async def test_shutdown_closes_everything_in_order(mocked_create_bot, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_jobs() -> None:
        calls.append("scheduler")

    async def fake_ai() -> None:
        calls.append("ai_client")

    async def fake_db() -> None:
        calls.append("db")

    monkeypatch.setattr(app_main, "shutdown_scheduler", fake_jobs)
    monkeypatch.setattr(app_main, "close_client", fake_ai)
    monkeypatch.setattr(app_main, "dispose_engine", fake_db)

    bot = mocked_create_bot.bot
    await app_main.shutdown(bot)

    assert calls == ["scheduler", "ai_client", "db"]        # jobs → AI → DB
    assert bot.session.closed is True


async def test_shutdown_survives_a_failing_bot_session(mocked_create_bot, monkeypatch) -> None:
    bot = mocked_create_bot.bot

    async def explode() -> None:
        raise RuntimeError("socket already gone")

    monkeypatch.setattr(bot.session, "close", explode)
    await app_main.shutdown(bot)                            # must not raise


# ═══════════════════════════════════════════════════════════════════════════
#  update-type coverage
# ═══════════════════════════════════════════════════════════════════════════
async def test_allowed_updates_covers_every_registered_observer(mocked_create_bot) -> None:
    """Telegram only sends update types listed in allowed_updates.

    A router handling an update type that is missing from the list would silently
    never receive anything — the handler exists but the bot never sees the event.
    """
    _bot, dispatcher = await app_main.bootstrap()
    handled = {
        name for name, observer in dispatcher.observers.items()
        if observer.handlers or any(r.observers.get(name) and r.observers[name].handlers
                                    for r in dispatcher.sub_routers)
    }
    allowed = set(app_main.ALLOWED_UPDATES)
    # "update" and "error" are internal aiogram observers, not Telegram update types
    missing = handled - allowed - {"update", "error", "telegram_event"}
    assert not missing, f"these update types have handlers but are not requested: {missing}"

    unused = allowed - handled
    assert not unused, f"requested from Telegram but nothing handles them: {unused}"
    await _bot.session.close()


# ═══════════════════════════════════════════════════════════════════════════
#  webhook mode
# ═══════════════════════════════════════════════════════════════════════════
async def test_webhook_server_handles_a_signed_update(mocked_create_bot, monkeypatch) -> None:
    """Start the real aiohttp webhook server and drive one update through it."""
    import aiohttp

    port = _free_port()
    secret = "test-webhook-secret"
    monkeypatch.setattr(settings, "webhook_url", "https://bot.example.com", raising=False)
    monkeypatch.setattr(settings, "webhook_path", "/webhook", raising=False)
    monkeypatch.setattr(settings, "webhook_secret", secret, raising=False)
    monkeypatch.setattr(settings, "webhook_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(settings, "webhook_port", port, raising=False)

    bot, dispatcher = await app_main.bootstrap()
    task = asyncio.create_task(app_main.run_webhook(bot, dispatcher))

    # wait for the server to come up
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            await asyncio.sleep(0.05)
    else:  # pragma: no cover — would mean the server never bound
        task.cancel()
        pytest.fail("the webhook server never started listening")

    api = mocked_create_bot.api
    try:
        # the webhook was registered with Telegram, carrying the secret token
        set_hooks = api.by_name("setWebhook")
        assert set_hooks, "run_webhook never called setWebhook"
        assert set_hooks[0].url == "https://bot.example.com/webhook"
        assert set_hooks[0].secret_token == secret
        assert set(set_hooks[0].allowed_updates) == set(app_main.ALLOWED_UPDATES)

        update = text_update("/start", tg_id=777555)
        payload = json.loads(update.model_dump_json(by_alias=True, exclude_none=True))

        async with aiohttp.ClientSession() as client:
            # a request without the secret must be rejected
            bad = await client.post(f"http://127.0.0.1:{port}/webhook", json=payload)
            assert bad.status in (401, 403), f"unsigned request was accepted ({bad.status})"

            good = await client.post(
                f"http://127.0.0.1:{port}/webhook", json=payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            )
            assert good.status == 200

        # the dispatcher really processed it: /start replied with the privacy policy
        for _ in range(60):
            if api.by_name("sendMessage"):
                break
            await asyncio.sleep(0.05)
        assert api.by_name("sendMessage"), "the webhook delivered nothing to the dispatcher"
        assert "الخصوصية" in api.texts("sendMessage")[0]
    finally:
        stop = runtime.extra.get("webhook_stop")
        if stop is not None:
            stop.set()
        await asyncio.wait_for(task, timeout=10)
        await bot.session.close()

    # leaving webhook mode cleans up after itself
    assert api.by_name("deleteWebhook")
    assert runtime.extra.get("webhook_stop") is not None


async def test_main_chooses_polling_when_no_webhook_url_is_set(mocked_create_bot, monkeypatch) -> None:
    monkeypatch.setattr(settings, "webhook_url", None, raising=False)
    chosen: list[str] = []

    async def fake_polling(bot, dispatcher):
        chosen.append("polling")

    async def fake_webhook(bot, dispatcher):
        chosen.append("webhook")

    monkeypatch.setattr(app_main, "run_polling", fake_polling)
    monkeypatch.setattr(app_main, "run_webhook", fake_webhook)
    async def fake_shutdown(bot) -> None:
        chosen.append("shutdown")

    monkeypatch.setattr(app_main, "start_scheduler", lambda: chosen.append("scheduler"))
    monkeypatch.setattr(app_main, "shutdown", fake_shutdown)
    monkeypatch.setattr(app_main, "configure_logging", lambda: None)

    await app_main.main()
    assert chosen == ["scheduler", "polling", "shutdown"]


async def test_main_chooses_webhook_when_a_url_is_configured(mocked_create_bot, monkeypatch) -> None:
    monkeypatch.setattr(settings, "webhook_url", "https://bot.example.com", raising=False)
    chosen: list[str] = []

    async def fake_polling(bot, dispatcher):
        chosen.append("polling")

    async def fake_webhook(bot, dispatcher):
        chosen.append("webhook")

    monkeypatch.setattr(app_main, "run_polling", fake_polling)
    monkeypatch.setattr(app_main, "run_webhook", fake_webhook)
    async def fake_shutdown(bot) -> None:
        chosen.append("shutdown")

    monkeypatch.setattr(app_main, "start_scheduler", lambda: chosen.append("scheduler"))
    monkeypatch.setattr(app_main, "shutdown", fake_shutdown)
    monkeypatch.setattr(app_main, "configure_logging", lambda: None)

    await app_main.main()
    assert chosen == ["scheduler", "webhook", "shutdown"]


async def test_main_shuts_down_even_when_the_transport_raises(mocked_create_bot, monkeypatch) -> None:
    """A polling crash must still run the shutdown path (jobs, AI client, DB engine)."""
    monkeypatch.setattr(settings, "webhook_url", None, raising=False)
    closed: list[str] = []

    async def exploding_polling(bot, dispatcher):
        raise RuntimeError("telegram is unreachable")

    monkeypatch.setattr(app_main, "run_polling", exploding_polling)
    async def fake_shutdown(bot) -> None:
        closed.append("shutdown")

    monkeypatch.setattr(app_main, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_main, "shutdown", fake_shutdown)
    monkeypatch.setattr(app_main, "configure_logging", lambda: None)

    with pytest.raises(RuntimeError):
        await app_main.main()
    assert closed == ["shutdown"]


def test_mocked_session_is_reusable() -> None:
    """Guard the harness itself: a fresh session records nothing until asked."""
    session = MockedSession()
    assert session.calls == []
    assert session.all_text() == ""
    _bot, api = make_bot()
    assert isinstance(api, MockedSession)
