"""Progress charts rendered to PNG bytes (spec §5.5) and sent inside the bot.

Arabic labels are reshaped + bidi-reordered before drawing, because matplotlib
does not do text shaping itself.  DejaVu Sans (bundled with matplotlib) covers
the Arabic presentation forms, so charts stay readable without extra fonts.
"""
from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

logger = logging.getLogger(__name__)

_DARK = "#0f172a"
_ACCENT = "#22c55e"
_ACCENT2 = "#38bdf8"
_WARN = "#f59e0b"
_GRID = "#e2e8f0"


def _arabic(text: str) -> str:
    """Reshape + reorder Arabic so matplotlib draws it correctly."""
    if not text:
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001 — fall back to raw text
        return text


def _font(lang: str) -> str | None:
    """Prefer a bundled Arabic TTF, else matplotlib's DejaVu Sans."""
    from app.config import settings

    for candidate in (settings.arabic_font_regular, "DejaVuSans.ttf"):
        if not candidate:
            continue
        from pathlib import Path

        path = Path(candidate)
        if not path.is_absolute():
            path = settings.fonts_dir / candidate
        if path.exists():
            try:
                font_manager.fontManager.addfont(str(path))
                return font_manager.FontProperties(fname=str(path)).get_name()
            except Exception:  # noqa: BLE001
                logger.debug("font registration failed for %s", path, exc_info=True)
    return None


def _label(text: str, lang: str) -> str:
    return _arabic(text) if lang.startswith("ar") else text


def _finish(fig: Any) -> bytes:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
#  charts
# ═══════════════════════════════════════════════════════════════════════════
def weight_chart(
    records: Sequence[tuple[date, float]],
    *,
    target_kg: float | None = None,
    lang: str = "ar",
    title: str | None = None,
    start_kg: float | None = None,
) -> bytes:
    fig, axis = plt.subplots(figsize=(8, 4.2))
    if not records:
        axis.text(0.5, 0.5, _label("لا توجد بيانات وزن بعد", lang), ha="center", va="center", fontsize=14)
        axis.axis("off")
        return _finish(fig)

    days = [r[0] for r in records]
    weights = [float(r[1]) for r in records]

    axis.plot(days, weights, color=_ACCENT, linewidth=2.2, marker="o", markersize=4,
              label=_label("الوزن", lang))
    # 7-point moving average — the honest signal
    if len(weights) >= 5:
        window = 5
        averages = [
            sum(weights[max(0, i - window + 1) : i + 1]) / len(weights[max(0, i - window + 1) : i + 1])
            for i in range(len(weights))
        ]
        axis.plot(days, averages, color=_ACCENT2, linewidth=1.6, linestyle="--",
                  label=_label("متوسط متحرك", lang))
    if target_kg:
        axis.axhline(float(target_kg), color=_WARN, linewidth=1.4, linestyle=":",
                     label=f"{_label('الهدف', lang)} {target_kg:g} kg")
    if start_kg:
        axis.axhline(float(start_kg), color="#94a3b8", linewidth=1.0, linestyle="-.", alpha=0.7)

    axis.set_title(_label(title or "تطور الوزن", lang), fontsize=15, fontweight="bold")
    axis.set_ylabel(_label("كيلوغرام", lang))
    axis.grid(True, color=_GRID, linewidth=0.7)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    axis.legend(loc="best", fontsize=9)

    delta = weights[-1] - weights[0]
    axis.text(
        0.015, 0.03,
        _label(f"التغيّر: {delta:+.1f} كغ | الحالي: {weights[-1]:.1f} كغ", lang),
        transform=axis.transAxes, fontsize=9.5, color="#334155",
    )
    fig.autofmt_xdate(rotation=0, ha="center")
    return _finish(fig)


def calories_chart(
    logs: Sequence[Any], *, lang: str = "ar", days: int = 21
) -> bytes:
    fig, axis = plt.subplots(figsize=(8, 4.2))
    rows = [log for log in logs if log.target_calories][-days:]
    if not rows:
        axis.text(0.5, 0.5, _label("لا توجد سجلات سعرات بعد", lang), ha="center", va="center", fontsize=14)
        axis.axis("off")
        return _finish(fig)

    days_axis = [log.log_date for log in rows]
    consumed = [int(log.consumed_calories or 0) for log in rows]
    targets = [int(log.target_calories or 0) for log in rows]

    axis.bar(days_axis, consumed, color=_ACCENT, alpha=0.85, width=0.7,
             label=_label("المستهلك", lang))
    axis.plot(days_axis, targets, color="#0f172a", linewidth=1.6, marker="_", markersize=14,
              label=_label("الهدف", lang))
    axis.set_title(_label("السعرات اليومية", lang), fontsize=15, fontweight="bold")
    axis.set_ylabel(_label("سعرة", lang))
    axis.grid(True, axis="y", color=_GRID, linewidth=0.7)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    axis.legend(loc="best", fontsize=9)

    average = sum(consumed) / len(consumed)
    average_target = sum(targets) / len(targets)
    axis.text(
        0.015, 0.95,
        _label(f"متوسطك: {average:.0f} سعرة | الهدف: {average_target:.0f} | الفرق: {average - average_target:+.0f}", lang),
        transform=axis.transAxes, fontsize=9.5, va="top", color="#334155",
    )
    fig.autofmt_xdate(rotation=0, ha="center")
    return _finish(fig)


def adherence_chart(logs: Sequence[Any], *, lang: str = "ar", days: int = 30) -> bytes:
    fig, axis = plt.subplots(figsize=(8, 3.6))
    rows = [log for log in logs if log.adherence_score is not None][-days:]
    if not rows:
        axis.text(0.5, 0.5, _label("لا توجد بيانات التزام بعد", lang), ha="center", va="center", fontsize=14)
        axis.axis("off")
        return _finish(fig)
    axis.fill_between(
        [log.log_date for log in rows], [float(log.adherence_score or 0) for log in rows],
        color=_ACCENT2, alpha=0.35,
    )
    axis.plot([log.log_date for log in rows], [float(log.adherence_score or 0) for log in rows],
              color=_ACCENT2, linewidth=2)
    axis.axhline(60, color=_WARN, linewidth=1.2, linestyle=":", label=_label("حد الالتزام", lang))
    axis.set_ylim(0, 100)
    axis.set_title(_label("نسبة الالتزام اليومي", lang), fontsize=14, fontweight="bold")
    axis.set_ylabel("%")
    axis.grid(True, color=_GRID, linewidth=0.7)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    axis.legend(loc="lower right", fontsize=9)
    return _finish(fig)


def measurements_chart(
    history: Sequence[Any], *, keys: Iterable[str] = ("waist_cm", "arm_cm", "chest_cm"), lang: str = "ar"
) -> bytes | None:
    labels = {
        "waist_cm": "الخصر", "chest_cm": "الصدر", "arm_cm": "الذراع",
        "thigh_cm": "الفخذ", "hip_cm": "الورك", "neck_cm": "الرقبة",
        "calf_cm": "الربلة", "shoulder_cm": "الكتف",
    }
    series: dict[str, list[tuple[date, float]]] = {}
    for row in history:
        for key in keys:
            value = getattr(row, key, None)
            if value:
                series.setdefault(key, []).append((row.recorded_on, float(value)))
    series = {k: sorted(v) for k, v in series.items() if len(v) >= 2}
    if not series:
        return None

    fig, axis = plt.subplots(figsize=(8, 4.0))
    colours = [_ACCENT, _ACCENT2, _WARN, "#a855f7", "#ef4444", "#14b8a6"]
    for index, (key, points) in enumerate(series.items()):
        axis.plot([p[0] for p in points], [p[1] for p in points],
                  linewidth=2, color=colours[index % len(colours)], marker="o", markersize=4,
                  label=_label(f"{labels.get(key, key)} ({points[-1][1] - points[0][1]:+.1f} سم)", lang))
    axis.set_title(_label("تطور القياسات", lang), fontsize=15, fontweight="bold")
    axis.set_ylabel(_label("سنتيمتر", lang))
    axis.grid(True, color=_GRID, linewidth=0.7)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    axis.legend(loc="best", fontsize=9)
    return _finish(fig)


def combined_chart(
    *,
    weights: Sequence[tuple[date, float]],
    logs: Sequence[Any],
    target_kg: float | None = None,
    lang: str = "ar",
) -> bytes:
    """Weight + calories on two stacked panels — one image, the whole story."""
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(8, 6.6), sharex=False)
    if weights:
        days = [w[0] for w in weights]
        values = [float(w[1]) for w in weights]
        top.plot(days, values, color=_ACCENT, linewidth=2.2, marker="o", markersize=4)
        if target_kg:
            top.axhline(float(target_kg), color=_WARN, linestyle=":", linewidth=1.3)
        top.set_title(_label("الوزن", lang), fontsize=13, fontweight="bold")
        top.set_ylabel("kg")
        top.grid(True, color=_GRID, linewidth=0.7)
        top.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    else:
        top.text(0.5, 0.5, _label("لا توجد أوزان", lang), ha="center")
        top.axis("off")

    rows = [log for log in logs if log.target_calories][-30:]
    if rows:
        bottom.bar([log.log_date for log in rows], [int(log.consumed_calories or 0) for log in rows],
                   color=_ACCENT2, alpha=0.85, width=0.7, label=_label("مستهلك", lang))
        bottom.plot([log.log_date for log in rows], [int(log.target_calories or 0) for log in rows],
                    color=_DARK, linewidth=1.4, label=_label("هدف", lang))
        bottom.set_title(_label("السعرات", lang), fontsize=13, fontweight="bold")
        bottom.set_ylabel(_label("سعرة", lang))
        bottom.grid(True, axis="y", color=_GRID, linewidth=0.7)
        bottom.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        bottom.legend(fontsize=8, loc="best")
    else:
        bottom.text(0.5, 0.5, _label("لا توجد سجلات أكل", lang), ha="center")
        bottom.axis("off")

    fig.tight_layout()
    return _finish(fig)


def streak_calendar(streak_days: Sequence[date], *, lang: str = "ar", weeks: int = 12) -> bytes:
    """GitHub-style activity heatmap of adherence days."""
    import numpy as np

    today = date.today()
    start = today - timedelta(weeks=weeks)
    day_set = set(streak_days)
    fig, axis = plt.subplots(figsize=(9, 2.2))
    grid = np.zeros((7, weeks + 1))
    cursor = start - timedelta(days=start.weekday())
    column = 0
    while cursor <= today:
        row = cursor.weekday()
        if cursor >= start:
            grid[row, min(column, grid.shape[1] - 1)] = 1.0 if cursor in day_set else 0.15
        cursor += timedelta(days=1)
        if row == 6:
            column += 1
    axis.imshow(grid, aspect="auto", cmap="YlGn", vmin=0, vmax=1)
    axis.set_xticks([])
    axis.set_yticks(range(7), [_label(day, lang) for day in ["ن", "ث", "ر", "خ", "ج", "س", "ح"]]
              if lang.startswith("ar") else ["M", "T", "W", "T", "F", "S", "S"], fontsize=8)
    axis.set_title(_label("أيام الالتزام", lang), fontsize=13, fontweight="bold")
    return _finish(fig)
