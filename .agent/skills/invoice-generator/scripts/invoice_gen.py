#!/usr/bin/env python3
"""
invoice_gen.py — Generate Catalyze invoices from Google Sheet time-log data.

Reads monthly tabs from the invoice spreadsheet, calculates totals using
a fixed hourly rate, and produces a professional PDF invoice.

Usage:
  python invoice_gen.py generate --month August [--year 2026] [--workspace catalyze]
  python invoice_gen.py list-tabs [--workspace catalyze]
  python invoice_gen.py validate --month August [--year 2026] [--workspace catalyze]
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from collections import OrderedDict

# ---------------------------------------------------------------------------
# paths / constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..', '..'))

sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'workspaces'))
import workspace_resolver as ws

INVOICES_DIR = os.path.join(REPO_ROOT, 'invoices')
os.makedirs(INVOICES_DIR, exist_ok=True)

HOURLY_RATE = 175_000  # IDR per hour
CURRENCY = 'Rp'
WIB = timezone(timedelta(hours=7))

SPREADSHEET_ID = '1WKRVlUzG1RY0nL-Eb-ahPn4neigBJ4iSsjAOT7C-dqA'

# Config
CONFIG_PATH = os.path.join(SCRIPT_DIR, '..', 'config.json')

def load_config():
    """Load invoice config from config.json."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

# Column positions (1-indexed for Sheets API)
COL_DATE = 0        # A
COL_TIME = 1        # B
COL_PROJECT = 2     # C
COL_DESCRIPTION = 3 # D
COL_TIME_HM = 4     # E
COL_TIME_DEC = 5    # F

# Month names for tab matching
MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]

# UTF-8 output on Windows
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass


# ---------------------------------------------------------------------------
# auth — reuses the catalyze workspace Drive token (same pattern as timesheet_writer)
# ---------------------------------------------------------------------------
def get_sheets_service(workspace='catalyze'):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = ['https://www.googleapis.com/auth/drive']
    ctx = ws.get(workspace)
    token_file = ctx.token('drive')

    if not os.path.exists(token_file):
        print(f'Error: No Drive token for workspace "{workspace}".', file=sys.stderr)
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, 'w') as f:
                f.write(creds.to_json())
        else:
            print('Error: Token expired and cannot refresh.', file=sys.stderr)
            sys.exit(1)

    return build('sheets', 'v4', credentials=creds)


# ---------------------------------------------------------------------------
# sheet helpers
# ---------------------------------------------------------------------------
def list_tabs(service):
    """Return list of sheet names in the invoice spreadsheet."""
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return [s['properties']['title'] for s in meta.get('sheets', [])]


def find_month_tab(tabs, month_name, year=None):
    """Find the tab matching the given month. Accepts 'August', 'Aug', etc."""
    month_lower = month_name.lower().strip()
    for tab in tabs:
        tab_lower = tab.lower().strip()
        if tab_lower == month_lower:
            return tab
        # Match 'August 2026' or 'Aug 2026'
        if month_lower in tab_lower:
            return tab
    # Try 3-letter abbreviations
    for i, full in enumerate(MONTH_NAMES):
        if full[:3].lower() == month_lower[:3]:
            for tab in tabs:
                if full[:3].lower() in tab.lower():
                    return tab
    return None


def read_month_data(service, tab_name):
    """Read all rows from a monthly tab. Returns list of dicts."""
    range_str = f"'{tab_name}'!A:F"
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=range_str,
    ).execute()
    rows = result.get('values', [])
    if not rows:
        return []

    entries = []
    for row in rows[1:]:  # skip header
        # Pad row to 6 columns
        while len(row) < 6:
            row.append('')
        row = [c.strip() if isinstance(c, str) else str(c).strip() for c in row]

        # Skip empty rows (no project and no description)
        if not row[COL_PROJECT] and not row[COL_DESCRIPTION]:
            continue
        # Skip header-like rows
        if row[COL_PROJECT].lower() in ('project', 'total', ''):
            if not row[COL_DESCRIPTION]:
                continue

        entry = {
            'date': row[COL_DATE],
            'time': row[COL_TIME],
            'project': row[COL_PROJECT],
            'description': row[COL_DESCRIPTION],
            'time_hm': row[COL_TIME_HM],
        }

        # Parse decimal hours
        try:
            entry['hours'] = float(row[COL_TIME_DEC])
        except (ValueError, IndexError):
            # Try calculating from H/M format
            entry['hours'] = _parse_hm_to_decimal(row[COL_TIME_HM])

        entries.append(entry)

    return entries


def _parse_hm_to_decimal(hm_str):
    """Convert '1h 30m' or '1:30' or '1.5' to decimal hours."""
    if not hm_str:
        return None
    hm_str = hm_str.strip().lower()

    # Pattern: "1h 30m" or "1h30m"
    m = re.match(r'(\d+)h\s*(\d+)?m?', hm_str)
    if m:
        hours = int(m.group(1))
        minutes = int(m.group(2)) if m.group(2) else 0
        return hours + minutes / 60.0

    # Pattern: "1:30"
    m = re.match(r'(\d+):(\d+)', hm_str)
    if m:
        hours = int(m.group(1))
        minutes = int(m.group(2))
        return hours + minutes / 60.0

    # Pattern: "1.5"
    try:
        return float(hm_str)
    except ValueError:
        return None


def get_last_work_date(entries):
    """Get the last work date from the entries (for invoice date)."""
    dates = []
    for e in entries:
        d = e.get('date', '')
        if not d:
            continue
        # Try various date formats
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d %B %Y', '%B %d, %Y', '%d %b %Y'):
            try:
                dates.append(datetime.strptime(d, fmt))
                break
            except ValueError:
                continue
    return max(dates) if dates else None


def find_month_index(month_name):
    """Return 1-12 for a month name (matches full name or 3-letter abbrev)."""
    name = month_name.strip().lower()
    for i, full in enumerate(MONTH_NAMES):
        if full.lower() == name or full[:3].lower() == name[:3]:
            return i + 1
    return None


def get_next_invoice_number(invoice_date):
    """Build the invoice number: INV-DDMMYY-XXX (e.g. INV-310826-001).
    The sequence number increments based on existing invoices for the same date."""
    prefix = invoice_date.strftime('%d%m%y') if isinstance(invoice_date, datetime) else \
        datetime.now(WIB).strftime('%d%m%y')

    # Find existing invoices for this date prefix to pick the next sequence
    existing_nums = []
    if os.path.exists(INVOICES_DIR):
        for f in os.listdir(INVOICES_DIR):
            m = re.match(rf'INV-{prefix}-(\d+)', f)
            if m:
                existing_nums.append(int(m.group(1)))
    next_num = max(existing_nums) + 1 if existing_nums else 1
    return f'INV-{prefix}-{next_num:03d}'


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------
def format_idr(amount):
    """Format amount as IDR: Rp1,234,567"""
    return f'Rp{amount:,.0f}'.replace(',', '.')


def format_rate(amount):
    """Format hourly rate: 175,000.00"""
    return f'{amount:,.2f}'.replace(',', '.')


def generate_pdf(entries, month_name, year, output_path):
    """Generate a professional PDF invoice."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm, cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
        HRFlowable, KeepTogether, Image
    )
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Load config
    cfg = load_config()
    bank_name = cfg.get('bank_name', 'BNI')
    account_number = cfg.get('account_number', '2017667507')
    account_name = cfg.get('account_name', 'Said Iskandar')
    signer_name = cfg.get('signer_name', 'Said Iskandar')
    company_name = cfg.get('company_name', 'PT Catalyze')
    company_address = cfg.get('company_address', ['Jl. Penyaringan No 31, Sanur', 'Denpasar Selatan, Bali, Indonesia'])
    signature_path = cfg.get('signature_image')
    materai_path = cfg.get('materai_image')

    # --- document setup ---
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=20*mm,
        rightMargin=20*mm,
        topMargin=20*mm,
        bottomMargin=25*mm,
    )

    # --- styles ---
    styles = getSampleStyleSheet()

    style_title = ParagraphStyle(
        'InvoiceTitle',
        parent=styles['Title'],
        fontSize=22,
        leading=28,
        spaceAfter=2*mm,
        textColor=colors.HexColor('#1a1a1a'),
        fontName='Helvetica-Bold',
    )

    style_normal = ParagraphStyle(
        'InvoiceNormal',
        parent=styles['Normal'],
        fontSize=9,
        leading=12,
        fontName='Helvetica',
        alignment=TA_LEFT,
    )

    style_small = ParagraphStyle(
        'InvoiceSmall',
        parent=styles['Normal'],
        fontSize=8,
        leading=10,
        fontName='Helvetica',
        alignment=TA_LEFT,
    )

    style_bold = ParagraphStyle(
        'InvoiceBold',
        parent=styles['Normal'],
        fontSize=9,
        leading=12,
        fontName='Helvetica-Bold',
    )

    style_right = ParagraphStyle(
        'InvoiceRight',
        parent=style_normal,
        alignment=TA_RIGHT,
    )

    style_center = ParagraphStyle(
        'InvoiceCenter',
        parent=style_normal,
        alignment=TA_CENTER,
    )

    style_header_cell = ParagraphStyle(
        'HeaderCell',
        parent=style_small,
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.white,
    )

    style_cell = ParagraphStyle(
        'Cell',
        parent=style_small,
        fontSize=8,
        leading=10,
    )

    style_cell_right = ParagraphStyle(
        'CellRight',
        parent=style_cell,
        alignment=TA_RIGHT,
    )

    style_cell_center = ParagraphStyle(
        'CellCenter',
        parent=style_cell,
        alignment=TA_CENTER,
    )

    style_total_label = ParagraphStyle(
        'TotalLabel',
        parent=style_bold,
        fontSize=10,
        alignment=TA_RIGHT,
    )

    style_total_value = ParagraphStyle(
        'TotalValue',
        parent=style_bold,
        fontSize=10,
        alignment=TA_RIGHT,
    )

    style_footer = ParagraphStyle(
        'Footer',
        parent=style_normal,
        fontSize=8,
        leading=10,
        fontName='Helvetica',
    )

    style_footer_bold = ParagraphStyle(
        'FooterBold',
        parent=style_footer,
        fontName='Helvetica-Bold',
    )

    # --- calculate totals ---
    grand_total = 0
    table_data = []

    for e in entries:
        hours = e.get('hours', 0) or 0
        total = hours * HOURLY_RATE
        grand_total += total

        table_data.append([
            Paragraph(e['project'] or '', style_cell),
            Paragraph(e['description'] or '', style_cell),
            Paragraph(format_rate(HOURLY_RATE), style_cell_right),
            Paragraph(f'{hours:.2f}', style_cell_right),
            Paragraph(format_idr(total), style_cell_right),
        ])

    # --- invoice date: last calendar day of the month ---
    cal = __import__('calendar')
    # Resolve month index from the tab name
    month_idx = find_month_index(month_name)
    if month_idx:
        last_day = cal.monthrange(year, month_idx)[1]
        invoice_dt = datetime(year, month_idx, last_day)
        invoice_date = invoice_dt.strftime('%d %B %Y')
    else:
        invoice_dt = datetime.now(WIB)
        invoice_date = f'Last day of {month_name} {year}'

    # --- invoice number ---
    invoice_number = get_next_invoice_number(invoice_dt)

    # --- build document ---
    elements = []

    # Header section
    elements.append(Paragraph('INVOICE', style_title))
    elements.append(Spacer(1, 3*mm))

    # Header info table
    header_data = [
        [Paragraph('<b>To:</b>', style_normal),
         Paragraph(company_name, style_normal)],
    ]
    for addr_line in company_address:
        header_data.append(['', Paragraph(addr_line, style_small)])
    header_data.extend([
        ['', ''],
        [Paragraph('<b>Invoice Date:</b>', style_normal),
         Paragraph(invoice_date, style_normal)],
        [Paragraph('<b>Invoice Number:</b>', style_normal),
         Paragraph(invoice_number, style_normal)],
        ['', ''],
        [Paragraph('<b>Name:</b>', style_normal),
         Paragraph(signer_name, style_normal)],
    ])

    header_table = Table(header_data, colWidths=[80, 300], hAlign='LEFT')
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 8*mm))

    # Divider
    elements.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#cccccc')))
    elements.append(Spacer(1, 5*mm))

    # Main invoice table header
    col_widths = [80, 200, 55, 50, 75]  # Project, Desc, Rate, Hours, Total
    invoice_header = [
        Paragraph('Project', style_header_cell),
        Paragraph('Description', style_header_cell),
        Paragraph('Hourly Rate', style_header_cell),
        Paragraph('Hours Spent', style_header_cell),
        Paragraph('Total', style_header_cell),
    ]

    # Build full table
    full_table_data = [invoice_header] + table_data

    # Grand total row
    grand_total_row = [
        '',
        '',
        '',
        Paragraph('<b>TOTAL (gross)</b>', style_total_label),
        Paragraph(f'<b>{format_idr(grand_total)}</b>', style_total_value),
    ]
    full_table_data.append(grand_total_row)

    # Create table
    invoice_table = Table(full_table_data, colWidths=col_widths, repeatRows=1)

    # Table styling
    table_style_cmds = [
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('ALIGN', (0, 0), (-1, 0), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        # Data rows
        ('FONTSIZE', (0, 1), (-1, -2), 8),
        ('ALIGN', (2, 1), (2, -1), 'RIGHT'),  # Rate
        ('ALIGN', (3, 1), (3, -1), 'RIGHT'),  # Hours
        ('ALIGN', (4, 1), (4, -1), 'RIGHT'),  # Total
        # Grand total row
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#ecf0f1')),
        ('FONTNAME', (3, -1), (4, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (3, -1), (4, -1), 10),
        # Borders
        ('GRID', (0, 0), (-1, -2), 0.5, colors.HexColor('#bdc3c7')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#2c3e50')),
        ('LINEBELOW', (0, -1), (-1, -1), 1, colors.HexColor('#2c3e50')),
        # Padding
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]

    # Alternate row colors for data rows
    for i in range(1, len(full_table_data) - 1):
        if i % 2 == 0:
            table_style_cmds.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#f8f9fa')))

    invoice_table.setStyle(TableStyle(table_style_cmds))
    elements.append(invoice_table)
    elements.append(Spacer(1, 10*mm))

    # Bank details section
    elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#cccccc')))
    elements.append(Spacer(1, 3*mm))
    elements.append(Paragraph('<b>Consultant\'s Bank Detail</b>', style_bold))
    elements.append(Spacer(1, 2*mm))

    bank_data = [
        [Paragraph('<b>Bank:</b>', style_normal), Paragraph(bank_name, style_normal)],
        [Paragraph('<b>Account Number:</b>', style_normal), Paragraph(account_number, style_normal)],
        [Paragraph('<b>Account Name:</b>', style_normal), Paragraph(account_name, style_normal)],
    ]
    bank_table = Table(bank_data, colWidths=[100, 200])
    bank_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    elements.append(bank_table)
    elements.append(Spacer(1, 3*mm))
    elements.append(Paragraph('<i>Note: Attach the Time Log that have been approved</i>', style_small))
    elements.append(Spacer(1, 8*mm))

    # Approval section
    elements.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#cccccc')))
    elements.append(Spacer(1, 3*mm))

    approval_data = [
        [Paragraph('<b>Issued by</b>', style_center),
         Paragraph('<b>Checked by</b>', style_center),
         Paragraph('<b>Approved by</b>', style_center)],
        ['', '', ''],
    ]

    # Build signature cell: signature on left, materai on right overlapping slightly
    sig_cell_items = []
    if signature_path and os.path.exists(signature_path):
        try:
            sig_cell_items.append(Image(signature_path, width=30*mm, height=12*mm))
        except Exception:
            pass
    if materai_path and os.path.exists(materai_path):
        try:
            sig_cell_items.append(Image(materai_path, width=22*mm, height=22*mm))
        except Exception:
            pass

    if len(sig_cell_items) == 2:
        # Nested table: signature left, materai right with negative padding to overlap
        inner_table = Table([sig_cell_items], colWidths=[32*mm, 18*mm])
        inner_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (0, 0), 'RIGHT'),
            ('ALIGN', (1, 0), (1, 0), 'LEFT'),
            ('LEFTPADDING', (1, 0), (1, 0), -6*mm),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('LEFTPADDING', (0, 0), (0, 0), 0),
        ]))
        sig_cell = inner_table
    elif sig_cell_items:
        sig_cell = sig_cell_items[0]
    else:
        sig_cell = Paragraph('_________________', style_center)

    approval_data.append([sig_cell, Paragraph('_________________', style_center), Paragraph('_________________', style_center)])
    approval_data.append([
        Paragraph(signer_name, style_center),
        Paragraph('HRGA Manager', style_center),
        Paragraph('MD', style_center),
    ])

    approval_table = Table(approval_data, colWidths=[150, 150, 150])
    approval_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(approval_table)

    # Build PDF
    doc.build(elements)
    return invoice_number, grand_total, invoice_date


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate_entries(entries):
    """Validate time-log entries. Returns (ok, errors)."""
    errors = []
    for i, e in enumerate(entries, 1):
        if e.get('hours') is None:
            errors.append(f'Row {i}: No hours data (no Time Decimal or Time H/M)')
        elif not isinstance(e['hours'], (int, float)) or e['hours'] < 0:
            errors.append(f'Row {i}: Invalid hours value: {e["hours"]}')

    total_hours = sum(e.get('hours', 0) or 0 for e in entries)
    if total_hours <= 0:
        errors.append('No valid hours found in any row')

    return len(errors) == 0, errors, total_hours


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_list_tabs(args):
    """List available tabs in the invoice spreadsheet."""
    service = get_sheets_service(args.workspace)
    tabs = list_tabs(service)
    print(f'Available tabs ({len(tabs)}):')
    for t in tabs:
        print(f'  - {t}')


def cmd_validate(args):
    """Validate a monthly tab without generating PDF."""
    service = get_sheets_service(args.workspace)
    tabs = list_tabs(service)

    month_name = args.month
    year = args.year or datetime.now(WIB).year

    tab = find_month_tab(tabs, month_name, year)
    if not tab:
        print(f'Error: No tab found for "{month_name}".')
        print(f'Available tabs: {", ".join(tabs)}')
        sys.exit(1)

    entries = read_month_data(service, tab)
    if not entries:
        print(f'Error: No data found in tab "{tab}".')
        sys.exit(1)

    ok, errors, total_hours = validate_entries(entries)
    print(f'Tab: {tab}')
    print(f'Rows: {len(entries)}')
    print(f'Total hours: {total_hours:.2f}')
    print(f'Total amount: {format_idr(total_hours * HOURLY_RATE)}')

    if ok:
        print('Validation: PASSED')
    else:
        print('Validation: FAILED')
        for e in errors:
            print(f'  - {e}')
        sys.exit(1)


def cmd_generate(args):
    """Generate invoice PDF from a monthly tab."""
    service = get_sheets_service(args.workspace)
    tabs = list_tabs(service)

    month_name = args.month
    year = args.year or datetime.now(WIB).year

    tab = find_month_tab(tabs, month_name, year)
    if not tab:
        print(f'Error: No tab found for "{month_name}".')
        print(f'Available tabs: {", ".join(tabs)}')
        sys.exit(1)

    entries = read_month_data(service, tab)
    if not entries:
        print(f'Error: No data found in tab "{tab}".')
        sys.exit(1)

    ok, errors, total_hours = validate_entries(entries)
    if not ok:
        print('Validation FAILED:')
        for e in errors:
            print(f'  - {e}')
        sys.exit(1)

    # Determine output filename
    display_month = tab  # Use the actual tab name
    filename = f'Invoice - Said - {display_month}.pdf'
    output_path = os.path.join(INVOICES_DIR, filename)

    # Check if exists
    if os.path.exists(output_path) and not args.force:
        print(f'Invoice already exists: {output_path}')
        print('Use --force to overwrite.')
        sys.exit(1)

    # Generate PDF
    invoice_number, grand_total, invoice_date = generate_pdf(
        entries, tab, year, output_path
    )

    # Output result
    print(f'Month invoiced: {tab}')
    print(f'Invoice date: {invoice_date}')
    print(f'Invoice number: {invoice_number}')
    print(f'Total hours: {total_hours:.2f}')
    print(f'Total amount: {format_idr(grand_total)}')
    print(f'PDF saved to: {output_path}')


def main():
    p = argparse.ArgumentParser(description='Catalyze Invoice Generator')
    p.add_argument('--workspace', default='catalyze', help='Workspace name')
    sub = p.add_subparsers(dest='cmd')

    lt = sub.add_parser('list-tabs', help='List available monthly tabs')

    val = sub.add_parser('validate', help='Validate a monthly tab')
    val.add_argument('--month', required=True, help='Month name (e.g. August)')
    val.add_argument('--year', type=int, default=None, help='Year (default: current)')

    gen = sub.add_parser('generate', help='Generate invoice PDF')
    gen.add_argument('--month', required=True, help='Month name (e.g. August)')
    gen.add_argument('--year', type=int, default=None, help='Year (default: current)')
    gen.add_argument('--force', action='store_true', help='Overwrite existing invoice')

    args = p.parse_args()
    if args.cmd == 'list-tabs':
        cmd_list_tabs(args)
    elif args.cmd == 'validate':
        cmd_validate(args)
    elif args.cmd == 'generate':
        cmd_generate(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
