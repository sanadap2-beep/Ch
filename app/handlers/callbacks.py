"""Backwards-compatible re-export of :mod:`app.callbacks`.

The factories live in ``app/callbacks.py`` (a leaf module) so that
``app.keyboards`` can import them without going through ``app.handlers`` —
which would otherwise create a circular import at package init time.
"""
from __future__ import annotations

from app.callbacks import (
    AdminCB,
    AdminUserCB,
    CoachCB,
    ConsultCB,
    ForcedCB,
    MenuCB,
    OnboardingCB,
    PlanCB,
    PointsCB,
    PrivacyCB,
    ProgressCB,
    ReferralCB,
    ReminderCB,
    ScanCB,
    SettingsCB,
)

__all__ = [
    "AdminCB", "AdminUserCB", "CoachCB", "ConsultCB", "ForcedCB", "MenuCB", "OnboardingCB",
    "PlanCB", "PointsCB", "PrivacyCB", "ProgressCB", "ReferralCB", "ReminderCB", "ScanCB",
    "SettingsCB",
]
