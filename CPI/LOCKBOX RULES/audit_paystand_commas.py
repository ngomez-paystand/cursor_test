#!/usr/bin/env python3
"""Audit unquoted/malformed payer commas in Paystand day exports (invoice, check, image metadata).

Scans every data row, including rows with an invoice number. Does not run OCR.
Report-only by default — never rewrites the raw export CSVs. Pass --fix to explicitly opt
into rewriting (kept for manual/ad-hoc use only; the normal pipeline never auto-fixes).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from queue_tif_review import (
    ensure_run_dir_exports_commas_clean,
    format_comma_audit_summary,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit (report-only by default) Paystand export payer commas.")
    ap.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Day folder (e.g. JUNE/06-22-2026)",
    )
    ap.add_argument(
        "--fix",
        action="store_true",
        help="Explicitly opt into rewriting the export CSVs (default is report-only).",
    )
    ap.add_argument(
        "--backup",
        action="store_true",
        help="When --fix is used, write .csv.bak backups first.",
    )
    args = ap.parse_args()
    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"Not a directory: {run_dir}")

    reports = ensure_run_dir_exports_commas_clean(
        run_dir,
        auto_fix=args.fix,
        backup=args.backup,
    )
    print(format_comma_audit_summary(reports, run_dir=run_dir))
    if any(r.misaligned for r in reports):
        sys.exit(1)


if __name__ == "__main__":
    main()
