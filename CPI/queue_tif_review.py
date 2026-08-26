#!/usr/bin/env python3
"""
Build a queue covering every invoice-detail row and the expected TIF path:
<image_dir>/<Transaction Id>.tif

Two scan types (see "Scan Type" column):
- "No Invoice" (Invoice Number = 0/empty): full scan — merchant AND check amount must match the
  TIF, same rules as before.
- "Invoice On File" (Invoice Number present): lighter misroute check only — the invoice number is
  assumed to already validate amount/application, so only the payee is checked. Needs Human? is
  yes ONLY when the OCR payee clearly matches a DIFFERENT already-known merchant (mail_stop_merchants.csv
  / merchant_aliases.csv) — a likely misroute. Ambiguous cases (illegible scan, or a payee that
  matches neither the expected merchant nor any other known merchant — most likely just an
  alias/DBA we haven't catalogued) are intentionally NOT flagged, to avoid burying real misroutes
  in noise from every unseen alias.

Output rows are sorted by Mail Stop (A-Z), then Invoice Number, then Transaction ID. Invoice Number echoes the Paystand export value for verification.

Merchant (payee) is resolved by Mail Stop via mail_stop_merchants.csv in CPI/ (embedded table is fallback only).
(under --run-dir, invoice folder, or script folder) can override entries (columns: Mail Stop, Merchant).

Page frames are read from each TIF for TIF Page Count and Needs Human. Use export_verification_previews.py
to rasterize every page to PNG under verification_previews/<Transaction Id>/.

Needs Human? is yes when Merchant Match or Amount Match is not Matched, or TIF/export/page rules fail.
Poor Scan (low OCR confidence) alone does not set Needs Human? when both matches are Matched.

Local OCR compares CSV Merchant (payee) and Check Amount to text read from all pages of each TIF.
Merchant Match / Amount Match are written as Matched, Not Matched, Missing, or Not Legible (internal logic still
uses yes/no/skipped). Scan Notes carry OCR diagnostics. Engine order: Tesseract if available
(set TESSERACT_CMD to the binary if it is not on PATH), else on macOS Apple Vision via pyobjc-framework-Vision.

TIF Path in the CSV is the .tif filename only (e.g. 43022185.tif); files live under the Image*Detail* folder.
CSV cells may still use a legacy file:// URI from older exports; scripts accept both.

Needs Human? (after Payer) is the most important column; Reason explains it. It is no only when the TIF is present,
page count is readable (when requested), export merchant/amount are present, and both matches are Matched.
Poor Scan may still appear in Scan Notes (without "human review") but does not force Needs Human? when both are Matched.
TIF presence is inferred from the image folder and TIF Path; there is no separate TIF Exists column.

Scan Notes contain only OCR match diagnostics (no duplicate of TIF Page Count / Merchant / Check Amount flags).

merchant_aliases.csv in CPI/ (columns Merchant, AlsoKnownAs) lists payee name variants for OCR matching.
"""
from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union
from urllib.parse import urlparse, unquote

from tif_scan_match import (
    analyze_tif_against_csv,
    analyze_tif_for_misroute,
    builtin_merchant_aliases,
    detect_non_check_document,
)

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
168	Global Surf Industries Inc
170	Iantrek, Inc.
171	Aloha Collection, Inc.
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


# Paystand export layouts: leading cols, Payer (may contain commas), trailing cols.
_PAYSTAND_INVOICE_HEAD_COLS = 6
_PAYSTAND_INVOICE_TAIL_COLS = 4
_PAYSTAND_CHECK_HEAD_COLS = 6
_PAYSTAND_CHECK_TAIL_COLS = 2
_PAYSTAND_IMAGE_META_HEAD_COLS = 10
_PAYSTAND_IMAGE_META_TAIL_COLS = 1


def _realigned_paystand_payer_fields(
    fields: list[str],
    expected_cols: int,
    *,
    head_cols: int,
    tail_cols: int,
) -> list[str]:
    """Merge extra CSV fields into Payer when commas in payer were not quoted."""
    n = expected_cols
    if len(fields) == n:
        return fields
    if len(fields) > n and len(fields) >= head_cols + tail_cols + 1:
        payer = ",".join(fields[head_cols : len(fields) - tail_cols])
        return fields[:head_cols] + [payer] + fields[-tail_cols:]
    if len(fields) < n:
        return fields + [""] * (n - len(fields))
    return fields


def _realigned_paystand_invoice_fields(fields: list[str], expected_cols: int) -> list[str]:
    return _realigned_paystand_payer_fields(
        fields,
        expected_cols,
        head_cols=_PAYSTAND_INVOICE_HEAD_COLS,
        tail_cols=_PAYSTAND_INVOICE_TAIL_COLS,
    )


def fix_paystand_export_csv(
    csv_path: Path,
    *,
    head_cols: int,
    tail_cols: int,
    backup: bool = True,
) -> int:
    """Rewrite a Paystand export CSV with payer commas quoted, and strip stray/misplaced
    quote characters left over from malformed export quoting (see MALFORMED_QUOTE in
    audit_paystand_csv_commas). Returns rows realigned or cleaned."""
    raw_text = csv_path.read_text(encoding="utf-8", errors="replace")
    raw_lines_all = raw_text.splitlines(keepends=True)
    reader = csv.reader(raw_lines_all)
    header = next(reader, None)
    if not header:
        return 0
    n = len(header)
    body = list(reader)
    if not body:
        return 0
    footer = body[-1]
    data = body[:-1]
    fixed: list[list[str]] = []
    realigned = 0
    raw_cursor = 1
    for fields in data:
        merged = any("\n" in (f or "") for f in fields)
        consumed_lines = 1 + sum((f or "").count("\n") for f in fields)
        row_raw_lines = raw_lines_all[raw_cursor : raw_cursor + consumed_lines]
        raw_cursor += consumed_lines
        malformed_quote = (not merged) and _row_has_malformed_quote(row_raw_lines)
        changed = len(fields) != n or malformed_quote
        if changed:
            realigned += 1
        aligned = _realigned_paystand_payer_fields(
            fields, n, head_cols=head_cols, tail_cols=tail_cols
        )
        if len(aligned) != n:
            raise ValueError(f"Could not realign row in {csv_path}: {fields[:8]}...")
        if malformed_quote and len(aligned) > head_cols:
            aligned = list(aligned)
            aligned[head_cols] = _strip_stray_quotes(aligned[head_cols])
        fixed.append(aligned)
    if backup:
        bak = csv_path.with_suffix(csv_path.suffix + ".bak")
        if not bak.is_file():
            import shutil

            shutil.copy2(csv_path, bak)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(fixed)
        w.writerow(footer)
    return realigned


@dataclass
class CommaIssueRow:
    line_no: int
    transaction_id_wrong: str
    transaction_id_correct: str
    invoice_number: str
    check_amount: str
    payer: str
    category: str
    merged: bool = False
    malformed_quote: bool = False


@dataclass
class CommaAuditReport:
    label: str
    path: Path
    data_rows: int
    misaligned: int
    buckets: dict[str, int] = field(default_factory=dict)
    issues: list[CommaIssueRow] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)
    fixed: int = 0

    @property
    def merged_rows(self) -> list[CommaIssueRow]:
        """Rows where an unterminated quote swallowed a following row (data loss risk)."""
        return [i for i in self.issues if i.merged]

    @property
    def malformed_quote_rows(self) -> list[CommaIssueRow]:
        """Rows with a stray/misplaced quote contained within a single row (no data loss;
        auto-fixable by dropping the stray quote character(s))."""
        return [i for i in self.issues if i.malformed_quote]


def _row_has_malformed_quote(raw_lines: list[str]) -> bool:
    """True if strict CSV parsing rejects this row's raw physical line(s).

    A properly quoted field (including one with correctly escaped internal quotes, e.g. a
    quoted nickname with each internal quote doubled) always parses fine under strict mode.
    Only a stray/misplaced quote character (one that isn't a valid escape or the true
    closing quote) trips this.
    """
    try:
        list(csv.reader(raw_lines, strict=True))
    except csv.Error:
        return True
    return False


def _strip_stray_quotes(value: str) -> str:
    """Drop stray literal double-quote characters left over from a malformed export field.

    Only double quotes are removed (the one CSV-breaking character relevant here) — every
    other character (apostrophes, punctuation, etc.) is left untouched.
    """
    return value.replace('"', "")


def _payer_comma_category(payer: str) -> str:
    up = (payer or "").upper()
    if re.search(r",\s*LLC\b", up):
        return "LLC"
    if re.search(r",\s*INC\.?\b", up):
        return "INC"
    if " DBA " in up or " D/B/A " in up:
        return "dba"
    if re.search(r",\s*(NE|OHIO|TX|MA|MO|AZ|IL|CO|FL|CA|NC|GA|UT|DE)\b", up):
        return "address"
    return "other"


def discover_check_csv(run_dir: Path) -> Path | None:
    cs = sorted(run_dir.glob("*Check*Detail*.csv"), key=lambda p: p.name)
    if not cs:
        return None
    pay = [p for p in cs if "Paystand" in p.name]
    return pay[0] if pay else cs[0]


def discover_image_metadata_csv(image_dir: Path) -> Path | None:
    p = image_dir / "metadata.csv"
    return p if p.is_file() else None


def audit_paystand_csv_commas(
    csv_path: Path,
    *,
    label: str,
    head_cols: int,
    tail_cols: int,
    transaction_id_header: str = "Transaction Id",
    invoice_number_header: str | None = "Invoice Number",
    check_amount_header: str | None = "Check Amount",
) -> CommaAuditReport:
    """Detect unquoted payer commas on every data row (invoice 0 and invoiced rows alike).

    Also detects two kinds of malformed quoting that a plain column-count check misses:
    - MERGED_ROWS_DATA_LOSS: an unterminated quote swallows the next physical row whole
      (a transaction disappears). Detected via an embedded raw newline in a parsed field.
    - MALFORMED_QUOTE: a stray/misplaced quote is fully contained within one row (no rows
      swallowed, column count often still correct by coincidence) but corrupts the Payer
      text. Detected by re-parsing each row's raw physical line(s) with strict CSV rules
      (`csv.reader(..., strict=True)`), which rejects any quote that isn't a valid escape
      or true closing quote — exactly the case a lenient parser silently "recovers" from.
    """
    raw_text = csv_path.read_text(encoding="utf-8", errors="replace")
    raw_lines_all = raw_text.splitlines(keepends=True)
    reader = csv.reader(raw_lines_all)
    header = next(reader, None)
    if not header:
        return CommaAuditReport(label, csv_path, 0, 0)
    n = len(header)
    body = list(reader)
    if not body:
        return CommaAuditReport(label, csv_path, 0, 0)
    footer = body[-1]
    data = body[:-1]
    tid_idx = header.index(transaction_id_header) if transaction_id_header in header else None
    inv_idx = (
        header.index(invoice_number_header)
        if invoice_number_header and invoice_number_header in header
        else None
    )
    amt_idx = (
        header.index(check_amount_header)
        if check_amount_header and check_amount_header in header
        else None
    )
    issues: list[CommaIssueRow] = []
    buckets: dict[str, int] = {}
    line_no = 2
    raw_cursor = 1  # raw_lines_all[0] is the header
    for fields in data:
        # A field that itself contains a raw newline means a quote was left open (e.g. a
        # stray extra `"` right before the closing quote) and csv.reader swallowed the next
        # physical line into this one. This can happen even when len(fields) == n by sheer
        # coincidence, silently merging two transactions into one row and losing one of them
        # entirely — the plain column-count check below would miss it, so check it first.
        merged = any("\n" in (f or "") for f in fields)
        consumed_lines = 1 + sum((f or "").count("\n") for f in fields)
        row_raw_lines = raw_lines_all[raw_cursor : raw_cursor + consumed_lines]
        raw_cursor += consumed_lines
        malformed_quote = (not merged) and _row_has_malformed_quote(row_raw_lines)
        if len(fields) == n and not merged and not malformed_quote:
            line_no += consumed_lines
            continue
        aligned = (
            fields
            if len(fields) == n
            else _realigned_paystand_payer_fields(
                fields, n, head_cols=head_cols, tail_cols=tail_cols
            )
        )
        payer = aligned[head_cols] if len(aligned) > head_cols else ""
        if merged:
            category = "MERGED_ROWS_DATA_LOSS"
        elif malformed_quote:
            category = "MALFORMED_QUOTE"
        else:
            category = _payer_comma_category(payer)
        buckets[category] = buckets.get(category, 0) + 1
        wrong_tid = (
            (fields[tid_idx] or "").strip()
            if tid_idx is not None and len(fields) > tid_idx
            else ""
        )
        correct_tid = (
            (aligned[tid_idx] or "").strip()
            if tid_idx is not None and len(aligned) > tid_idx
            else ""
        )
        inv = (
            (aligned[inv_idx] or "").strip()
            if inv_idx is not None and len(aligned) > inv_idx
            else ""
        )
        amt = (
            (aligned[amt_idx] or "").strip()
            if amt_idx is not None and len(aligned) > amt_idx
            else ""
        )
        issues.append(
            CommaIssueRow(
                line_no=line_no,
                transaction_id_wrong=wrong_tid,
                transaction_id_correct=correct_tid,
                invoice_number=inv,
                check_amount=amt,
                payer=payer,
                category=category,
                merged=merged,
                malformed_quote=malformed_quote,
            )
        )
        line_no += consumed_lines
    return CommaAuditReport(
        label=label,
        path=csv_path,
        data_rows=len(data),
        misaligned=len(issues),
        buckets=buckets,
        issues=issues,
        footer=footer,
    )


def audit_run_dir_exports(run_dir: Path) -> list[CommaAuditReport]:
    """Audit invoice, check, and image metadata exports under a day folder."""
    invoice_csv, image_dir = discover_invoice_and_images(run_dir)
    reports = [
        audit_paystand_csv_commas(
            invoice_csv,
            label="invoice",
            head_cols=_PAYSTAND_INVOICE_HEAD_COLS,
            tail_cols=_PAYSTAND_INVOICE_TAIL_COLS,
        )
    ]
    check_csv = discover_check_csv(run_dir)
    if check_csv:
        reports.append(
            audit_paystand_csv_commas(
                check_csv,
                label="check",
                head_cols=_PAYSTAND_CHECK_HEAD_COLS,
                tail_cols=_PAYSTAND_CHECK_TAIL_COLS,
                invoice_number_header=None,
                check_amount_header="Amount",
            )
        )
    meta_csv = discover_image_metadata_csv(image_dir)
    if meta_csv:
        reports.append(
            audit_paystand_csv_commas(
                meta_csv,
                label="image metadata",
                head_cols=_PAYSTAND_IMAGE_META_HEAD_COLS,
                tail_cols=_PAYSTAND_IMAGE_META_TAIL_COLS,
                invoice_number_header=None,
                check_amount_header="Amount",
            )
        )
    return reports


def ensure_run_dir_exports_commas_clean(
    run_dir: Path,
    *,
    auto_fix: bool = True,
    backup: bool = False,
) -> list[CommaAuditReport]:
    """Audit payer comma alignment; optionally rewrite exports with quoted payers."""
    layout = {
        "invoice": (_PAYSTAND_INVOICE_HEAD_COLS, _PAYSTAND_INVOICE_TAIL_COLS),
        "check": (_PAYSTAND_CHECK_HEAD_COLS, _PAYSTAND_CHECK_TAIL_COLS),
        "image metadata": (_PAYSTAND_IMAGE_META_HEAD_COLS, _PAYSTAND_IMAGE_META_TAIL_COLS),
    }
    initial = audit_run_dir_exports(run_dir)
    fixed_by_label: dict[str, int] = {}
    if auto_fix:
        for report in initial:
            if report.misaligned <= 0:
                continue
            head, tail = layout[report.label]
            fixed_by_label[report.label] = fix_paystand_export_csv(
                report.path, head_cols=head, tail_cols=tail, backup=backup
            )
    final = audit_run_dir_exports(run_dir) if fixed_by_label else initial
    initial_by_label = {r.label: r for r in initial}
    for report in final:
        report.fixed = fixed_by_label.get(report.label, 0)
        if report.fixed:
            # The successfully-fixed rows (comma realignment, malformed-quote cleanup) no
            # longer show up in a fresh re-audit — restore the pre-fix diagnostics here so
            # the summary still reports what was actually wrong/changed. Rows that could NOT
            # be fixed (merged/data-loss) are untouched by the fix step, so they're identical
            # between initial and final either way.
            pre = initial_by_label.get(report.label)
            if pre:
                report.buckets = pre.buckets
                report.issues = pre.issues
    return final


def format_comma_audit_summary(reports: list[CommaAuditReport], *, run_dir: Path) -> str:
    lines = [f"Comma audit: {run_dir}"]
    total = sum(r.misaligned for r in reports)
    fixed = sum(r.fixed for r in reports)
    for r in reports:
        lines.append(
            f"  {r.label} ({r.path.name}): {r.data_rows} data rows, "
            f"{r.misaligned} misaligned"
            + (f", fixed {r.fixed}" if r.fixed else "")
        )
        if r.buckets:
            parts = ", ".join(f"{k}={v}" for k, v in sorted(r.buckets.items()))
            lines.append(f"    categories: {parts}")
    if total == 0:
        lines.append("  OK — no unquoted payer commas detected.")
    elif fixed:
        lines.append(f"  Repaired {fixed} row(s) across export file(s).")
    wrong_tid = [
        i
        for r in reports
        for i in r.issues
        if i.transaction_id_wrong
        and i.transaction_id_correct
        and i.transaction_id_wrong != i.transaction_id_correct
    ]
    if wrong_tid:
        lines.append(
            f"  Transaction Id would have shifted on {len(wrong_tid)} row(s) before fix."
        )
        for issue in wrong_tid[:8]:
            inv_note = f", inv={issue.invoice_number}" if issue.invoice_number else ", inv=0"
            lines.append(
                f"    line {issue.line_no}: {issue.transaction_id_wrong} -> "
                f"{issue.transaction_id_correct}{inv_note} [{issue.category}]"
            )
        if len(wrong_tid) > 8:
            lines.append(f"    … and {len(wrong_tid) - 8} more")
    malformed = [(r, i) for r in reports for i in r.malformed_quote_rows]
    if malformed:
        lines.append(
            f"  Cleaned {len(malformed)} malformed-quote Payer value(s) "
            "(stray \" dropped; no columns shifted, no rows lost):"
        )
        for r, issue in malformed[:8]:
            lines.append(
                f"    {r.label} ({r.path.name}) line {issue.line_no}: "
                f"tid={issue.transaction_id_correct or issue.transaction_id_wrong} "
                f"payer(before)={issue.payer[:60]!r} -> "
                f"payer(after)={_strip_stray_quotes(issue.payer)[:60]!r}"
            )
        if len(malformed) > 8:
            lines.append(f"    … and {len(malformed) - 8} more")
    merged = [(r, i) for r in reports for i in r.merged_rows]
    if merged:
        lines.append(
            "  \U0001f6a8 CRITICAL: unterminated quote swallowed a following row "
            "(DATA LOSS — a transaction is missing/merged). This is NOT auto-fixable; "
            "edit the raw export by hand (remove the stray extra \" before the closing "
            "quote) and re-run before trusting this day's output:"
        )
        for r, issue in merged:
            lines.append(
                f"    {r.label} ({r.path.name}) line {issue.line_no}: "
                f"payer={issue.payer[:80]!r}..."
            )
    return "\n".join(lines)


def has_critical_comma_issues(reports: list[CommaAuditReport]) -> bool:
    """True if any report has an unfixable merged/data-loss row."""
    return any(r.merged_rows for r in reports)


def load_invoice_export_rows(invoice_csv: Path) -> list[dict]:
    """
    Paystand Invoice Detail rows for processing.

    Payer names often contain unquoted commas (e.g. "VANA & SONS, LLC"); realign by
    keeping the first 6 and last 4 columns fixed and merging the middle into Payer.

    The export always includes a non-transaction footer as the last line (often sparse /
    no Transaction Id); drop it so lockbox and queue counts match real payments only.
    """
    with invoice_csv.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        n = len(header)
        rows: list[dict] = []
        for fields in reader:
            aligned = _realigned_paystand_invoice_fields(fields, n)
            if len(aligned) != n:
                continue
            rows.append(dict(zip(header, aligned)))
    if rows:
        rows = rows[:-1]
    return rows


def load_invoice_export_footer(invoice_csv: Path) -> tuple[str, str] | None:
    """Paystand Invoice Detail footer: row count and total amount (last CSV line)."""
    with invoice_csv.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        next(reader, None)
        last: list[str] | None = None
        for fields in reader:
            last = fields
    if not last or len(last) < 2:
        return None
    count = (last[0] or "").strip()
    total = (last[1] or "").strip()
    if not count and not total:
        return None
    return count, total


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

    Separately, rows missing a CSV check amount where the TIF confidently contains no bank-check
    image at all (mailed notice, legal filing, envelope, internal policy, vendor letter — see
    detect_non_check_document) short-circuit in build_record with Needs Human? = no and
    Reason = "Not a check: <label>." before this function is even called.
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

    if ex and "Export missing check amount" in export_issues:
        non_check_reason = detect_non_check_document(tif_path)
        if non_check_reason:
            return {
                "Mail Stop": (row.get("Mail Stop") or "").strip(),
                "Deposit Date": (row.get("Deposit Date") or "").strip(),
                "Merchant": merchant_payee,
                "Check Amount": (row.get("Check Amount") or "").strip(),
                "Transaction ID": tid,
                "Invoice Number": str(row.get("Invoice Number") or "").strip(),
                "Scan Type": "No Invoice",
                "Payer": (row.get("Payer") or "").strip(),
                "Needs Human?": "no",
                "Reason": f"Not a check: {non_check_reason}.",
                "Merchant Match": "Not Checked",
                "Amount Match": "Not Checked",
                "TIF Path": tif_path.name,
                "TIF Page Count": str(tif_page_count(tif_path)) if with_pages else "",
                "Scan Notes": f"TIF has no PAY TO THE ORDER OF anchor on any page; "
                f"OCR text matches known non-check pattern: {non_check_reason}.",
            }

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
    # Poor Scan alone does not require review when payee and amount both matched.
    needs_human = (
        (not ex)
        or (pages == "?")
        or csv_incomplete
        or (not m_ok)
        or (not a_ok)
        or (handwritten and not (m_ok and a_ok))
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
        "Scan Type": "No Invoice",
        "Payer": (row.get("Payer") or "").strip(),
        "Needs Human?": "yes" if needs_human else "no",
        "Reason": reason,
        "Merchant Match": match_status_display(merchant_match),
        "Amount Match": match_status_display(amount_match),
        "TIF Path": tif_path.name,
        "TIF Page Count": str(pages) if pages != "" else "",
        "Scan Notes": scan_notes_final,
    }


def misroute_reason(status: str, matched_other_merchant: str | None) -> str:
    """Closed vocabulary for Reason on 'Invoice On File' rows (amount is not checked here)."""
    if status == "yes":
        return "Merchant matches expected payee."
    if status == "no" and matched_other_merchant:
        return f'Possible Misroute: OCR payee matches "{matched_other_merchant}".'
    if status == "no_legible":
        return "Scan not legible; not reviewed (invoice already on file)."
    if status == "skipped":
        return "TIF file not found or merchant missing; not reviewed."
    return "Invoice already on file; payee not recognized."


def build_misroute_record(
    row: dict,
    tif: Path,
    *,
    with_pages: bool = True,
    merchant_payee: str = "",
    all_merchants: list[str],
    merchant_aliases: dict[str, list[str]] | None = None,
) -> dict:
    """Lighter scan for invoice lines that already have an invoice number on file: only flags a
    likely misroute (OCR payee matches a different KNOWN merchant). Amount is not checked."""
    tif_path = tif
    ex = tif_path.is_file()
    pages: int | str = ""
    if with_pages and ex:
        pages = tif_page_count(tif_path)
    tid = (row.get("Transaction Id") or "").strip()

    result = analyze_tif_for_misroute(
        tif_path,
        merchant_csv=merchant_payee,
        all_merchants=all_merchants,
        merchant_aliases=(
            merchant_aliases if merchant_aliases is not None else builtin_merchant_aliases()
        ),
    )
    needs_human = result.status == "no"
    reason = misroute_reason(result.status, result.matched_other_merchant)
    merchant_match_display = {
        "yes": "Matched",
        "no": "Different Merchant",
        "unrecognized": "Not Matched",
        "no_legible": "Not Legible",
        "skipped": "Missing",
    }.get(result.status, result.status)

    return {
        "Mail Stop": (row.get("Mail Stop") or "").strip(),
        "Deposit Date": (row.get("Deposit Date") or "").strip(),
        "Merchant": merchant_payee,
        "Check Amount": (row.get("Check Amount") or "").strip(),
        "Transaction ID": tid,
        "Invoice Number": str(row.get("Invoice Number") or "").strip(),
        "Scan Type": "Invoice On File",
        "Payer": (row.get("Payer") or "").strip(),
        "Needs Human?": "yes" if needs_human else "no",
        "Reason": reason,
        "Merchant Match": merchant_match_display,
        "Amount Match": "Not Checked",
        "TIF Path": tif_path.name,
        "TIF Page Count": str(pages) if pages != "" else "",
        "Scan Notes": result.scan_notes,
    }


_VISUAL_CLEAR_NOTE = (
    "Additional visual review by LUCAS confirmed payee and amount on TIF."
)


def parse_visual_clear_ids(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in (raw or "").replace(" ", ",").split(","):
        tid = part.strip()
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def apply_visual_review_clears(queue_csv: Path, transaction_ids: list[str]) -> tuple[int, list[str]]:
    """Mark visually confirmed false alarms as Needs Human? = no in an existing queue CSV.

    Does not re-run OCR. Only rows currently flagged yes for the given Transaction IDs
    are rewritten (Merchant/Amount Match flipped to Matched; Reason set to the same
    closed-vocab success string the OCR path uses). Returns (rows_cleared, missing_tids).
    """
    wanted = {t.strip() for t in transaction_ids if t.strip()}
    if not wanted:
        raise SystemExit("No Transaction IDs given to --visual-clear.")
    if not queue_csv.is_file():
        raise SystemExit(f"Queue CSV not found: {queue_csv}")

    with queue_csv.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "Transaction ID" not in fieldnames or "Needs Human?" not in fieldnames:
        raise SystemExit(f"Queue CSV missing Transaction ID or Needs Human?: {queue_csv}")

    found: set[str] = set()
    cleared = 0
    for row in rows:
        tid = (row.get("Transaction ID") or "").strip()
        if tid not in wanted:
            continue
        found.add(tid)
        if (row.get("Needs Human?") or "").strip().lower() != "yes":
            continue
        scan_type = (row.get("Scan Type") or "").strip()
        row["Needs Human?"] = "no"
        row["Merchant Match"] = "Matched"
        notes = (row.get("Scan Notes") or "").strip()
        notes = notes.replace(
            "Visual review: payee and amount confirmed on TIF.", ""
        ).replace(
            "Visual review: payee confirmed on TIF (not a misroute).", ""
        ).strip()
        if scan_type == "Invoice On File":
            row["Reason"] = "Merchant matches expected payee."
        else:
            row["Reason"] = "Merchant and Check Amount matches."
            row["Amount Match"] = "Matched"
        if _VISUAL_CLEAR_NOTE not in notes:
            row["Scan Notes"] = f"{notes} {_VISUAL_CLEAR_NOTE}".strip() if notes else _VISUAL_CLEAR_NOTE
        else:
            row["Scan Notes"] = notes
        cleared += 1

    with queue_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    missing = [t for t in transaction_ids if t.strip() and t.strip() not in found]
    return cleared, missing


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
    ap.add_argument(
        "--comma-audit-only",
        action="store_true",
        help="Audit unquoted/malformed payer commas in invoice/check/image exports (report "
        "only, never rewrites files); skip OCR queue.",
    )
    ap.add_argument(
        "--visual-clear",
        default="",
        help="Comma-separated Transaction IDs visually confirmed as false alarms. Updates "
        "tif_review_queue.csv in place (Needs Human?=no); does not re-run OCR.",
    )
    args = ap.parse_args()

    if args.visual_clear:
        tids = parse_visual_clear_ids(args.visual_clear)
        if args.run_dir is not None:
            queue_csv = args.run_dir.resolve() / "tif_review_queue.csv"
        elif args.output is not None:
            queue_csv = args.output
        else:
            raise SystemExit("--visual-clear requires --run-dir or --output (the queue CSV).")
        cleared, missing = apply_visual_review_clears(queue_csv, tids)
        print(f"Visual review: cleared {cleared} row(s) in {queue_csv}")
        if missing:
            print(
                "Warning: Transaction ID(s) not found in queue: " + ", ".join(missing),
                flush=True,
            )
        return

    if args.run_dir is not None:
        run_dir = args.run_dir.resolve()
        # Comma audit never rewrites the raw exports (auto_fix=False, always — see
        # cpi-lockbox-comma-audit.mdc). ANY issue found (unquoted comma, malformed quote, or
        # merged/data-loss rows) stops processing immediately; the user fixes the raw invoice/
        # check/image-metadata CSVs by hand and re-runs.
        comma_reports = ensure_run_dir_exports_commas_clean(
            run_dir,
            auto_fix=False,
        )
        any_issue = any(r.misaligned for r in comma_reports)
        if not args.quiet or args.comma_audit_only or any_issue:
            print(format_comma_audit_summary(comma_reports, run_dir=run_dir), flush=True)
            print(flush=True)
        if args.comma_audit_only:
            raise SystemExit(1 if any_issue else 0)
        if any_issue:
            print(
                "Aborting: comma audit found issue(s) in the raw export(s) above "
                "(unquoted comma, malformed quote, and/or merged/data-loss row). Files are "
                "left untouched — fix the raw invoice/check/image-metadata CSV(s) by hand "
                "and re-run before generating the queue or lockbox report.",
                flush=True,
            )
            raise SystemExit(3)

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
    all_merchants = sorted({v.strip() for v in merchant_lookup.values() if v.strip()})

    # Every Paystand invoice-detail row is emitted (same Transaction Id may appear on multiple
    # rows — multi-invoice checks, or a mix of invoice-0 and invoiced lines for the same check).
    queue_rows: list[tuple[dict, Path, str]] = []
    invoice_rows = load_invoice_export_rows(args.invoice_csv)
    required = {"Transaction Id", "Invoice Number", "Check Amount", "Mail Stop"}
    if invoice_rows:
        sample = invoice_rows[0]
        if not required.issubset(sample.keys()):
            raise SystemExit(f"Missing CSV columns. Required: {required}")

    for row in invoice_rows:
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
        if is_missing_invoice(row.get("Invoice Number", "")):
            rows_out.append(
                build_record(
                    row,
                    tif,
                    with_pages=not args.no_tif_pages,
                    merchant_payee=merchant_lookup.get(ms, ""),
                    merchant_aliases=merchant_aliases,
                )
            )
        else:
            rows_out.append(
                build_misroute_record(
                    row,
                    tif,
                    with_pages=not args.no_tif_pages,
                    merchant_payee=merchant_lookup.get(ms, ""),
                    all_merchants=all_merchants,
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
        "Scan Type",
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
        nh_no_invoice = sum(
            1
            for r in rows_out
            if r.get("Needs Human?") == "yes" and r.get("Scan Type") == "No Invoice"
        )
        nh_invoiced = sum(
            1
            for r in rows_out
            if r.get("Needs Human?") == "yes" and r.get("Scan Type") == "Invoice On File"
        )
        not_a_check = sum(1 for r in rows_out if (r.get("Reason") or "").startswith("Not a check"))
        not_a_check_note = f"; {not_a_check} not-a-check TIF(s) auto-cleared" if not_a_check else ""
        if not args.quiet:
            if not args.no_tif_pages:
                print(
                    f"Summary: {nh_no_invoice} need human review (no invoice); "
                    f"{nh_invoiced} possible misroute(s) (invoice on file); {mp} multi-page TIF(s)"
                    f"{not_a_check_note}."
                )
            else:
                print(
                    f"Summary: {nh_no_invoice} need human review (no invoice); "
                    f"{nh_invoiced} possible misroute(s) (invoice on file){not_a_check_note}. "
                    f"(TIF page count skipped; omit --no-tif-pages to read pages.)"
                )


if __name__ == "__main__":
    main()
