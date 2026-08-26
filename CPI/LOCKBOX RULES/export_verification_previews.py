#!/usr/bin/env python3
"""
Export every page of each TIF listed in tif_review_queue.csv to PNGs under
<run_dir>/verification_previews/<Transaction Id>/page_01.png ...
TIF Path is normally the .tif filename only; legacy queues may use a file:// URI instead.
Requires Pillow.
"""
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from queue_tif_review import discover_invoice_and_images, tif_path_from_queue_cell


def extract_pages(tif_path: Path, out_dir: Path, max_pages: int | None) -> None:
    from PIL import Image

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(tif_path) as im:
        n = int(getattr(im, "n_frames", 1))
        limit = n if max_pages is None else min(max_pages, n)
        for i in range(limit):
            im.seek(i)
            rgb = im.convert("RGB")
            num = f"{i + 1:02d}"
            dest = out_dir / f"page_{num}.png"
            rgb.save(dest, "PNG")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="PNG previews from queued TIFs (all pages per TIF by default)."
    )
    ap.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Day folder containing tif_review_queue.csv",
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
    queue_csv = run_dir / "tif_review_queue.csv"
    if not queue_csv.is_file():
        raise SystemExit(f"Missing {queue_csv} (run queue_tif_review.py first).")

    _, image_dir = discover_invoice_and_images(run_dir)

    base_out = run_dir / "verification_previews"
    if base_out.exists():
        shutil.rmtree(base_out)
    base_out.mkdir(parents=True, exist_ok=True)

    count = 0
    with queue_csv.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = (row.get("Transaction ID") or "").strip()
            raw = (row.get("TIF Path") or "").strip()
            tif = tif_path_from_queue_cell(raw, image_dir)
            if not tid or not tif.is_file():
                continue
            extract_pages(tif, base_out / tid, args.max_pages)
            count += 1
    cap = f"up to {args.max_pages} page(s)" if args.max_pages else "all pages"
    print(f"Wrote previews under {base_out} ({count} transaction(s), {cap} per TIF).")


if __name__ == "__main__":
    main()
