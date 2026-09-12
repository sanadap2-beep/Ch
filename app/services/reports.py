"""Reports: weekly/monthly AI analysis + full-journey PDF export (spec §3.ب, §5.10)."""
from __future__ import annotations

import io
import logging
from datetime import timedelta
from typing import Any

from app.ai.service import AIService
from app.config import settings
from app.db.models import User, utcnow
from app.db.repositories import Repos
from app.services.charts import calories_chart, measurements_chart, weight_chart
from app.services.goal_registry import get_goal
from app.services.metabolism import bmi, bmi_label, days_to_goal
from app.utils.text import plain

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(self, repos: Repos, ai: AIService) -> None:
        self.repos = repos
        self.ai = ai

    # ── data collection ──────────────────────────────────────────────────────
    async def collect(self, user: User, days: int = 7) -> dict[str, Any]:
        end = user.local_today()
        start = end - timedelta(days=days - 1)
        weights = await self.repos.tracking.weight_history(user.id, days=days + 30)
        measurements = await self.repos.tracking.measurement_history(user.id, limit=12)
        summary = await self.repos.tracking.summary_between(user.id, start, end)
        workouts = await self.repos.plans.workout_stats(user.id, days=days)
        trend = await self.repos.tracking.weight_trend(user.id, weeks=max(2, days // 7))
        meals = await self.repos.tracking.meals_between(user.id, start, end)

        meal_rows = [
            {
                "date": str(m.entry_date),
                "name": m.meal_name or m.kind,
                "calories": int(m.calories or 0),
                "protein_g": float(m.protein_g or 0),
                "kind": m.kind,
            }
            for m in meals[-60:]
        ]
        weight_rows = [
            {"date": str(w.recorded_on), "kg": float(w.weight_kg)} for w in weights[-40:]
        ]
        measurement_rows = [
            {"date": str(m.recorded_on), **m.as_dict()} for m in measurements[:8]
        ]
        streak_days = await self.repos.tracking.streak_state(user.id)
        return {
            "period_days": days,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "weights": weight_rows,
            "measurements": measurement_rows,
            "nutrition": summary,
            "training": workouts,
            "trend": trend,
            "meals": meal_rows,
            "streak": streak_days,
            "plan": {
                "goal": get_goal(user.goal).label_ar,
                "calories": user.target_calories,
                "protein_g": user.target_protein_g,
                "carbs_g": user.target_carbs_g,
                "fats_g": user.target_fats_g,
                "water_ml": user.water_target_ml,
                "bmr": round(user.bmr) if user.bmr else None,
                "tdee": round(user.tdee) if user.tdee else None,
                "bmi": bmi(user.weight_kg, user.height_cm),
            },
            "adjustments": list(user.plan_adjustments or [])[-6:],
        }

    # ── AI reports ───────────────────────────────────────────────────────────
    async def periodic(self, user: User, *, period: str = "week", lang: str = "ar") -> str:
        days = 7 if period == "week" else 30
        stats = await self.collect(user, days=days)
        outcome = await self.ai.weekly_report(
            user,
            period=period,
            lang=lang,
            weights=stats["weights"],
            measurements=stats["measurements"],
            meals_summary=stats["nutrition"],
            workouts=stats["training"],
            stats={
                "trend": stats["trend"],
                "plan": stats["plan"],
                "streak": {"current": user.streak_current, "best": user.streak_best, **stats["streak"]},
                "adjustments": stats["adjustments"],
                "meals_sample": stats["meals"][-25:],
            },
        )
        from app.utils.text import normalise

        return normalise(outcome.text)

    async def weekly(self, user: User, lang: str = "ar") -> str:
        return await self.periodic(user, period="week", lang=lang)

    async def monthly(self, user: User, lang: str = "ar") -> str:
        return await self.periodic(user, period="month", lang=lang)

    # ── charts ───────────────────────────────────────────────────────────────
    async def charts(self, user: User, *, lang: str = "ar", days: int = 60) -> dict[str, bytes]:
        weights = await self.repos.tracking.weight_history(user.id, days=days)
        logs = list(await self.repos.tracking.last_logs(user.id, days=days))
        measurements = await self.repos.tracking.measurement_history(user.id, limit=20)
        out: dict[str, bytes] = {}
        pairs = [(w.recorded_on, float(w.weight_kg)) for w in weights]
        out["weight"] = weight_chart(pairs, target_kg=user.target_weight_kg, lang=lang,
                                     start_kg=pairs[0][1] if pairs else None)
        out["calories"] = calories_chart(logs, lang=lang)
        measurement_png = measurements_chart(measurements, lang=lang)
        if measurement_png:
            out["measurements"] = measurement_png
        return out

    # ── PDF export ───────────────────────────────────────────────────────────
    async def export_pdf(self, user: User, *, lang: str = "ar", include_charts: bool = True) -> bytes:
        stats = await self.collect(user, days=90)
        badges = await self.repos.economy.badges_of(user.id)
        ledger = await self.repos.economy.ledger(user.id, limit=40)
        plan = await self.repos.plans.any_active_plan(user.id)
        charts = await self.charts(user, lang=lang, days=90) if include_charts else {}
        return build_pdf(
            user=user,
            stats=stats,
            badges=[b.badge_key for b in badges],
            ledger=[(e.created_at, e.amount, e.reason, e.note) for e in ledger],
            plan_title=plan.title if plan else None,
            charts=charts,
            lang=lang,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  PDF builder (Arabic-aware)
# ═══════════════════════════════════════════════════════════════════════════
def _shape(text: str) -> str:
    """Reshape Arabic for reportlab (no shaping engine built in)."""
    if not text:
        return ""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(str(text)))
    except Exception:  # noqa: BLE001
        return str(text)


def _fonts() -> tuple[str, str]:
    """Return (regular, bold) registered font names."""
    from pathlib import Path

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates: list[tuple[str, str]] = []
    for name in (settings.arabic_font_regular, "NotoNaskhArabic-Regular.ttf", "Amiri-Regular.ttf"):
        if not name:
            continue
        path = Path(name)
        if not path.is_absolute():
            path = settings.fonts_dir / name
        if path.exists():
            candidates.append((str(path), str(path)))
    for system in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if Path(system).exists():
            candidates.append((system, system))
    if not candidates:
        return "Helvetica", "Helvetica-Bold"

    regular = candidates[0][0]
    bold = candidates[-1][0]
    try:
        pdfmetrics.registerFont(TTFont("CoachAR", regular))
        pdfmetrics.registerFont(TTFont("CoachAR-Bold", bold if bold != regular else regular))
        return "CoachAR", "CoachAR-Bold"
    except Exception:  # noqa: BLE001
        logger.warning("TTF registration failed, falling back to Helvetica", exc_info=True)
        return "Helvetica", "Helvetica-Bold"


def build_pdf(
    *,
    user: User,
    stats: dict[str, Any],
    badges: list[str],
    ledger: list[tuple[Any, int, str, str | None]],
    plan_title: str | None,
    charts: dict[str, bytes] | None = None,
    lang: str = "ar",
) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image as RLImage,
    )
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    charts = charts or {}
    regular, bold = _fonts()
    ar = lang.startswith("ar")
    align = TA_RIGHT if ar else TA_CENTER

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=styles["Title"], fontName=bold, fontSize=20, alignment=TA_CENTER)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName=bold, fontSize=13, alignment=align,
                        textColor=colors.HexColor("#0f172a"), spaceBefore=8, spaceAfter=4)
    body = ParagraphStyle("b", parent=styles["BodyText"], fontName=regular, fontSize=10, alignment=align,
                          leading=15)
    small = ParagraphStyle("s", parent=body, fontSize=8.5, textColor=colors.HexColor("#475569"))

    def L(text: str) -> str:
        return _shape(text) if ar else str(text)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, title="Coach report", author="AI Fitness Coach",
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
    )
    story: list[Any] = []

    story.append(Paragraph(L("تقرير الرحلة الكامل" if ar else "Full Journey Report"), title_style))
    story.append(Paragraph(L(f"أُعِدّ في {utcnow().strftime('%Y-%m-%d %H:%M')} UTC" if ar else
                             f"Generated {utcnow().strftime('%Y-%m-%d %H:%M')} UTC"), small))
    story.append(Spacer(1, 6 * mm))

    # profile
    profile_rows = [
        [L("الاسم" if ar else "Name"), str(user.full_name)],
        [L("المعرّف" if ar else "Telegram ID"), str(user.tg_id)],
        [L("العمر / الجنس" if ar else "Age / Sex"), f"{user.age or '—'} / {user.gender or '—'}"],
        [L("الطول / الوزن" if ar else "Height / Weight"), f"{user.height_cm or '—'} cm / {user.weight_kg or '—'} kg"],
        [L("الهدف" if ar else "Goal"), get_goal(user.goal).describe(lang)],
        [L("المعدات" if ar else "Equipment"), str(user.equipment or "—")],
        [L("النشاط" if ar else "Activity"), str(user.activity_level or "—")],
        [L("الإصابات/الحالات" if ar else "Injuries/Conditions"),
         ", ".join(str(i.get("area") if isinstance(i, dict) else i) for i in (user.injuries or [])) or "—"],
        [L("مسجل منذ" if ar else "Member since"), user.created_at.strftime("%Y-%m-%d")],
    ]
    story.append(Paragraph(L("الملف الشخصي" if ar else "Profile"), h2))
    story.append(_table(profile_rows, regular, bold, ar, [55 * mm, 115 * mm]))

    # targets
    plan_info = stats.get("plan") or {}
    target_rows = [
        [L("BMR" if ar else "BMR"), str(plan_info.get("bmr") or "—")],
        [L("TDEE" if ar else "TDEE"), str(plan_info.get("tdee") or "—")],
        [L("السعرات المستهدفة" if ar else "Target calories"), f"{plan_info.get('calories') or '—'} kcal"],
        [L("بروتين / كارب / دهون" if ar else "P / C / F"),
         f"{plan_info.get('protein_g') or 0} / {plan_info.get('carbs_g') or 0} / {plan_info.get('fats_g') or 0} g"],
        [L("الماء" if ar else "Water"), f"{plan_info.get('water_ml') or 0} ml"],
        [L("مؤشر كتلة الجسم" if ar else "BMI"),
         f"{plan_info.get('bmi') or '—'} ({bmi_label(plan_info.get('bmi'), lang)})"],
    ]
    if user.target_weight_kg:
        remaining_days = days_to_goal(float(user.weight_kg or 0), float(user.target_weight_kg))
        target_rows.append([L("الوزن الهدف" if ar else "Target weight"),
                            f"{user.target_weight_kg} kg" + (f" (~{remaining_days} يوم)" if remaining_days else "")])
    story.append(Paragraph(L("الخطة الغذائية الحالية" if ar else "Current nutrition plan"), h2))
    story.append(_table(target_rows, regular, bold, ar, [55 * mm, 115 * mm]))

    # progress summary
    nutrition = stats.get("nutrition") or {}
    training = stats.get("training") or {}
    trend = stats.get("trend") or {}
    summary_rows = [
        [L("الفترة" if ar else "Period"), f"{stats.get('start')} → {stats.get('end')}"],
        [L("أيام مسجلة" if ar else "Logged days"), str(nutrition.get("days", 0))],
        [L("متوسط السعرات" if ar else "Avg calories"),
         f"{nutrition.get('avg_calories', 0)} / {nutrition.get('avg_target', 0)} kcal"],
        [L("متوسط البروتين" if ar else "Avg protein"), f"{nutrition.get('avg_protein', 0)} g"],
        [L("متوسط الماء" if ar else "Avg water"), f"{nutrition.get('avg_water_ml', 0)} ml"],
        [L("التمارين المكتملة" if ar else "Workouts done"),
         f"{training.get('completed', 0)}/{training.get('planned', 0)} ({training.get('completion_rate', 0)}%)"],
        [L("متوسط الالتزام" if ar else "Avg adherence"), f"{nutrition.get('adherence', 0)}%"],
        [L("اتجاه الوزن" if ar else "Weight trend"),
         f"{trend.get('per_week_kg', 0)} kg/week ({trend.get('samples', 0)} قياس)"],
        [L("الالتزام المتتالي" if ar else "Streak"), f"{user.streak_current} يوم (أفضل: {user.streak_best})"],
    ]
    story.append(Paragraph(L("ملخص التقدم (90 يومًا)" if ar else "Progress summary (90 days)"), h2))
    story.append(_table(summary_rows, regular, bold, ar, [55 * mm, 115 * mm]))

    # charts
    for key, label in (("weight", "الوزن" if ar else "Weight"), ("calories", "السعرات" if ar else "Calories"),
                       ("measurements", "القياسات" if ar else "Measurements")):
        png = charts.get(key)
        if not png:
            continue
        story.append(Paragraph(L(label), h2))
        image = RLImage(io.BytesIO(png))
        ratio = 165 * mm / image.imageWidth
        image.drawWidth = 165 * mm
        image.drawHeight = image.imageHeight * ratio
        story.append(image)

    story.append(PageBreak())

    # weight history
    weight_rows = [[L("التاريخ" if ar else "Date"), L("الوزن (كغ)" if ar else "Weight (kg)")]]
    weight_rows += [[row["date"], f"{row['kg']:.1f}"] for row in (stats.get("weights") or [])[-30:]]
    if len(weight_rows) > 1:
        story.append(Paragraph(L("سجل الوزن" if ar else "Weight log"), h2))
        story.append(_table(weight_rows, regular, bold, ar, [85 * mm, 85 * mm], header=True))

    measurement_rows = stats.get("measurements") or []
    if measurement_rows:
        head = [L("التاريخ" if ar else "Date"), L("خصر" if ar else "Waist"), L("صدر" if ar else "Chest"),
                L("ذراع" if ar else "Arm"), L("فخذ" if ar else "Thigh")]
        rows = [head]
        for row in measurement_rows[:15]:
            rows.append([
                row["date"], str(row.get("waist_cm") or "—"), str(row.get("chest_cm") or "—"),
                str(row.get("arm_cm") or "—"), str(row.get("thigh_cm") or "—"),
            ])
        story.append(Paragraph(L("القياسات" if ar else "Measurements"), h2))
        story.append(_table(rows, regular, bold, ar, [40 * mm, 32 * mm, 32 * mm, 32 * mm, 32 * mm], header=True))

    # badges
    if badges:
        from app.services.streak import badge_label

        story.append(Paragraph(L("الشارات" if ar else "Badges"), h2))
        story.append(Paragraph(L("، ".join(badge_label(b, lang) for b in badges)), body))

    # points ledger
    if ledger:
        rows = [[L("التاريخ" if ar else "Date"), L("النقاط" if ar else "Points"),
                 L("السبب" if ar else "Reason"), L("ملاحظة" if ar else "Note")]]
        for created, amount, reason, note in ledger[:30]:
            rows.append([
                created.strftime("%Y-%m-%d %H:%M") if hasattr(created, "strftime") else str(created),
                f"{amount:+d}", str(reason), plain(str(note or ""))[:60],
            ])
        story.append(Paragraph(L("سجل النقاط" if ar else "Points ledger"), h2))
        story.append(_table(rows, regular, bold, ar, [38 * mm, 22 * mm, 40 * mm, 70 * mm], header=True))

    if plan_title:
        story.append(Paragraph(L("البرنامج الحالي" if ar else "Current programme"), h2))
        story.append(Paragraph(L(str(plan_title)), body))

    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        L(
            "هذا التقرير للتتبع الشخصي وليس تشخيصًا طبيًا. عند وجود ألم أو حالة صحية راجع طبيبًا."
            if ar
            else "This report is for personal tracking only and is not a medical diagnosis."
        ),
        small,
    ))

    doc.build(story)
    return buffer.getvalue()


def _table(
    rows: list[list[str]], regular: str, bold: str, ar: bool, widths: list[float], *, header: bool = False
) -> Any:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table, TableStyle

    align = TA_RIGHT if ar else 0
    cell = ParagraphStyle("cell", fontName=regular, fontSize=9, alignment=align, leading=12)
    cell_bold = ParagraphStyle("cellb", fontName=bold, fontSize=9, alignment=align, leading=12)

    data = [[Paragraph(_shape(str(c)) if ar else str(c), cell_bold if (header and r == 0) else cell)
             for c in row] for r, row in enumerate(rows)]
    table = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table
