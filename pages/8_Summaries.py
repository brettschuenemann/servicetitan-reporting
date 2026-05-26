"""Summaries — historical record of every AI summary generated.

As of May 26, 2026 the home dashboard is read-only — the only paths that
create new summaries are the Friday-evening weekly cron (`weekly_email`)
and manual pins (`manual_pin`). The older `dashboard`-sourced rows are
duplicate regenerations from before the read-only fix landed; the source
filter hides them by default.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from lib.auth import require_password
from lib.database import db
from lib.style import apply_mobile_styles

st.set_page_config(page_title="Summaries · ServiceTitan Reporting", layout="wide")
apply_mobile_styles()
require_password()
st.title("AI summary history")
st.caption(
    "Weekly cron + manually pinned summaries are shown by default. "
    "Pre-fix dashboard regenerations are hidden — toggle the Source filter "
    "in the sidebar to bring them back."
)


@st.cache_data(ttl=60, show_spinner=False)
def load_summaries() -> pd.DataFrame:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, generated_at, period_start, period_end,
                   window_days, source, summary_md, raw_brief
            FROM ai_summaries
            ORDER BY generated_at DESC
            """
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


df = load_summaries()

if df.empty:
    st.info(
        "No summaries logged yet. The first one will be written the next time "
        "the home dashboard's AI summary regenerates (or when the weekly email "
        "fires Friday morning)."
    )
    st.stop()

# ---- Filter bar ----
# Default hides 'dashboard' source: those rows are the duplicate
# regenerations from before the May 26 read-only fix and don't represent
# distinct analyses. Toggle the multiselect to surface them.
with st.sidebar:
    st.header("Filters")
    sources = sorted(df["source"].dropna().unique().tolist())
    default_sources = [s for s in sources if s != "dashboard"]
    chosen_sources = st.multiselect("Source", sources, default=default_sources)
    min_date = df["generated_at"].dt.date.min() if pd.api.types.is_datetime64_any_dtype(df["generated_at"]) else pd.to_datetime(df["generated_at"]).dt.date.min()
    max_date = date.today()
    after = st.date_input("Generated on or after", min_date, min_value=min_date, max_value=max_date)

# Apply filters
df["generated_at"] = pd.to_datetime(df["generated_at"])
mask = df["source"].isin(chosen_sources) & (df["generated_at"].dt.date >= after)
filtered = df[mask]

st.caption(
    f"Showing **{len(filtered):,}** of {len(df):,} summaries. "
    f"Most recent: {df['generated_at'].max().strftime('%b %d, %Y · %I:%M %p UTC')}."
)

# ---- Header chips ----
total_by_source = filtered.groupby("source").size().to_dict()
chips = " · ".join(f"**{s}**: {n}" for s, n in total_by_source.items())
if chips:
    st.markdown(chips)

st.divider()

# ---- List ----
for i, row in enumerate(filtered.itertuples()):
    gen_at = row.generated_at
    period = f"{row.period_start} → {row.period_end}" if row.period_start else "(no period)"
    source_label = {"dashboard": "🖥️", "weekly_email": "📧", "cli": "💻"}.get(row.source, "•")
    header = (
        f"{source_label}  {gen_at.strftime('%a %b %d, %Y · %I:%M %p')}  "
        f"·  {row.window_days}-day window ({period})  ·  {row.source or 'unknown'}"
    )
    with st.expander(header, expanded=(i == 0)):
        # Escape $ for Streamlit's markdown renderer (DB stores unescaped).
        st.markdown(row.summary_md.replace("$", r"\$"))
        with st.expander("Show the raw data the AI saw"):
            st.code(row.raw_brief or "(no brief saved)", language="text")

# ---- CSV export ----
st.divider()
csv = filtered[
    ["id", "generated_at", "period_start", "period_end", "window_days", "source", "summary_md"]
].to_csv(index=False).encode("utf-8")
st.download_button("Download all (CSV)", csv, file_name="ai_summaries.csv", mime="text/csv")
