"""Router registry — order matters.

aiogram evaluates routers in registration order, so the most specific routers
(admin, gated flows, state-bound handlers) come first and the catch-all
``fallback`` router last.  ``media`` must come after ``scan``/``progress`` so
that photos sent *inside* those flows are handled by the flow, not by the
global food-scan shortcut.
"""
from __future__ import annotations

from aiogram import Router

from app.handlers import (
    admin,
    coach,
    consult,
    fallback,
    media,
    onboarding,
    plans,
    points,
    progress,
    scan,
    settings,
    start,
)

routers: list[Router] = [
    admin.router,
    start.router,
    onboarding.router,
    consult.router,
    coach.router,
    scan.router,
    progress.router,
    plans.router,
    points.router,
    settings.router,
    media.router,
    fallback.router,
]

__all__ = ["routers"]
