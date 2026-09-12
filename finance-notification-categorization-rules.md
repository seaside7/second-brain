# Transaction Categorization Rules — BNI / BRI / GoPay Email Notifications

Goal: turn raw bank/e-wallet email notifications into categorized transactions
**without** calling an LLM per-transaction (too expensive). This is a
deterministic, keyword + pattern based rule engine. Run an LLM only on the
small leftover "Uncategorized" bucket, periodically.

---

## 1. Step 0 — Parse the raw email into a normalized record

Before any categorization, extract these fields from each email (BNI, BRI,
and GoPay all include them, just in different layouts):

| Field | Example | Notes |
|---|---|---|
| `source` | `BNI`, `BRI`, `BCA`, `GoPay` | from sender domain / subject prefix |
| `direction` | `debit` (money out) / `credit` (money in) | keyword-based, see §2 |
| `amount` | `150000` | strip `Rp`, `.`, `,` |
| `datetime` | transaction timestamp | |
| `counterparty_name` | e.g. `SITI/wife`, `PLN`, `INDOMARET` | from "Ke/Kepada/To", "Dari/From", or merchant field |
| `counterparty_account` | account/VA number or wallet ID | used to match your wife's account |
| `reference_note` | free text / berita / catatan field | often has "BAYAR LISTRIK", "TOP UP", etc. |
| `channel` | `mobile banking`, `ATM`, `QRIS`, `merchant debit` | helps disambiguate top-up vs bill vs merchant purchase |

Keep the raw email body too — the leftover ~5% you can't classify goes to
manual review or a batched LLM pass later, not one call per email.

---

## 2. Step 1 — Direction (debit vs credit)

Match on subject/body keywords first, this is the most reliable signal:

- **Credit (money in):** `Transfer Masuk`, `Dana Masuk`, `Kredit`, `Received`, `Terima`
- **Debit (money out):** `Transfer Keluar`, `Pembayaran`, `Pembelian`, `Debet`, `Sent`, `Top Up` (from the *sending* account's perspective)

BNI/BRI notification emails almost always say explicitly "Debit" or "Kredit"
in a labeled field — trust that field over free text if present.

---

## 3. Step 2 — Category decision tree

Apply rules **top to bottom**, first match wins. This ordering matters —
put the most specific rule before the general one (e.g. "top-up to GoPay"
must be caught before it falls into the generic "transfer out" bucket).

```
1. INTERNAL MOVE (exclude from spending totals)
   IF direction = debit
   AND destination is one of YOUR OWN accounts/wallets
       (BNI acct → GoPay, BRI acct → GoPay, GoPay → BNI/BRI, etc.)
   → category = "Internal Transfer" (not income, not expense)

2. TRANSFER TO PERSON (your rule — already set up)
   IF counterparty_account/name matches a known contact
       (e.g. wife's account/VA/wallet ID)
   → category = "Transfer to [Contact Name]"
   → NOTE: decide up front whether this counts as "spending" for you.
     Recommended: tag it "Family Transfer" as its own category, separate
     from "Expense", so it doesn't inflate your grocery/bills numbers —
     since she'll spend it on groceries herself, which you mentioned.

3. TOP-UP E-WALLET (external, i.e. NOT your own wallet — e.g. topping up
   someone else's OVO/DANA/GoPay, or a game/voucher top-up)
   IF reference_note contains: "Top Up", "Isi Ulang", "Voucher"
   AND destination ≠ your own wallet (already excluded in rule 1)
   → category = "Top Up (3rd party)"

4. BILL PAYMENT
   IF counterparty_name / biller_code matches known utility billers:
       - Electricity: `PLN`, `PLN POSTPAID`, `PLN PREPAID`, `TOKEN LISTRIK`
       - Water: `PDAM`, `AETRA`, `PALYJA`
       - Internet/TV: `INDIHOME`, `FIRST MEDIA`, `BIZNET`
       - Phone/pulsa/data: `TELKOMSEL`, `XL`, `INDOSAT`, `PULSA`, `PAKET DATA`
       - Insurance/BPJS: `BPJS KESEHATAN`, `BPJS KETENAGAKERJAAN`
       - Credit card bill: `KARTU KREDIT`, `PAYMENT CC`
   → category = "Bills — [subtype]"

5. GROCERIES / RETAIL (merchant-code / QRIS matching)
   IF counterparty_name matches known grocery/retail merchants:
       `INDOMARET`, `ALFAMART`, `SUPERINDO`, `HYPERMART`, `TRANSMART`,
       `RANCH MARKET`, or MCC code for supermarkets (if your bank includes MCC)
   → category = "Groceries"

6. FOOD DELIVERY / RIDE-HAILING (common on GoPay specifically)
   IF counterparty_name matches: `GOJEK`, `GOFOOD`, `GRAB`, `GRABFOOD`,
      `SHOPEEFOOD`
   → category = "Food & Transport"

7. E-COMMERCE
   IF counterparty_name matches: `TOKOPEDIA`, `SHOPEE`, `LAZADA`, `BLIBLI`
   → category = "Online Shopping"

8. INCOME
   IF direction = credit
   AND source is NOT one of your own accounts (rule 1 already caught internal)
   AND (sender matches known payroll/client name, OR reference_note contains
        "GAJI", "SALARY", "PAYROLL", "INVOICE")
   → category = "Income"

9. ATM / CASH WITHDRAWAL
   IF channel = "ATM" AND reference_note contains "Tarik Tunai" / "Withdrawal"
   → category = "Cash Withdrawal"

10. FALLBACK
    → category = "Uncategorized — needs review"
    → Batch these weekly/monthly and either review manually or send the
      *small batch* through an LLM in one call (cheap) instead of per-email.
```

---

## 4. Why this order matters

- **Internal transfers must be excluded first**, otherwise every top-up
  from BNI/BRI → GoPay looks like "spending," and then the *second* leg
  (GoPay → merchant) also looks like spending — double counting the same
  money as two separate expenses.
- **Person-to-person transfers** (your wife) should be their own category,
  not lumped into "Bills" or "Groceries" — you don't know what she'll
  spend it on, so don't guess.
- **Merchant/biller name matching is the highest-signal, lowest-cost rule**
  you have. Bank notifications almost always include the merchant or biller
  name verbatim (`PLN`, `INDOMARET`, etc.) — build a lookup table (a simple
  key-value dictionary you keep updating) rather than trying to parse
  natural language.

---

## 5. Practical notes for BNI / BRI / BCA / GoPay specifically

- **BNI & BRI** notification emails typically have a structured layout with
  explicit labels: "Jenis Transaksi" (transaction type), "Nominal" (amount),
  "Ke Rekening" / "Dari Rekening" (to/from account), "Keterangan" (notes).
  Parse by label, not by guessing position in the text.
- **BCA** e-statement/notification emails (myBCA / KlikBCA) use their own
  label set: "Tipe Transaksi" or "Jenis Mutasi" (Debit/Kredit), "Keterangan"
  (which often already contains the merchant/biller name in caps, e.g.
  `TRSF E-BANKING DB PLN` or `BIAYA ADM`), and a "No Rekening Tujuan" field
  for the destination account. BCA's "Keterangan" field is noisier than
  BNI/BRI's — it often bundles the transaction method (`TRSF E-BANKING`,
  `QRIS`, `BI-FAST`) together with the counterparty, so strip the method
  prefix before merchant-matching (rule 5–8 in the tree).
- **GoPay** notifications/receipts usually show a merchant name and a
  transaction type tag (`Top Up`, `Transfer`, `Payment`, `Pay Later`)
  directly — this is often cleaner to parse than bank emails.
- **Adding a new bank later:** just extend Step 0 with that bank's label
  names → the same 4 normalized fields (direction, amount, counterparty,
  note). The category tree in §3 needs no changes — it only reads the
  normalized fields, never the raw bank-specific format. So yes, it can
  "read anything," as long as each new source gets its own small Step-0
  mapping.
- Keep a small **merchant lookup table** (dictionary of
  `merchant_string → category`) that you update over time. This is where
  almost all the real-world accuracy comes from — the decision tree above
  is just the skeleton; the merchant table is the muscle.
- Keep a small **contacts table** (name/account/VA/wallet ID → person) for
  rule 2 — you said you already have this for your wife; extend it as
  needed for other recurring transfers (parents, helper, etc.).

---

## 6. Suggested category taxonomy (final list)

- Income
- Internal Transfer (excluded from spending totals)
- Family Transfer (to wife / other known contacts)
- Bills — Electricity / Water / Internet / Phone / Insurance / Credit Card
- Groceries
- Food & Transport
- Online Shopping
- Top Up (3rd party)
- Cash Withdrawal
- Uncategorized

This keeps your dashboard's "spending" total accurate: Internal Transfer
and Family Transfer are tracked but not counted as your personal expense,
avoiding the double-counting problem from routing money through GoPay.
