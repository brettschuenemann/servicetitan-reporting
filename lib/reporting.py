"""Turn raw ServiceTitan API rows into typed pandas DataFrames."""
from __future__ import annotations

import pandas as pd

JOB_DATE_COLS = ("createdOn", "modifiedOn", "completedOn", "scheduledDate", "firstAppointmentDate")
INVOICE_DATE_COLS = ("createdOn", "modifiedOn", "invoiceDate", "dueDate", "paidOn", "depositedOn")
INVOICE_NUMERIC_COLS = ("total", "subTotal", "salesTax", "balance", "discountTotal")


def jobs_to_dataframe(jobs: list[dict]) -> pd.DataFrame:
    if not jobs:
        return pd.DataFrame()
    df = pd.DataFrame(jobs)
    for col in JOB_DATE_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True).dt.tz_convert(None)
    return df


def invoices_to_dataframe(invoices: list[dict]) -> pd.DataFrame:
    if not invoices:
        return pd.DataFrame()
    df = pd.DataFrame(invoices)
    for col in INVOICE_DATE_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True).dt.tz_convert(None)
    for col in INVOICE_NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "businessUnit" in df.columns:
        df["businessUnitName"] = df["businessUnit"].apply(
            lambda v: v.get("name") if isinstance(v, dict) else None
        )
    if "customer" in df.columns:
        df["customerName"] = df["customer"].apply(
            lambda v: v.get("name") if isinstance(v, dict) else None
        )
    return df
