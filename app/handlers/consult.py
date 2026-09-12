"""Free general consultation (spec §3.أ) with exercise video links."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ai.errors import AIBudgetExceeded, AIConfigurationError, AIError
from app.config import settings
from app.db.models import User
from app.db.repositories.economy import DailyLimitReached
from app.handlers.callbacks import ConsultCB, MenuCB
from app.handlers.states import ConsultFlow
from app.i18n import t
from app.keyboards import consult_examples, consult_menu
from app.services.context import Services
from app.utils.tg import answer_long, edit_or_send, send_long, typing

logger = logging.getLogger(__name__)

router = Router(name="consult")

MENU_LABELS = {"💬 الاستشارة العامة", "💬 General consult"}


async def ask(
    event: Message | CallbackQuery,
    question: str,
    *,
    user: User,
    lang: str,
    services: Services,
    state: FSMContext,
) -> None:
    """Shared path for button examples, typed questions and transcribed voice."""
    question = (question or "").strip()
    if not question:
        return
    await state.clear()
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    try:
        async with typing(event.bot, chat_id):
            result = await services.consult.ask(user, question, lang=lang)
    except DailyLimitReached:
        await answer_long(event, t(lang, "consult.limit", limit=settings.consult_free_daily_limit))
        return
    except AIBudgetExceeded:
        await answer_long(event, t(lang, "budget.hit"))
        return
    except AIConfigurationError as exc:
        logger.error("AI misconfigured: %s", exc)
        await answer_long(event, t(lang, "common.ai_no_key"))
        return
    except AIError as exc:
        logger.warning("consult AI error: %s", exc)
        await answer_long(event, t(lang, "common.ai_busy"))
        return

    await answer_long(
        event, result.text, reply_markup=consult_menu(lang, consult_examples(lang)),
        disable_web_page_preview=True,
    )


async def open_consult(event: Message | CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(ConsultFlow.question)
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, t(lang, "consult.intro"),
                           reply_markup=consult_menu(lang, consult_examples(lang)))
    else:
        await send_long(event.bot, event.chat.id, t(lang, "consult.intro"),
                        reply_markup=consult_menu(lang, consult_examples(lang)))


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(message: Message, user: User, lang: str, state: FSMContext) -> None:
    if not user.onboarding_done:
        await message.answer(t(lang, "onb.welcome"))
        return
    await open_consult(message, lang, state)


@router.callback_query(MenuCB.filter(F.action == "consult"))
async def menu_callback(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await open_consult(callback, lang, state)


@router.callback_query(ConsultCB.filter(F.action == "start"))
async def start_question(callback: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(ConsultFlow.question)
    await callback.answer(t(lang, "consult.ask_question"))


@router.callback_query(ConsultCB.filter(F.action == "example"))
async def use_example(
    callback: CallbackQuery, cb: ConsultCB, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    await ask(callback, cb.arg, user=user, lang=lang, services=services, state=state)


@router.message(ConsultFlow.question, F.text)
async def typed_question(
    message: Message, user: User, lang: str, services: Services, state: FSMContext
) -> None:
    await ask(message, message.text or "", user=user, lang=lang, services=services, state=state)
