# Transactions - Logic & Requirements

Home of the personal transactions system (bank / e-wallet tracking, categorization, reconciliation, reporting). Single source of truth for this menu. Operational skill reference: `.agent/skills/transactions/SKILL.md` (kept in place - this doc is the human-facing spec, the SKILL.md is the runtime spec).

## 1. Purpose & scope

Track every money movement across BCA, BNI, Mandiri (Livin'), GoPay and related wallets; categorize them; reconcile transfers between accounts; and produce period summaries. Scope covers inflow (income, loans in, travel advances), outflow (bills, debt payments, shopping, top-ups, cash) and internal movement (transfers between own accounts, e-money top-ups) so the dashboard reports correctly without double counting.

Explicitly out of scope: executing transfers or payments (read-only analysis only).

## 2. Inputs / sources

| Source | Format | How it arrives |
|---|---|---|
| BCA transaction emails (klikbca.com) | Email | `gmail_sync.py` fetch at 23:59 WIB (+ startup catch-up >25h) |
| BNI transaction emails (bni.co.id) | Email | Same Gmail sync |
| Mandiri / Livin' transaction emails (bankmandiri.co.id) | Email | Same Gmail sync; has the trickiest parsing (see SKILL.md Livin' notes) |
| GoPay statement PDFs | PDF upload | `POST /api/transactions/upload` -> `parsers/gopay_pdf.py` |
| Bank statement PDFs / screenshots (manual recap) | PDF / image | Manually loaded, cited per row e.g. `BCA PDF p.19` |
| User confirmation | Chat | Resolves `Perlu konfirmasi` / `Dikonfirmasi pengguna` flags |
| Local SQLite DB | DB | `.agent/workspaces/personal/state/transactions.db` (WAL mode) |

Verified senders list lives in `_VERIFIED_SENDERS` inside `scripts/gmail_sync.py` (mirror in `config/accounts.json` is documentation only - keep both in sync).

## 3. Logic & rules

### 3.1 Ingestion

- Gmail fetch runs 23:59 WIB daily (env `TRANSACTIONS_SYNC_HOUR/MINUTE`); startup catch-up if last sync > 25h ago; manual trigger `POST /api/transactions/sync/gmail`.
- GoPay PDF upload goes through the lifecycle: upload -> `import/preview` -> `import/confirm` (reverses on `import/{id}/delete`).
- Duplicate detection before insert: exact + fuzzy (`duplicates.py`), across all sources.

### 3.2 Normalization (Step 0)

Every source is normalized into: `source`, `direction` (debit/credit), `amount` (strip `Rp`, `.`, `,`), `datetime`, `counterparty_name`, `counterparty_account`/VA/wallet ID, `reference_note`, `channel`. Keep the raw body/row for the small manual-review bucket.

### 3.3 Direction (Step 1)

Keyword on subject/body, trust an explicit labeled `Debit`/`Kredit` field when present:

- Credit (money in): `Transfer Masuk`, `Dana Masuk`, `Kredit`, `Received`, `Terima`
- Debit (money out): `Transfer Keluar`, `Pembayaran`, `Pembelian`, `Debet`, `Sent`, `Top Up` (from the *sending* account's view)

### 3.4 Category decision tree (Step 2) - first match wins, top to bottom

1. **Internal move** (excluded from spend): debit to one of YOUR OWN accounts/wallets (bank -> GoPay, GoPay -> bank, etc.) -> `Transfer internal`.
2. **Person transfer**: counterparty matches known contact (wife etc.) -> `Transfer keluarga` / named category, tracked separately from expenses.
3. **3rd-party top-up**: note has `Top Up` / `Isi Ulang` / `Voucher`, destination is NOT your wallet -> `Top Up (3rd party)`.
4. **Bill payment**: biller match -> `Bills - <subtype>` (PLN, PDAM, INDIHOME, TELKOMSEL, BPJS, card). In the recap sheet these appear as `Tagihan rumah`, `KPR`, `Cicilan kendaraan`, `Asuransi`.
5. **Groceries / retail**: merchant match -> `Groceries` / `Belanja`.
6. **Food & transport**: GOJEK/GOFOOD/GRAB/SHOPEEFOOD -> `Food & Transport`; transport merchants -> `Transportasi`.
7. **E-commerce**: TOKOPEDIA/SHOPEE/LAZADA/BLIBLI -> `Online Shopping`.
8. **Income**: credit + not internal + payroll/client match or `GAJI`/`SALARY`/`PAYROLL`/`INVOICE` -> `Income`.
9. **ATM / cash**: `Tarik Tunai` / `Withdrawal` -> `Cash Withdrawal`.
10. **Fallback**: any leftover -> `Perlu konfirmasi` / uncategorized review bucket (batched through an LLM, never one call per row).

**Why this order matters:** internal moves must be excluded first or every bank-to-wallet top-up double counts as spending (bank leg + wallet leg). Person transfers are their own category, never guessed into Bills/Groceries. Merchant/biller name matching is the highest-signal, lowest-cost rule - keep the lookup table (`merchant_string -> category`) updated; it is where real accuracy lives.

### 3.5 Split fields (recap ledger)

Each ledger row carries a money-out split into four non-overlapping buckets so Ringkasan can total each independently:

- `Pendapatan` (income column)
- `Pengeluaran bank` (real spend out of bank accounts)
- `Pembayaran eksternal` (card / e-wallet funded outside the bank statement, e.g. GoPay Coins, Visa card)
- `Pinjaman masuk` (money received as loans, tracked but not income)

### 3.6 Transfer reconciliation

Opposite-direction same-nominal pairs across accounts get linked (`Transfer` table, IDs `T01+`, matched both sides, status `Cocok` / `Dikonfirmasi pengguna`). Balance tab cross-checks per account: opening + credits - debits = closing, observed change reported.

## 4. Outputs

- **API routes** (personal-only, 403 for samudera): overview, list, detail, spending by category, transfer links, accounts, review queue, import history, rules, audit; POST upload/preview/confirm/delete, gmail sync, categorize, edit, review respond, transfers ops, rules/accounts create.
- **Reports**: spending, cash-flow, fee, category breakdown (`reports.py`).
- **Recap spreadsheet** (analysis deliverable): see Appendix 1 - currently under study, not yet adopted as the official output.
- **Finance engine** (`personal-finance` skill, read-only: no transfers ever executed): analysis, 30-day forecast, afford/ask, briefing against the finance Google Sheet.

## 5. Data storage

| Item | Path | Git |
|---|---|---|
| Transaction DB (SQLite, WAL) | `.agent/workspaces/personal/state/transactions.db` | gitignored (state dir) |
| Uploaded PDFs | `.agent/workspaces/personal/state/uploads/<fingerprint>/` | gitignored |
| Account registry + senders | `.agent/skills/transactions/config/accounts.json` | tracked |
| Finance config (sheet ID, income, scenarios) | `.agent/workspaces/personal/finance.json` | tracked |
| Drill-down DB caches | `.agent/workspaces/personal/state/deepdive_cache.json`, `investment_cache.json` | gitignored |
| Trading sheet state | `.agent/workspaces/personal/state/trading_sheets/` | gitignored |

Credentials: Gmail token `token_gmail.json`, Drive token `token_drive.json` (never committed). Account numbers masked before storage; only masked forms kept.

## 6. Acceptance criteria

- Every bank/e-wallet notification or uploaded statement lands exactly once (no dupes, no gaps).
- Direction is correct for inbound vs outbound, including self-addresses BI-Fast (Livin' case) resolving to internal transfer.
- A bank-to-wallet top-up is never counted as spending; the wallet spending leg is the only spend.
- Person transfers (wife etc.) never inflate Bills/Groceries totals.
- Transfers between accounts link on both sides or are flagged for confirmation.
- Balance cross-check holds (opening + in - out = closing) per account for a recap period.
- `Perlu konfirmasi` rows are reviewable and resolvable to a category or marked confirmed.

## 7. Change Requests

Backfilled entries from backups + recorded decisions. Append only, newest at the bottom.

- `2026-09-11 | pre-backfill state | BCA data was empty/partial; a backfill pass restored the ledger | changed: transactions.db restored from prebackfill backup (bca_backfill.py path)`
- `2026-09-12 | taxonomy migration | taxonomy reworked to final category set | changed: migrate_taxonomy.py + reprocess all rows (backup taxonomy-backup-2026-09-12.bak)`
- `2026-09-12 | transfer cleanup | internal transfers normalized so bank-to-wallet legs no longer read as spending | changed: transfer linking rules (backup transfer-cleanup-2026-09-12.bak)`
- `2026-09-13 | vehicle service category | added vehicle/service split for car-related spend | changed: taxonomy + reprocess (backup vehicle-service-2026-09-13.bak)`
- `2026-09-24 | BCA backfill complete | full BCA mutation coverage confirmed through Sep 24 | changed: transactions.db + backups taxonomy-backup-2026-09-24.bak`
- `2026-09-30 | daily recap convention | every finance/trading ask records a dated recap; transactions keeps a per-menu requirements doc in this folder | changed: CLAUDE.md checklist + docs/requirements/*`

---

## Appendix 1 - Rekap spreadsheet (under study, NO action taken)

User shared `Rekap Keuangan 25 September - 25 Oktober 2026` (sheet id `1vTcM2HK7muq_AArjhjQSE7udn4mf7faI4Rq3bbDx9vQ`) on 2026-10-01 with instruction to learn it first, take no action. Not yet adopted as an official output of this menu; do not reference it as current until a change request approves it. Learned structure (read-only):

- Tabs: `Grafik` (119x12), `Ringkasan` (metrics + category table with bank-vs-external split and "Dibayar 24 Sep" adjustment), `Transaksi` (ledger, R001+, 18 cols incl. 4-way split and `Lokasi / konteks`), `Transfer` (T01-T09, Cocok/Dikonfirmasi pengguna), `Saldo` (per-account opening/closing + observed change), `Detail QRIS Solo` (excl. KAI, 1,652,690), `Rincian Dinda` (lump transfer 1,467,000 itemized + 2,900 `Perlu konfirmasi`), `Detail Pengeluaran Bank` (itemized to 91,749,538, ties to Ringkasan).
- Example period: total in 95,290,976 (Gaji 41,250,976 + Pinjaman masuk 52,340,000 + dinas Maju 1,700,000), bank outflow 92,542,538, closing BCA+Mandiri 5,880,795.
- Example taxonomy used: 33 categories (Makan dan minum, Biaya bank, Pembayaran utang, Transfer internal, Top-up (ShopeePay/OVO/e-money), Padel/Hotel/Tiket, Belanja/Groceries/Apotek/RS Anak, Tagihan rumah/KPR/Cicilan kendaraan/Pinjol, Gaji/Dana perjalanan kerja/Pinjaman masuk, Dinda/Bulanan Dinda/Transfer keluarga, dll.).

Awaiting the owner's go-ahead to wire this format into the requirements (a change request entry would then be appended above).