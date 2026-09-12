# Fonts — الخطوط

Charts (`app/services/charts.py`) and PDF reports (`app/services/reports.py`) need an
**Arabic-capable** font. Resolution order:

1. `ARABIC_FONT_PATH` from `.env` (absolute path to a `.ttf`).
2. The first existing file in this folder (`assets/fonts/*.ttf`).
3. System fallbacks: `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`,
   `/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf`.

If nothing is found, charts still render but Arabic may appear as boxes.

## Recommended fonts

| Font | Use | Get it |
|---|---|---|
| **Noto Naskh Arabic** | best for reports/charts | `apt install fonts-noto-core` or <https://fonts.google.com/noto/specimen/Noto+Naskh+Arabic> |
| **Amiri** | elegant Naskh | `apt install fonts-hosny-amiri` |
| **Cairo / Tajawal** | modern UI look | <https://fonts.google.com> |
| DejaVu Sans | bundled fallback (limited Arabic) | already on most Linux images |

Install example:

```bash
cp /usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf assets/fonts/
echo 'ARABIC_FONT_PATH=/opt/ai-coach/assets/fonts/NotoNaskhArabic-Regular.ttf' >> .env
```

Licence check before shipping: use OFL-licensed fonts (all of the above are) and keep their
`OFL.txt` next to the `.ttf` if you redistribute them. The `.gitignore` in this folder keeps
binary font files out of the repository — commit only this README.
