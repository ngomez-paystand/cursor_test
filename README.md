# cursor_test

Tools for CPI lockbox TIF review workflows (Paystand invoice-missing queue).

## CPI scripts

The shareable agent lives in `CPI/LOCKBOX RULES/` (scripts, mail-stop/alias CSVs, and lockbox rules). Daily Paystand folders stay next to it under `CPI/`.

Run from the `CPI/` directory:

```bash
pip install -r "LOCKBOX RULES/requirements-ocr.txt"
python3 "LOCKBOX RULES/queue_tif_review.py" --run-dir ./MAY/MM-DD-YYYY
python3 "LOCKBOX RULES/export_verification_previews.py" --run-dir ./MAY/MM-DD-YYYY
```

### Reference data (in git)

| File | Purpose |
|------|---------|
| `LOCKBOX RULES/mail_stop_merchants.csv` | Mail Stop → merchant legal name (payee) |
| `LOCKBOX RULES/merchant_aliases.csv` | Alternate payee strings on checks (OCR variants) |

Edit these CSVs to add mail stops or aliases without changing Python code.

### Daily Paystand data (local only)

Per-day folders (`MAY/MM-DD-YYYY/`), Paystand invoice/check CSVs, `.tif` images, and generated `tif_review_queue.csv` are **not** committed (see `.gitignore`).
