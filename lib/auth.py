"""Tiny shared-password gate for Streamlit.

In production, set `app_password` in `.streamlit/secrets.toml` (or in Streamlit
Cloud's app secrets). If no password is configured, the gate is a no-op — useful
for local development. Call `require_password()` at the top of every page.
"""
from __future__ import annotations

import hmac
import os

import streamlit as st


def _configured_password() -> str | None:
    """Return the configured password, or None if no auth is set up."""
    try:
        pw = st.secrets.get("app_password")
        if pw:
            return str(pw)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass
    return os.environ.get("APP_PASSWORD") or None


def require_password() -> None:
    """Block the page until the visitor enters the shared password."""
    expected = _configured_password()
    if not expected:
        return  # no password configured — open access

    if st.session_state.get("_authenticated"):
        return

    st.title("ServiceTitan Reporting")
    pw = st.text_input("Password", type="password", key="_pw_input")
    if st.button("Sign in", type="primary") or pw:
        if pw and hmac.compare_digest(pw, expected):
            st.session_state["_authenticated"] = True
            try:
                del st.session_state["_pw_input"]
            except KeyError:
                pass
            st.rerun()
        elif pw:
            st.error("Incorrect password.")
    st.stop()
