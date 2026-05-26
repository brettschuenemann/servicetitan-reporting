"""Shared brand styling, modern CSS, and grouped sidebar navigation.

Call `apply_mobile_styles()` after `st.set_page_config()` on every page.
It does three things:
  1. Injects the Pure Comfort brand CSS (Inter font, palette, modernized
     cards / tables / expanders / spacing)
  2. Hides Streamlit's auto-generated flat sidebar nav
  3. Renders our custom grouped sidebar nav in its place

Per-page filters and content render *below* the custom nav inside the
sidebar — no per-page changes needed beyond the existing pattern.

Helpers:
  - `chart_height(kind)`              — consistent plotly heights
  - `status_color(value)`             — Styler cell color for status text
  - `style_status_columns(df, cols)`  — apply status colors to a dataframe
  - `empty_state(message)`            — branded "no data" panel
  - `page_header(title, subtitle)`    — branded page title block
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
GREEN = "#10B981"     # bumped saturation for modern feel
SKY = "#078CD3"
SKY_LIGHT = "#80CFF9"
AMBER = "#F59E0B"
PURPLE = "#8B5CF6"

# Surfaces & typography
APP_BG = "#FAFBFC"        # subtle off-white so cards pop
CARD_BG = "#FFFFFF"
TEXT = "#0F172A"          # slate-900 — sharper than near-black for modern feel
MUTED = "#64748B"         # slate-500
SUBTLE = "#94A3B8"        # slate-400 (for the lightest captions)
BORDER = "#E2E8F0"        # slate-200
BORDER_STRONG = "#CBD5E1" # slate-300

# Order matters — first color dominates single-series charts.
BRAND_PALETTE = [PRIMARY, NAVY, ALERT, GREEN, AMBER, SKY, PURPLE, SKY_LIGHT]

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
    """Apply status colors to one or more columns. Returns a Styler object.

    Uses Styler.map when available (pandas ≥ 2.1) and falls back to the
    older Styler.applymap so we work on whatever pandas Streamlit Cloud
    happens to install.
    """
    existing = [c for c in columns if c in df.columns]
    if not existing:
        return df
    styler = df.style
    fn = getattr(styler, "map", None) or styler.applymap
    return fn(status_color, subset=existing)


# ---------- Sidebar navigation grouping ----------
# Pages grouped by use case. Each entry is (page_path, label, icon).
# st.page_link auto-highlights the current page; section dividers + headers
# turn the long flat list into something scannable.
NAV_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("", [
        ("app.py",                       "Dashboard",         "🏠"),
    ]),
    ("Daily workflow", [
        ("pages/13_Call_List.py",        "Call list",         "📞"),
        ("pages/12_Outcomes.py",         "Outcomes",          "📝"),
    ]),
    ("Performance", [
        ("pages/2_Revenue.py",           "Revenue",           "💰"),
        ("pages/10_Margin.py",           "Margin",            "📊"),
        ("pages/1_Jobs.py",              "Jobs",              "🧰"),
        ("pages/6_Technicians.py",       "Technicians",       "👷"),
    ]),
    ("Growth & retention", [
        ("pages/3_Followups.py",         "Followups",         "🎯"),
        ("pages/4_Sources.py",           "Lead sources",      "📡"),
        ("pages/7_Memberships.py",       "Memberships",       "🤝"),
        ("pages/9_Sleeping.py",          "Sleeping customers", "💤"),
    ]),
    ("Customers & calls", [
        ("pages/11_Customers.py",        "Customers",         "👥"),
        ("pages/5_Calls.py",             "Calls",             "☎️"),
    ]),
    ("Archive", [
        ("pages/8_Summaries.py",         "AI summaries",      "📚"),
    ]),
]


def _render_sidebar_nav() -> None:
    """Render the custom grouped sidebar nav.

    Streamlit's auto-nav is hidden via CSS in `_CSS`; this replaces it.
    Wraps each section in a styled header so the long list scans cleanly.
    Safe to call multiple times — `st.page_link` is idempotent.
    """
    with st.sidebar:
        # Brand block at the top
        st.markdown(
            f"""
            <div class="pc-sidebar-brand">
              <div class="pc-sidebar-mark">PC</div>
              <div>
                <div class="pc-sidebar-title">Pure Comfort</div>
                <div class="pc-sidebar-sub">Reporting</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        for section_label, items in NAV_GROUPS:
            if section_label:
                st.markdown(
                    f'<div class="pc-nav-section">{section_label}</div>',
                    unsafe_allow_html=True,
                )
            for path, label, icon in items:
                try:
                    st.page_link(path, label=label, icon=icon)
                except Exception:
                    # Page file missing or path wrong — render a disabled
                    # caption rather than crash the whole sidebar.
                    st.caption(f"{icon} {label} (unavailable)")

        # Subtle divider before any per-page sidebar content
        st.markdown(
            '<div class="pc-nav-end"></div>',
            unsafe_allow_html=True,
        )


# ---------- Branded CSS (modern polish + mobile) ----------
_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

/* ========== Typography + base ========== */
html, body, [class*="css"], [data-testid="stAppViewContainer"] {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  color: {TEXT};
}}

/* Subtle app background so white cards have something to sit on */
[data-testid="stAppViewContainer"] > .main {{
  background-color: {APP_BG};
}}

/* Branded headings — tighter tracking, modern weight scale */
h1, h2, h3, h4 {{
  color: {NAVY} !important;
  font-weight: 700 !important;
  letter-spacing: -0.015em;
  line-height: 1.25;
}}
h1 {{ font-weight: 800 !important; letter-spacing: -0.025em; }}
h2 {{ font-size: 1.35rem !important; margin-top: 0.5rem !important; }}
h3 {{ font-size: 1.1rem !important; }}

/* Better caption color hierarchy */
[data-testid="stCaptionContainer"] {{
  color: {MUTED} !important;
  font-size: 0.85rem !important;
}}

/* ========== Sidebar nav ========== */
/* Hide Streamlit's auto-generated flat page nav — we render our own */
[data-testid="stSidebarNav"] {{ display: none !important; }}

/* Brand block at the top of the sidebar */
.pc-sidebar-brand {{
  display: flex;
  align-items: center;
  gap: 0.65rem;
  padding: 0.25rem 0 1rem 0;
  margin-bottom: 0.5rem;
  border-bottom: 1px solid {BORDER};
}}
.pc-sidebar-mark {{
  width: 34px; height: 34px;
  border-radius: 8px;
  background: linear-gradient(135deg, {PRIMARY} 0%, {NAVY} 100%);
  display: flex; align-items: center; justify-content: center;
  color: white; font-weight: 800; font-size: 0.85rem;
  letter-spacing: -0.04em;
  box-shadow: 0 1px 2px rgba(0, 33, 77, 0.15);
}}
.pc-sidebar-title {{
  font-size: 1rem; font-weight: 800; color: {NAVY};
  letter-spacing: -0.015em; line-height: 1.1;
}}
.pc-sidebar-sub {{
  font-size: 0.7rem; color: {MUTED}; margin-top: 0.1rem;
  text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600;
}}

/* Section header above each nav group */
.pc-nav-section {{
  font-size: 0.66rem;
  font-weight: 700;
  color: {SUBTLE};
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 0.85rem 0.5rem 0.35rem;
  margin-top: 0.25rem;
}}

/* Style the page_link entries to look like a sleek nav */
[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] {{
  padding: 0.45rem 0.6rem !important;
  border-radius: 6px !important;
  font-size: 0.92rem !important;
  font-weight: 500 !important;
  color: {TEXT} !important;
  transition: background-color .12s ease, color .12s ease;
}}
[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"]:hover {{
  background-color: rgba(0, 102, 238, 0.06) !important;
  color: {PRIMARY} !important;
}}
/* Current-page highlight (Streamlit applies aria-current="page") */
[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"][aria-current="page"] {{
  background-color: rgba(0, 102, 238, 0.10) !important;
  color: {PRIMARY} !important;
  font-weight: 600 !important;
}}

/* Spacing before per-page sidebar content (date filters, etc.) */
.pc-nav-end {{
  border-top: 1px solid {BORDER};
  margin: 1rem 0 0.75rem 0;
}}

/* Trim sidebar's top padding now that nav is denser */
[data-testid="stSidebar"] > div:first-child {{
  padding-top: 1.25rem;
}}

/* ========== KPI metric cards — modernized ========== */
[data-testid="stMetric"] {{
  background-color: {CARD_BG};
  border: 1px solid {BORDER};
  border-radius: 12px;
  padding: 1rem 1.15rem 0.85rem;
  transition: border-color .15s ease, box-shadow .15s ease, transform .15s ease;
  position: relative;
  overflow: hidden;
}}
[data-testid="stMetric"]:hover {{
  border-color: {BORDER_STRONG};
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.06);
  transform: translateY(-1px);
}}
[data-testid="stMetricValue"] {{
  font-weight: 700;
  color: {NAVY};
  font-size: 1.85rem !important;
  line-height: 1.1 !important;
  letter-spacing: -0.02em;
}}
[data-testid="stMetricLabel"] p {{
  font-weight: 600;
  color: {MUTED};
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 0.7rem !important;
}}
[data-testid="stMetricDelta"] {{
  font-weight: 600;
  font-size: 0.8rem !important;
}}

/* ========== Containers (st.container(border=True)) ========== */
[data-testid="stVerticalBlockBorderWrapper"] {{
  border-radius: 12px !important;
  border-color: {BORDER} !important;
  background-color: {CARD_BG};
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
  transition: box-shadow .15s ease;
}}
[data-testid="stVerticalBlockBorderWrapper"]:hover {{
  box-shadow: 0 2px 6px rgba(15, 23, 42, 0.05);
}}

/* ========== Buttons ========== */
.stButton button {{
  font-weight: 600;
  border-radius: 8px;
  border: 1px solid {BORDER};
  transition: all .12s ease;
}}
.stButton button:hover {{
  border-color: {PRIMARY};
  color: {PRIMARY};
}}
.stButton button:active {{ transform: translateY(1px); }}
/* Primary variant — solid brand fill */
.stButton button[kind="primary"] {{
  background-color: {PRIMARY} !important;
  border-color: {PRIMARY} !important;
  color: white !important;
  box-shadow: 0 1px 2px rgba(0, 102, 238, 0.25);
}}
.stButton button[kind="primary"]:hover {{
  background-color: {NAVY} !important;
  border-color: {NAVY} !important;
  color: white !important;
  box-shadow: 0 2px 6px rgba(0, 33, 77, 0.25);
}}

/* ========== Alerts (info/success/warning/error) ========== */
[data-testid="stAlert"] {{
  border-radius: 10px;
  border-left-width: 4px;
  font-size: 0.92rem;
}}

/* ========== Expanders ========== */
[data-testid="stExpander"] {{
  border-radius: 10px !important;
  border-color: {BORDER} !important;
  background-color: {CARD_BG};
  overflow: hidden;
}}
[data-testid="stExpander"] summary {{
  font-weight: 600 !important;
  color: {NAVY} !important;
  padding: 0.75rem 1rem !important;
}}
[data-testid="stExpander"] summary:hover {{
  background-color: rgba(0, 102, 238, 0.03);
}}

/* ========== Data tables (st.dataframe) ========== */
[data-testid="stDataFrame"] {{
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid {BORDER};
}}
/* Header row */
[data-testid="stDataFrame"] [role="columnheader"] {{
  background-color: {APP_BG} !important;
  color: {NAVY} !important;
  font-weight: 600 !important;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: 0.75rem !important;
  border-bottom: 1px solid {BORDER_STRONG} !important;
}}
/* Row hover */
[data-testid="stDataFrame"] [role="row"]:hover [role="gridcell"] {{
  background-color: rgba(0, 102, 238, 0.03) !important;
}}

/* ========== Inputs (text, select, date) ========== */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stDateInput"] input,
[data-testid="stNumberInput"] input,
.stSelectbox [data-baseweb="select"] > div {{
  border-radius: 8px !important;
  border-color: {BORDER} !important;
  font-size: 0.92rem;
}}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {{
  border-color: {PRIMARY} !important;
  box-shadow: 0 0 0 3px rgba(0, 102, 238, 0.12) !important;
}}

/* ========== Tabs ========== */
.stTabs [data-baseweb="tab-list"] {{
  gap: 0.25rem;
  border-bottom: 1px solid {BORDER};
}}
.stTabs [data-baseweb="tab"] {{
  font-weight: 600;
  color: {MUTED};
  padding: 0.5rem 0.85rem;
  border-radius: 6px 6px 0 0;
}}
.stTabs [aria-selected="true"] {{
  color: {PRIMARY} !important;
}}

/* ========== Dividers — softer than default ========== */
hr {{
  margin-top: 1.5rem !important;
  margin-bottom: 1.5rem !important;
  border-color: {BORDER} !important;
  border-top-width: 1px !important;
}}

/* Hide Streamlit's noisy footer */
footer {{ visibility: hidden !important; height: 0 !important; }}
[data-testid="stDecoration"] {{ display: none !important; }}

/* ========== Branded "app header" component (home page) ========== */
.pc-header {{
  display: flex;
  align-items: center;
  gap: 0.85rem;
  padding: 0.5rem 0 1.25rem 0;
  margin-bottom: 1rem;
  border-bottom: 2px solid {NAVY};
}}
.pc-header .pc-mark {{
  width: 40px; height: 40px;
  border-radius: 10px;
  background: linear-gradient(135deg, {PRIMARY} 0%, {NAVY} 100%);
  display: flex; align-items: center; justify-content: center;
  color: white; font-weight: 800; font-size: 1.05rem;
  letter-spacing: -0.04em;
  box-shadow: 0 2px 6px rgba(0, 102, 238, 0.2);
}}
.pc-header .pc-title {{
  font-size: 1.6rem; font-weight: 800; color: {NAVY};
  letter-spacing: -0.025em; line-height: 1;
}}
.pc-header .pc-subtitle {{
  font-size: 0.88rem; color: {MUTED}; margin-top: 0.2rem;
}}

/* ========== Empty-state card ========== */
.pc-empty {{
  border: 1px dashed {BORDER_STRONG};
  border-radius: 12px;
  padding: 2rem 1.5rem;
  text-align: center;
  color: {MUTED};
  background: {APP_BG};
  font-size: 0.95rem;
}}

/* ========== Mobile overrides ========== */
@media (max-width: 768px) {{
  .block-container {{
    padding-top: 0.75rem !important;
    padding-bottom: 1rem !important;
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
  }}
  h1 {{ font-size: 1.45rem !important; line-height: 1.2 !important; }}
  h2 {{ font-size: 1.15rem !important; }}
  h3 {{ font-size: 1.0rem !important; }}
  [data-testid="stMetricValue"] {{ font-size: 1.4rem !important; }}
  [data-testid="stMetricDelta"] {{ font-size: 0.7rem !important; }}
  [data-testid="stMetric"] {{ padding: 0.75rem 0.9rem 0.65rem; }}
  [data-testid="stDataFrame"] {{ overflow-x: auto !important; }}
  .stButton button {{ min-height: 44px !important; }}
  /* Nav links should also feel tappable */
  [data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] {{
    padding: 0.55rem 0.6rem !important;
  }}
}}

@media (min-width: 769px) {{
  .block-container {{ padding-top: 1.5rem; max-width: 1280px; }}
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
    """Inject the brand CSS + render the custom sidebar nav.

    Call once per page right after st.set_page_config(). The custom
    sidebar nav appears at the top of the sidebar; per-page sidebar
    content (date pickers, etc.) renders below it.
    """
    global _PALETTE_APPLIED
    if not _PALETTE_APPLIED:
        _apply_plotly_brand_palette()
        _PALETTE_APPLIED = True
    st.markdown(_CSS, unsafe_allow_html=True)
    _render_sidebar_nav()


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
    glyph = f'<div style="font-size: 1.75rem; margin-bottom: 0.5rem;">{icon}</div>' if icon else ""
    st.markdown(
        f'<div class="pc-empty">{glyph}{message}</div>',
        unsafe_allow_html=True,
    )
