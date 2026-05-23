"""Shared CSS + chart-height helpers for consistent, mobile-friendly rendering.

Call `apply_mobile_styles()` after `st.set_page_config()` on every page.
Use `chart_height()` instead of hardcoding plotly heights — it picks a good
default and could be wired to viewport hints later.
"""
from __future__ import annotations

import streamlit as st

_CSS = """
<style>
/* Trim Streamlit's default padding on small screens */
@media (max-width: 768px) {
  .block-container {
    padding-top: 1rem !important;
    padding-bottom: 1rem !important;
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
  }
  /* Tighten headings */
  h1 { font-size: 1.45rem !important; line-height: 1.2 !important; }
  h2 { font-size: 1.2rem !important; }
  h3 { font-size: 1.05rem !important; }
  /* Tighten metric cards so 4-up wraps cleanly to 2x2 */
  [data-testid="stMetricLabel"] p { font-size: 0.72rem !important; }
  [data-testid="stMetricValue"] { font-size: 1.35rem !important; }
  [data-testid="stMetricDelta"] { font-size: 0.7rem !important; }
  /* Make the caption text a touch smaller */
  [data-testid="stCaptionContainer"] { font-size: 0.8rem !important; }
  /* Horizontal-scroll wide tables instead of cramming */
  [data-testid="stDataFrame"] { overflow-x: auto !important; }
  /* Larger touch targets for buttons */
  .stButton button { min-height: 44px !important; }
}

/* Tighten desktop spacing a touch as well so the page feels less airy */
@media (min-width: 769px) {
  .block-container { padding-top: 1.5rem; }
}

/* Hide the "Made with Streamlit" footer to recover vertical space */
footer { visibility: hidden !important; height: 0 !important; }

/* Polish: tighter divider */
hr { margin-top: 1rem !important; margin-bottom: 1rem !important; }
</style>
"""


def apply_mobile_styles() -> None:
    """Inject the shared CSS. Call once per page after set_page_config."""
    st.markdown(_CSS, unsafe_allow_html=True)


def chart_height(kind: str = "default") -> int:
    """Pick a consistent, mobile-friendlier chart height.

    `kind` is a hint: "compact" (small), "default" (most charts), or "tall"
    (year-over-year style multi-series).
    """
    return {
        "compact": 240,
        "default": 300,
        "tall": 360,
    }.get(kind, 300)
