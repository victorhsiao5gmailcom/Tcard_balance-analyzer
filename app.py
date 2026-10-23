"""TCard dashboard.  Run:  python app.py   (or gunicorn app:server)"""
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output, State, ctx, no_update
from dash.exceptions import PreventUpdate

from analysis import BALANCE_SUFFIX, balance_accounts, choose_freq, fit_runout, load_transactions

CSV_PATH = Path(os.environ.get("TCARD_CSV", Path(__file__).resolve().parent / "tcard_transactions.csv"))
UPDATES_ENABLED = os.environ.get("TCARD_UPDATE", "1") != "0"   # set to 0 when using sample data
FREQ_NAMES = {"D": "daily", "W": "weekly", "M": "monthly"}
_cache = {"mtime": None, "df": None}


def get_data():
    """Re-reads the CSV only when the scraper has changed it."""
    mtime = os.path.getmtime(CSV_PATH)
    if _cache["mtime"] != mtime:
        _cache.update(mtime=mtime, df=load_transactions(CSV_PATH))
    return _cache["df"]


# ---- background update: runs tcard.run() (login window + fetch) without blocking the dashboard
_job = {"state": "idle", "message": "", "finished": 0.0}
_job_lock = threading.Lock()


def _run_update():
    try:
        import tcard                              # imported lazily so the dashboard works without playwright
        added = tcard.run()
        _job.update(state="done", message=f"Update finished: {added} new transaction(s).")
    except Exception as e:                        # login timeout, site changed, playwright missing, ...
        _job.update(state="error", message=f"Update failed: {e}")
    finally:
        _job["finished"] = time.time()


def start_update():
    with _job_lock:
        if _job["state"] == "running":
            return False
        _job.update(state="running",
                    message="A Chromium window has opened: log in and complete Duo there...")
        threading.Thread(target=_run_update, daemon=True).start()
        return True


app = Dash(__name__, title="TCard dashboard", suppress_callback_exceptions=True)
server = app.server                       # for gunicorn

if os.environ.get("TCARD_USER"):          # optional login; needs HTTPS in production
    import dash_auth
    dash_auth.BasicAuth(app, {os.environ["TCARD_USER"]: os.environ["TCARD_PASS"]})


def label(text):
    return html.Div(text, style={"fontWeight": 600, "margin": "14px 0 4px"})


def update_bar():
    return html.Div([
        html.Button("Update data", id="update-btn", n_clicks=0, disabled=not UPDATES_ENABLED),
        html.Span(id="status", style={"marginLeft": 12, "color": "#555"}),
        dcc.Interval(id="poll", interval=2000),
        dcc.Location(id="url", refresh=True),
        dcc.Store(id="loaded-at", data=time.time()),
    ], style={"marginBottom": 8})


def serve_layout():                       # runs on every page load -> picks up new data
    try:
        df = get_data()
    except FileNotFoundError:
        return html.Div([update_bar(), html.Div(f"No data yet ({CSV_PATH.name}). Click 'Update data' to fetch it.")],
                        style={"padding": 24, "fontFamily": "system-ui, sans-serif"})
    accounts = balance_accounts(df)
    locations = sorted(df["Location"].unique())
    types = sorted(df["Transaction Type"].unique())
    d0, d1 = df["Date/Time"].min().date(), df["Date/Time"].max().date()

    sidebar = html.Div([
        html.H3("Filters", style={"marginTop": 0}),
        label("Account"),
        dcc.Dropdown(id="account", options=accounts, value=accounts[0] if accounts else None, clearable=False),
        label("Date range"),
        dcc.DatePickerRange(id="dates", min_date_allowed=d0, max_date_allowed=d1,
                            start_date=d0, end_date=d1, display_format="YYYY-MM-DD"),
        label("Spending chart scale"),
        dcc.RadioItems(id="scale", value="auto", options=[
            {"label": " Auto", "value": "auto"}, {"label": " Day", "value": "D"},
            {"label": " Week", "value": "W"}, {"label": " Month", "value": "M"}]),
        label("Payment type"),
        dcc.Checklist(id="types", options=types, value=types),
        label("Location"),
        html.Button("All", id="loc-all", n_clicks=0), html.Button("None", id="loc-none", n_clicks=0,
                                                                    style={"marginLeft": 6}),
        dcc.Checklist(id="locations", options=locations, value=locations,
                      style={"marginTop": 8}, labelStyle={"display": "block", "margin": "3px 0"}),
    ], style={"width": 300, "padding": 16, "background": "#f5f6f8", "overflowY": "auto"})

    main = html.Div([
        update_bar(),
        html.Div(id="summary", style={"display": "flex", "gap": 12, "flexWrap": "wrap"}),
        dcc.Graph(id="balance-fig", style={"height": "42vh"}),
        dcc.Graph(id="spend-fig", style={"height": "42vh"}),
    ], style={"flex": 1, "padding": 16, "minWidth": 0})

    return html.Div([sidebar, main], style={"display": "flex", "height": "100vh",
                                            "fontFamily": "system-ui, sans-serif"})


app.layout = serve_layout


def card(title, value, sub=""):
    return html.Div([html.Div(title, style={"fontSize": 12, "color": "#666"}),
                     html.Div(value, style={"fontSize": 20, "fontWeight": 600}),
                     html.Div(sub, style={"fontSize": 12, "color": "#666"})],
                    style={"background": "#f5f6f8", "padding": "8px 14px", "borderRadius": 8, "minWidth": 150})


@app.callback(Output("status", "children"), Output("update-btn", "disabled"), Output("url", "search"),
              Input("poll", "n_intervals"), Input("update-btn", "n_clicks"), State("loaded-at", "data"))
def poll_update(_n, _clicks, loaded_at):
    if ctx.triggered_id == "update-btn" and UPDATES_ENABLED:
        start_update()
    state = _job["state"]
    if state == "idle":
        text = (f"Data last updated {datetime.fromtimestamp(CSV_PATH.stat().st_mtime):%Y-%m-%d %H:%M}"
                if CSV_PATH.exists() else "")
    else:
        text = _job["message"]
    # new data arrived after this page was built -> reload so filters/ranges pick it up
    reload = f"?v={int(time.time())}" if state == "done" and _job["finished"] > loaded_at else no_update
    return text, state == "running" or not UPDATES_ENABLED, reload


@app.callback(Output("locations", "value"), Input("loc-all", "n_clicks"), Input("loc-none", "n_clicks"),
              State("locations", "options"), prevent_initial_call=True)
def select_locations(_all, _none, options):
    return list(options) if ctx.triggered_id == "loc-all" else []


@app.callback(Output("balance-fig", "figure"), Output("spend-fig", "figure"), Output("summary", "children"),
              Input("account", "value"), Input("dates", "start_date"), Input("dates", "end_date"),
              Input("scale", "value"), Input("types", "value"), Input("locations", "value"))
def update(account, start, end, scale, types, locations):
    if not (start and end and account):
        raise PreventUpdate
    df = get_data()
    start, end = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)   # end day inclusive
    rows = df[(df["Account Name"] == account) & (df["Date/Time"] >= start) & (df["Date/Time"] < end)]

    # ---- balance + linear regression (uses all of the account's rows, ignores type/location filters)
    bal = go.Figure()
    col = account + BALANCE_SUFFIX
    fit = fit_runout(rows["Date/Time"], rows[col]) if len(rows) else None
    bal.add_scatter(x=rows["Date/Time"], y=rows[col], mode="lines", line_shape="hv", name="Balance")
    run_out = "n/a"
    if fit:
        last = rows["Date/Time"].max()
        x_end = last
        if fit["zero"] is not None:
            run_out = f"{fit['zero']:%Y-%m-%d}"
            if fit["zero"] - last <= pd.Timedelta(days=730):
                x_end = fit["zero"]                       # extend the trend line to zero
        xs = [fit["t0"], x_end]
        ys = [fit["intercept"] + fit["slope"] * (x - fit["t0"]).total_seconds() / 86400 for x in xs]
        bal.add_scatter(x=xs, y=ys, mode="lines", line={"dash": "dash"}, name="Linear fit")
        bal.add_hline(y=0, line_color="#c0392b", line_width=1)
    bal.update_layout(title=f"{account}: balance", margin={"t": 40, "b": 30}, yaxis_title="CAD",
                      legend={"orientation": "h"})

    # ---- spending: filtered by type/location, bucketed according to the selected range
    sel = rows[rows["Transaction Type"].isin(types or []) & rows["Location"].isin(locations or [])]
    freq = choose_freq((end - start).days) if scale == "auto" else scale
    spend = go.Figure()
    if not sel.empty:
        sel = sel.assign(bucket=sel["Date/Time"].dt.to_period(freq).dt.start_time)
        g = sel.groupby(["Location", "Transaction Type", "bucket"], as_index=False)["Amount"].sum()
        for loc, part in g.groupby("Location"):
            spend.add_bar(x=part["bucket"], y=part["Amount"], name=loc, customdata=part["Transaction Type"],
                          hovertemplate="%{x|%Y-%m-%d}<br>%{customdata}: %{y:.2f}<extra>" + loc + "</extra>")
    spend.update_layout(barmode="relative", title=f"Transactions ({FREQ_NAMES[freq]}; debits below zero)",
                        margin={"t": 40, "b": 30}, yaxis_title="CAD", legend={"orientation": "h"})

    spent = -sel.loc[sel["Amount"] < 0, "Amount"].sum()
    added = sel.loc[sel["Amount"] > 0, "Amount"].sum()
    current = df[col].iloc[-1]                      # newest row carries every account's current balance
    cards = [card("Current balance", f"${current:,.2f}"),
             card("Estimated run-out", run_out,
                  f"{(fit['zero'] - pd.Timestamp.now()).days} days from today" if fit and fit["zero"] is not None
                  else "balance not decreasing" if fit else "need 2+ points"),
             card("Burn rate (fit)", f"${-fit['slope']:,.2f}/day" if fit else "n/a",
                  f"R² = {fit['r2']:.2f}" if fit else ""),
             card("Selected spending", f"${spent:,.2f}", f"{len(sel)} transactions, +${added:,.2f} credits")]
    return bal, spend, cards


if __name__ == "__main__":
    if UPDATES_ENABLED and os.environ.get("TCARD_UPDATE_ON_START", "1") == "1":
        start_update()          # opens the Chromium login window; the page reloads itself when done
    # debug=False: the debug reloader would start a second process and run the scraper twice
    app.run(debug=False, host=os.environ.get("TCARD_HOST", "127.0.0.1"), port=8050)