"""Shared brand styling + chart + table helpers — keeps the look consistent.

Call `apply_mobile_styles()` after `st.set_page_config()` on every page.
It applies the Pure Comfort brand theme:
  - Inter font from Google Fonts
  - Pure Comfort color palette for headings, sidebar header, accents
  - Brand color sequence as the Plotly default
  - Mobile-friendly CSS overrides
  - Table styling (zebra rows, branded header), KPI card polish, empty-state styling

Helpers:
  - `chart_height(kind)` — consistent plotly heights
  - `status_color(value)` — pandas Styler cell color for status text
  - `style_status_columns(df, columns)` — apply status colors to a df for st.dataframe
  - `empty_state(message)` — branded "no data" panel
  - `page_header(title, subtitle)` — branded page title block
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.io as pio
import streamlit as st

# ---------- Pure Comfort brand palette ----------
NAVY = "#00214D"
PRIMARY = "#0066EE"   # Pure Comfort blue
ALERT = "#F34039"
GREEN = "#8AC74C"
SKY = "#078CD3"
SKY_LIGHT = "#80CFF9"
LIGHT_BG = "#F7F7F7"
TEXT = "#1A1A1A"
MUTED = "#6b7280"
BORDER = "#e5e7eb"

# Order matters — first color dominates single-series charts.
BRAND_PALETTE = [PRIMARY, NAVY, ALERT, GREEN, SKY, SKY_LIGHT]

# ---------- Status colors for table cells ----------
STATUS_COLORS = {
    # Good / closed / earning
    "Active": GREEN, "Booked": GREEN, "Sold": GREEN, "Completed": GREEN,
    "Won": GREEN, "Done": GREEN, "Paid": GREEN,
    # Pending / in-flight
    "Open": PRIMARY, "Scheduled": PRIMARY, "Working": PRIMARY,
    "Dispatched": PRIMARY, "InProgress": PRIMARY,
    # Bad / lost / missed
    "Abandoned": ALERT, "Unbooked": ALERT, "Dismissed": ALERT,
    "Cancelled": ALERT, "Canceled": ALERT, "Failed": ALERT,
    # Neutral / informational
    "Expired": MUTED, "Excused": MUTED, "NotLead": MUTED, "Hold": MUTED,
}


def status_color(val) -> str:
    """Pandas Styler cell-level CSS — color + weight by status string."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    color = STATUS_COLORS.get(str(val).strip(), TEXT)
    return f"color: {color}; font-weight: 600;"


def style_status_columns(df: pd.DataFrame, columns: list[str]):
    """Apply status colors to one or more columns. Returns a Styler object."""
    existing = [c for c in columns if c in df.columns]
    if not existing:
        return df
    return df.style.map(status_color, subset=existing)


# ---------- Branded CSS (mobile + desktop polish) ----------
_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

/* Pure Comfort font + base color */
html, body, [class*="css"], [data-testid="stAppViewContainer"] {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}

/* Branded headings */
h1, h2, h3 {{
  color: {NAVY} !important;
  font-weight: 700 !important;
  letter-spacing: -0.01em;
}}
h1 {{ font-weight: 800 !important; }}

/* Sidebar header — small Pure Comfort wordmark above the nav */
[data-testid="stSidebar"] [data-testid="stSidebarNav"]::before {{
  content: "Pure Comfort";
  display: block;
  padding: 0.75rem 1.25rem 0.25rem 1.25rem;
  font-size: 1.15rem;
  font-weight: 800;
  color: {NAVY};
  letter-spacing: -0.01em;
}}
[data-testid="stSidebar"] [data-testid="stSidebarNav"]::after {{
  content: "Reporting dashboard";
  display: block;
  padding: 0 1.25rem 0.75rem 1.25rem;
  font-size: 0.78rem;
  font-weight: 500;
  color: {MUTED};
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-bottom: 1px solid {BORDER};
  margin-bottom: 0.5rem;
}}

/* KPI metric polish */
[data-testid="stMetric"] {{
  background-color: white;
  border: 1px solid {BORDER};
  border-radius: 10px;
  padding: 1rem 1.25rem 0.85rem 1.25rem;
  transition: border-color .15s ease, box-shadow .15s ease;
}}
[data-testid="stMetric"]:hover {{
  border-color: {SKY_LIGHT};
  box-shadow: 0 1px 4px rgba(0, 102, 238, 0.08);
}}
[data-testid="stMetricValue"] {{
  font-weight: 700;
  color: {NAVY};
  font-size: 1.7rem !important;
  line-height: 1.15 !important;
}}
[data-testid="stMetricLabel"] p {{
  font-weight: 500;
  color: {MUTED};
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: 0.72rem !important;
}}

/* Primary buttons */
.stButton button {{
  font-weight: 600;
  border-radius: 6px;
  transition: transform .08s ease;
}}
.stButton button:active {{ transform: translateY(1px); }}

/* st.info / st.success / st.warning / st.error — softer corners + brand tint */
[data-testid="stAlert"] {{
  border-radius: 8px;
  border-left-width: 4px;
}}

/* Expanders */
.streamlit-expanderHeader, [data-testid="stExpander"] summary {{
  font-weight: 600;
  color: {NAVY};
}}

/* Tighter dividers */
hr {{
  margin-top: 1rem !important;
  margin-bottom: 1rem !important;
  border-color: {BORDER} !important;
}}

/* Hide Streamlit footer */
footer {{ visibility: hidden !important; height: 0 !important; }}

/* Branded "app header" component (used on home page) */
.pc-header {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem 0 1.25rem 0;
  border-bottom: 2px solid {NAVY};
  margin-bottom: 1.25rem;
}}
.pc-header .pc-mark {{
  width: 36px; height: 36px;
  border-radius: 8px;
  background: linear-gradient(135deg, {PRIMARY} 0%, {NAVY} 100%);
  display: flex; align-items: center; justify-content: center;
  color: white; font-weight: 800; font-size: 1.1rem;
  letter-spacing: -0.04em;
}}
.pc-header .pc-title {{
  font-size: 1.5rem; font-weight: 800; color: {NAVY};
  letter-spacing: -0.01em; line-height: 1;
}}
.pc-header .pc-subtitle {{
  font-size: 0.85rem; color: {MUTED}; margin-top: 0.15rem;
}}

/* Empty-state card */
.pc-empty {{
  border: 1px dashed {BORDER};
  border-radius: 10px;
  padding: 1.5rem;
  text-align: center;
  color: {MUTED};
  background: {LIGHT_BG};
}}

/* ---------- Mobile ---------- */
@media (max-width: 768px) {{
  .block-container {{
    padding-top: 1rem !important;
    padding-bottom: 1rem !important;
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
  }}
  h1 {{ font-size: 1.45rem !important; line-height: 1.2 !important; }}
  h2 {{ font-size: 1.2rem !important; }}
  h3 {{ font-size: 1.05rem !important; }}
  [data-testid="stMetricValue"] {{ font-size: 1.35rem !important; }}
  [data-testid="stMetricDelta"] {{ font-size: 0.7rem !important; }}
  [data-testid="stMetric"] {{ padding: 0.75rem 1rem 0.65rem 1rem; }}
  [data-testid="stCaptionContainer"] {{ font-size: 0.8rem !important; }}
  [data-testid="stDataFrame"] {{ overflow-x: auto !important; }}
  .stButton button {{ min-height: 44px !important; }}
}}

@media (min-width: 769px) {{
  .block-container {{ padding-top: 1.5rem; }}
}}
</style>
"""


def _apply_plotly_brand_palette() -> None:
    """Set the brand palette as the Plotly default for all px.* charts."""
    px.defaults.color_discrete_sequence = BRAND_PALETTE
    pio.templates["pure_comfort"] = pio.templates["plotly_white"]
    tmpl = pio.templates["pure_comfort"]
    tmpl.layout.font.family = "Inter, -apple-system, BlinkMacSystemFont, sans-serif"
    tmpl.layout.font.color = TEXT
    tmpl.layout.colorway = BRAND_PALETTE
    tmpl.layout.title.font.color = NAVY
    tmpl.layout.xaxis.gridcolor = "#eef0f3"
    tmpl.layout.yaxis.gridcolor = "#eef0f3"
    pio.templates.default = "pure_comfort"


_PALETTE_APPLIED = False


def apply_mobile_styles() -> None:
    """Inject the brand CSS + apply the Plotly palette. Call once per page."""
    global _PALETTE_APPLIED
    if not _PALETTE_APPLIED:
        _apply_plotly_brand_palette()
        _PALETTE_APPLIED = True
    st.markdown(_CSS, unsafe_allow_html=True)


def chart_height(kind: str = "default") -> int:
    """Pick a consistent, mobile-friendlier chart height."""
    return {"compact": 240, "default": 300, "tall": 360}.get(kind, 300)


def page_header(title: str, subtitle: str | None = None, mark: str = "PC") -> None:
    """Render a branded page header. Use on the home page; other pages can use st.title."""
    sub_html = f'<div class="pc-subtitle">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f'''<div class="pc-header">
              <div class="pc-mark">{mark}</div>
              <div>
                <div class="pc-title">{title}</div>
                {sub_html}
              </div>
            </div>''',
        unsafe_allow_html=True,
    )


def empty_state(message: str, icon: str | None = None) -> None:
    """Render a branded empty-state card. `icon` is an optional unicode glyph."""
    glyph = f'<div style="font-size: 1.5rem; margin-bottom: 0.5rem;">{icon}</div>' if icon else ""
    st.markdown(
        f'<div class="pc-empty">{glyph}{message}</div>',
        unsafe_allow_html=True,
    )
