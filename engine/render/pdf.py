"""Single-model PDF renderer for Weekly Cash reports (pure ReportLab, zero PIL).

Implements the frozen signature:
- `render_weekly_pdfs(model: ReportModel, out_dir: Path) -> dict[str, Path]`
  producing `Weekly-Liquidity-Update.pdf` (1-2 pages portrait) and
  `13-Week-Cash-Flow.pdf` (strictly 1 page landscape).

All figures are read directly from `ReportModel` without recomputing business math.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
from xml.sax.saxutils import escape as xml_escape


def _ensure_reportlab_pillow_free() -> None:
    """Allow pure-vector/text ReportLab imports when Pillow is not installed."""
    if "PIL" not in sys.modules and importlib.util.find_spec("PIL") is None:
        pil_mod = types.ModuleType("PIL")
        pil_img = types.ModuleType("PIL.Image")
        setattr(pil_mod, "Image", pil_img)
        sys.modules["PIL"] = pil_mod
        sys.modules["PIL.Image"] = pil_img


_ensure_reportlab_pillow_free()

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from engine.model import CheckItem, KpiField, ReportModel, SourceRef

# ---------------------------------------------------------------------------
# Color palette (executive finance aesthetic)
# ---------------------------------------------------------------------------

NAVY = colors.HexColor("#162B4D")
NAVY_MID = colors.HexColor("#264573")
TEXT_DARK = colors.HexColor("#1E293B")
TEXT_MUTED = colors.HexColor("#64748B")
BORDER = colors.HexColor("#CBD5E1")
BORDER_DARK = colors.HexColor("#94A3B8")
SURFACE = colors.HexColor("#F8FAFC")
SURFACE_ALT = colors.HexColor("#F1F5F9")

BLUE_TEXT = colors.HexColor("#1E40AF")
BLUE_BG = colors.HexColor("#EFF6FF")
BLUE_BORDER = colors.HexColor("#93C5FD")

GREEN_TEXT = colors.HexColor("#166534")
GREEN_BG = colors.HexColor("#DCFCE7")
GREEN_BORDER = colors.HexColor("#86EFAC")

AMBER_TEXT = colors.HexColor("#92400E")
AMBER_BG = colors.HexColor("#FEF3C7")
AMBER_BORDER = colors.HexColor("#FCD34D")

RED_TEXT = colors.HexColor("#991B1B")
RED_BG = colors.HexColor("#FEE2E2")
RED_BORDER = colors.HexColor("#FCA5A5")

WHITE = colors.white

TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# Exact Decimal formatting helpers (zero float rounding drift)
# ---------------------------------------------------------------------------


def _to_decimal(value: float | int | str | Decimal | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def format_usd(value: float | int | str | Decimal | None, *, signed: bool = False) -> str:
    """Format a monetary amount with exact cent precision."""
    dec = _to_decimal(value)
    if dec is None:
        return "—"
    q = dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if q < 0:
        return f"-${abs(q):,.2f}"
    if signed and q > 0:
        return f"+${q:,.2f}"
    return f"${q:,.2f}"


def format_usd_compact(value: float | int | str | Decimal | None) -> str:
    """Compact axis label formatting (e.g. $1.84M or $450k)."""
    dec = _to_decimal(value)
    if dec is None:
        return "—"
    abs_val = abs(dec)
    sign = "-" if dec < 0 else ""
    if abs_val >= Decimal("1000000"):
        m = (abs_val / Decimal("1000000")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        return f"{sign}${m:.2f}M"
    if abs_val >= Decimal("1000"):
        k = (abs_val / Decimal("1000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return f"{sign}${k:,}k"
    return f"{sign}${abs_val.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def format_kpi_value(kpi: KpiField) -> str:
    """Format a KpiField identically across PDF and HTML surfaces."""
    if kpi.value is None:
        return "—"
    if kpi.unit == "text" or isinstance(kpi.value, str):
        return str(kpi.value)
    if kpi.unit == "USD":
        return format_usd(kpi.value)
    dec = _to_decimal(kpi.value)
    if dec is None:
        return str(kpi.value)
    if kpi.unit == "count":
        return f"{int(dec.quantize(Decimal('1'), rounding=ROUND_HALF_UP)):,}"
    if kpi.unit == "pct":
        q = dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if (q * 100) % 10 == 0:
            return f"{q:.1f}%"
        return f"{q:.2f}%"
    return str(kpi.value)


def format_comparison(kpi: KpiField) -> str:
    """Format optional KPI comparison line."""
    if kpi.comparison_value is None and not kpi.comparison_label:
        return ""
    label = kpi.comparison_label or kpi.comparison_kind.title()
    if kpi.comparison_value is None:
        return label
    if kpi.unit == "USD":
        val_str = format_usd(kpi.comparison_value)
    elif kpi.unit == "count":
        dec = _to_decimal(kpi.comparison_value)
        val_str = f"{int(dec):,}" if dec is not None else str(kpi.comparison_value)
    elif kpi.unit == "pct":
        dec = _to_decimal(kpi.comparison_value)
        val_str = f"{dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):.1f}%" if dec is not None else str(kpi.comparison_value)
    else:
        val_str = str(kpi.comparison_value)
    return f"{label}: {val_str}"


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return xml_escape(str(value))


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _edition_badge_text(model: ReportModel) -> tuple[str, colors.Color, colors.Color]:
    """Return (badge_text, text_color, bg_color) for draft/edition status."""
    meta = model.meta
    if meta.is_approved or meta.status == "approved":
        ed = meta.edition_number if meta.edition_number is not None else 1
        return (f"APPROVED · EDITION {ed}", GREEN_TEXT, GREEN_BG)
    return (f"DRAFT {meta.draft_number}", AMBER_TEXT, AMBER_BG)


def _extract_cash_floor(model: ReportModel) -> tuple[Decimal | None, str]:
    """Read minimum cash floor value and label directly from ReportModel."""
    min_kpi = model.kpi_map().get("min_cash_balance")
    if min_kpi and min_kpi.comparison_value is not None:
        dec = _to_decimal(min_kpi.comparison_value)
        label = min_kpi.comparison_label or "Minimum Cash Floor"
        if dec is not None:
            return dec, label
    raw_floor = model.sections.get("cash_floor")
    if raw_floor is not None:
        dec = _to_decimal(raw_floor)
        if dec is not None:
            return dec, "Minimum Cash Floor"
    assumptions = model.sections.get("assumptions")
    if isinstance(assumptions, dict):
        for candidate in (
            assumptions.get("cash_floor"),
            assumptions.get("cash_floor_usd"),
            (assumptions.get("weekly") or {}).get("cash_floor_usd")
            if isinstance(assumptions.get("weekly"), dict)
            else None,
        ):
            if candidate is not None:
                dec = _to_decimal(candidate)
                if dec is not None:
                    return dec, "Minimum Cash Floor"
    return None, "Minimum Cash Floor"


# ---------------------------------------------------------------------------
# Typography styles
# ---------------------------------------------------------------------------


def _build_styles() -> dict[str, ParagraphStyle]:
    return {
        "company": ParagraphStyle(
            "company",
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            textColor=colors.HexColor("#CBD5E1"),
        ),
        "title": ParagraphStyle(
            "title",
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=18,
            textColor=WHITE,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName="Helvetica",
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#E2E8F0"),
        ),
        "section": ParagraphStyle(
            "section",
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=NAVY,
            spaceBefore=8,
            spaceAfter=3,
        ),
        "subsection_blocking": ParagraphStyle(
            "subsection_blocking",
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=11,
            textColor=RED_TEXT,
            spaceBefore=4,
            spaceAfter=2,
        ),
        "subsection_review": ParagraphStyle(
            "subsection_review",
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=11,
            textColor=AMBER_TEXT,
            spaceBefore=5,
            spaceAfter=2,
        ),
        "subsection_note": ParagraphStyle(
            "subsection_note",
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=11,
            textColor=BLUE_TEXT,
            spaceBefore=5,
            spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body",
            fontName="Helvetica",
            fontSize=8,
            leading=10.5,
            textColor=TEXT_DARK,
        ),
        "body_muted": ParagraphStyle(
            "body_muted",
            fontName="Helvetica",
            fontSize=7.5,
            leading=9.5,
            textColor=TEXT_MUTED,
        ),
        "body_bold": ParagraphStyle(
            "body_bold",
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10.5,
            textColor=TEXT_DARK,
        ),
        "cell": ParagraphStyle(
            "cell",
            fontName="Helvetica",
            fontSize=7.5,
            leading=9.5,
            textColor=TEXT_DARK,
        ),
        "cell_bold": ParagraphStyle(
            "cell_bold",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9.5,
            textColor=TEXT_DARK,
        ),
        "cell_right": ParagraphStyle(
            "cell_right",
            fontName="Helvetica",
            fontSize=7.5,
            leading=9.5,
            alignment=TA_RIGHT,
            textColor=TEXT_DARK,
        ),
        "cell_right_bold": ParagraphStyle(
            "cell_right_bold",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9.5,
            alignment=TA_RIGHT,
            textColor=TEXT_DARK,
        ),
        "cell_center": ParagraphStyle(
            "cell_center",
            fontName="Helvetica",
            fontSize=7.5,
            leading=9.5,
            alignment=TA_CENTER,
            textColor=TEXT_DARK,
        ),
        "th": ParagraphStyle(
            "th",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9.5,
            textColor=NAVY,
        ),
        "th_right": ParagraphStyle(
            "th_right",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9.5,
            alignment=TA_RIGHT,
            textColor=NAVY,
        ),
        "th_center": ParagraphStyle(
            "th_center",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9.5,
            alignment=TA_CENTER,
            textColor=NAVY,
        ),
        "kpi_label": ParagraphStyle(
            "kpi_label",
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=9,
            textColor=TEXT_MUTED,
        ),
        "kpi_value": ParagraphStyle(
            "kpi_value",
            fontName="Helvetica-Bold",
            fontSize=12.5,
            leading=15,
            textColor=NAVY,
        ),
        "kpi_meta": ParagraphStyle(
            "kpi_meta",
            fontName="Helvetica",
            fontSize=6.8,
            leading=8.8,
            textColor=TEXT_MUTED,
        ),
        "badge": ParagraphStyle(
            "badge",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9.5,
            alignment=TA_CENTER,
        ),
        "callout": ParagraphStyle(
            "callout",
            fontName="Helvetica",
            fontSize=7.8,
            leading=10,
            textColor=TEXT_DARK,
        ),
        "footer_text": ParagraphStyle(
            "footer_text",
            fontName="Helvetica",
            fontSize=6.8,
            leading=8.8,
            textColor=TEXT_MUTED,
        ),
        # Compact landscape styles
        "ls_th": ParagraphStyle(
            "ls_th",
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=8.5,
            textColor=NAVY,
        ),
        "ls_th_right": ParagraphStyle(
            "ls_th_right",
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=8.5,
            alignment=TA_RIGHT,
            textColor=NAVY,
        ),
        "ls_th_center": ParagraphStyle(
            "ls_th_center",
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=8.5,
            alignment=TA_CENTER,
            textColor=NAVY,
        ),
        "ls_cell": ParagraphStyle(
            "ls_cell",
            fontName="Helvetica",
            fontSize=7.2,
            leading=8.5,
            textColor=TEXT_DARK,
        ),
        "ls_cell_bold": ParagraphStyle(
            "ls_cell_bold",
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=8.5,
            textColor=NAVY,
        ),
        "ls_cell_right": ParagraphStyle(
            "ls_cell_right",
            fontName="Helvetica",
            fontSize=7.2,
            leading=8.5,
            alignment=TA_RIGHT,
            textColor=TEXT_DARK,
        ),
        "ls_cell_right_bold": ParagraphStyle(
            "ls_cell_right_bold",
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=8.5,
            alignment=TA_RIGHT,
            textColor=NAVY,
        ),
        "ls_cell_center": ParagraphStyle(
            "ls_cell_center",
            fontName="Helvetica",
            fontSize=7.2,
            leading=8.5,
            alignment=TA_CENTER,
            textColor=TEXT_DARK,
        ),
    }


# ---------------------------------------------------------------------------
# Numbered canvas for clean page X of Y footers
# ---------------------------------------------------------------------------


def _make_numbered_canvas(footer_left: str):
    class _NumberedCanvas(canvas.Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._saved_page_states: list[dict[str, Any]] = []

        def showPage(self) -> None:
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            num_pages = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self._draw_page_footer(num_pages)
                super().showPage()
            super().save()

        def _draw_page_footer(self, page_count: int) -> None:
            page_w, _ = self._pagesize
            self.saveState()
            self.setStrokeColor(BORDER)
            self.setLineWidth(0.5)
            self.line(28, 22, page_w - 28, 22)
            self.setFont("Helvetica", 6.8)
            self.setFillColor(TEXT_MUTED)
            self.drawString(28, 12, footer_left)
            self.drawRightString(
                page_w - 28,
                12,
                f"Page {self._pageNumber} of {page_count}",
            )
            self.restoreState()

    return _NumberedCanvas


# ---------------------------------------------------------------------------
# Pure-ReportLab vector cash trajectory chart (zero PIL)
# ---------------------------------------------------------------------------


class _CashTrajectoryChart(Flowable):
    """Compact pure-vector ending cash line chart with minimum cash floor."""

    def __init__(
        self,
        weeks: list[dict[str, Any]],
        cash_floor: Decimal | None,
        floor_label: str,
        revised_weeks: set[int],
        width: float,
        height: float = 122.0,
    ) -> None:
        super().__init__()
        self.weeks = weeks
        self.cash_floor = cash_floor
        self.floor_label = floor_label
        self.revised_weeks = revised_weeks
        self.width = width
        self.height = height

    def draw(self) -> None:
        c = self.canv
        w = self.width
        h = self.height

        # Outer card frame
        c.saveState()
        c.setFillColor(SURFACE)
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.6)
        c.roundRect(0, 0, w, h, 3, fill=1, stroke=1)

        # Title inside chart frame
        c.setFont("Helvetica-Bold", 7.8)
        c.setFillColor(NAVY)
        c.drawString(10, h - 13, "Projected Ending Cash Trajectory vs. Minimum Cash Floor")

        # Legend top-right
        lx = w - 215
        ly = h - 11
        c.setStrokeColor(NAVY_MID)
        c.setLineWidth(1.5)
        c.line(lx, ly, lx + 14, ly)
        c.setFillColor(TEXT_MUTED)
        c.setFont("Helvetica", 6.8)
        c.drawString(lx + 18, ly - 2.5, "Ending Cash")

        if self.cash_floor is not None:
            fx = lx + 82
            c.setStrokeColor(RED_TEXT)
            c.setLineWidth(1.0)
            c.setDash(3, 2)
            c.line(fx, ly, fx + 14, ly)
            c.setDash()
            c.setFillColor(RED_TEXT)
            c.drawString(fx + 18, ly - 2.5, f"{self.floor_label}: {format_usd(self.cash_floor)}")

        if not self.weeks:
            c.setFont("Helvetica", 8)
            c.setFillColor(TEXT_MUTED)
            c.drawCentredString(w / 2, h / 2, "No forecast weeks present in model.")
            c.restoreState()
            return

        # Plot bounds
        pad_l = 58.0
        pad_r = 22.0
        pad_b = 20.0
        pad_t = 24.0
        inset_x = 16.0
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_b - pad_t
        inner_x0 = pad_l + inset_x
        inner_w = max(plot_w - 2 * inset_x, 1.0)

        values: list[float] = []
        for wk in self.weeks:
            d = _to_decimal(wk.get("ending_cash"))
            values.append(float(d) if d is not None else 0.0)

        all_vals = list(values)
        if self.cash_floor is not None:
            all_vals.append(float(self.cash_floor))

        v_min = min(all_vals)
        v_max = max(all_vals)
        span = v_max - v_min
        if span <= 0:
            span = max(abs(v_max) * 0.2, 100000.0)
        y_lo = v_min - span * 0.16
        y_hi = v_max + span * 0.18

        def map_x(idx: int) -> float:
            if len(self.weeks) == 1:
                return inner_x0 + inner_w / 2
            return inner_x0 + (inner_w * idx) / (len(self.weeks) - 1)

        def map_y(val: float) -> float:
            return pad_b + ((val - y_lo) / (y_hi - y_lo)) * plot_h

        # Revised week vertical highlight bands (drawn BEFORE gridlines, clipped inside plot area)
        n = len(self.weeks)
        step = inner_w / max(n - 1, 1)
        for idx, wk in enumerate(self.weeks):
            wk_num = wk.get("week_num")
            if wk_num in self.revised_weeks:
                cx = map_x(idx)
                band_w = min(step * 0.52, 26.0)
                bx0 = max(pad_l + 2.0, cx - band_w / 2)
                bx1 = min(pad_l + plot_w - 2.0, cx + band_w / 2)
                c.setFillColor(BLUE_BG)
                c.rect(bx0, pad_b, max(bx1 - bx0, 0.0), plot_h, fill=1, stroke=0)

        # Horizontal gridlines (4 levels)
        c.setLineWidth(0.4)
        for i in range(4):
            frac = i / 3
            gv = y_lo + (y_hi - y_lo) * frac
            gy = pad_b + plot_h * frac
            c.setStrokeColor(BORDER)
            c.line(pad_l, gy, pad_l + plot_w, gy)
            c.setFont("Helvetica", 6.5)
            c.setFillColor(TEXT_MUTED)
            c.drawRightString(pad_l - 6, gy - 2, format_usd_compact(gv))

        # Minimum cash floor dashed line
        if self.cash_floor is not None:
            fy = map_y(float(self.cash_floor))
            c.setStrokeColor(RED_TEXT)
            c.setLineWidth(1.0)
            c.setDash(4, 3)
            c.line(pad_l, fy, pad_l + plot_w, fy)
            c.setDash()

        # Ending cash polyline
        c.setStrokeColor(NAVY_MID)
        c.setLineWidth(1.6)
        for idx in range(n - 1):
            c.line(map_x(idx), map_y(values[idx]), map_x(idx + 1), map_y(values[idx + 1]))

        # Points + X-axis labels
        min_idx = min(range(n), key=lambda i: values[i])
        for idx, wk in enumerate(self.weeks):
            cx = map_x(idx)
            cy = map_y(values[idx])
            wk_num = wk.get("week_num", idx + 1)

            if idx == min_idx:
                c.setFillColor(AMBER_TEXT)
                c.setStrokeColor(WHITE)
                c.setLineWidth(0.8)
                c.circle(cx, cy, 3.2, fill=1, stroke=1)
                # Label minimum point above or below to avoid border collision
                c.setFont("Helvetica-Bold", 6.5)
                c.setFillColor(AMBER_TEXT)
                lbl = f"Min W{wk_num}: {format_usd_compact(values[idx])}"
                lbl_y = cy + 6 if cy < pad_b + plot_h - 14 else cy - 11
                c.drawCentredString(max(pad_l + 42, min(cx, pad_l + plot_w - 42)), lbl_y, lbl)
            elif wk_num in self.revised_weeks:
                c.setFillColor(BLUE_TEXT)
                c.setStrokeColor(WHITE)
                c.setLineWidth(0.8)
                c.circle(cx, cy, 3.0, fill=1, stroke=1)
            else:
                c.setFillColor(NAVY)
                c.circle(cx, cy, 2.2, fill=1, stroke=0)

            # X-axis week label
            c.setFont("Helvetica-Bold" if wk_num in self.revised_weeks else "Helvetica", 6.5)
            c.setFillColor(BLUE_TEXT if wk_num in self.revised_weeks else TEXT_MUTED)
            c.drawCentredString(cx, 7, f"W{wk_num}")

        c.restoreState()


# ---------------------------------------------------------------------------
# Weekly-Liquidity-Update.pdf (Portrait, 1-2 pages)
# ---------------------------------------------------------------------------


def _build_portrait_header(model: ReportModel, styles: dict[str, ParagraphStyle], width: float) -> Table:
    badge_text, badge_fg, badge_bg = _edition_badge_text(model)
    meta = model.meta
    gate_text = (
        "APPROVAL BLOCKED"
        if model.blocking_checks
        else ("PENDING DECISION" if any(p.needs_decision for p in model.pending_changes) else "APPROVAL CLEAR")
    )
    meta_parts = [
        f"Period: {_esc(meta.period_label)} ({_esc(meta.period)})",
        f"Generated: {_esc(meta.created_at)}",
    ]
    if meta.approved_by:
        meta_parts.append(f"Approved by: {_esc(meta.approved_by)}")
    elif meta.approver:
        meta_parts.append(f"Approver: {_esc(meta.approver)}")
    meta_parts.append(f"v{_esc(meta.version)}")

    left_block = [
        Paragraph(_esc(meta.company_name.upper()), styles["company"]),
        Paragraph("Weekly Liquidity Update", styles["title"]),
        Paragraph(" · ".join(meta_parts), styles["subtitle"]),
    ]

    badge_style = ParagraphStyle(
        "hdr_badge",
        parent=styles["badge"],
        textColor=badge_fg,
    )
    gate_fg = RED_TEXT if model.blocking_checks else (AMBER_TEXT if not model.can_approve else GREEN_TEXT)
    gate_bg = RED_BG if model.blocking_checks else (AMBER_BG if not model.can_approve else GREEN_BG)
    gate_style = ParagraphStyle("hdr_gate", parent=styles["badge"], fontSize=6.8, textColor=gate_fg)

    right_table = Table(
        [
            [Paragraph(_esc(badge_text), badge_style)],
            [Paragraph(_esc(gate_text), gate_style)],
        ],
        colWidths=[136],
    )
    right_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), badge_bg),
                ("BACKGROUND", (0, 1), (0, 1), gate_bg),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LINEBELOW", (0, 0), (0, 0), 3, NAVY),
            ]
        )
    )

    tbl = Table([[left_block, right_table]], colWidths=[width - 148, 148])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, 0), 12),
                ("RIGHTPADDING", (1, 0), (1, 0), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )
    return tbl


def _build_kpi_grid(kpis: list[KpiField], styles: dict[str, ParagraphStyle], width: float) -> Table | None:
    if not kpis:
        return None
    cols = 3
    col_w = width / cols
    rows: list[list[Any]] = []
    for i in range(0, len(kpis), cols):
        chunk = kpis[i : i + cols]
        row_cells: list[Any] = []
        for kpi in chunk:
            comp = format_comparison(kpi)
            lines = [
                Paragraph(f"{_esc(kpi.label.upper())} <font color='#94A3B8'>({_esc(kpi.status)})</font>", styles["kpi_label"]),
                Paragraph(_esc(format_kpi_value(kpi)), styles["kpi_value"]),
            ]
            if comp:
                lines.append(Paragraph(_esc(comp), styles["kpi_meta"]))
            if kpi.note:
                lines.append(Paragraph(_esc(kpi.note), styles["kpi_meta"]))
            row_cells.append(lines)
        while len(row_cells) < cols:
            row_cells.append("")
        rows.append(row_cells)

    tbl = Table(rows, colWidths=[col_w] * cols)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SURFACE),
                ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.6, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return tbl


def _build_revision_callout(
    revisions: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
    width: float,
    *,
    compact: bool = False,
) -> Table:
    lines: list[Any] = []
    heading = "Confirmed Operator Revision(s) Applied" if not compact else "Revision Applied:"
    if compact:
        parts = []
        for rev in revisions:
            parts.append(
                f"<b>{_esc(rev.get('change_id', 'REV'))}</b> · {_esc(rev.get('vendor', ''))} "
                f"({_esc(rev.get('invoice_ref', ''))}, {format_usd(rev.get('amount'))}): "
                f"W{rev.get('old_week', '?')} ({_esc(rev.get('old_due_date', ''))}) → "
                f"W{rev.get('new_week', '?')} ({_esc(rev.get('new_due_date', ''))}) "
                f"confirmed by {_esc(rev.get('confirmed_by', 'operator'))}"
            )
        lines.append(
            Paragraph(
                f"<font color='#1E40AF'><b>{heading}</b></font> " + " | ".join(parts),
                ParagraphStyle("rev_c", parent=styles["callout"], fontSize=7.2, leading=9),
            )
        )
    else:
        lines.append(
            Paragraph(
                f"<font color='#1E40AF'><b>{heading}</b></font>",
                styles["callout"],
            )
        )
        for rev in revisions:
            src = f" · Source: {_esc(rev.get('source_file'))}" if rev.get("source_file") else ""
            lines.append(
                Paragraph(
                    f"• <b>{_esc(rev.get('change_id', 'REV'))}</b> — {_esc(rev.get('vendor', ''))} "
                    f"({_esc(rev.get('invoice_ref', ''))}, <b>{format_usd(rev.get('amount'))}</b>): "
                    f"moved from Week {rev.get('old_week', '?')} ({_esc(rev.get('old_due_date', ''))}) "
                    f"to Week {rev.get('new_week', '?')} ({_esc(rev.get('new_due_date', ''))}); "
                    f"confirmed by {_esc(rev.get('confirmed_by', 'operator'))} "
                    f"on {_esc(rev.get('confirmed_at', ''))}{src}.",
                    styles["callout"],
                )
            )

    tbl = Table([[lines]], colWidths=[width])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BLUE_BG),
                ("BOX", (0, 0), (-1, -1), 0.7, BLUE_BORDER),
                ("LINEBEFORE", (0, 0), (0, -1), 3.0, BLUE_TEXT),
                ("TOPPADDING", (0, 0), (-1, -1), 4 if compact else 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4 if compact else 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return tbl


def _build_variance_bridge_section(
    model: ReportModel,
    styles: dict[str, ParagraphStyle],
    width: float,
) -> list[Flowable]:
    bridge = model.variance_bridge
    if bridge is None:
        return []

    items: list[Flowable] = [
        Paragraph("Cash Variance Bridge", styles["section"]),
        Paragraph(
            f"Reconciliation from <b>{_esc(bridge.baseline_label)}</b> ({_esc(bridge.baseline_period)}) "
            f"to current balance as of <b>{_esc(bridge.shared_date)}</b>.",
            styles["body_muted"],
        ),
        Spacer(1, 3),
    ]

    col_w = [width * 0.42, width * 0.24, width * 0.18, width * 0.16]
    rows: list[list[Any]] = [
        [
            Paragraph("Bridge Item", styles["th"]),
            Paragraph("Source & Reference", styles["th"]),
            Paragraph("Notes", styles["th"]),
            Paragraph("Amount", styles["th_right"]),
        ],
        [
            Paragraph(f"Prior Baseline — {_esc(bridge.baseline_label)}", styles["cell_bold"]),
            Paragraph(_esc(bridge.baseline_period), styles["cell"]),
            Paragraph("Opening reference", styles["cell"]),
            Paragraph(format_usd(bridge.prior_value), styles["cell_right_bold"]),
        ],
    ]

    for drv in bridge.drivers:
        src_bits = [b for b in (drv.source_file, drv.source_ref, drv.source_date) if b]
        rows.append(
            [
                Paragraph(_esc(drv.label), styles["cell"]),
                Paragraph(_esc(" · ".join(src_bits)), styles["cell"]),
                Paragraph(_esc(drv.note), styles["cell"]),
                Paragraph(format_usd(drv.amount, signed=True), styles["cell_right"]),
            ]
        )

    rows.append(
        [
            Paragraph("Net Variance vs. Prior Baseline", styles["cell_bold"]),
            Paragraph("—", styles["cell"]),
            Paragraph("Model delta", styles["cell"]),
            Paragraph(format_usd(bridge.delta, signed=True), styles["cell_right_bold"]),
        ]
    )
    rows.append(
        [
            Paragraph(f"Current Balance ({_esc(bridge.shared_date)})", styles["cell_bold"]),
            Paragraph("ReportModel", styles["cell"]),
            Paragraph("Reconciled balance", styles["cell"]),
            Paragraph(format_usd(bridge.current_value), styles["cell_right_bold"]),
        ]
    )

    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    style_cmds: list[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), SURFACE_ALT),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 1), (-1, -1), 0.35, BORDER),
        ("BACKGROUND", (0, 1), (-1, 1), SURFACE),
        ("BACKGROUND", (0, len(rows) - 2), (-1, len(rows) - 2), SURFACE),
        ("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), BLUE_BG),
        ("LINEABOVE", (0, len(rows) - 2), (-1, len(rows) - 2), 0.7, BORDER_DARK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    tbl.setStyle(TableStyle(style_cmds))
    items.append(tbl)

    if bridge.moved_payments:
        moved_bits = []
        for mp in bridge.moved_payments:
            inv = mp.get("invoice_ref", "")
            vnd = mp.get("vendor", "")
            amt = format_usd(mp.get("amount"))
            old_w = mp.get("old_week", "?")
            new_w = mp.get("new_week", "?")
            moved_bits.append(f"{_esc(inv)} ({_esc(vnd)}, {amt}: W{old_w} → W{new_w})")
        items.append(Spacer(1, 2))
        items.append(
            Paragraph(
                "<b>Moved Payments (Invoice-Matched):</b> " + "; ".join(moved_bits),
                styles["body_muted"],
            )
        )

    return items


def _build_ar_section(
    model: ReportModel,
    styles: dict[str, ParagraphStyle],
    width: float,
) -> list[Flowable]:
    ar = model.sections.get("ar_aging")
    if not isinstance(ar, dict):
        return []

    as_of = str(ar.get("as_of", "N/A"))
    src_file = str(ar.get("source_file", ""))
    caveat_mismatch = as_of != model.meta.period
    caveat_text = (
        f"<b>As-Of Caveat:</b> A/R aging snapshot is as of <b>{_esc(as_of)}</b>"
        + (f" ({_esc(src_file)})" if src_file else "")
        + (
            f" — predates cash period {_esc(model.meta.period)}; collections posted after {_esc(as_of)} are not yet reflected in aging buckets."
            if caveat_mismatch
            else " — aligned with cash report period."
        )
    )

    items: list[Flowable] = [
        Paragraph("Overdue Accounts Receivable (61–90 Days & >90 Days)", styles["section"]),
    ]

    caveat_tbl = Table([[Paragraph(caveat_text, styles["callout"])]], colWidths=[width])
    caveat_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), AMBER_BG if caveat_mismatch else SURFACE),
                ("BOX", (0, 0), (-1, -1), 0.6, AMBER_BORDER if caveat_mismatch else BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    items.append(caveat_tbl)
    items.append(Spacer(1, 3))

    # Aging buckets strip (read directly from model)
    buckets = ar.get("buckets")
    if isinstance(buckets, dict):
        b_cols = [
            ("Current", buckets.get("current")),
            ("1–30 Days", buckets.get("d1_30")),
            ("31–60 Days", buckets.get("d31_60")),
            ("61–90 Days", buckets.get("d61_90")),
            (">90 Days", buckets.get("over_90")),
            ("Total A/R", ar.get("total")),
        ]
        b_tbl = Table(
            [
                [Paragraph(lbl, styles["th_right"]) for lbl, _ in b_cols],
                [Paragraph(format_usd(val), styles["cell_right_bold"]) for _, val in b_cols],
            ],
            colWidths=[width / 6.0] * 6,
        )
        b_tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), SURFACE_ALT),
                    ("BACKGROUND", (3, 1), (4, 1), AMBER_BG),
                    ("BACKGROUND", (5, 1), (5, 1), SURFACE),
                    ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                    ("INNERGRID", (0, 0), (-1, -1), 0.35, BORDER),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        items.append(b_tbl)
        items.append(Spacer(1, 4))

    overdue = ar.get("overdue_items") or []
    col_w = [
        width * 0.13,
        width * 0.22,
        width * 0.10,
        width * 0.10,
        width * 0.06,
        width * 0.08,
        width * 0.12,
        width * 0.19,
    ]
    rows: list[list[Any]] = [
        [
            Paragraph("Invoice", styles["th"]),
            Paragraph("Customer", styles["th"]),
            Paragraph("Branch", styles["th"]),
            Paragraph("Due Date", styles["th"]),
            Paragraph("DPD", styles["th_right"]),
            Paragraph("Bucket", styles["th_center"]),
            Paragraph("Amount", styles["th_right"]),
            Paragraph("Note", styles["th"]),
        ]
    ]
    if overdue:
        for item in overdue:
            bucket = str(item.get("bucket", ""))
            b_style = ParagraphStyle(
                "b_tag",
                parent=styles["cell_center"],
                fontName="Helvetica-Bold",
                textColor=RED_TEXT if ">" in bucket else AMBER_TEXT,
            )
            rows.append(
                [
                    Paragraph(_esc(item.get("invoice_id", "")), styles["cell_bold"]),
                    Paragraph(_esc(item.get("customer", "")), styles["cell"]),
                    Paragraph(_esc(item.get("branch", "")), styles["cell"]),
                    Paragraph(_esc(item.get("due_date", "")), styles["cell"]),
                    Paragraph(_esc(item.get("days_past_due", "")), styles["cell_right"]),
                    Paragraph(_esc(bucket), b_style),
                    Paragraph(format_usd(item.get("amount")), styles["cell_right_bold"]),
                    Paragraph(_esc(item.get("note", "")), styles["cell"]),
                ]
            )
    else:
        rows.append(
            [
                Paragraph("No overdue invoices in the 61–90 or >90 day buckets.", styles["cell"]),
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )

    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    cmds: list[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), SURFACE_ALT),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 1), (-1, -1), 0.35, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if not overdue:
        cmds.append(("SPAN", (0, 1), (-1, 1)))
    else:
        for idx in range(1, len(rows)):
            if idx % 2 == 0:
                cmds.append(("BACKGROUND", (0, idx), (-1, idx), SURFACE))
    tbl.setStyle(TableStyle(cmds))
    items.append(tbl)
    return items


def _build_checks_subtable(
    checks: list[CheckItem],
    empty_msg: str,
    header_bg: colors.Color,
    accent_color: colors.Color,
    styles: dict[str, ParagraphStyle],
    width: float,
) -> Table:
    col_w = [
        width * 0.12,
        width * 0.21,
        width * 0.35,
        width * 0.11,
        width * 0.11,
        width * 0.10,
    ]
    rows: list[list[Any]] = [
        [
            Paragraph("ID", styles["th"]),
            Paragraph("Check", styles["th"]),
            Paragraph("Detail & Next Step", styles["th"]),
            Paragraph("Owner", styles["th"]),
            Paragraph("Amount", styles["th_right"]),
            Paragraph("Blocks?", styles["th_center"]),
        ]
    ]
    if not checks:
        rows.append([Paragraph(_esc(empty_msg), styles["cell"]), "", "", "", "", ""])
    else:
        for chk in checks:
            detail_html = _esc(chk.detail)
            if chk.next_step:
                detail_html += f"<br/><font color='#64748B'><i>Next: {_esc(chk.next_step)}</i></font>"
            blk_style = ParagraphStyle(
                "blk_s",
                parent=styles["cell_center"],
                fontName="Helvetica-Bold",
                textColor=RED_TEXT if chk.blocks_approval else GREEN_TEXT,
            )
            rows.append(
                [
                    Paragraph(_esc(chk.id), styles["cell_bold"]),
                    Paragraph(f"{_esc(chk.title)}<br/><font color='#64748B'>{_esc(chk.group)}</font>", styles["cell"]),
                    Paragraph(detail_html, styles["cell"]),
                    Paragraph(_esc(chk.owner or "—"), styles["cell"]),
                    Paragraph(format_usd(chk.amount) if chk.amount is not None else "—", styles["cell_right"]),
                    Paragraph("YES" if chk.blocks_approval else "NO", blk_style),
                ]
            )

    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    cmds: list[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, accent_color),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 1), (-1, -1), 0.35, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if not checks:
        cmds.append(("SPAN", (0, 1), (-1, 1)))
    tbl.setStyle(TableStyle(cmds))
    return tbl


def _build_checks_section(
    model: ReportModel,
    styles: dict[str, ParagraphStyle],
    width: float,
) -> list[Flowable]:
    blocking = model.blocking_checks
    open_review = model.open_review_checks
    notes = model.note_checks

    items: list[Flowable] = [
        Paragraph("Control Checks & Approval Gate", styles["section"]),
        Paragraph(
            f"<b>Blocking Checks:</b> {len(blocking)} · "
            f"<b>Open Review (Non-Blocking):</b> {len(open_review)} · "
            f"<b>Notes:</b> {len(notes)} · "
            f"<b>Approval Eligible:</b> {'Yes' if model.can_approve else 'No'}",
            styles["body_muted"],
        ),
        Paragraph("Blocking Checks — Prevent Approval", styles["subsection_blocking"]),
        _build_checks_subtable(
            blocking,
            "None — 0 blocking checks open (controls do not block approval).",
            RED_BG,
            RED_TEXT,
            styles,
            width,
        ),
        Paragraph("Open Review Items — Non-Blocking (Do Not Prevent Approval)", styles["subsection_review"]),
        _build_checks_subtable(
            open_review,
            "No open review items.",
            AMBER_BG,
            AMBER_TEXT,
            styles,
            width,
        ),
    ]
    if notes:
        items.append(Paragraph("Informational Notes", styles["subsection_note"]))
        items.append(
            _build_checks_subtable(
                notes,
                "",
                BLUE_BG,
                BLUE_TEXT,
                styles,
                width,
            )
        )
    return items


def _format_source_citation(src: SourceRef) -> str:
    """Format a source citation respecting programmatic privacy walls.

    Restricted or non-attachable sources are cited strictly by filename and date only.
    """
    as_of_part = f" (as of {src.as_of})" if src.as_of else ""
    if src.sensitivity == "restricted" or not src.attachable:
        return f"<b>{_esc(src.file_name)}</b>{_esc(as_of_part)} <font color='#991B1B'>[RESTRICTED — CITATION ONLY, NOT ATTACHABLE]</font>"
    return f"<b>{_esc(src.file_name)}</b>{_esc(as_of_part)} <font color='#64748B'>[{_esc(src.sensitivity.upper())} · attachable]</font>"


def _build_sources_footer_box(
    model: ReportModel,
    styles: dict[str, ParagraphStyle],
    width: float,
) -> Table:
    lines: list[Any] = [
        Paragraph("<b>Source Citations &amp; Data Privacy Boundary</b>", styles["footer_text"]),
    ]
    if model.sources:
        joined = " &nbsp;|&nbsp; ".join(_format_source_citation(s) for s in model.sources)
        lines.append(Paragraph(joined, styles["footer_text"]))
    else:
        lines.append(Paragraph("No source references recorded.", styles["footer_text"]))

    tbl = Table([[lines]], colWidths=[width])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SURFACE),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return tbl


def _render_liquidity_update_pdf(model: ReportModel, out_path: Path) -> Path:
    styles = _build_styles()
    margin_x = 28
    page_w, _ = letter
    usable_w = page_w - 2 * margin_x

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=24,
        bottomMargin=30,
        title=f"{model.meta.company_name} — Weekly Liquidity Update ({model.meta.period})",
        author=model.meta.company_name,
    )

    story: list[Flowable] = [
        _build_portrait_header(model, styles, usable_w),
        Spacer(1, 6),
    ]

    # Executive narrative headline/summary if present
    headline = model.narrative.get("headline") or model.narrative.get("summary")
    if headline:
        story.append(Paragraph(_esc(headline), styles["body"]))
        story.append(Spacer(1, 5))

    # Revision callout if revisions are present
    revisions = model.sections.get("revisions") or []
    if revisions:
        story.append(_build_revision_callout(revisions, styles, usable_w, compact=False))
        story.append(Spacer(1, 5))

    # Pending unconfirmed changes banner if any
    open_pending = [p for p in model.pending_changes if p.needs_decision]
    if open_pending:
        p_lines = [Paragraph("<font color='#92400E'><b>Pending Unconfirmed Operator Change(s)</b></font>", styles["callout"])]
        for p in open_pending:
            p_lines.append(
                Paragraph(
                    f"• <b>{_esc(p.id)}</b> ({_esc(p.kind)}): {_esc(p.summary)} — awaiting explicit confirmation or decline.",
                    styles["callout"],
                )
            )
        p_tbl = Table([[p_lines]], colWidths=[usable_w])
        p_tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), AMBER_BG),
                    ("BOX", (0, 0), (-1, -1), 0.7, AMBER_BORDER),
                    ("LINEBEFORE", (0, 0), (0, -1), 3.0, AMBER_TEXT),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(p_tbl)
        story.append(Spacer(1, 5))

    # KPI strip
    kpi_grid = _build_kpi_grid(model.kpis, styles, usable_w)
    if kpi_grid is not None:
        story.append(kpi_grid)
        story.append(Spacer(1, 4))

    # Variance bridge
    bridge_flowables = _build_variance_bridge_section(model, styles, usable_w)
    if bridge_flowables:
        story.append(KeepTogether(bridge_flowables))

    # Overdue A/R
    ar_flowables = _build_ar_section(model, styles, usable_w)
    if ar_flowables:
        story.append(KeepTogether(ar_flowables))

    # Control checks (blocking vs open_review strictly separated)
    checks_flowables = _build_checks_section(model, styles, usable_w)
    story.append(KeepTogether(checks_flowables))

    # Source citation footer box
    story.append(Spacer(1, 8))
    story.append(KeepTogether([_build_sources_footer_box(model, styles, usable_w)]))

    badge_text, _, _ = _edition_badge_text(model)
    footer_left = f"{model.meta.company_name} · Weekly Liquidity Update · {model.meta.period} · {badge_text}"
    doc.build(story, canvasmaker=_make_numbered_canvas(footer_left))
    return out_path


# ---------------------------------------------------------------------------
# 13-Week-Cash-Flow.pdf (Strictly 1 page landscape)
# ---------------------------------------------------------------------------


def _render_cash_flow_pdf(model: ReportModel, out_path: Path) -> Path:
    styles = _build_styles()
    page_w, _ = landscape(letter)  # 792 x 612 pt
    margin_x = 26
    usable_w = page_w - 2 * margin_x  # 740 pt

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=landscape(letter),
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=20,
        bottomMargin=26,
        title=f"{model.meta.company_name} — 13-Week Cash Flow ({model.meta.period})",
        author=model.meta.company_name,
    )

    badge_text, badge_fg, badge_bg = _edition_badge_text(model)
    meta = model.meta
    kmap = model.kpi_map()

    # Compact landscape header bar with inline KPI highlights
    left_hdr = [
        Paragraph(_esc(meta.company_name.upper()), styles["company"]),
        Paragraph("13-Week Cash Flow Forecast", ParagraphStyle("ls_t", parent=styles["title"], fontSize=13, leading=15)),
        Paragraph(
            f"Period: {_esc(meta.period_label)} ({_esc(meta.period)}) · Generated: {_esc(meta.created_at)}",
            ParagraphStyle("ls_s", parent=styles["subtitle"], fontSize=7.5, leading=9.5),
        ),
    ]

    pill_keys = [
        ("opening_cash", "Opening Cash"),
        ("week1_ending_cash", "Week 1 Ending"),
        ("week13_ending_cash", "Week 13 Ending"),
        ("min_cash_balance", "Minimum Cash"),
    ]
    pill_cells: list[Any] = []
    for k, fallback_lbl in pill_keys:
        kf = kmap.get(k)
        lbl = kf.label if kf else fallback_lbl
        val = format_kpi_value(kf) if kf else "—"
        pill_cells.append(
            [
                Paragraph(
                    f"<font color='#CBD5E1'>{_esc(lbl.upper())}</font>",
                    ParagraphStyle("p_l", fontName="Helvetica-Bold", fontSize=6.2, leading=7.5, alignment=TA_RIGHT),
                ),
                Paragraph(
                    f"<font color='#FFFFFF'>{_esc(val)}</font>",
                    ParagraphStyle("p_v", fontName="Helvetica-Bold", fontSize=9.2, leading=11, alignment=TA_RIGHT),
                ),
            ]
        )

    badge_p = Paragraph(
        _esc(badge_text),
        ParagraphStyle("ls_b", parent=styles["badge"], fontSize=7.2, textColor=badge_fg),
    )

    right_grid = Table([pill_cells + [[badge_p]]], colWidths=[78, 86, 86, 104, 106])
    right_grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (4, 0), (4, 0), badge_bg),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    hdr_tbl = Table([[left_hdr, right_grid]], colWidths=[usable_w - 466, 466])
    hdr_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, 0), 10),
                ("RIGHTPADDING", (1, 0), (1, 0), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    story: list[Flowable] = [hdr_tbl, Spacer(1, 5)]

    # Highlighted revision callout when a payment moved
    revisions = model.sections.get("revisions") or []
    revised_weeks: set[int] = set()
    for rev in revisions:
        if isinstance(rev.get("old_week"), int):
            revised_weeks.add(rev["old_week"])
        if isinstance(rev.get("new_week"), int):
            revised_weeks.add(rev["new_week"])

    if revisions:
        story.append(_build_revision_callout(revisions, styles, usable_w, compact=True))
        story.append(Spacer(1, 5))

    # 13-week compact forecast table
    weeks: list[dict[str, Any]] = list(model.sections.get("forecast_weeks") or [])
    cash_floor, floor_label = _extract_cash_floor(model)

    col_w = [
        34,   # Wk
        68,   # Week Ending
        86,   # Opening Cash
        82,   # Inflows
        82,   # Outflows
        82,   # Net Cash
        88,   # Ending Cash
        usable_w - (34 + 68 + 86 + 82 + 82 + 82 + 88),  # Notes (218 pt)
    ]

    rows: list[list[Any]] = [
        [
            Paragraph("Wk", styles["ls_th_center"]),
            Paragraph("Week Ending", styles["ls_th"]),
            Paragraph("Opening Cash", styles["ls_th_right"]),
            Paragraph("Inflows", styles["ls_th_right"]),
            Paragraph("Outflows", styles["ls_th_right"]),
            Paragraph("Net Cash", styles["ls_th_right"]),
            Paragraph("Ending Cash", styles["ls_th_right"]),
            Paragraph("Key Flows / Revision Notes", styles["ls_th"]),
        ]
    ]

    min_kpi = kmap.get("min_cash_balance")
    min_kpi_dec = _to_decimal(min_kpi.value) if min_kpi else None

    min_row_indices: list[int] = []
    rev_row_indices: list[int] = []
    breach_row_indices: list[int] = []

    for idx, wk in enumerate(weeks[:13], start=1):
        wk_num = wk.get("week_num", idx)
        ending_dec = _to_decimal(wk.get("ending_cash"))
        is_min = min_kpi_dec is not None and ending_dec is not None and ending_dec == min_kpi_dec
        is_rev = wk_num in revised_weeks
        is_breach = cash_floor is not None and ending_dec is not None and ending_dec < cash_floor

        if is_min:
            min_row_indices.append(idx)
        if is_rev:
            rev_row_indices.append(idx)
        if is_breach:
            breach_row_indices.append(idx)

        note_text = _truncate(str(wk.get("notes") or ""), 54)
        wk_label = f"W{wk_num}*" if is_rev else f"W{wk_num}"

        net_dec = _to_decimal(wk.get("net_cash"))
        net_style = styles["ls_cell_right"]
        if net_dec is not None and net_dec < 0:
            net_style = ParagraphStyle("ls_neg", parent=styles["ls_cell_right"], textColor=RED_TEXT)
        elif net_dec is not None and net_dec > 0:
            net_style = ParagraphStyle("ls_pos", parent=styles["ls_cell_right"], textColor=GREEN_TEXT)

        rows.append(
            [
                Paragraph(_esc(wk_label), styles["ls_cell_center"]),
                Paragraph(_esc(wk.get("week_ending", "")), styles["ls_cell"]),
                Paragraph(format_usd(wk.get("opening_cash")), styles["ls_cell_right"]),
                Paragraph(format_usd(wk.get("inflows")), styles["ls_cell_right"]),
                Paragraph(format_usd(wk.get("outflows")), styles["ls_cell_right"]),
                Paragraph(format_usd(wk.get("net_cash"), signed=True), net_style),
                Paragraph(format_usd(wk.get("ending_cash")), styles["ls_cell_right_bold"]),
                Paragraph(_esc(note_text), styles["ls_cell"]),
            ]
        )

    if len(rows) == 1:
        rows.append([Paragraph("No forecast weeks provided in model.sections['forecast_weeks'].", styles["ls_cell"])] + [""] * 7)

    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    cmds: list[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), SURFACE_ALT),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, NAVY),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 1), (-1, -1), 0.3, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if len(weeks) == 0:
        cmds.append(("SPAN", (0, 1), (-1, 1)))
    else:
        for r in range(1, len(rows)):
            if r % 2 == 0:
                cmds.append(("BACKGROUND", (0, r), (-1, r), SURFACE))
        for r in rev_row_indices:
            cmds.append(("BACKGROUND", (0, r), (-1, r), BLUE_BG))
        for r in min_row_indices:
            cmds.append(("BACKGROUND", (6, r), (6, r), AMBER_BG))
        for r in breach_row_indices:
            cmds.append(("BACKGROUND", (6, r), (6, r), RED_BG))

    tbl.setStyle(TableStyle(cmds))
    story.append(tbl)
    story.append(Spacer(1, 5))

    # Minimum cash floor callout strip
    floor_bits = []
    if cash_floor is not None:
        floor_bits.append(f"<b>{_esc(floor_label)}:</b> {format_usd(cash_floor)}")
    if min_kpi is not None:
        floor_bits.append(f"<b>Projected Minimum Ending Cash:</b> {format_kpi_value(min_kpi)}")
        if min_kpi.note:
            floor_bits.append(_esc(min_kpi.note))
    if revised_weeks:
        floor_bits.append("<i>* Shaded week(s) reflect confirmed payment timing revision.</i>")
    if floor_bits:
        floor_tbl = Table(
            [[Paragraph(" &nbsp;|&nbsp; ".join(floor_bits), ParagraphStyle("fl", parent=styles["callout"], fontSize=7.2, leading=9))]],
            colWidths=[usable_w],
        )
        floor_tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), AMBER_BG if breach_row_indices else SURFACE_ALT),
                    ("BOX", (0, 0), (-1, -1), 0.5, AMBER_BORDER if breach_row_indices else BORDER),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(floor_tbl)
        story.append(Spacer(1, 5))

    # Vector chart (compact height so 1-page guarantee always holds)
    chart_h = 114.0 if revisions else 128.0
    story.append(
        _CashTrajectoryChart(
            weeks=weeks[:13],
            cash_floor=cash_floor,
            floor_label=floor_label,
            revised_weeks=revised_weeks,
            width=usable_w,
            height=chart_h,
        )
    )

    # Compact source footer line
    if model.sources:
        story.append(Spacer(1, 4))
        src_line = "Sources: " + " · ".join(
            f"{_esc(s.file_name)} ({_esc(s.as_of or 'n/a')}"
            + (", restricted — citation only" if (s.sensitivity == "restricted" or not s.attachable) else "")
            + ")"
            for s in model.sources
        )
        story.append(Paragraph(src_line, styles["footer_text"]))

    footer_left = f"{model.meta.company_name} · 13-Week Cash Flow Forecast · {model.meta.period} · {badge_text}"
    doc.build(story, canvasmaker=_make_numbered_canvas(footer_left))
    return out_path


# ---------------------------------------------------------------------------
# Public frozen entrypoint
# ---------------------------------------------------------------------------


def render_weekly_pdfs(model: ReportModel, out_dir: Path) -> dict[str, Path]:
    """Render both weekly cash executive PDFs directly from a single ReportModel.

    Returns:
        `{"liquidity_pdf": Path, "cash_flow_pdf": Path}`
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    liquidity_path = out_dir / "Weekly-Liquidity-Update.pdf"
    cash_flow_path = out_dir / "13-Week-Cash-Flow.pdf"

    _render_liquidity_update_pdf(model, liquidity_path)
    _render_cash_flow_pdf(model, cash_flow_path)

    return {
        "liquidity_pdf": liquidity_path,
        "cash_flow_pdf": cash_flow_path,
    }
