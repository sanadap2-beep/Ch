"""Structured callback-data factories (aiogram 3 ``CallbackData``).

Every inline button in the bot is typed here, so callback parsing can never
break at runtime and admin actions always carry their target id.
"""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="m"):
    """Main-menu / navigation actions."""

    action: str                       # consult|coach|scan|progress|plan|points|referral|settings|admin|home
    arg: str = ""


class OnboardingCB(CallbackData, prefix="onb"):
    step: str                         # gender|activity|goal|equipment|budget|food|cancel|skip
    value: str = ""


class PrivacyCB(CallbackData, prefix="prv"):
    action: str                       # accept|decline|policy|export|delete_photos|delete_account|confirm_delete


class ForcedCB(CallbackData, prefix="fsub"):
    action: str = "check"


class ConsultCB(CallbackData, prefix="cns"):
    action: str                       # start|example|menu
    arg: str = ""


class CoachCB(CallbackData, prefix="coa"):
    action: str                       # unlock|confirm_unlock|status|today|skip_meal|workout_done|report|menu
    arg: str = ""


class ScanCB(CallbackData, prefix="scn"):
    action: str                       # start|today|manual|menu
    arg: str = ""


class ProgressCB(CallbackData, prefix="prg"):
    action: str                       # weight|measurements|photo|chart|overview|report_week|report_month|export|menu
    arg: str = ""


class PlanCB(CallbackData, prefix="pln"):
    action: str                       # generate|today|mark_done|view|next_phase|menu
    kind: str = ""


class PointsCB(CallbackData, prefix="pts"):
    action: str                       # balance|ledger|get|referral|subscription|menu
    page: int = 0


class ReferralCB(CallbackData, prefix="ref"):
    action: str                       # link|share|stats|menu


class SettingsCB(CallbackData, prefix="set"):
    action: str                       # language|reminders|profile|goal|timezone|privacy|menu
    arg: str = ""


class ReminderCB(CallbackData, prefix="rem"):
    kind: str
    action: str = "toggle"            # toggle|on|off|time
    arg: str = ""


class AdminCB(CallbackData, prefix="adm"):
    action: str                       # dash|stats|users|search|user|ban|unban|grant|deduct|profile|
                                      # bc|bc_send|ai|channels|config|audit|subs|models|caps|menu
    arg: str = ""
    page: int = 0


class AdminUserCB(CallbackData, prefix="adu"):
    action: str                       # card|ban|unban|grant|deduct|profile|sub|photos|ledger|back|search
    user_id: str
    arg: str = ""
    page: int = 0


class BadgeCB(CallbackData, prefix="bdg"):
    action: str = "list"
