# Invoice Generator

Generate Catalyze invoices from Google Sheet time-log data.

## Usage

```bash
# List available monthly tabs
python .agent/skills/invoice-generator/scripts/invoice_gen.py list-tabs

# Validate data before generating
python .agent/skills/invoice-generator/scripts/invoice_gen.py validate --month August

# Generate invoice PDF
python .agent/skills/invoice-generator/scripts/invoice_gen.py generate --month August
```

## Configuration

Edit `.agent/skills/invoice-generator/config.json` to set:
- `bank_name`, `account_number`, `account_name` — bank details
- `signature_image` — path to signature PNG/JPG (relative to repo root, or absolute)
- `signer_name` — name under "Issued by"
- `company_name`, `company_address` — client details

## Spreadsheet

- Sheet ID: `1WKRVlUzG1RY0nL-Eb-ahPn4neigBJ4iSsjAOT7C-dqA`
- Tabs: monthly names (`April`, `May`, `June`, `July`, `August`, etc.)
- Columns: A=Date, B=Time, C=Projects, D=Description, E=Time(H/M), F=Time(Decimal)

## Output

PDFs saved to `invoices/Invoice - Said - [Month].pdf`

## Auth

Uses `catalyze` workspace Drive token (OAuth2 with auto-refresh).
