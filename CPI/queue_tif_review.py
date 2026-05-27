#!/usr/bin/env python3
"""
Build a queue of transactions with no detected invoice (Invoice Number = 0/empty)
and the expected TIF path: <image_dir>/<Transaction Id>.tif

Output rows are sorted by Mail Stop (A-Z), then Invoice Number, then Transaction ID. Invoice Number echoes the Paystand export value for verification.

Merchant (payee) is resolved by Mail Stop via mail_stop_merchants.csv in CPI/ (embedded table is fallback only).
(under --run-dir, invoice folder, or script folder) can override entries (columns: Mail Stop, Merchant).

Page frames are read from each TIF for TIF Page Count and Needs Human. Use export_verification_previews.py
to rasterize every page to PNG under verification_previews/<Transaction Id>/.

Needs Human? is the primary decision column for the queue: it must be yes whenever Merchant Match or Amount Match
is blank, Missing, Not Legible, or Not Matched — only when both are Matched and there is no Poor Scan flag
(and file/export/page rules pass) is it no.

Local OCR compares CSV Merchant (payee) and Check Amount to text read from all pages of each TIF.
Merchant Match / Amount Match are written as Matched, Not Matched, Missing, or Not Legible (internal logic still
uses yes/no/skipped). Scan Notes carry OCR diagnostics. Engine order: Tesseract if available
(set TESSERACT_CMD to the binary if it is not on PATH), else on macOS Apple Vision via pyobjc-framework-Vision.

TIF Path in the CSV is the .tif filename only (e.g. 43022185.tif); files live under the Image*Detail* folder.
CSV cells may still use a legacy file:// URI from older exports; scripts accept both.

Needs Human? (after Payer) is the most important column; Reason explains it. It is yes unless the TIF is present,
page count is readable (when requested), the export has merchant and amount, both internal matches are yes
(CSV columns show Matched), and there is no Poor Scan flag.
TIF presence is inferred from the image folder and TIF Path; there is no separate TIF Exists column.

Scan Notes contain only OCR match diagnostics (no duplicate of TIF Page Count / Merchant / Check Amount flags).

merchant_aliases.csv in CPI/ (columns Merchant, AlsoKnownAs) lists payee name variants for OCR matching.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Union
from urllib.parse import urlparse, unquote

from tif_scan_match import analyze_tif_against_csv, builtin_merchant_aliases


def tif_path_from_queue_cell(cell: str, image_dir: Path) -> Path:
    """
    Resolve TIF Path CSV cell to a filesystem path (may not exist).

    New queues store a file:// URI (absolute). Older CSVs used the filename only;
    those are resolved under image_dir.
    """
    raw = (cell or "").strip()
    if not raw:
        return Path()
    if raw.startswith("file:"):
        return Path(unquote(urlparse(raw).path or ""))
    return image_dir / Path(raw).name

# Mail Stop -> merchant legal name (payee receiving funds). Optional CSV overrides these values.
_MAIL_STOP_MERCHANT_LINES = """
101	Paystand Corporate
102	Trustarc
103	ALLIED RUBBER & GASKET CO INC
104	REAL Homeownership Trust
105	Seashine MH Master Trust
106	Westrock Coffee Roasting
107	Westrock Coffee Services
108	Peak Design
109	Super 7
110	Discovery Health Services LLC
111	MD Solutions International Inc.
112	DHMD INC.
113	Discovery Health MD LLC
114	Booster Fuels Inc
115	PolySource LLC
116	eTrepid Inc.
117	Bevsource Inc
118	Associated Brewing Company LLC
119	Oofos Inc.
120	UOVO LLC
121	ProctorU Inc.
122	Triten Law LLP
123	Dickson
124	AutoReturn US LLC
125	World Oil Corp.
126	World Oil Marketing
127	Lunday-Thagard Company (World Oil)
128	Ribost Terminal LLC (World Oil)
129	Asbury Environmental Services (World Oil)
130	SRC Arizona LLC (World Oil)
131	DeMenno-Kerdoon (World Oil)
132	D/K Environmental (World Oil)
133	Pan Pacific Petroleum Company Inc. (World Oil)
134	Roth Shopping Center Holding Co. LLC (World Oil)
135	Roth Retail Property Holding Co. LLC (World Oil)
136	Bost Land LLC (World Oil)
139	Beech Valley Solutions LLC
140	Howler Brothers
141	Indeed Flex
142	PRX Inc.
143	We Are SCP
144	Sidecar Health Insurance Solutions
146	Rowley Hardware Inc.
147	Geoforce Inc
148	Pinnacle Publishing LLC
149	Topple Diagnostics
150	The Concussion Center
151	Avanti Restaurant Solutions Inc.
152	Clearpath
153	Relay Inc
154	DiningRD
155	Nonstop Administration and Insurance Services Inc - 155
157	Brite Computers
158	JEM Associates West Inc.
159	Brody Chemical Company Inc
160	Tripleseat Software LLC
161	Amanda Blu Co. LLC
162	CBUSA LLC
163	AlphaSense Inc
164	Tegus Inc
165	Early Stage Solutions, LLC
166	Sharetru
201	Nonstop Administration and Insurance Services Inc - 201
"""


def _parse_mail_stop_merchant_lines(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1].strip()
    return out


MAIL_STOP_MERCHANT = _parse_mail_stop_merchant_lines(_MAIL_STOP_MERCHANT_LINES)


def is_missing_invoice(value: str) -> bool:
    t = (value or "").strip()
    return t in ("0", "0.0", "")


def discover_invoice_and_images(run_dir: Path) -> tuple[Path, Path]:
    """
    Within run_dir, find a *Invoice*Detail*.csv and a folder *Image*Detail* (e.g. Paystand_Image_Detail_*) with .tif files.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(str(run_dir))
    cs = sorted(run_dir.glob("*Invoice*Detail*.csv"), key=lambda p: p.name)
    if not cs:
        raise FileNotFoundError(f"No *Invoice*Detail*.csv found under {run_dir}")
    if len(cs) > 1:
        # If several, prefer a filename containing "Paystand"
        pay = [p for p in cs if "Paystand" in p.name]
        invoice_csv = pay[0] if pay else cs[0]
    else:
        invoice_csv = cs[0]
    imgs = sorted([p for p in run_dir.iterdir() if p.is_dir() and "Image" in p.name and "Detail" in p.name])
    if not imgs:
        raise FileNotFoundError(
            f"No *Image*Detail* folder in {run_dir} (e.g. Paystand_Image_Detail_MM_DD_YYYY)"
        )
    if len(imgs) > 1:
        # Prefer names starting with Paystand_
        pay = [p for p in imgs if p.name.startswith("Paystand")]
        image_dir = pay[0] if pay else imgs[0]
    else:
        image_dir = imgs[0]
    return invoice_csv, image_dir


def load_merchant_csv_overrides(search_dirs: list[Path]) -> dict[str, str]:
    """Merge Mail Stop -> Merchant from every mail_stop_merchants.csv found (later files override earlier)."""
    out: dict[str, str] = {}
    for d in search_dirs:
        p = d / "mail_stop_merchants.csv"
        if not p.is_file():
            continue
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "Mail Stop" not in reader.fieldnames:
                continue
            for row in reader:
                ms = (row.get("Mail Stop") or "").strip()
                if not ms:
                    continue
                out[ms] = (row.get("Merchant") or "").strip()
    return out


def merchant_lookup_merged(search_dirs: list[Path]) -> dict[str, str]:
    merged = load_merchant_csv_overrides(search_dirs)
    if not merged:
        merged = dict(MAIL_STOP_MERCHANT)
    return merged


def load_merchant_aliases(search_dirs: list[Path]) -> dict[str, list[str]]:
    """Merchant -> alternate strings that may appear on checks (from merchant_aliases.csv; builtins if missing)."""
    out: dict[str, list[str]] = {}
    for d in search_dirs:
        p = d / "merchant_aliases.csv"
        if not p.is_file():
            continue
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "Merchant" not in reader.fieldnames:
                continue
            aka_col = "AlsoKnownAs" if "AlsoKnownAs" in reader.fieldnames else "Alias"
            if aka_col not in reader.fieldnames:
                continue
            for row in reader:
                m = (row.get("Merchant") or "").strip()
                aka = (row.get(aka_col) or "").strip()
                if not m or not aka:
                    continue
                out.setdefault(m, []).append(aka)
    if not out:
        out = builtin_merchant_aliases()
    return out


def unique_dirs(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def tif_page_count(tif: Path) -> Union[int, str]:
    """Return total page/frame count for the TIF (all pages), or '?' on read error, '' if missing."""
    if not tif.is_file():
        return ""
    try:
        from PIL import Image

        with Image.open(tif) as im:
            return int(getattr(im, "n_frames", 1))
    except Exception:
        return "?"


def export_incomplete_reasons(row: dict, merchant_payee: str) -> list[str]:
    """
    For invoice-number-missing queue rows, only check amount and merchant (Mail Stop lookup) matter.
    Does not consider routing or DDA. Missing Mail Stop lookup is labeled No Merchant for operators.
    """
    reasons: list[str] = []
    amt = (row.get("Check Amount") or "").strip()
    if not amt:
        reasons.append("Export missing check amount")
    if not (merchant_payee or "").strip():
        reasons.append("No Merchant")
    return reasons


def row_is_multipage_by_page_count(row: dict) -> bool:
    """True when TIF Page Count column is a number > 1."""
    t = (row.get("TIF Page Count") or "").strip()
    return t.isdigit() and int(t) > 1


def match_status_display(internal: str) -> str:
    """Human-readable CSV values (tif_scan_match still uses yes/no/skipped/no_legible)."""
    key = (internal or "").strip().lower()
    return {
        "yes": "Matched",
        "no": "Not Matched",
        "skipped": "Missing",
        "no_legible": "Not Legible",
    }.get(key, internal or "")


def _reason_append(labels: list[str], phrase: str) -> None:
    """Preserve order; no duplicates."""
    if phrase not in labels:
        labels.append(phrase)


def needs_human_reason(
    needs_human: bool,
    *,
    ex: bool,
    pages: int | str,
    csv_incomplete: bool,
    export_issues: list[str],
    merchant_match: str,
    amount_match: str,
    handwritten: bool,
) -> str:
    """
    Closed vocabulary for Reason when Needs Human? is yes (joined with '; ').
    Allowed phrases only — no free-form OCR snippets:
      TIF File Not Found; Could not read TIF page count;
      Missing merchant in CSV; Missing check amount in CSV; Missing merchant and check amount in CSV;
      Scan not legible; Merchant Doesn't Match; Check Amount Doesn't Match;
      Merchant and Check Amount Don't Match; Poor Scan.
    """
    if not needs_human:
        return "Merchant and Check Amount matches."

    labels: list[str] = []

    if not ex:
        _reason_append(labels, "TIF File Not Found")
    if pages == "?":
        _reason_append(labels, "Could not read TIF page count")

    if csv_incomplete:
        miss_m = "No Merchant" in export_issues
        miss_a = "Export missing check amount" in export_issues
        if miss_m and miss_a:
            _reason_append(labels, "Missing merchant and check amount in CSV")
        elif miss_m:
            _reason_append(labels, "Missing merchant in CSV")
        elif miss_a:
            _reason_append(labels, "Missing check amount in CSV")

    if ex:
        m_low = (merchant_match or "").strip().lower()
        a_low = (amount_match or "").strip().lower()

        if m_low == "no_legible" and a_low == "no_legible":
            _reason_append(labels, "Scan not legible")
        elif m_low or a_low:
            if m_low == "no" and a_low == "no":
                _reason_append(labels, "Merchant and Check Amount Don't Match")
            else:
                if m_low == "no" or (
                    bool(m_low) and m_low not in ("yes", "skipped", "no_legible")
                ):
                    _reason_append(labels, "Merchant Doesn't Match")
                elif m_low == "skipped" and "No Merchant" not in export_issues:
                    _reason_append(labels, "Missing merchant in CSV")

                if a_low == "no" or (
                    bool(a_low) and a_low not in ("yes", "skipped", "no_legible")
                ):
                    _reason_append(labels, "Check Amount Doesn't Match")
                elif (
                    a_low == "skipped"
                    and "Export missing check amount" not in export_issues
                ):
                    _reason_append(labels, "Missing check amount in CSV")

        if handwritten:
            _reason_append(labels, "Poor Scan")

    return "; ".join(labels)


def build_record(
    row: dict,
    tif: Path,
    *,
    with_pages: bool = True,
    merchant_payee: str = "",
    merchant_aliases: dict[str, list[str]] | None = None,
) -> dict:
    tif_path = tif
    ex = tif_path.is_file()
    pages: int | str = ""
    if with_pages and ex:
        pages = tif_page_count(tif_path)
    elif with_pages and not ex:
        pages = ""
    export_issues = export_incomplete_reasons(row, merchant_payee)
    incomplete = bool(export_issues)
    tid = (row.get("Transaction Id") or "").strip()

    scan = analyze_tif_against_csv(
        tif_path,
        merchant_csv=merchant_payee,
        check_amount_csv=(row.get("Check Amount") or "").strip(),
        merchant_aliases=(
            merchant_aliases if merchant_aliases is not None else builtin_merchant_aliases()
        ),
    )
    merchant_match = scan.merchant_match
    amount_match = scan.amount_match
    scan_notes_final = scan.scan_notes
    handwritten = scan.handwritten_review

    csv_incomplete = incomplete
    m_ok = (merchant_match or "").strip().lower() == "yes"
    a_ok = (amount_match or "").strip().lower() == "yes"
    # Authoritative flag: any blank/Missing/not legible/not matched column or Poor Scan → yes.
    needs_human = (
        (not ex)
        or (pages == "?")
        or csv_incomplete
        or (not m_ok)
        or (not a_ok)
        or handwritten
    )

    reason = needs_human_reason(
        needs_human,
        ex=ex,
        pages=pages,
        csv_incomplete=csv_incomplete,
        export_issues=export_issues,
        merchant_match=merchant_match,
        amount_match=amount_match,
        handwritten=handwritten,
    )

    return {
        "Mail Stop": (row.get("Mail Stop") or "").strip(),
        "Deposit Date": (row.get("Deposit Date") or "").strip(),
        "Merchant": merchant_payee,
        "Check Amount": (row.get("Check Amount") or "").strip(),
        "Transaction ID": tid,
        "Invoice Number": str(row.get("Invoice Number") or "").strip(),
        "Payer": (row.get("Payer") or "").strip(),
        "Needs Human?": "yes" if needs_human else "no",
        "Reason": reason,
        "Merchant Match": match_status_display(merchant_match),
        "Amount Match": match_status_display(amount_match),
        "TIF Path": tif_path.name,
        "TIF Page Count": str(pages) if pages != "" else "",
        "Scan Notes": scan_notes_final,
    }


def main() -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="List TIF review queue (invoice number missing).")
    ap.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Day folder (e.g. ./04-28-2026) containing the Invoice*Detail*.csv and Paystand_*Image*Detail* folder. "
        "If set, overrides --invoice-csv and --image-dir with auto-discovered paths.",
    )
    ap.add_argument(
        "--invoice-csv",
        type=Path,
        default=here / "Paystand_Invoice_Detail_04_27_2026.csv",
        help="Invoice detail export (Invoice Number column).",
    )
    ap.add_argument(
        "--image-dir",
        type=Path,
        default=here / "Paystand_Image_Detail_04_27_2026",
        help="Folder containing <Transaction Id>.tif files",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output CSV (English columns). Default with --run-dir: <run_dir>/tif_review_queue.csv. "
        "If omitted without --run-dir: stdout.",
    )
    ap.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print errors (no paths summary or row count line).",
    )
    ap.add_argument(
        "--no-tif-pages",
        action="store_true",
        help="Do not read each TIF's page count (faster; TIF Page Count and page-related Notes left empty).",
    )
    args = ap.parse_args()

    if args.run_dir is not None:
        run_dir = args.run_dir.resolve()
        inv, img = discover_invoice_and_images(run_dir)
        args.invoice_csv = inv
        args.image_dir = img
        if args.output is None:
            args.output = run_dir / "tif_review_queue.csv"
        if not args.quiet:
            print(
                f"Run directory: {run_dir}\n  invoice CSV: {args.invoice_csv.name}\n  image folder: {args.image_dir.name}\n",
                flush=True,
            )
            print(
                "  (OCR per row can take several minutes; progress lines follow.)\n",
                flush=True,
            )

    if not args.invoice_csv.is_file():
        raise SystemExit(f"CSV not found: {args.invoice_csv}")
    if not args.image_dir.is_dir():
        raise SystemExit(f"Folder not found: {args.image_dir}")

    merchant_lookup_dirs = unique_dirs(
        [
            here,
            *( [args.run_dir.resolve()] if args.run_dir else [] ),
            args.invoice_csv.resolve().parent,
        ]
    )
    merchant_lookup = merchant_lookup_merged(merchant_lookup_dirs)
    merchant_aliases = load_merchant_aliases(merchant_lookup_dirs)

    # Every Paystand row with missing invoice is emitted (same Transaction Id may appear on multiple rows).
    queue_rows: list[tuple[dict, Path, str]] = []
    with args.invoice_csv.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        required = {"Transaction Id", "Invoice Number", "Check Amount", "Mail Stop"}
        if not required.issubset(reader.fieldnames or []):
            raise SystemExit(f"Missing CSV columns. Required: {required}")

        for row in reader:
            if not is_missing_invoice(row.get("Invoice Number", "")):
                continue
            tid = (row.get("Transaction Id") or "").strip()
            if not tid:
                continue
            tif = args.image_dir / f"{tid}.tif"
            ms = (row.get("Mail Stop") or "").strip()
            queue_rows.append((row, tif, ms))

    rows_out: list[dict] = []
    nq = len(queue_rows)
    for i, (row, tif, ms) in enumerate(queue_rows, start=1):
        if not args.quiet:
            tid = (row.get("Transaction Id") or "").strip()
            print(f"  OCR {i}/{nq} Transaction ID {tid} …", flush=True)
        rows_out.append(
            build_record(
                row,
                tif,
                with_pages=not args.no_tif_pages,
                merchant_payee=merchant_lookup.get(ms, ""),
                merchant_aliases=merchant_aliases,
            )
        )

    out_fields = [
        "Mail Stop",
        "Deposit Date",
        "Merchant",
        "Check Amount",
        "Transaction ID",
        "Invoice Number",
        "Payer",
        "Needs Human?",
        "Reason",
        "Merchant Match",
        "Amount Match",
        "TIF Path",
        "TIF Page Count",
        "Scan Notes",
    ]
    rows_out.sort(
        key=lambda r: (
            (r.get("Mail Stop") or "").strip().lower(),
            (r.get("Invoice Number") or "").strip().lower(),
            (r.get("Transaction ID") or "").strip().lower(),
        )
    )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=out_fields)
            w.writeheader()
            w.writerows(rows_out)
        if not args.quiet:
            print(f"Wrote: {args.output} ({len(rows_out)} rows)")
    else:
        w = csv.DictWriter(__import__("sys").stdout, fieldnames=out_fields)
        w.writeheader()
        w.writerows(rows_out)

    if args.output:
        missing = sum(
            1
            for r in rows_out
            if not tif_path_from_queue_cell(r.get("TIF Path") or "", args.image_dir).is_file()
        )
        if missing and not args.quiet:
            print(f"Warning: {missing} transaction(s) with no .tif at the expected path.")
        mp = sum(1 for r in rows_out if row_is_multipage_by_page_count(r))
        nh = sum(1 for r in rows_out if r.get("Needs Human?") == "yes")
        if not args.quiet:
            if not args.no_tif_pages:
                print(f"Summary: {nh} need human review; {mp} multi-page TIF(s).")
            else:
                print(
                    f"Summary: {nh} need human review. "
                    f"(TIF page count skipped; omit --no-tif-pages to read pages.)"
                )


if __name__ == "__main__":
    main()
