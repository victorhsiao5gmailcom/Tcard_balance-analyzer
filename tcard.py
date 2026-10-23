import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

TARGET_URL = "https://eacct-utsc-sp.transactcampus.com/eAccounts/AccountTransaction.aspx"
SUMMARY_URL = "https://eacct-utsc-sp.transactcampus.com/eAccounts/AccountSummary.aspx"
BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "chromium_profile"
CSV_FILE = Path(os.environ.get("TCARD_CSV", BASE_DIR / "tcard_transactions.csv"))
LOGIN_TIMEOUT_MS = 10 * 60 * 1000    # give up if nobody logs in within 10 minutes

BEGIN_PICKER_ID = "ctl00_MainContent_BeginRadDateTimePicker"
END_PICKER_ID = "ctl00_MainContent_EndRadDateTimePicker"
SEARCH_BUTTON = "#MainContent_ContinueButton"

SITE_COLUMNS = ["Date/Time", "Account Name", "Card Number", "Location",
                "Transaction Type", "Amount"]
DROP_COLUMNS = []   # e.g. ["Card Number"] if you don't want it in the CSV

FIRST_RUN_START = datetime(2026, 8, 1)
CHUNK_DAYS = 365    # first run is split into chunks of this size

# "(10.37) CAD" -> sign "(", number "10.37", currency "CAD"
MONEY_PATTERN = r"^\s*(\(|-)?\s*\$?([\d,]+\.?\d*)\)?\s*([A-Z]{3})?"


# ---------- browser helpers ----------

def open_and_login(p):
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR), headless=False,
        viewport={"width": 1200, "height": 850},
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(TARGET_URL)
    print("Complete login + Duo in the browser if prompted...")
    page.wait_for_selector(SEARCH_BUTTON, timeout=LOGIN_TIMEOUT_MS)   # only exists on the query page
    print("Logged in.")
    return context, page


def click_and_wait(page, locator):
    """Click something that triggers an ASP.NET AJAX postback and wait until it is done."""
    with page.expect_response(
        lambda r: r.request.method == "POST" and "AccountTransaction.aspx" in r.url,
        timeout=120_000,
    ):
        locator.click()
    page.wait_for_function(
        "!Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack()",
        timeout=120_000,
    )
    page.wait_for_timeout(300)


def set_picker(page, picker_id, dt):
    page.evaluate(
        """([id, y, m, d, h, mi]) =>
            $find(id).set_selectedDate(new Date(y, m - 1, d, h, mi))""",
        [picker_id, dt.year, dt.month, dt.day, dt.hour, dt.minute],
    )


def read_rows(page):
    """Only real data rows; the pager is never included."""
    return page.evaluate("""() => {
        const grid = document.querySelector('#ctl00_MainContent_ResultRadGrid_ctl00');
        if (!grid) return [];
        return [...grid.querySelectorAll('tr.rgRow, tr.rgAltRow')]
            .map(tr => [...tr.children].map(td => td.innerText.trim()));
    }""")


def page_info(page):
    """(current_page, total_pages); (1, 1) if there is no pager."""
    nums = page.evaluate("""() => {
        const info = document.querySelector('.rgInfoPart');
        return info ? [...info.querySelectorAll('strong')].map(s => parseInt(s.innerText)) : [];
    }""")
    return (nums[0], nums[1]) if len(nums) >= 2 else (1, 1)


def go_next_page(page, current):
    pager = page.locator(".rgNumPart").first
    link = pager.get_by_role("link", name=str(current + 1), exact=True)
    if link.count() == 0:                       # next page is behind the "..." button
        link = pager.locator("a[title='Next Pages']")
    click_and_wait(page, link.first)
    page.wait_for_function(
        """n => {
            const s = document.querySelector('.rgInfoPart strong');
            return s && parseInt(s.innerText) === n;
        }""",
        arg=current + 1, timeout=60_000,
    )


def fetch_range(page, start, end):
    """Returns the raw rows for the range, newest first (the site's order)."""
    page.goto(TARGET_URL)                       # fresh query form (session cookie is kept)
    page.wait_for_selector(SEARCH_BUTTON, timeout=60_000)
    set_picker(page, BEGIN_PICKER_ID, start)
    set_picker(page, END_PICKER_ID, end)
    click_and_wait(page, page.locator(SEARCH_BUTTON))

    rows = []
    while True:
        rows += read_rows(page)
        cur, total = page_info(page)
        print(f"    page {cur}/{total}")
        if cur >= total:
            break
        go_next_page(page, cur)
    return rows


def fetch_balances(page):
    """Current balance of every account on the Account Summary page: {name: value}."""
    page.goto(SUMMARY_URL)
    page.wait_for_selector("div.account .accountBalance", timeout=60_000)
    cards = page.evaluate("""() =>
        [...document.querySelectorAll('div.account')].map(d => ({
            name: d.querySelector('.accountName')?.innerText.trim(),
            balance: d.querySelector('.accountBalance')?.innerText.trim(),
        }))""")
    balances = {}
    for c in cards:
        m = re.match(MONEY_PATTERN, c["balance"] or "")
        if not (c["name"] and m):
            print(f"WARNING: could not read balance for {c}")
            continue
        value = float(m.group(2).replace(",", ""))
        balances[c["name"]] = -value if m.group(1) else value
    print("Current balances:", balances)
    return balances


# ---------- cleaning / merging ----------

def to_dataframe(rows):
    rows = [r for r in rows if len(r) == len(SITE_COLUMNS)]
    df = pd.DataFrame(rows, columns=SITE_COLUMNS)
    if df.empty:
        return df

    # "10/22/2026 5:21 PM" -> "2026-10-22 17:21"
    dt = pd.to_datetime(df["Date/Time"], format="%m/%d/%Y %I:%M %p")
    df["Date/Time"] = dt.dt.strftime("%Y-%m-%d %H:%M")

    # "(10.37) CAD" -> -10.37 and "CAD"
    parts = df["Amount"].str.extract(MONEY_PATTERN)
    if parts[1].isna().any():
        print("WARNING: some Amount values could not be parsed; check the CSV.")
    value = parts[1].str.replace(",", "", regex=False).astype(float)
    value = value.where(parts[0].isna(), -value)
    df["Amount"] = value.map(lambda v: "" if pd.isna(v) else f"{v:.2f}")
    df["Currency"] = parts[2]

    df = df.drop(columns=DROP_COLUMNS).fillna("").astype(str)
    return df


def merge_new_only(existing, fetched):
    """Keep only rows not already stored. Count-based, so two genuinely identical
    transactions (same minute, place, amount) are both kept."""
    if existing.empty or fetched.empty:
        return fetched
    cols = list(fetched.columns)
    existing_counts = existing.groupby(cols).size().to_dict()
    seen, keep = {}, []
    for row in fetched.itertuples(index=False, name=None):
        seen[row] = seen.get(row, 0) + 1
        keep.append(seen[row] > existing_counts.get(row, 0))
    return fetched[keep]


BALANCE_SUFFIX = " Balance"


def is_balance_column(name):
    return name == "Balance" or name.endswith(BALANCE_SUFFIX)


def add_balance(df, balances):
    """Add one balance column per account, e.g. 'TBucks Balance'.

    Each column shows that account's balance right AFTER the row's moment in time
    (rows belonging to other accounts simply carry the value forward). The site only
    shows the balance as of now, so it is reconstructed backwards:
        balance after row i = current balance - sum(that account's amounts after row i)
    `df` must be in chronological order (oldest first) with a unique index.
    Works in integer cents to avoid floating-point drift.
    """
    cents = (df["Amount"].replace("", "0").astype(float) * 100).round().astype("int64")
    out = df.copy()

    for account, current in balances.items():
        own = cents.where(df["Account Name"] == account, 0)   # only this account's amounts
        cumulative = own.cumsum()
        later = cumulative.iloc[-1] - cumulative                # what happens after each row
        after = round(current * 100) - later
        out[account + BALANCE_SUFFIX] = after.map(lambda c: f"{c / 100:.2f}")

    missing = sorted(set(df["Account Name"]) - set(balances))
    if missing:
        print(f"WARNING: no current balance found for {missing}; they get no balance column.")
    return out


# ---------- main ----------

def run():
    """Fetch new transactions + balances and update the CSV. Returns the number of new rows."""
    with sync_playwright() as p:
        context, page = open_and_login(p)
        try:
            end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

            if CSV_FILE.exists():
                existing = pd.read_csv(CSV_FILE, dtype=str).fillna("")
                last = pd.to_datetime(existing["Date/Time"]).max()
                start = last.to_pydatetime().replace(hour=0, minute=0)   # re-check the last day
                print(f"Incremental fetch from {start:%Y-%m-%d}")
            else:
                existing = pd.DataFrame()
                start = FIRST_RUN_START
                print(f"First run, fetching from {start:%Y-%m-%d}")

            # Balance columns are derived, so they are not part of the duplicate comparison
            existing = existing.drop(columns=[c for c in existing.columns if is_balance_column(c)])

            total = existing.copy()
            cursor = start
            while cursor < end:
                chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
                print(f"  Querying {cursor:%Y-%m-%d} -> {chunk_end:%Y-%m-%d}")
                # Reverse to oldest-first so same-minute transactions keep their real order
                df = to_dataframe(fetch_range(page, cursor, chunk_end)[::-1])
                new = merge_new_only(total, df)
                total = pd.concat([total, new], ignore_index=True)
                print(f"    {len(df)} rows fetched, {len(new)} new")
                cursor = chunk_end

            added = len(total) - len(existing)

            if total.empty:
                print("Nothing to save.")
            else:
                balances = fetch_balances(page)
                total = total.sort_values("Date/Time", kind="stable").reset_index(drop=True)
                total = add_balance(total, balances)
                total.to_csv(CSV_FILE, index=False)
                print(f"Added {added} new rows. Total in CSV: {len(total)}")
            return added
        finally:
            context.close()


if __name__ == "__main__":
    run()