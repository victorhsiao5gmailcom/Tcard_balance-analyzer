"""Data loading, cleaning and regression helpers for the dashboard."""
import re

import numpy as np
import pandas as pd

BALANCE_SUFFIX = " Balance"


def clean_location(raw):
    """'STG - Lawson Dining Hall STG Lawson POS 1' -> 'STG - Lawson Dining Hall'.

    Terminal names repeat the first word of the location ('STG ... STG Lawson POS 1'),
    so everything from that repeat onwards is dropped, then any trailing 'POS n'.
    Adjust here if your locations follow a different pattern.
    """
    s = str(raw).strip()
    if s:
        cut = re.search(rf"\s{re.escape(s.split(' ', 1)[0])}\s", s)
        if cut:
            s = s[: cut.start()]
    s = re.sub(r"\s+POS\s*\d+$", "", s, flags=re.I)
    return s or "Unknown"


def load_transactions(path):
    df = pd.read_csv(path, dtype=str).fillna("")
    df["Date/Time"] = pd.to_datetime(df["Date/Time"])
    df["Amount"] = pd.to_numeric(df["Amount"])
    df["Location"] = df["Location"].map(clean_location)
    for col in df.columns:
        if col.endswith(BALANCE_SUFFIX):
            df[col] = pd.to_numeric(df[col])
    return df.sort_values("Date/Time", kind="stable").reset_index(drop=True)


def balance_accounts(df):
    return [c[: -len(BALANCE_SUFFIX)] for c in df.columns if c.endswith(BALANCE_SUFFIX)]


def choose_freq(span_days):
    """Bucket size that keeps the spending chart readable for the selected range."""
    if span_days <= 31:
        return "D"
    if span_days <= 180:
        return "W"
    return "M"


def fit_runout(times, balances):
    """Linear regression of balance vs time. Returns None if it can't be fitted.

    Keys: t0 (time of x=0), slope (per day), intercept, r2, zero (Timestamp when the
    fitted line reaches 0, or None if the balance is not decreasing).
    """
    times = pd.Series(times).reset_index(drop=True)
    y = pd.Series(balances).astype(float).reset_index(drop=True)
    if len(times) < 2 or times.nunique() < 2:
        return None
    t0 = times.min()
    x = ((times - t0).dt.total_seconds() / 86400).to_numpy()
    slope, intercept = np.polyfit(x, y.to_numpy(), 1)
    pred = slope * x + intercept
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - ((y - pred) ** 2).sum() / ss_tot if ss_tot else 1.0
    zero = t0 + pd.Timedelta(days=-intercept / slope) if slope < 0 else None
    return {"t0": t0, "slope": slope, "intercept": intercept, "r2": r2, "zero": zero}
