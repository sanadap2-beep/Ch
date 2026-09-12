"""API cost & usage observability (spec §4 admin stats + §5.11 cost control)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import settings
from app.db.models import User
from app.db.repositories import Repos


@dataclass(slots=True)
class UserBudget:
    daily_points_spent: int
    monthly_points_spent: int
    daily_cap: int
    monthly_cap: int
    consult_left: int
    coach_left: int
    vision_left: int
    free_scans_left: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "daily_points_spent": self.daily_points_spent,
            "monthly_points_spent": self.monthly_points_spent,
            "daily_cap": self.daily_cap,
            "monthly_cap": self.monthly_cap,
            "consult_left": self.consult_left,
            "coach_left": self.coach_left,
            "vision_left": self.vision_left,
            "free_scans_left": self.free_scans_left,
        }


class UsageService:
    def __init__(self, repos: Repos) -> None:
        self.repos = repos

    async def platform(self, days: int = 7) -> dict[str, Any]:
        data = await self.repos.ai_usage.breakdown(days=days)
        data["today_cost_usd"] = float(await self.repos.ai_usage.today_cost())
        data["daily_cap_usd"] = settings.global_daily_usd_cap
        data["cap_used_pct"] = (
            round(100 * data["today_cost_usd"] / settings.global_daily_usd_cap, 1)
            if settings.global_daily_usd_cap
            else 0.0
        )
        return data

    async def for_user(self, user: User, days: int = 30) -> dict[str, Any]:
        await self.repos.economy.ensure_windows(user)
        stats = await self.repos.ai_usage.user_cost(user.id, days=days)
        stats.update(
            {
                "points_balance": int(user.points_balance or 0),
                "daily_points_spent": int(user.daily_points_spent or 0),
                "monthly_points_spent": int(user.monthly_points_spent or 0),
                "daily_consult_count": int(user.daily_consult_count or 0),
                "daily_coach_messages": int(user.daily_coach_messages or 0),
                "daily_vision_count": int(user.daily_vision_count or 0),
            }
        )
        return stats

    def budget_of(self, user: User) -> UserBudget:
        return UserBudget(
            daily_points_spent=int(user.daily_points_spent or 0),
            monthly_points_spent=int(user.monthly_points_spent or 0),
            daily_cap=settings.user_daily_points_cap,
            monthly_cap=settings.user_monthly_points_cap,
            consult_left=max(0, settings.consult_free_daily_limit - int(user.daily_consult_count or 0)),
            coach_left=max(0, settings.coach_daily_message_limit - int(user.daily_coach_messages or 0)),
            vision_left=max(0, settings.vision_daily_limit - int(user.daily_vision_count or 0)),
            free_scans_left=max(0, settings.food_scan_free_daily - int(user.daily_food_scans_free_used or 0)),
        )

    def format_admin(self, data: dict[str, Any], lang: str = "ar") -> str:
        ar = lang.startswith("ar")
        by_task = "\n".join(
            f"• {row['task']}: {row['calls']} نداء — ${row['cost_usd']:.4f} ({row['tokens']:,} توكن)"
            if ar
            else f"• {row['task']}: {row['calls']} calls — ${row['cost_usd']:.4f} ({row['tokens']:,} tokens)"
            for row in data.get("by_task", [])
        ) or ("—" if ar else "—")
        by_model = "\n".join(
            f"• <code>{row['model']}</code>: {row['calls']} — ${row['cost_usd']:.4f}"
            for row in data.get("by_model", [])
        ) or "—"
        by_day = "\n".join(
            f"• {row['date']}: {row['calls']} نداء — ${row['cost_usd']:.4f}"
            for row in data.get("by_day", [])
        )
        head = (
            f"<b>🤖 استهلاك API — آخر {data['days']} أيام</b>\n"
            f"• التكلفة الإجمالية: <b>${data['total_cost_usd']:.4f}</b>\n"
            f"• نداء اليوم: ${data.get('today_cost_usd', 0):.4f} من سقف ${data.get('daily_cap_usd', 0):.2f} "
            f"({data.get('cap_used_pct', 0)}%)\n"
            f"• عدد النداءات: {data['calls']} | أخطاء/تجاوزات: {data['errors']}\n\n"
            f"<b>حسب المهمة:</b>\n{by_task}\n\n<b>حسب الموديل:</b>\n{by_model}\n\n<b>يوميًا:</b>\n{by_day}"
            if ar
            else f"<b>🤖 API usage — last {data['days']} days</b>\n"
            f"• Total cost: <b>${data['total_cost_usd']:.4f}</b>\n"
            f"• Today: ${data.get('today_cost_usd', 0):.4f} of ${data.get('daily_cap_usd', 0):.2f} cap "
            f"({data.get('cap_used_pct', 0)}%)\n"
            f"• Calls: {data['calls']} | errors/fallbacks: {data['errors']}\n\n"
            f"<b>By task</b>\n{by_task}\n\n<b>By model</b>\n{by_model}\n\n<b>By day</b>\n{by_day}"
        )
        return head

    async def model_pricing_report(self) -> str:
        """Show the live routing + price table (useful to verify cost strategy)."""
        lines = ["<b>🧩 توزيع الموديلات والأسعار</b>"]
        from app.constants import AITask

        for task in AITask:
            models = settings.task_models(task)
            primary = models[0] if models else "—"
            price_in, price_out = settings.price_of(primary)
            lines.append(
                f"• {task.value}: <code>{primary}</code> — ${price_in}/M إدخال، ${price_out}/M إخراج"
            )
        return "\n".join(lines)

    def estimated_usd(self, tokens_in: int, tokens_out: int, model: str) -> Decimal:
        price_in, price_out = settings.price_of(model)
        return Decimal(str(round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)))
