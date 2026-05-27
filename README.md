# cursor_test

Tools for CPI lockbox TIF review workflows (Paystand invoice-missing queue).

## CPI scripts

Run from the `CPI/` directory:

```bash
pip install -r requirements-ocr.txt
python3 queue_tif_review.py --run-dir ./MAY/MM-DD-YYYY
python3 export_verification_previews.py --run-dir ./MAY/MM-DD-YYYY
```

### Reference data (in git)

| File | Purpose |
|------|---------|
| `mail_stop_merchants.csv` | Mail Stop → merchant legal name (payee) |
| `merchant_aliases.csv` | Alternate payee strings on checks (OCR variants) |

Edit these CSVs to add mail stops or aliases without changing Python code.

### Daily Paystand data (local only)

Per-day folders (`MAY/MM-DD-YYYY/`), Paystand invoice/check CSVs, `.tif` images, and generated `tif_review_queue.csv` are **not** committed (see `.gitignore`).
