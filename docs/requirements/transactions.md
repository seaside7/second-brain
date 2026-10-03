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
| Transaction screenshots | Image | `POST /api/transactions/upload/screenshot` -> `vision_extract.py` (Gemini vision), preview-only |
| Recap spreadsheet export (one-time) | CSV / JSON | `POST /api/transactions/recap/preview` + `/recap/confirm` -> `sheets_import.py` |
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

### 3.7 Recap import (one-time, from Google Sheet export)

The recap Google Sheet is a learning reference + one-time starting dataset. There is NO ongoing Sheets integration: ongoing input stays on dashboard Upload (PDFs/screenshots) + the daily email sync. Import flow:

- Export the recap `Transaksi` tab to CSV (or JSON list) and upload it in the Transactions Imports tab.
- `preview_recap` (dry-run, never writes): classifies rows into parents (bank debits) vs allocation children vs excluded (summary/adjustment/no-date), then matches each parent against the ledger by amount + date, weighted by direction and description-token overlap. Buckets: matched (`exact`/`likely`), new (no match), ambiguous (same amount+day across directions or tied candidates).
- `confirm_recap` (one transaction, rollback on error): 
  - Confirmed recap categories/splits OVERRIDE automatic categorization even at high confidence, except rows with an existing manual correction, which become conflicts and stay untouched (shown in the preview).
  - Unconfirmed recap categories fill only uncategorized / low-confidence ledger rows.
  - All changes are recorded as corrections rows (reason `recap_import`, actor `user`) so reprocessing never re-overrides them, and every row writes a `sheet_import_log` entry for idempotent re-runs.
  - New rows insert through source_documents (kind `sheet`) -> extracted_txns -> ledger_txns and are protected the same way.
  - Allocation-child rows (split of a parent) become `txn_splits` linked to the parent; they are never separate debits. Sum mismatch is audited, never rebalanced.
  - Ambiguous rows and any new rows that are not explicitly approved (`apply_hashes`) are held to review (`held_review`), never auto-inserted.
- Column mapping (Indonesian headers -> canonical fields) lives in `RECAP_COLUMN_ALIASES` in `sheets_import.py`.

## 4. Outputs

- **API routes** (personal-only, 403 for samudera): overview, list, detail, spending by category, transfer links, accounts, review queue, import history, rules, audit, recap preview/confirm; POST upload/preview/confirm/delete, screenshot upload, gmail sync, categorize, edit, review respond, transfers ops, rules/accounts create.
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
- `2026-10-03 | schema v5 -> v6 + preview & screenshot + trips/splits + one-time recap import | sheets + trips/splits landed; uploads default to preview; recap import engine + tests + API + Imports-tab UI; Appendix 1 recap adopted as one-time starting data only (no ongoing Sheets integration) | changed: schema.py v6 (sheet kind + sheet_import_log), import_engine.py (preview-only upload, upload_screenshot, _row_flags, _to_int_amount), vision_extract.py (Gemini vision, no model_router), trips.py + splits.py + tests, sheets_import.py + tests/test_sheets_import.py (121 tests green), transactions_api.py + tab-transactions.js + style.css (recap preview/confirm endpoints + Imports-tab recap card)`
- `2026-10-03 | PENDING | true transactions deploy: schema v6 migration runs on next connect; recap CSV export from owner still needed to validate RECAP_COLUMN_ALIASES against real headers; trips/splits API routes + reports + categorize fixes (top-up vs toll, own_person_transfer) still queued from the prior task | changed: none yet`
- `2026-10-03 | recap import shipped + applied | one-time recap CSV import (134 rows) validated against real headers; preview/confirm UI + Imports-tab recap card; family collapse (allocation slices -> single parent debit, 2,500,000 @ 09-25 + 1,467,000 @ 09-30) + credit-row parse fix; dry-run 0 matched / 126 new / 0 ambiguous / 10 allocations; prod VPS import 43 inserted / 68 enriched / 13 held / 2 conflicts; local scratch DB also imported (123 inserted, 3 dup) | changed: sheets_import.py + tests (126 green), transactions_api.py, tab-transactions.js, style.css, schema (schema v5->v6 on existing DBs via _migrate), docs; commit ea9444b | backup: transactions.bak-2026-10-03 (VPS + local)`
- `2026-10-03 | Kredit Pintar VA shown as inflow | BCA email sign-flip fixed: payee name containing 'kredit' ('PT KREDIT PINTAR INDONESIA', a VA bill payment) no longer forces direction 'in'; counterparty names stripped before direction sniffing (blanked only when value carries kredit/credit, real inbound cues untouched); categorize rules added (kredit pintar VA -> Loans/Online Credit; credit card bill -> Utilities/Credit Card); VPS rows 590/984/994 repaired in->out + recategorized with audit/correction records | changed: gmail_sync.py (_has_inflow_signal/_strip_payee_for_sniff), categorize.py, tests (130 green); commit 9c60e24; VPS ledger fixed in place`
- `2026-10-03 | GoPay summary shown as Rp35.781.118 uncategorized top-up | GoPay monthly summary newsletter ("Here's what you spent in September") was parsed as a real 'top_up' transaction because the body contains 'Top Up'; the month's Expense total became one fake row. Guard added in _parse_gopay (summary phrases/subject -> no rows) + regression test; 2 fabricated rows removed from prod VPS ledger (Aug 17,015,894 lid 68; Sep 35,781,118 lid 1016) with audit trail | changed: gmail_sync.py (_parse_gopay summary guard), tests (131 green); commit a39d8c5; VPS ledger cleaned (677 rows)`
- `2026-10-03 | Trips feature + Solo trip backfill | New Trips sub-view in the Transactions tab groups a journey's costs. Accounting rules resolved: 'internal_transfer'/'top_up'/'void' never count even when assigned, income natures ('income'/'refund'/'cashback') report as movement not spend, a parent carrying ANY split never counts whole (its split amounts, each pinned to its own trip, carry the spend), and a parent may hold at most one split per (category, trip). API adds GET trips / trips/<id> / trips/<id>/candidates and POST trips/create, trips/<id>/update, trips/assign, trips/unassign, splits/set, splits/<id>/unsplit plus a trip= filter on the all-transactions list. Solo trip "Solo Sep 2026 (26-28 Sep)" backfilled on the VPS: 614 amount corrected to 2,754,500 and re-categorized Traveloka to Tiket kereta (parser double-counted the 1,500 fee, fee row 615 kept), 587/592 re-categorized Makan, 597 Transportasi, 952 Perlu konfirmasi, 1035 Hotel, 965/963 review confirmed, Dinda 1,467,000 allocation re-parented from 1064 to 985 with the four Makan slices merged into one 669,000 trip-pinned allocation and 1064 voided, 29 ledger rows assigned. Ledger-true trip spend_total = 8,333,494 (DELTA +5,000 vs recap sheet: sheet double-counts Sanjaya 31,000 and omits Nata Hotel 26,000; owner approved), per-category Makan 2,127,001 / Hotel 1,291,303 / Transportasi 55,000 / Digital dan langganan 431,790 / Perlu konfirmasi 100,000 / Belanja 173,900 / Tiket kereta 4,154,500 | changed: store.py + reports.py (trip_spend_total, trip_breakdown, TRIP_EXCLUDED_NATURES / TRIP_INCOME_NATURES), transactions_api.py (trips/splits routes + trip filter), tab-transactions.js (Trips view, All-txns trip dropdown), style.css, tests/test_trip_reports.py (full suite 135 tests green) | backup: VPS transactions.db.bak-20261003-175920`
- `2026-10-03 | taxonomy dedup merge | Folded duplicate legacy English categories into their Indonesian canon under parent buckets (owner approved): Food & Dining -> Makan dan minum, Bank Fee -> Biaya bank, Internal Transfer -> Transfer internal, Family Transfer -> Transfer keluarga (334 ledger rows repointed, legacy category rows deleted); typo fixed Pembarayan Pinjol -> Pembayaran Pinjol; loan categories (Pembayaran utang, Pembayaran Pinjol, Cicilan kendaraan, KPR) grouped under the Loans parent alongside Friend Loan/Repayment, Loan Payment, Online Credit. Income parent holds Gaji, Dana perjalanan kerja, Project Payment, Refund. Wallet top-ups and Pinjaman masuk left as-is (owner passed on those). Categorizer _CAT rules rewired to the canonical names so auto-fire can not recreate the deleted EN categories. Trip Solo spend unchanged at 8,333,494; flows re-verified (income->in, transfers->both, spend->out). | changed: categorize.py (_CAT), tests/test_categories.py (full suite 135 tests green), VPS transactions.db migrated in place | backup: VPS transactions.db.bak-merge-20261003-182516`
- `2026-10-03 | AirPay ShopeePay VA disambiguation + description enrichment | Owner flagged the AirPay VA rows ('VA - PT AirPay ... / ShopeePay') as undecidable: the BCA email's only identity is the masked VA holder plus the 'Kirim ke ...' line. Parser now enriches VA descriptions with 'VA no ... | Name <holder> | Kirim ke ...' so the receiving account is visible in the dashboard (16 existing VPS rows re-enriched in place from raw emails). Categorizer rule: a bank-debit AirPay VA whose product is SHOPEE Bill stays in the spend flow ('shopee' -> Online Shopping); SHOPEEPAY wallet top-ups addressed to our OWN wallet ('Kirim ke Said Iskandar') become Top-up ShopeePay (top_up, excluded from spend); any other holder (masked 'DINX LUTXXXXX') stays needs_review until the wallet owner is verified. Owner confirmed DINX LUT is unknown -> the 11 DINX ShopeePay rows were held: nature needs_review, category Top-up ShopeePay (271, group Transfers), review_status uncategorized, txn_status needs_review, alert note + audit trail. Trip Solo spend re-verified unchanged at 8,333,494 (none of the 16 are trip-assigned; spends in doubt are now excluded from totals). | changed: gmail_sync.py (_bca_journal_payee VA holder/VA-no/kirim-ke enrichment), categorize.py (_own_airpay_topup + 0.7 split + _CAT topup_shopeepay), tests/test_categories.py (135+2 tests green), VPS transactions.db (16 descriptions re-enriched, 11 rows held) | backup: VPS transactions.db.bak-airpay-20261003-183703`
- `2026-10-03 | wallet VA detail for EVERY wallet (OVO via Visionet, generalized) | Owner asked the same holder detail for the OVO rows ('VA - PT Visionet Internasional / OVO'). Audit showed all 4 Visionet rows are OWN OVO wallet top-ups - unmasked holder 'SAID ISKANDAR', VA 39358081998986707 - but 1001/534 were misfiled as Makan dan minum and 956/959 as expense/review. Generalised the wallet-VA rule: _THIRD_PARTY_ACQUIRERS now airpay+visionet, wallet top-ups detected via _wallet_acquirer (SHOPEE Bill stays a purchase), own-wallet proof via 'Name SAID ISKANDAR' (unmasked) OR 'Kirim ke Said Iskandar'; unknown-holder wallets stay review. Parser enrichment fixed to carry ONLY the 'Kirim ke ...' Description line (Visionet emails have no Description - the old rule leaked 'Reference No. :...' into descriptions). All 4 OVO rows -> nature top_up, Top-up OVO (272, group Transfers), confirmed ok (525,000 out of spend); every va_payment row re-enriched (18). Trip Solo re-verified 8,333,494 / 30 members. Full suite 135+4 tests green | changed: categorize.py (_wallet_acquirer/_own_wallet_va/_WALLET_ACQUIRERS/_WALLET_TOPUP_CATS/topup_ovo), gmail_sync.py (_bca_journal_payee Description-line guard), tests/test_categories.py (OVO own/other holder tests), VPS transactions.db (18 va rows re-enriched, 4 OVO rows re-filed) | backup: VPS transactions.db.bak-ovo-20261003-184356`

---

## Appendix 1 - Rekap spreadsheet (adopted for ONE-TIME import, no ongoing integration)

User shared `Rekap Keuangan 25 September - 25 Oktober 2026` (sheet id `1vTcM2HK7muq_AArjhjQSE7udn4mf7faI4Rq3bbDx9vQ`) on 2026-10-01 with instruction to learn it first, take no action. On 2026-10-03 the owner APPROVED a one-time import of the recap as starting data + learning reference, with NO ongoing Google Sheets integration (no new dashboard; the enhanced existing dashboard remains the input surface). Decision details (owner spec):

- Confirmed recap categories/splits override automatic categorization even at high confidence.
- Existing manual corrections are preserved; where they conflict with confirmed recap data, both are shown in the preview for review.
- Unconfirmed recap categories fill only uncategorized / low-confidence rows.
- Every import change records its source; imported confirmations are protected from reprocessing.
- Ambiguous rows are held to review, never auto-inserted.

Learned structure (read-only reference): tabs `Grafik` (119x12), `Ringkasan` (metrics + category table with bank-vs-external split and "Dibayar 24 Sep" adjustment), `Transaksi` (ledger, R001+, 18 cols incl. 4-way split and `Lokasi / konteks`), `Transfer` (T01-T09, Cocok/Dikonfirmasi pengguna), `Saldo` (per-account opening/closing + observed change), `Detail QRIS Solo` (excl. KAI, 1,652,690), `Rincian Dinda` (lump transfer 1,467,000 itemized + 2,900 `Perlu konfirmasi`), `Detail Pengeluaran Bank` (itemized to 91,749,538, ties to Ringkasan).

Example period: total in 95,290,976 (Gaji 41,250,976 + Pinjaman masuk 52,340,000 + dinas Maju 1,700,000), bank outflow 92,542,538, closing BCA+Mandiri 5,880,795.

Example taxonomy used: 33 categories (Makan dan minum, Biaya bank, Pembayaran utang, Transfer internal, Top-up (ShopeePay/OVO/e-money), Padel/Hotel/Tiket, Belanja/Groceries/Apotek/RS Anak, Tagihan rumah/KPR/Cicilan kendaraan/Pinjol, Gaji/Dana perjalanan kerja/Pinjaman masuk, Dinda/Bulanan Dinda/Transfer keluarga, dll.).

Implementation: `sheets_import.py` reads a CSV (or JSON) export of the `Transaksi` tab via `RECAP_COLUMN_ALIASES` and drives the flow in 3.7. The owner still needs to export the recap to CSV so the header mapping can be validated against the real columns and a dry-run preview run against a temp copy.- `2026-10-03 | Review queue Confirm button + Belanja -> Online Shopping merge | Owner found the category dropdown does not confirm a row when the SAME category is re-picked (no change event fires, nothing POSTed) - the only reliable actions were Skip (remove from queue but keep needs_review, stays out of totals) or a category change. Added an explicit Confirm button per review row (shown when a category is already set) that POSTs /edit with the CURRENT category: review_status->ok, nature resolved, txn_status->confirmed, so an already-correct row is confirmed without touching its category. Also merged category Belanja (276, group '') into Online Shopping (29, group Shopping) per owner: 6 ledger rows repointed, category 276 deleted. Confirmed the two owner-visible review rows: 992 (AirPay/Shopee Bill, 53,483, Belanja->Online Shopping) and 605 (Proteksi Jiwa Axa Mandiri, 1,000, Asuransi) -> expense/ok/confirmed. Review queue now empty (0 uncategorized). | changed: tab-transactions.js (_renderReview Confirm button + _reviewConfirm), style.css (tx-btn-ok), VPS transactions.db (merge + 2 confirms, audit logged); trip Solo re-verified unchanged at 8,333,494 (both rows outside the 26-28 Sep window, none trip-assigned) | backup: VPS transactions.db.bak-merge-belanja-20261003-200938`
