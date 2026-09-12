"""Progress facade: weigh-ins, tape measurements and the daily overview."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from app.db.models import User
from app.db.repositories import Repos
from app.i18n import t
from app.services.charts import combined_chart
from app.services.coach import CoachService
from app.services.metabolism import bmi, bmi_label, days_to_goal
from app.services.streak import StreakService
from app.utils.text import truncate

logger = logging.getLogger(__name__)


class ProgressService:
    def __init__(self, repos: Repos, coach: CoachService | None = None, streaks: StreakService | None = None) -> None:
        self.repos = repos
        self.coach = coach
        self.streaks = streaks or StreakService(repos)

    async def log_weight(self, user: User, weight_kg: float, *, lang: str = "ar") -> str:
        previous = await self.repos.tracking.latest_weight(user.id)
        first = await self._first_weight(user)
        await self.repos.tracking.record_weight(user, weight_kg)

        recalibration = None
        if self.coach is not None:
            recalibration = await self.coach.maybe_recalibrate(user)
        badges = await self.streaks.check_achievement_badges(user)
        streak = await self.streaks.register_activity(user)
        badges = list(dict.fromkeys([*badges, *streak.new_badges]))

        ar = lang.startswith("ar")
        delta = f"{weight_kg - float(previous.weight_kg):+.1f} كغ" if previous and ar else (
            f"{weight_kg - float(previous.weight_kg):+.1f} kg" if previous else "—"
        )
        total = f"{weight_kg - float(first):+.1f} كغ" if first and ar else (
            f"{weight_kg - float(first):+.1f} kg" if first else "—"
        )
        text = t(lang, "prog.weight_saved", weight=f"{weight_kg:.1f}", delta=delta, total=total)

        value = bmi(user.weight_kg, user.height_cm)
        if value:
            text += f"\n• BMI: {value} ({bmi_label(value, lang)})"
        if user.target_weight_kg:
            left = abs(float(user.weight_kg or 0) - float(user.target_weight_kg))
            eta = days_to_goal(float(user.weight_kg or 0), float(user.target_weight_kg))
            text += (
                f"\n• {'باقي للوصول للهدف' if ar else 'To your target'}: {left:.1f} كغ"
                + (f" (~{eta} {'يوم' if ar else 'days'} بوتيرة آمنة)" if eta else "")
            )
        if recalibration and recalibration.changed:
            text += (
                f"\n\n🔧 <b>{'تعديل تلقائي لخطة السعرات' if ar else 'Automatic calorie adjustment'}</b>\n"
                f"{recalibration.reason_ar}\n"
                f"{recalibration.old_calories} → <b>{recalibration.new_calories}</b> سعرة"
            )
        if badges:
            from app.services.streak import badge_label

            text += "\n\n" + "\n".join(
                t(lang, "streak.badge", badge=badge_label(b, lang)) for b in badges
            )
        return truncate(text, 3900)

    async def log_measurements(self, user: User, values: dict[str, float], *, lang: str = "ar") -> str:
        if not values:
            return t(lang, "common.error")
        record = await self.repos.tracking.record_measurements(user, values)
        history = await self.repos.tracking.measurement_history(user.id, limit=2)
        ar = lang.startswith("ar")
        labels = {
            "waist_cm": "الخصر" if ar else "Waist", "chest_cm": "الصدر" if ar else "Chest",
            "arm_cm": "الذراع" if ar else "Arm", "thigh_cm": "الفخذ" if ar else "Thigh",
            "hip_cm": "الورك" if ar else "Hip", "neck_cm": "الرقبة" if ar else "Neck",
            "calf_cm": "الربلة" if ar else "Calf", "shoulder_cm": "الكتف" if ar else "Shoulder",
            "body_fat_pct": "نسبة الدهون" if ar else "Body fat",
        }
        previous = history[1].as_dict() if len(history) > 1 else {}
        lines = []
        for key, value in record.as_dict().items():
            delta = ""
            if key in previous:
                difference = value - previous[key]
                delta = f" ({difference:+.1f})"
            lines.append(f"• {labels.get(key, key)}: <b>{value:g}</b> سم{delta}")
        await self.streaks.register_activity(user)
        return truncate(t(lang, "prog.measurements_saved", list="\n" + "\n".join(lines)), 3900)

    async def overview(self, user: User, *, lang: str = "ar", days: int = 30) -> str:
        ar = lang.startswith("ar")
        end = user.local_today()
        start = end - timedelta(days=days)
        summary = await self.repos.tracking.summary_between(user.id, start, end)
        workouts = await self.repos.plans.workout_stats(user.id, days=days)
        trend = await self.repos.tracking.weight_trend(user.id, weeks=4)
        measurements = await self.repos.tracking.measurement_history(user.id, limit=2)

        lines = [t(lang, "prog.title")]
        lines.append(
            f"⚖️ {'الوزن' if ar else 'Weight'}: {user.weight_kg or '—'} كغ | "
            f"{'الاتجاه' if ar else 'Trend'}: {trend.get('per_week_kg', 0):+.2f} كغ/أسبوع "
            f"({trend.get('samples', 0)} {'قياس' if ar else 'samples'})"
        )
        if measurements:
            latest = measurements[0].as_dict()
            text = "، ".join(f"{k.replace('_cm', '')}: {v:g}" for k, v in list(latest.items())[:5])
            lines.append(f"📏 {'قياسات' if ar else 'Measurements'} ({measurements[0].recorded_on}): {text}")
        lines.append(
            f"🍽️ {'متوسط السعرات' if ar else 'Avg kcal'}: {summary.get('avg_calories', 0)}/"
            f"{summary.get('avg_target', 0)} | {'بروتين' if ar else 'protein'}: {summary.get('avg_protein', 0)} جم"
        )
        lines.append(
            f"🏋️ {'تمارين' if ar else 'Workouts'}: {workouts.get('completed', 0)}/{workouts.get('planned', 0)} "
            f"({workouts.get('completion_rate', 0)}%) | {'التزام' if ar else 'adherence'}: {summary.get('adherence', 0)}%"
        )
        lines.append(self.streaks.summary(user, lang))
        if self.coach is not None:
            log = await self.repos.tracking.daily_log(user, end)
            lines.append("\n" + self.coach.build_day_context(user, log, trend=trend, lang=lang))
        return truncate("\n".join(lines), 3900)

    async def combined_chart(self, user: User, *, lang: str = "ar", days: int = 60) -> bytes:
        weights = await self.repos.tracking.weight_history(user.id, days=days)
        logs = list(await self.repos.tracking.last_logs(user.id, days=days))
        pairs = [(w.recorded_on, float(w.weight_kg)) for w in weights]
        return combined_chart(weights=pairs, logs=logs, target_kg=user.target_weight_kg, lang=lang)

    async def _first_weight(self, user: User) -> float | None:
        from sqlalchemy import select

        from app.db.models import WeightRecord

        row = (
            await self.repos.session.execute(
                select(WeightRecord.weight_kg)
                .where(WeightRecord.user_id == user.id)
                .order_by(WeightRecord.recorded_on)
                .limit(1)
            )
        ).scalar_one_or_none()
        return float(row) if row else None


def _unused(_: Any) -> None:  # pragma: no cover — keeps linters quiet about imports
    return None
