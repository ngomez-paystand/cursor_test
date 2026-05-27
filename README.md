# cursor_test

Tools for CPI lockbox TIF review workflows (Paystand invoice-missing queue).

## CPI scripts

Run from the `CPI/` directory:

```bash
python3 queue_tif_review.py --run-dir ./MM-DD-YYYY -o ./MM-DD-YYYY/tif_queue.csv
python3 export_verification_previews.py --run-dir ./MM-DD-YYYY
python3 tif_scan_match.py --run-dir ./MM-DD-YYYY
```

Operational CSV/TIF data stays local and is not committed to git.
