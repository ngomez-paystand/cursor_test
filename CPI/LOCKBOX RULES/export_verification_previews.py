#!/usr/bin/env python3
"""
Export every unique TIF in the queue to PNGs under
<run_dir>/verification_previews/<Transaction Id>/page_01.png ...

queue_tif_review.py already does this after OCR. This script is for a standalone re-export.
Requires Pillow.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from cpi_xlsx import load_queue_rows, resolve_tif_review_queue
from queue_tif_review import (
    discover_invoice_and_images,
    write_verification_previews,
)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="PNG previews from queued TIFs (all pages per TIF by default)."
    )
    ap.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Day folder containing tif_review_queue.xlsx",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="Optional cap on pages per TIF (default: export every page).",
    )
    args = ap.parse_args()
    run_dir = args.run_dir.resolve()
    queue_path = resolve_tif_review_queue(run_dir)
    if not queue_path.is_file():
        raise SystemExit(f"Missing {queue_path} (run queue_tif_review.py first).")

    _, image_dir = discover_invoice_and_images(run_dir)
    _fields, rows = load_queue_rows(queue_path)
    base_out = write_verification_previews(
        run_dir, image_dir, rows, max_pages=args.max_pages
    )
    cap = f"up to {args.max_pages} page(s)" if args.max_pages else "all pages"
    n = sum(1 for p in base_out.iterdir() if p.is_dir()) if base_out.is_dir() else 0
    print(f"Wrote previews under {base_out} ({n} transaction(s), {cap} per TIF).")


if __name__ == "__main__":
    main()
