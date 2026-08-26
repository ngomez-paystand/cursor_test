# cursor_test

CPI lockbox TIF review (Paystand). The shareable agent is **`CPI/LOCKBOX RULES/`**.

Daily Paystand folders (`CPI/AUGUST/08-26-2026/`, etc.) stay local and are not in git.

## What is in `CPI/LOCKBOX RULES/`

Scripts, mail-stop/alias tables, and the four lockbox Cursor rules (same filenames). Cursor still loads those rules via symlinks in `.cursor/rules/`.

| File | Purpose |
|------|---------|
| `queue_tif_review.py` | Comma audit + OCR queue → `tif_review_queue.csv` |
| `build_lockbox_report.py` | Lockbox Excel (`Good?`) from the queue |
| `audit_paystand_commas.py` | Comma audit only (does not rewrite exports) |
| `tif_scan_match.py` | OCR match + “not a check” patterns |
| `mail_stop_merchants.csv` | Mail Stop → merchant |
| `merchant_aliases.csv` | Payee aliases on checks |
| `cpi-lockbox-*.mdc` | Agent rules (audit, visual review, misroute, re-run confirm) |

## Setup (teammate)

1. Get access to this private repo (`ngomez-paystand/cursor_test`).
2. Clone it and open that folder in Cursor.
3. From `CPI/`:

```bash
pip install -r "LOCKBOX RULES/requirements-ocr.txt"
```

A Cursor **Share → Team** link is the chat history only. It does not install this folder. Clone the repo for the agent.

## Run a day

Put invoice, check, and image-detail under `CPI/<MONTH>/<MM-DD-YYYY>/`. Then from `CPI/`:

```bash
python3 "LOCKBOX RULES/queue_tif_review.py" --run-dir ./AUGUST/08-26-2026
python3 "LOCKBOX RULES/build_lockbox_report.py" --run-dir ./AUGUST/08-26-2026
```

If the comma audit finds any issue, processing stops. Fix the raw invoice/check/`metadata.csv` by hand and re-run. Those source files are never auto-rewritten.

Visually confirmed false alarms (no OCR re-run):

```bash
python3 "LOCKBOX RULES/queue_tif_review.py" --run-dir ./AUGUST/08-26-2026 --visual-clear TID1,TID2
python3 "LOCKBOX RULES/build_lockbox_report.py" --run-dir ./AUGUST/08-26-2026
```

## Edit reference data

Add mail stops or aliases in the two CSVs above. No Python change needed. Ask before re-running OCR for a day that was already processed.
