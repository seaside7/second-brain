# TEMPLATE - Stock Deep Dive

Gunakan template ini SEKALIGUS dengan data package (carrier) yang diberikan
secara terpisah. Tulis laporan LENGKAP sekali saja, dalam satu respons, bahasa
Indonesia. Jangan bertanya lanjutan - semua data sudah ada di carrier.

## Aturan keras

1. **Tiga lapisan data - jangan pernah mencampurnya.**
   - [Fakta yang dilaporkan] = angka yang memang dikembalikan sumber (Yahoo
     Finance), ditandai jelas.
   - [Metrik terhitung] = turunan dari fakta, wajib tulis rumusnya (mis. PBV =
     harga / book value).
   - [Interpretasi analis] = pendapat Anda, selalu diawali "Menurut interpretasi
     analis:" atau tanda serupa. JANGAN menulis interpretasi sebagai fakta.
2. **Jangan pernah mengarang angka.** Jika sebuah nilai TIDAK ADA di carrier,
   jangan mengisinya. Tulis "tidak tersedia dari sumber" dan masuk daftar
   Missing data.
3. **Pisahkan tanggal pasar vs periode pelaporan.** "Harga pasar per <timestamp>"
   adalah data LIVE. "Periode pelaporan keuangan" adalah periode laporan (mis.
   FY2025). Jangan menyebut angka laporan sebagai "data hari ini".
4. **Laporan triwulanan**: bandingkan tiap kuartal dengan kuartal yang SAMA
   tahun lalu (YoY), bukan hanya dengan kuartal sebelumnya. Jika carrier
   menandai kuartalan tidak tersedia, buat analisis tahunan dan daftarkan
   kuartalan di under Missing data (jangan berhenti).
5. **Float & kepemilikan**: floatShares dilaporkan sebagai NILAI saja, jangan
   diterjemahkan "sold to public vs held back". Persentase kepemilikan
   institusional adalah INDIKATOR, bukan sinyal "whale".
6. **Peer**: gunakan peer dari carrier (industri sama). Jelaskan bila model
   bisnis tidak langsung sebanding. Jangan menambah peer yang tidak ada di carrier.
7. **Retrieve-first**: jika carrier punya `documents_existing`, rujuk dokumen itu
   alih-alih meminta lagi. Jika tidak ada dan ada Missing data, buat permintaan
   SPESIFIK: perusahaan, periode pelaporan, field yang dibutuhkan, dokumen yang
   disarankan, dan di mana field itu biasanya berada (per prioritas sumber:
   1) filing/disclosure IDX, 2) laporan keuangan tahunan/kuartalan auditan,
   3) materi investor relations, 4) Yahoo Finance).
8. **Simpulan final**: "Analisis ringkasan" - BUKAN rekomendasi beli/jual.
   Nyatakan eksplisit bahwa ini bukan saran investasi. Jika bukti tidak lengkap,
   tandai simpulan sebagai PROVISIONAL.
9. **Gaya**: laporan formal, bahasa Indonesia, tanpa em-dash, tabel rapi,
   bullet point padat. Boleh menyisipkan istilah teknis (PBV, ROE, NPL, CAR)
   apa adanya.

## Struktur laporan

Judul: **Stock Deep Dive - <company_name> (<ticker>)**

Baris metadata wajib:
- Tanggal analisis: <analysis_date>
- Periode pelaporan keuangan: <financial_reporting_period.merged>
- Harga pasar per: <market_price_timestamp> (data live; terpisah dari periode pelaporan)

Sections:
1. Ringkasan Eksekutif
2. Company & Business Overview (gunakan business_summary dari carrier; ringkas)
3. Snapshot Pasar Saat Ini (harga, perubahan, 52-week range, kapitalisasi, mata
   uang, timing/status pasar, sumber)
4. Tren Pendapatan, Laba, dan EPS 3-5 Tahun (dari statements.annual; tulis YoY
   dan catat bahwa EPS adalah perkiraan = net_income / shares outstanding bila
   itu satu-satunya yang tersedia)
5. Profitabilitas: margin, ROE, ROA, ROIC (gunakan profitability; tandai yang
   missing)
6. Kualitas Arus Kas (dari cash_flow; jangan mengarang - kemungkinan besar
   missing dan perlu dokumen resmi)
7. Kesehatan Neraca & Utang (dari balance; net debt, D/E perkiraan dengan
   rumus; tandai jatuh tempo & transaksi berelasi yang missing)
8. Valuasi: PE, PBV, EV/EBITDA, EV/Revenue (valuation; beri konteks terhadap
   peer; tandai EV/EBITDA sebagai perkiraan bila memang perkiraan)
9. Riwayat & Keberlanjutan Dividen (dividends; payout ratio bila ada; kalau
   missing, minta laporan resmi)
10. Kepemilikan Saham, Public Float, Dilusi, Indikator Institusional (ownership;
    float sebagai nilai + float%; rincian pengendali/institusional yang missing)
11. Metrik Spesifik Industri (industry_metrics; bank: NPL, CAR, NIM, LDR, CASA,
    cost of credit - hampir pasti missing dari Yahoo, daftarkan di bawah)
12. Perbandingan dengan 3-5 Peer Sejenis (peers; tabel; jelaskan keterbandingan)
13. Hal-hal yang Terlihat Baik
14. Hal-hal yang Terlihat Lemah / Mengkhawatirkan
15. Katalis & Risiko
16. Interpretasi Bull, Base, dan Bear
17. Missing Data & Dokumen yang Dibutuhkan (dari missing_data + documents_existing)
18. Sumber & Timestamp (dari sources_and_timestamps; sertakan tanda cache bila
    statement diambil dari cache)

Tutup dengan:

**Analisis Ringkasan** - padat, merangkum poin utama, eksplisit PROVISIONAL
apabila ada data penting yang belum lengkap, dan pernyataan: "Ini analisis
tambahan, bukan rekomendasi beli/jual, dan bukan merupakan nasihat investasi."