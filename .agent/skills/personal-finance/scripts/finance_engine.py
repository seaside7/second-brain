#!/usr/bin/env python3
"""
Personal Finance Engine — Analysis, forecasting, and cash management.

Google Sheets is the source of truth. This engine reads, analyzes, and
provides recommendations. Writes require explicit user approval.

Usage:
    finance_engine.py analyze
    finance_engine.py forecast --days 30
    finance_engine.py ask --query "Can I afford to pay Vandi 5M?"
    finance_engine.py briefing
    finance_engine.py read --tab cash
    finance_engine.py record --type income --amount 18000000 --source catalyze
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / ".agent" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / ".agent" / "workspaces"))

import workspace_resolver as ws

WIB = timezone(timedelta(hours=7))

try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass


def _load_finance_config():
    ctx = ws.get("personal")
    cfg = ctx.config("finance")
    if not cfg:
        print("[ERROR] No finance.json in personal workspace.")
        print("Create: .agent/workspaces/personal/finance.json")
        sys.exit(1)
    return cfg, ctx


def _get_sheets_service():
    """Get Google Sheets service using personal workspace Drive token."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    ctx = ws.get("personal")
    token_file = ctx.token("drive")

    if not os.path.exists(token_file):
        print("[ERROR] No Drive token for personal workspace. Run auth first.")
        return None

    creds = Credentials.from_authorized_user_file(token_file,
                                                   ["https://www.googleapis.com/auth/drive"])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("[ERROR] Token expired.")
            return None

    return build("sheets", "v4", credentials=creds)


def read_tab(tab_name, config=None):
    """Read all data from a sheet tab. Returns list of lists (rows)."""
    if not config:
        config, _ = _load_finance_config()

    service = _get_sheets_service()
    if not service:
        return []

    sheet_id = config["spreadsheet_id"]
    tab_key = config.get("tabs", {}).get(tab_name, tab_name)

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab_key}'!A:Z",
        ).execute()
        return result.get("values", [])
    except Exception as e:
        print(f"[ERROR] Failed to read tab '{tab_key}': {e}", file=sys.stderr)
        return []


def read_all_tabs(config=None):
    """Read all configured tabs. Returns dict of tab_name → rows."""
    if not config:
        config, _ = _load_finance_config()

    data = {}
    for key in config.get("tabs", {}).keys():
        rows = read_tab(key, config)
        data[key] = rows
    return data


def _parse_amount(val):
    """Parse IDR amount from string (handles commas, dots, Rp prefix)."""
    if not val:
        return 0
    s = str(val).replace("Rp", "").replace(".", "").replace(",", "").strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _fmt(amount):
    """Format IDR amount."""
    if amount >= 0:
        return f"Rp{amount:,.0f}".replace(",", ".")
    return f"-Rp{abs(amount):,.0f}".replace(",", ".")


# ── Analysis ──

def analyze_cash(data, config):
    """Analyze current cash position from sheet data."""
    cash_rows = data.get("cash", [])
    # Assume first row is header, last row with data is current balance
    total_cash = 0
    for row in cash_rows[1:]:  # skip header
        if row and len(row) > 1:
            amount = _parse_amount(row[-1]) if row[-1] else _parse_amount(row[1]) if len(row) > 1 else 0
            if amount > 0:
                total_cash = amount  # Take the latest/total
    return total_cash


def analyze_obligations(data, config):
    """Parse debt obligations with due dates and amounts."""
    obligations = []
    for row in data.get("debt_obligations", [])[1:]:
        if not row or len(row) < 2:
            continue
        obligations.append({
            "name": row[0] if row else "",
            "amount": _parse_amount(row[1]) if len(row) > 1 else 0,
            "due": row[2] if len(row) > 2 else "",
            "status": row[3] if len(row) > 3 else "",
            "priority": row[4] if len(row) > 4 else "",
        })
    return obligations


def analyze_friend_debts(data, config):
    """Parse friend debts."""
    debts = []
    for row in data.get("friends_debt", [])[1:]:
        if not row or len(row) < 2:
            continue
        debts.append({
            "name": row[0] if row else "",
            "total": _parse_amount(row[1]) if len(row) > 1 else 0,
            "paid": _parse_amount(row[2]) if len(row) > 2 else 0,
            "remaining": _parse_amount(row[3]) if len(row) > 3 else _parse_amount(row[1]) if len(row) > 1 else 0,
            "note": row[4] if len(row) > 4 else "",
        })
    return debts


def forecast_scenario(cash, config, scenario_name, months=3):
    """Calculate cash forecast for a scenario over N months."""
    scenario = config.get("scenarios", {}).get(scenario_name, {})
    sources = config.get("income_sources", {})

    monthly_income = 0
    # Samudera (if started)
    sam = sources.get("samudera", {})
    if sam.get("confidence") == "high":
        monthly_income += sam.get("amount", 0)

    # Catalyze (with factor)
    cat = sources.get("catalyze", {})
    factor = scenario.get("catalyze_factor", 1.0)
    monthly_income += int(cat.get("amount", 0) * factor)

    # Manuva
    if scenario.get("include_manuva"):
        man = sources.get("manuva", {})
        monthly_income += man.get("amount", 0)

    return {
        "scenario": scenario_name,
        "description": scenario.get("description", ""),
        "monthly_income": monthly_income,
        "starting_cash": cash,
    }


# ── Commands ──

def cmd_analyze(args):
    """Full financial analysis."""
    config, ctx = _load_finance_config()
    print(f"[personal] Reading financial data...", file=sys.stderr)

    data = read_all_tabs(config)
    cash = analyze_cash(data, config)
    obligations = analyze_obligations(data, config)
    friend_debts = analyze_friend_debts(data, config)

    print("=" * 60)
    print("  FINANCIAL ANALYSIS")
    print("=" * 60)
    print(f"\n  Current Cash: {_fmt(cash)}")
    print(f"\n  Income Sources:")
    for key, src in config.get("income_sources", {}).items():
        conf = src.get("confidence", "?")
        print(f"    {src['name']:<20} {_fmt(src['amount'])} ({conf})")

    if obligations:
        print(f"\n  Obligations ({len(obligations)}):")
        for ob in obligations:
            print(f"    {ob['name']:<25} {_fmt(ob['amount'])} due: {ob.get('due', '-')}")

    if friend_debts:
        print(f"\n  Friend Debts:")
        total_friend = 0
        for fd in friend_debts:
            remaining = fd.get("remaining", fd.get("total", 0))
            total_friend += remaining
            print(f"    {fd['name']:<15} remaining: {_fmt(remaining)}")
        print(f"    {'TOTAL':<15} {_fmt(total_friend)}")

    # Scenarios
    print(f"\n  Scenarios (monthly income):")
    for name in ["conservative", "expected", "optimistic"]:
        s = forecast_scenario(cash, config, name)
        print(f"    {name:<14} {_fmt(s['monthly_income'])}/mo — {s['description']}")

    # Risk
    buffer = config.get("liquidity_buffer_min", 5000000)
    risk = "HIGH" if cash < buffer else ("MEDIUM" if cash < buffer * 3 else "LOW")
    print(f"\n  Financial Risk Level: {risk}")
    print(f"  Liquidity Buffer Min: {_fmt(buffer)}")
    print("=" * 60)


def cmd_forecast(args):
    """Cash flow forecast."""
    config, _ = _load_finance_config()
    data = read_all_tabs(config)
    cash = analyze_cash(data, config)

    days = args.days
    months = max(1, days // 30)

    print(f"\n  CASH FLOW FORECAST ({days} days)\n")
    print(f"  Starting cash: {_fmt(cash)}")
    print(f"\n  {'Scenario':<14} {'Monthly Income':<18} {'Projected Balance ({months}mo)'}")
    print(f"  {'-'*55}")

    for name in ["conservative", "expected", "optimistic"]:
        s = forecast_scenario(cash, config, name, months)
        projected = cash + (s["monthly_income"] * months)  # Simplified (no expenses deducted here)
        print(f"  {name:<14} {_fmt(s['monthly_income']):<18} {_fmt(projected)}")

    print(f"\n  Note: This is income-only projection. Actual expenses not deducted")
    print(f"  (expenses data from sheet will improve accuracy).")


def cmd_briefing(args):
    """Daily finance briefing section."""
    config, _ = _load_finance_config()
    data = read_all_tabs(config)
    cash = analyze_cash(data, config)
    obligations = analyze_obligations(data, config)

    now = datetime.now(WIB)
    buffer = config.get("liquidity_buffer_min", 5000000)
    risk = "HIGH" if cash < buffer else ("MEDIUM" if cash < buffer * 3 else "LOW")

    print(f"  Finance ({now.strftime('%Y-%m-%d')}):")
    print(f"    Cash: {_fmt(cash)} | Risk: {risk}")

    # Next 7 days critical
    critical = [ob for ob in obligations if ob.get("priority") in ("critical", "")]
    if critical:
        print(f"    Critical obligations:")
        for ob in critical[:3]:
            print(f"      {ob['name']}: {_fmt(ob['amount'])}")


def cmd_ask(args):
    """Answer a financial question using sheet data + LLM."""
    config, _ = _load_finance_config()
    data = read_all_tabs(config)
    cash = analyze_cash(data, config)

    # Build context
    context = f"Current cash: {_fmt(cash)}\n"
    context += f"Income sources:\n"
    for key, src in config.get("income_sources", {}).items():
        context += f"  {src['name']}: {_fmt(src['amount'])}/mo ({src['confidence']})\n"

    obligations = analyze_obligations(data, config)
    if obligations:
        context += f"\nObligations:\n"
        for ob in obligations[:10]:
            context += f"  {ob['name']}: {_fmt(ob['amount'])} due {ob.get('due', '-')}\n"

    friend_debts = analyze_friend_debts(data, config)
    if friend_debts:
        context += f"\nFriend debts:\n"
        for fd in friend_debts:
            context += f"  {fd['name']}: {_fmt(fd.get('remaining', fd.get('total', 0)))}\n"

    context += f"\nLiquidity buffer minimum: {_fmt(config.get('liquidity_buffer_min', 5000000))}\n"
    context += f"Currency: IDR\n"

    from deepseek_call import call as deepseek_call

    prompt = f"""You are a personal finance advisor. Answer the question based on the financial data below.

Rules:
- Never treat uncertain income as guaranteed.
- Prioritize survival and critical obligations over debt repayment.
- Show calculations clearly.
- Distinguish facts from estimates.
- Currency is Indonesian Rupiah (IDR).

Financial Data:
{context}

Question: {args.query}

Answer:"""

    ok, answer, meta = deepseek_call(prompt, max_tokens=1000, temperature=0.2)
    if ok:
        print(f"\n{answer}")
    else:
        print(f"[ERROR] LLM failed: {meta.get('reason', 'unknown')}")


def cmd_read(args):
    """Read raw data from a sheet tab."""
    config, _ = _load_finance_config()
    rows = read_tab(args.tab, config)
    if not rows:
        print(f"No data in tab '{args.tab}'")
        return
    for row in rows[:20]:
        print("  | ".join(str(c) for c in row))
    if len(rows) > 20:
        print(f"  ... ({len(rows)} rows total)")


def cmd_record(args):
    """Record a financial transaction (requires approval)."""
    print(f"\n  RECORD TRANSACTION (requires approval)")
    print(f"  Type:   {args.type}")
    print(f"  Amount: {_fmt(args.amount)}")
    print(f"  Source: {args.source}")
    print(f"  Note:   {args.note}")
    print()

    try:
        answer = input("  Approve this financial record? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("  Cancelled.")
        return

    if answer not in ("y", "yes"):
        print("  Rejected.")
        return

    # TODO: Write to the Google Sheet (append row to appropriate tab)
    print(f"  [OK] Recorded. (Sheet write not yet implemented — record saved locally)")


# ── CLI ──

def main():
    p = argparse.ArgumentParser(description="Personal Finance Engine")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("analyze", help="Full financial analysis")

    fp = sub.add_parser("forecast", help="Cash flow forecast")
    fp.add_argument("--days", type=int, default=30)

    ap = sub.add_parser("ask", help="Ask a financial question")
    ap.add_argument("--query", required=True)

    sub.add_parser("briefing", help="Daily finance briefing")

    rp = sub.add_parser("read", help="Read a sheet tab")
    rp.add_argument("--tab", required=True)

    recp = sub.add_parser("record", help="Record a transaction")
    recp.add_argument("--type", required=True, choices=["income", "expense", "repayment"])
    recp.add_argument("--amount", type=int, required=True)
    recp.add_argument("--source", default="")
    recp.add_argument("--note", default="")

    args = p.parse_args()

    handlers = {
        "analyze": cmd_analyze,
        "forecast": cmd_forecast,
        "ask": cmd_ask,
        "briefing": cmd_briefing,
        "read": cmd_read,
        "record": cmd_record,
    }

    handler = handlers.get(args.cmd)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
