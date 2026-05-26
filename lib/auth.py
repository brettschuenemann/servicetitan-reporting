"""Tiered shared-password gate for Streamlit.

Two roles:
  - admin (APP_PASSWORD / app_password) → access to every page
  - csr   (CSR_PASSWORD / csr_password)  → access ONLY to pages that call
                                            `require_csr_password()`

Pages that should be CSR-accessible (Call List, Outcomes handler, etc.) call
`require_csr_password()` at the top. Everything else keeps the existing
`require_password()` — admin-only.

If neither password is configured the gate is a no-op (local dev). If only
the admin password is set, CSR-gated pages still require the admin pw.
"""
from __future__ import annotations

import hmac
import os

import streamlit as st


def _configured(secret_name: str, env_name: str) -> str | None:
    try:
        pw = st.secrets.get(secret_name)
        if pw:
            return str(pw)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass
    return os.environ.get(env_name) or None


def _admin_pw() -> str | None:
    return _configured("app_password", "APP_PASSWORD")


def _csr_pw() -> str | None:
    return _configured("csr_password", "CSR_PASSWORD")


def _current_role() -> str | None:
    """Either 'admin', 'csr', or None (not authenticated). Backward-compat
    with the legacy `_authenticated` flag — treat that as admin."""
    role = st.session_state.get("_auth_role")
    if role:
        return role
    if st.session_state.get("_authenticated"):
        return "admin"
    return None


def _prompt_and_validate(allowed: list[tuple[str, str]]) -> None:
    """Render the password prompt. `allowed` is a list of (password, role)
    pairs that grant access. First match wins; admin role always preferred."""
    if not allowed:
        return  # no auth configured at all → open access
    st.title("ServiceTitan Reporting")
    pw = st.text_input("Password", type="password", key="_pw_input")
    submitted = st.button("Sign in", type="primary")
    if submitted or pw:
        if not pw:
            st.stop()
        # Prefer admin if both somehow match (defense in depth)
        admin_match = any(hmac.compare_digest(pw, p) for p, r in allowed if r == "admin")
        csr_match   = any(hmac.compare_digest(pw, p) for p, r in allowed if r == "csr")
        if admin_match:
            st.session_state["_auth_role"] = "admin"
            st.session_state["_authenticated"] = True  # legacy flag
        elif csr_match:
            st.session_state["_auth_role"] = "csr"
            # Do NOT set _authenticated — that's reserved for admin/legacy use.
        else:
            st.error("Incorrect password.")
            st.stop()
        try:
            del st.session_state["_pw_input"]
        except KeyError:
            pass
        st.rerun()
    st.stop()


def require_password() -> None:
    """Admin-only gate. CSR-authenticated users get re-prompted."""
    admin_pw = _admin_pw()
    csr_pw = _csr_pw()
    # Truly no auth configured → open access (dev mode).
    if not admin_pw and not csr_pw:
        return
    if _current_role() == "admin":
        return
    # If CSR is configured but admin isn't, this page is locked-and-broken
    # rather than open. Refuse to render and tell the operator to fix the
    # config instead of silently granting access.
    if not admin_pw:
        st.error(
            "🔒 Admin password not configured. This page is admin-only. "
            "Set `APP_PASSWORD` in `.env` or `app_password` in "
            "`.streamlit/secrets.toml` to enable admin access."
        )
        st.stop()
    _prompt_and_validate([(admin_pw, "admin")])


def require_csr_password() -> None:
    """Gate that accepts EITHER the admin password OR the CSR password.
    Used on Fey-facing pages (Call List, Outcomes)."""
    admin_pw = _admin_pw()
    csr_pw = _csr_pw()
    if not admin_pw and not csr_pw:
        return  # no auth configured
    if _current_role() in ("admin", "csr"):
        return
    allowed: list[tuple[str, str]] = []
    if admin_pw:
        allowed.append((admin_pw, "admin"))
    if csr_pw:
        allowed.append((csr_pw, "csr"))
    _prompt_and_validate(allowed)
