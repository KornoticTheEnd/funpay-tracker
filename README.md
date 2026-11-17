# WoW: Forever gold tracker (FunPay)

Snapshots the public FunPay listing for WoW: Forever gold (https://funpay.com/en/chips/258/)
every 3 hours with GitHub Actions and publishes an interactive report with GitHub Pages.

- `funpay_scraper.py` – fetches the listing once and appends every offer to `data/offers_YYYY-MM-DD.csv`
- `funpay_report.py` – builds `docs/index.html` (latest report), one page per day and `docs/archive.html`
- `.github/workflows/scrape.yml` – the schedule; collection stops after `STOP_AFTER`

Sales are estimated from stock decreases between snapshots, so treat them as a trend indicator.
