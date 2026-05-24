"""Shared brand styling + chart helpers — keeps the look consistent.

Call `apply_mobile_styles()` after `st.set_page_config()` on every page.
It applies the Pure Air brand theme:
  - Inter font from Google Fonts
  - Pure Air color palette for headings, sidebar header, accents
  - Brand color sequence as the Plotly default
  - Mobile-friendly CSS overrides

Use `chart_height()` instead of hardcoding plotly heights.
"""
from __future__ import annotations

import plotly.express as px
import plotly.io as pio
import streamlit as st

# Pure Air brand palette (sourced from pure-comfort.com stylesheet).
NAVY = "#00214D"
PRIMARY = "#0066EE"   # Pure Air blue
ALERT = "#F34039"
GREEN = "#8AC74C"
SKY = "#078CD3"
SKY_LIGHT = "#80CFF9"
LIGHT_BG = "#F7F7F7"
TEXT = "#1A1A1A"

# Color sequence used by px.bar / px.line / etc. when no `color` arg is set.
# Order matters — the first color dominates single-series charts.
BRAND_PALETTE = [PRIMARY, NAVY, ALERT, GREEN, SKY, SKY_LIGHT]


_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

/* Pure Air font + base color */
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

/* Sidebar header — small Pure Air wordmark above the nav */
[data-testid="stSidebar"] [data-testid="stSidebarNav"]::before {{
  content: "Pure Air";
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
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-bottom: 1px solid #e5e7eb;
  margin-bottom: 0.5rem;
}}

/* Metric polish — bigger value, branded color */
[data-testid="stMetricValue"] {{
  font-weight: 700;
  color: {NAVY};
}}
[data-testid="stMetricLabel"] p {{
  font-weight: 500;
  color: #4b5563;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: 0.78rem !important;
}}

/* Primary buttons get the brand color (Streamlit's theme primaryColor handles
   most of this; this tightens the corner radius and weight). */
.stButton button[kind="primary"], .stButton button {{
  font-weight: 600;
  border-radius: 6px;
}}

/* Subtle bordered containers */
[data-testid="stExpander"], [data-testid="stContainer"] [data-testid="stVerticalBlock"] > div:has(> div > div > div > [data-testid="stHorizontalBlock"]) {{
  border-radius: 8px;
}}

/* Tighter dividers */
hr {{
  margin-top: 1rem !important;
  margin-bottom: 1rem !important;
  border-color: #e5e7eb !important;
}}

/* Hide the "Made with Streamlit" footer */
footer {{ visibility: hidden !important; height: 0 !important; }}

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
    # Also nudge the default template font + colors
    pio.templates["pure_air"] = pio.templates["plotly_white"]
    tmpl = pio.templates["pure_air"]
    tmpl.layout.font.family = "Inter, -apple-system, BlinkMacSystemFont, sans-serif"
    tmpl.layout.font.color = TEXT
    tmpl.layout.colorway = BRAND_PALETTE
    tmpl.layout.title.font.color = NAVY
    tmpl.layout.xaxis.gridcolor = "#eef0f3"
    tmpl.layout.yaxis.gridcolor = "#eef0f3"
    pio.templates.default = "pure_air"


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
