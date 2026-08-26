"""
Local OCR scan: compare CSV Merchant (payee) and Check Amount to text read from a .tif.

- Merchant: strong normalization; prefer match in text after PAY TO THE ORDER OF (including OCR-warped pay
  lines), across all pages before citing an incidental payee on page 1; else full document with preference for
  pages that show PAY/order context. Extra needles for common OCR swaps (first-letter U/V,
  single P/F inside long names).
  Many remittance page-1 layouts repeat the payee top-left (e.g. "Print As: DINING RD"); CamelCase CSV
  merchants like DiningRD also generate a spaced needle (Dining RD) for OCR. A later page may be the
  literal check: payee in caps after PAY TO THE ORDER OF and the numeric amount in the right-hand box
  (security lines sometimes break OCR — we recover $ amounts by keeping only digits in the dollars part).
- Amount: numeric equality if any candidate on any page matches CSV; Scan Notes list every distinct OCR
  reading (page + raw) that equals the CSV when there are multiple; also parses legal lines like
  \"Seven Hundred Eighty Dollars and 01 Cents\" when the numeric box OCR is unreliable.
  Remittance/stub pages often put the check total on a summary line (e.g. \"Net Amount\" / \"Grand total\").
- Scan Notes: English; records anchor vs global payee match; when the payee appears on several pages,
  lists all of them; amount notes enumerate multiple matching OCR readings when present.

OCR backends (first match wins):
1) Tesseract: set TESSERACT_CMD or CPI_TESSERACT to the binary path, or install so `which tesseract`
   works; also needs `pip install pytesseract`.
2) macOS only: Apple Vision (on-device) if `pip install pyobjc-framework-Vision` and no Tesseract found.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

# Loose pattern for OCR-mangled payee line: full "PAY TO THE ORDER OF", or Chase-style "TO THE ORDER OF"
# when "Pay" was split onto the legal-amount line above.
_PAY_TO_ANCHOR = re.compile(
    r"(?:P\s*A\s*Y\s*)?\s*T\s*O\s*(?:T\s*H\s*E\s*)?\s*O\s*R\s*D\s*E\s*R\s*\s*O\s*F",
    re.IGNORECASE,
)

# Vision OCR often splits the pay line across rows and misreads tokens, e.g. "Pay Ta" / "The Urder Or"
# (for Pay To / The Order Of). Used together with _PAY_TO_ANCHOR so later pages with the literal check
# still match before falling back to an incidental payee mention on page 1.
_PAY_TO_ANCHOR_SPLIT_LINE = re.compile(
    r"P\s*A\s*Y\s+T\w\s*[\s\n]{0,6}T\s*H\s*E\s+(?:O\s*R\s*D\s*E\s*R|U\s*R\s*D\s*E\s*R)\s+(?:O\s*[FR]\b|T\s*O\b)",
    re.IGNORECASE | re.DOTALL,
)

# Chase Online Bill Payment: pay line often OCRs as separate lines "To / the / Orde:" (47990635).
_CHASE_BILL_PAY_ORDER = re.compile(
    r"To\s+the\s+Ord(?:er)?\s*:?\s*(?:\n\s*of\b)?",
    re.IGNORECASE | re.DOTALL,
)

_CHASE_ONLINE_BILL_PAYMENT = re.compile(r"CHASE\s+ONLINE\s+BILL\s+PAYMENT", re.IGNORECASE)


def _pay_anchor_present(page_text: str) -> bool:
    """True if this page likely contains a check pay line (standard or OCR-warped wording)."""
    if not page_text:
        return False
    return bool(
        _PAY_TO_ANCHOR.search(page_text)
        or _PAY_TO_ANCHOR_SPLIT_LINE.search(page_text)
        or (
            _CHASE_ONLINE_BILL_PAYMENT.search(page_text)
            and _CHASE_BILL_PAY_ORDER.search(page_text)
        )
    )


def _uovo_lockbox_payee_region(snippet: str) -> bool:
    """UOVO remittance block when Vision drops the payee name (Chase bill-pay layout)."""
    n = _normalize_merchant(snippet)
    return "PO BOX 989746" in n and "WEST SACRAMENTO" in n

# Money-like tokens (avoid matching years as 2026 alone by requiring $ or .cents or comma-thousands)
_MONEY_PATTERNS = [
    re.compile(r"\*\s*(\d{1,3}(?:,\d{3})+|\d+)\s*\.\s*(\d{1,2})\b"),
    re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)\s*\.\s*(\d{1,2})\b"),
    re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)\b(?!\s*\.)"),
    re.compile(r"(?<![\d,\.])(\d{1,3}(?:,\d{3})+)\s*\.\s*(\d{1,2})\b"),
    # OCR space after comma-thousands: "2, 551.74" -> $2,551.74 (49111958).
    re.compile(r"(?<![\d,\.])(\d{1,3}(?:,\s*\d{3})+)\s*\.\s*(\d{1,2})\b"),
    re.compile(r"(?<![\d,\.])(\d{1,3}(?:,\d{3})+)\b(?!\s*\.)"),
    # spaced thousands e.g. "1 256.80"
    re.compile(r"(?<![\d,\.])(\d{1,3}(?:\s+\d{3})+)\s*\.\s*(\d{1,2})\b"),
    re.compile(r"(?<![\d,\.])(\d+)\s*\.\s*(\d{1,2})\b"),
    re.compile(r"(?<![\d,\.])(\d+)\b(?!\s*\.)"),
]

# Totals on remittance / advice pages (label and amount may be non-adjacent in OCR).
_SUMMARY_LABEL_AMOUNT_PATTERNS = [
    re.compile(
        r"(?i)\bnet\s+amount\b\s*[\s\S]{0,360}?\$?\s*(\d{1,3}(?:,\d{3})+|\d{1,3}(?:\s+\d{3})+|\d+)\s*\.\s*(\d{2})\b"
    ),
    re.compile(
        r"(?i)\bgrand\s+total\b\s*[\s\S]{0,360}?\$?\s*(\d{1,3}(?:,\d{3})+|\d{1,3}(?:\s+\d{3})+|\d+)\s*\.\s*(\d{2})\b"
    ),
]

# Literal check amount box: $ then dollars (may include commas, spaces, or security-stroke junk) then .cc
_BOXED_DOLLAR_AMOUNT = re.compile(r"\$\s*([^\$\n\r]{0,36}?)\s*\.\s*(\d{2})\b")

# US Treasury (and similar): security box reads like "$***60211*52" or OCR "5***60211*52" ($ misread as 5).
_TREASURY_STAR_BOX_AMOUNT = re.compile(
    r"(?:\$|5)\s*\*+\s*(\d{1,3}(?:,\d{3})+|\d+)\s*\*?\s*(\d{2})\b"
)

# Security box when commas read as periods: **********2,018.44* -> OCR "**********2.018.44*".
_STAR_BOX_DOT_THOUSANDS_AMOUNT = re.compile(
    r"\*{2,}\s*(\d{1,3}(?:\.\d{3})+\.\d{2})\*?"
)

# Same dot-thousands body without stars (e.g. remittance page repeats "2.018.44").
_DOT_THOUSANDS_CENTS_SPAN = re.compile(r"(?<![\d])(\d{1,3}(?:\.\d{3})+\.\d{2})(?!\d)")

_LEGAL_SUFFIX_TOKENS = frozenset(
    {
        "LLC",
        "INC",
        "CORP",
        "CORPORATION",
        "CORPORATE",
        "COMPANY",
        "CO",
        "LTD",
        "LP",
        "LLP",
        "DBA",
        "PC",
        "PLLC",
    }
)

# Trailing tokens often omitted on checks (payee line shows trade name only).
_TRAILING_DESCRIPTOR_TOKENS = frozenset(
    {
        "SOFTWARE",
        "SERVICES",
        "SOLUTIONS",
        "SYSTEMS",
        "TECHNOLOGIES",
        "TECHNOLOGY",
        "GROUP",
        "HOLDINGS",
        "INTERNATIONAL",
    }
)

# Avoid needles like "AMERICAN" alone when stripping "SOFTWARE" from a short trade name.
_MIN_TRADE_NAME_TOKEN_LEN = 10

# Cap how many duplicate amount hits we list in Scan Notes (remainder summarized).
_MAX_AMOUNT_NOTE_HITS = 12

# Built-in merchant aliases (CSV Merchant -> alternate strings that may appear on checks).
_DEFAULT_MERCHANT_ALIASES: dict[str, list[str]] = {
    "Tripleseat Software LLC": [
        "GATHER TECHNOLOGIES",
        "Gather Technologies",
        "FLOORPLANS",
        "Floorplans",
    ],
    "Brite Computers": ["BRITE", "Brite"],
    "Early Stage Solutions, LLC": ["DLA LLC", "DLA LLC."],
    "Peak Design": ["PEAK DESIGN LTD", "PEAK DESIGN LTD."],
    "Howler Brothers": ["HOWLER BROS", "Howler Bros"],
    "Rowley Hardware Inc.": ["ROWLEY COMPANY", "Rowley Company"],
    "DiningRD": [
        "HEALTH TECHNOLOGIES INC",
        "HEALTH TECHNOLOGIES",
        "HEALTH TECNOLOGIES INC",
        "NUTRITION ALLIANCE LLC",
        "Nutrition Alliance LLC",
    ],
    # Trade name on invoices / check stub; legal payee in Paystand export.
    # Checks often abbreviate "Company" as "CO."; Vision may misread BREWING as BRENNO (44268951).
    "Associated Brewing Company LLC": [
        "THE REAL AMERICAN BEER",
        "ASSOCIATED BREWING CO LLC",
        "ASSOCIATED BREWING CO. LLC",
        "ASSOCIATED BRENNO CO LLC",
        # Pay line often omits CO/LLC; Vision may drop I (46846865: ASSOCATED BREWING).
        "ASSOCIATED BREWING",
        "ASSOCATED BREWING",
        # Legal/DBA payee on checks (50657715).
        "RAHM INC",
        "Rahm Inc",
        # Brand/DBA name on checks (51300454).
        "AMERICAN REBEL BEVERAGES",
        "American Rebel Beverages",
    ],
    # Mail Stop merchant vs Wells-style pay line (44133120 / 44133121).
    "UOVO LLC": ["TY ART LLC"],
    # Vision often reads the second O in OOFOS as zero (44425239);
    # leading O as Q (50657737: QOFOS INC).
    "Oofos Inc.": [
        "O0FOS INC",
        "O0FOS INC.",
        "THE O0FOS INC",
        "THE O0FOS INC.",
        "QOFOS INC",
        "QOFOS INC.",
    ],
    # Vision may read r as I in ProctorU (44954649: ProctoIu, Inc).
    "ProctorU Inc.": ["PROCTOIU INC", "PROCTOIU, INC", "ProctoIu, Inc", "ProctoIu Inc"],
    # Citibank official checks pay PAYSTAND INC; export merchant is Paystand Corporate.
    "Paystand Corporate": ["PAYSTAND INC", "Paystand Inc"],
    # Vision may drop D in Indeed (50419163: INDEE FLEX).
    "Indeed Flex": ["INDEE FLEX"],
}


class ScanResult(NamedTuple):
    merchant_match: str
    amount_match: str
    scan_notes: str
    handwritten_review: bool


def _normalize_merchant(s: str) -> str:
    """Strong normalization for payee / merchant comparison."""
    t = unicodedata.normalize("NFKD", (s or "").strip())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.upper()
    t = re.sub(r"[^A-Z0-9\s&]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _normalize_merchant_compact(s: str) -> str:
    return _normalize_merchant(s).replace(" ", "")


def builtin_merchant_aliases() -> dict[str, list[str]]:
    return {k: list(v) for k, v in _DEFAULT_MERCHANT_ALIASES.items()}


def _merchant_core_variants(norm: str) -> list[str]:
    """Strip trailing legal suffixes, then trailing corporate descriptors (e.g. SOFTWARE)."""
    tokens = norm.split()
    variants: list[str] = []
    while tokens:
        variants.append(" ".join(tokens))
        last = tokens[-1]
        if last in _LEGAL_SUFFIX_TOKENS:
            tokens = tokens[:-1]
        elif last in _TRAILING_DESCRIPTOR_TOKENS:
            nxt = tokens[:-1]
            if len(nxt) == 1 and len(nxt[0]) < _MIN_TRADE_NAME_TOKEN_LEN:
                break
            tokens = nxt
        else:
            break
    return variants


def _camel_case_spaced(s: str) -> str:
    """e.g. DiningRD -> Dining RD for OCR that splits the payee name."""
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", (s or "").strip())
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", t)


def _merchant_pf_single_substitution_needles(norm_variant: str) -> list[tuple[str, str]]:
    """Weak scans sometimes misread P<->F inside a token (e.g. TRIPLESEAT vs TRIFLESEAT)."""
    compact_len = len(norm_variant.replace(" ", ""))
    if compact_len < 10:
        return []
    swap = {"P": "F", "F": "P"}
    out: list[tuple[str, str]] = []
    for i, ch in enumerate(norm_variant):
        alt = swap.get(ch)
        if alt is None:
            continue
        nv = norm_variant[:i] + alt + norm_variant[i + 1 :]
        nc = nv.replace(" ", "")
        out.append((nv, nc))
    return out


def _merchant_first_token_uv_alternate(norm_variant: str) -> list[tuple[str, str]]:
    """Common OCR confusion: leading U<->V or U<->L on first token (UOVO vs VOVO / LOVO)."""
    parts = norm_variant.split()
    if len(parts) < 1:
        return []
    w0 = parts[0]
    if len(w0) < 2 or not w0[1].isalpha():
        return []
    alts: list[str] = []
    if w0[0] == "U":
        alts.extend(["V" + w0[1:], "L" + w0[1:]])
    elif w0[0] == "V":
        alts.append("U" + w0[1:])
    elif w0[0] == "L":
        alts.append("U" + w0[1:])
    out: list[tuple[str, str]] = []
    for a0 in alts:
        nv = " ".join([a0] + parts[1:])
        nc = nv.replace(" ", "")
        if nv:
            out.append((nv, nc))
    return out


def _merchant_llc_llo_suffix_variants(norm_variant: str) -> list[tuple[str, str]]:
    """LLC misread as LLO on checks (50444274: LOVO LLO for UOVO LLC)."""
    parts = norm_variant.split()
    if len(parts) < 2 or parts[-1] != "LLC":
        return []
    nv = " ".join(parts[:-1] + ["LLO"])
    return [(nv, nv.replace(" ", ""))]


def merchant_match_needles(
    merchant_raw: str, aliases: dict[str, list[str]] | None
) -> list[tuple[str, str, str | None]]:
    """Unique (normalized, compact, alias_label) strings to search for in OCR.

    alias_label is the configured AlsoKnownAs text when the needle comes from merchant_aliases;
    None when derived from the export merchant name only.
    """
    raw = (merchant_raw or "").strip()
    if not raw:
        return []
    aliases = aliases or {}
    extras: list[str] = []
    for key in (raw, raw.upper(), raw.lower()):
        if key in aliases:
            extras.extend(aliases[key])
    spaced = _camel_case_spaced(raw)
    seeded: list[tuple[str, str | None]] = [(raw, None)]
    if spaced != raw:
        seeded.append((spaced, None))
    for alias in extras:
        seeded.append((alias, alias))
    dup: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str | None]] = []
    for seed, alias_label in seeded:
        n = _normalize_merchant(seed)
        for variant in _merchant_core_variants(n):
            comp = variant.replace(" ", "")
            t = (variant, comp)
            if variant and t not in dup:
                dup.add(t)
                out.append((variant, comp, alias_label))
            for nv, nc in _merchant_first_token_uv_alternate(variant):
                tt = (nv, nc)
                if tt not in dup:
                    dup.add(tt)
                    out.append((nv, nc, alias_label))
                for lnv, lnc in _merchant_llc_llo_suffix_variants(nv):
                    tt2 = (lnv, lnc)
                    if tt2 not in dup:
                        dup.add(tt2)
                        out.append((lnv, lnc, alias_label))
            for lnv, lnc in _merchant_llc_llo_suffix_variants(variant):
                tt = (lnv, lnc)
                if tt not in dup:
                    dup.add(tt)
                    out.append((lnv, lnc, alias_label))
            for nv, nc in _merchant_pf_single_substitution_needles(variant):
                tt = (nv, nc)
                if tt not in dup:
                    dup.add(tt)
                    out.append((nv, nc, alias_label))
    return out


def _merchant_found_in(norm_csv: str, norm_compact_csv: str, haystack: str) -> bool:
    h = _normalize_merchant(haystack)
    hc = h.replace(" ", "")
    if not norm_csv:
        return False
    return norm_csv in h or norm_compact_csv in hc


def _needle_match_any(
    needles: list[tuple[str, str, str | None]], haystack: str
) -> bool:
    for nv, nc, _ in needles:
        if _merchant_found_in(nv, nc, haystack):
            return True
    return False


def _needle_match_any_strict(
    needles: list[tuple[str, str, str | None]], haystack: str
) -> bool:
    """Like _needle_match_any but requires a whole word/phrase boundary match.

    Used only for the cross-merchant misroute check, where a plain substring hit is a costly
    false positive — e.g. the bare alias-core needle "DLA" (from "DLA LLC") matching inside the
    unrelated word "MIDLAND". The normal single-merchant scan keeps the looser substring match
    (a human already reviews those rows), but scanning ~60 other merchants against arbitrary OCR
    text needs the stricter check to stay trustworthy.
    """
    h = _normalize_merchant(haystack)
    for nv, _nc, _ in needles:
        if not nv:
            continue
        pattern = r"(?<![A-Z0-9])" + re.escape(nv) + r"(?![A-Z0-9])"
        if re.search(pattern, h):
            return True
    return False


def _merchant_match_alias_label(
    needles: list[tuple[str, str, str | None]], haystack: str
) -> str | None:
    """Alias label when match relies on merchant_aliases; None if export name matched directly."""
    for nv, nc, alias_label in needles:
        if alias_label is None and _merchant_found_in(nv, nc, haystack):
            return None
    for nv, nc, alias_label in needles:
        if alias_label is not None and _merchant_found_in(nv, nc, haystack):
            return alias_label
    return None


def _alias_label_for_note(alias_label: str, haystack: str) -> str:
    """Prefer the OCR spelling from haystack when reporting a synonym match."""
    norm_alias = _normalize_merchant(alias_label).replace(" ", "")
    if not norm_alias:
        return alias_label
    for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9\s&.\-]{0,80}", haystack):
        span = m.group(0).strip()
        if not span:
            continue
        if _normalize_merchant(span).replace(" ", "") == norm_alias:
            return span
        if norm_alias in _normalize_merchant(span).replace(" ", ""):
            return span
    return alias_label


def _synonym_match_note(merchant_raw: str, alias_label: str, haystack: str) -> str:
    ocr_text = _alias_label_for_note(alias_label, haystack)
    return (
        f'Payee matched via synonym: OCR "{ocr_text}" → export merchant {merchant_raw}.'
    )


def parse_csv_amount_to_decimal(raw: str) -> Decimal | None:
    t = (raw or "").strip()
    if not t:
        return None
    t = t.replace(",", "").replace("$", "").strip()
    if not re.fullmatch(r"\d+\.?\d*", t):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _decimal_from_groups(int_part: str, frac: str | None) -> Decimal | None:
    int_part = int_part.replace(",", "")
    if not int_part.isdigit():
        return None
    if frac is None or frac == "":
        return Decimal(int_part)
    if not frac.isdigit():
        return None
    return Decimal(f"{int_part}.{frac}")


def _decimal_from_dot_thousands_cents(body: str) -> Decimal | None:
    """Interpret OCR like '8.071.68' as 8071.68 (dots as thousands separator + cents)."""
    parts = body.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    if len(parts[1]) != 3 or len(parts[2]) != 2:
        return None
    try:
        dollars = int(parts[0]) * 1000 + int(parts[1])
        return Decimal(dollars) + Decimal(parts[2]) / 100
    except (ValueError, InvalidOperation):
        return None


def _ocr_fix_amount_dollars_part(dollars_raw: str) -> str:
    """Fix common Vision OCR noise in the integer part of a boxed $ amount."""
    s = (dollars_raw or "").strip()
    # Leading 1 misread as t/l before comma-thousands (48718835: $t,470.00).
    s = re.sub(r"^([tl])(?=,)", "1", s, flags=re.IGNORECASE)
    return s


def _format_usd_amount_note(d: Decimal) -> str:
    """Canonical USD display for Scan Notes (comma thousands, two decimals)."""
    q = d.quantize(Decimal("0.01"))
    neg = q < 0
    q = abs(q)
    s = format(q, "f")
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    chunks: list[str] = []
    while whole:
        chunks.append(whole[-3:])
        whole = whole[:-3]
    body = ",".join(reversed(chunks))
    return f"-${body}.{frac}" if neg else f"${body}.{frac}"


# Vision OCR often reads a leading '$' as '5' before stub totals, e.g. "$8,071.68" -> "58.071.68".
_MANGLED_BUCK_AS_FIVE = re.compile(r"\b5(\d\.\d{3}\.\d{2})\b")


def _mangled_leading_five_money_candidates(text: str) -> list[tuple[str, Decimal]]:
    """Hits when '$' was misread as '5'; notes use corrected USD text, not the bad OCR span."""
    found: list[tuple[str, Decimal]] = []
    for m in _MANGLED_BUCK_AS_FIVE.finditer(text):
        d = _decimal_from_dot_thousands_cents(m.group(1))
        if d is None or d < Decimal("100") or d >= Decimal("100000000"):
            continue
        found.append((_format_usd_amount_note(d), d))
    return found


# US check legal lines: "Seven Hundred Eighty Dollars and 01 Cents" (also … and 01/100 Cents).
_WRITTEN_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    # Common OCR misreads of number words (nt->nl, etc.; 51129157: "Twenly" for Twenty).
    "twenly": 20,
    "thirly": 30,
    "fourly": 40,
    "fifly": 50,
    "sevenly": 70,
    "eighly": 80,
    "ninely": 90,
}


def _parse_written_integer_dollars(phrase: str) -> int | None:
    """Parse dollar amount spelled out (no 'Dollars' / cents clause). Typical checks: under 1M."""
    words = [w for w in re.findall(r"[a-zA-Z]+", phrase.lower()) if w]
    if not words:
        return None
    total = 0
    current = 0
    for w in words:
        if w == "hundred":
            if current == 0:
                current = 1
            current *= 100
        elif w == "thousand":
            total += current * 1000
            current = 0
        elif w == "million":
            total += current * 1_000_000
            current = 0
        elif w in _WRITTEN_NUMBER_WORDS:
            current += _WRITTEN_NUMBER_WORDS[w]
        else:
            return None
    n = total + current
    return n if 0 <= n < 10_000_000 else None


_WRITTEN_AMOUNT_FIRST_WORD = (
    r"(?:Zero|One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve|Thirteen|"
    r"Fourteen|Fifteen|Sixteen|Seventeen|Eighteen|Nineteen|Twenty|Thirty|Forty|Fifty|"
    r"Sixty|Seventy|Eighty|Ninety|"
    r"Twenly|Thirly|Fourly|Fifly|Sevenly|Eighly|Ninely)"
)


def _written_dollars_and_cents_candidates(text: str) -> list[tuple[str, Decimal]]:
    """
    Legal tender line OCR often survives when the numeric box is garbled (e.g. security pattern).
    Matches: \"… Dollars and 01 Cents\" or \"… and 01/100 Cents\".
    Must start with a spelled number (avoids matching \"Pay\" + newline + …).
    """
    out: list[tuple[str, Decimal]] = []
    pat = (
        rf"(?is)\b({_WRITTEN_AMOUNT_FIRST_WORD}[A-Za-z\s\-]{{0,160}}?)\s+"
        r"Dollars\s+and\s+(\d{2})(?:\s*/\s*100)?\s+Cents\b"
    )
    for m in re.finditer(pat, text):
        dollars_int = _parse_written_integer_dollars(m.group(1))
        if dollars_int is None:
            continue
        try:
            d = Decimal(f"{dollars_int}.{m.group(2)}")
        except InvalidOperation:
            continue
        if d <= 0 or d >= Decimal("100000000"):
            continue
        raw = m.group(0).strip()
        if len(raw) > 160:
            raw = raw[:157] + "..."
        out.append((raw, d))
    # Citibank official check: cents OCR as "ANDOOCENTS" after Dollars (48718835).
    pat_mangled_oo_cents = (
        rf"(?is)\b({_WRITTEN_AMOUNT_FIRST_WORD}[A-Za-z\s\-]{{0,160}}?)\s+"
        r"Dollars\s+AND\s*O{2,}CENTS?\*?"
    )
    for m in re.finditer(pat_mangled_oo_cents, text):
        dollars_int = _parse_written_integer_dollars(m.group(1))
        if dollars_int is None:
            continue
        try:
            d = Decimal(f"{dollars_int}.00")
        except InvalidOperation:
            continue
        if d <= 0 or d >= Decimal("100000000"):
            continue
        raw = m.group(0).strip()
        if len(raw) > 160:
            raw = raw[:157] + "..."
        out.append((raw, d))
    # Cents also spelled out: "… Dollars And Eighty One Cents" (51129157).
    pat_written_cents = (
        rf"(?is)\b({_WRITTEN_AMOUNT_FIRST_WORD}[A-Za-z\s\-]{{0,160}}?)\s+"
        r"Dollars\s+and\s+([A-Za-z][A-Za-z\s\-]{0,40}?)\s+Cents\b"
    )
    for m in re.finditer(pat_written_cents, text):
        dollars_int = _parse_written_integer_dollars(m.group(1))
        cents_int = _parse_written_integer_dollars(m.group(2))
        if dollars_int is None or cents_int is None or not (0 <= cents_int < 100):
            continue
        try:
            d = Decimal(f"{dollars_int}.{cents_int:02d}")
        except InvalidOperation:
            continue
        if d <= 0 or d >= Decimal("100000000"):
            continue
        raw = m.group(0).strip()
        if len(raw) > 160:
            raw = raw[:157] + "..."
        out.append((raw, d))
    # Chase-style: "PAY *TWO THOUSAND EIGHTEEN AND 44 / 100" (no Dollars/Cents words).
    pat_frac = (
        rf"(?is)(?:PAY\s*\*?\s*)?({_WRITTEN_AMOUNT_FIRST_WORD}[A-Za-z\s\-]{{0,160}}?)\s+"
        r"and\s+(\d{2})\s*/\s*100\b"
    )
    for m in re.finditer(pat_frac, text):
        dollars_int = _parse_written_integer_dollars(m.group(1))
        if dollars_int is None:
            continue
        try:
            d = Decimal(f"{dollars_int}.{m.group(2)}")
        except InvalidOperation:
            continue
        if d <= 0 or d >= Decimal("100000000"):
            continue
        raw = m.group(0).strip()
        if len(raw) > 160:
            raw = raw[:157] + "..."
        out.append((raw, d))
    return out


def _dot_thousands_cents_amount_candidates(text: str) -> list[tuple[str, Decimal]]:
    """OCR uses periods as thousands separators: 2.018.44 -> $2,018.44."""
    found: list[tuple[str, Decimal]] = []
    for m in _DOT_THOUSANDS_CENTS_SPAN.finditer(text):
        body = m.group(1)
        d = _decimal_from_dot_thousands_cents(body)
        if d is None or d <= 0 or d >= Decimal("100000000"):
            continue
        found.append((body, d))
    for m in _STAR_BOX_DOT_THOUSANDS_AMOUNT.finditer(text):
        body = m.group(1)
        d = _decimal_from_dot_thousands_cents(body)
        if d is None or d <= 0 or d >= Decimal("100000000"):
            continue
        note = _format_usd_amount_note(d)
        found.append((note, d))
    return found


def extract_amount_candidates(text: str) -> list[tuple[str, Decimal]]:
    """Return (raw_span, value) for plausible monetary amounts in OCR text.

    Same dollar value may appear more than once per page (e.g. boxed \"$…\" and a second regex span).
    Stub totals where OCR misreads a leading ``$`` as ``5`` still match; Scan Notes show normalized ``$…`` text.
    Treasury-style amount boxes with asterisks (``\$***60211*52`` or OCR ``5***60211*52``) are parsed.
    """
    out: list[tuple[str, Decimal]] = []
    seen_raw_val: set[tuple[str, str]] = set()

    def add(raw_in: str, d: Decimal, max_raw: int = 140) -> None:
        raw = (raw_in or "").strip()
        if len(raw) > max_raw:
            raw = raw[: max_raw - 3] + "..."
        key = (raw, str(d))
        if key in seen_raw_val:
            return
        seen_raw_val.add(key)
        out.append((raw, d))

    for pat in _SUMMARY_LABEL_AMOUNT_PATTERNS:
        for m in pat.finditer(text):
            raw = (m.group(0) or "").strip()
            try:
                int_part = m.group(1).replace(",", "").replace(" ", "")
                d = _decimal_from_groups(int_part, m.group(2))
            except Exception:
                continue
            if d is None or d <= 0 or d >= Decimal("100000000"):
                continue
            add(raw, d)

    for raw, d in _written_dollars_and_cents_candidates(text):
        add(raw, d, max_raw=160)

    for m in _BOXED_DOLLAR_AMOUNT.finditer(text):
        dollars_raw = _ocr_fix_amount_dollars_part(m.group(1) or "")
        whole_digits = re.sub(r"\D", "", dollars_raw)
        if not whole_digits or len(whole_digits) > 12:
            continue
        try:
            d = _decimal_from_groups(whole_digits, m.group(2))
        except Exception:
            continue
        if d is None or d <= 0 or d >= Decimal("100000000"):
            continue
        raw = (m.group(0) or "").strip()
        add(raw, d, max_raw=120)

    for m in _TREASURY_STAR_BOX_AMOUNT.finditer(text):
        try:
            int_part = m.group(1).replace(",", "").replace(" ", "")
            d = _decimal_from_groups(int_part, m.group(2))
        except Exception:
            continue
        if d is None or d <= 0 or d >= Decimal("100000000"):
            continue
        raw = (m.group(0) or "").strip()
        add(raw, d, max_raw=80)

    for raw, d in _dot_thousands_cents_amount_candidates(text):
        add(raw, d, max_raw=80)

    for pat in _MONEY_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(0).strip()
            g = m.groups()
            try:
                if len(g) == 2 and g[1] is not None and str(g[1]).strip() != "":
                    int_part = g[0].replace(",", "").replace(" ", "")
                    d = _decimal_from_groups(int_part, g[1])
                else:
                    int_part = (g[0] or "").replace(",", "").replace(" ", "")
                    d = _decimal_from_groups(int_part, None)
            except Exception:
                continue
            if d is None or d <= 0 or d >= Decimal("100000000"):
                continue
            add(raw, d)

    for raw, d in _mangled_leading_five_money_candidates(text):
        add(raw, d)
    return out


def resolve_tesseract_binary() -> str | None:
    """Tesseract executable path, or None."""
    for key in ("TESSERACT_CMD", "CPI_TESSERACT"):
        p = (os.environ.get(key) or "").strip()
        if p and Path(p).is_file():
            return p
    w = shutil.which("tesseract")
    if w and Path(w).is_file():
        return w
    for p in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if Path(p).is_file():
            return p
    return None


def _macos_vision_importable() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Foundation  # noqa: F401
        import Quartz  # noqa: F401
        import Vision  # noqa: F401

        return True
    except Exception:
        return False


def _select_ocr_backend() -> tuple[str, str]:
    """
    Returns (backend, hint_or_path).
    backend: 'tesseract' | 'vision' | 'none'
    For tesseract, hint_or_path is the binary path to assign to pytesseract.
    """
    tpath = resolve_tesseract_binary()
    if tpath:
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            return "none", "pytesseract is not installed (python3 -m pip install pytesseract)."
        return "tesseract", tpath
    if _macos_vision_importable():
        return "vision", ""
    msg = (
        "No OCR engine available. Options: (1) Install Tesseract and pytesseract, or set "
        "TESSERACT_CMD to the tesseract binary; (2) On macOS: python3 -m pip install pyobjc-framework-Vision "
        "for on-device Apple Vision OCR."
    )
    return "none", msg


def _ocr_tif_pages_tesseract(tif_path: Path, tesseract_bin: str) -> tuple[list[tuple[int, str, float | None]], str]:
    import pytesseract
    from PIL import Image

    pytesseract.pytesseract.tesseract_cmd = tesseract_bin
    pages: list[tuple[int, str, float | None]] = []
    try:
        with Image.open(tif_path) as im:
            n = int(getattr(im, "n_frames", 1))
            for i in range(n):
                im.seek(i)
                gray = im.convert("L")
                txt = pytesseract.image_to_string(gray, lang="eng") or ""
                pages.append((i + 1, txt, None))
    except Exception as e:
        return [], f"Could not OCR TIF (Tesseract): {e}"
    return pages, ""


def _vision_ocr_pil_rgb(pil_rgb) -> tuple[str, float | None]:
    """
    Apple Vision text recognition on a Pillow RGB image.
    Returns (newline-joined strings, mean confidence 0..1 or None).
    """
    import Foundation
    import Quartz
    import Vision

    buf = io.BytesIO()
    pil_rgb.save(buf, format="PNG")
    raw = buf.getvalue()
    nsdata = Foundation.NSData.dataWithBytes_length_(raw, len(raw))
    src = Quartz.CGImageSourceCreateWithData(nsdata, None)
    if not src:
        return "", None
    cg_img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if not cg_img:
        return "", None
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_img, None)
    lines: list[str] = []
    confidences: list[float] = []

    def completion(request, error) -> None:
        if error is not None:
            return
        observations = request.results() or []
        for observation in observations:
            try:
                candidates = observation.topCandidates_(1)
                if not candidates or len(candidates) < 1:
                    continue
                rt = candidates[0]
                s = rt.string()
                if s:
                    lines.append(s)
                    try:
                        confidences.append(float(rt.confidence()))
                    except Exception:
                        confidences.append(0.5)
            except Exception:
                continue

    req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(completion)
    try:
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    except Exception:
        pass
    try:
        req.setUsesLanguageCorrection_(True)
    except Exception:
        pass
    ok, _err = handler.performRequests_error_([req], None)
    if not ok:
        return "", None
    text = "\n".join(lines)
    avg_conf = sum(confidences) / len(confidences) if confidences else None
    return text, avg_conf


def _ocr_tif_pages_vision(tif_path: Path) -> tuple[list[tuple[int, str, float | None]], str]:
    from PIL import Image

    pages: list[tuple[int, str, float | None]] = []
    try:
        with Image.open(tif_path) as im:
            n = int(getattr(im, "n_frames", 1))
            for i in range(n):
                im.seek(i)
                rgb = im.convert("RGB")
                txt, pconf = _vision_ocr_pil_rgb(rgb)
                pages.append((i + 1, txt or "", pconf))
    except Exception as e:
        return [], f"Could not OCR TIF (Apple Vision): {e}"
    return pages, ""


@lru_cache(maxsize=None)
def ocr_tif_pages(tif_path: Path) -> tuple[list[tuple[int, str, float | None]], str, str]:
    """
    Rasterize each frame of the TIF and OCR to plain text.
    Returns (list of (1-based page index, text, mean_confidence_or_None), error_message or "", engine_label).
    engine_label: 'tesseract' | 'vision' | ''

    Cached per TIF path for the life of the process: a single check can appear on multiple
    invoice-line rows (multi-invoice checks, or a mix of invoice-0 and invoiced lines), and each
    is OCR'd independently — caching avoids re-running the (slow) OCR pass on the same image.
    """
    backend, hint = _select_ocr_backend()
    if backend == "none":
        return [], hint, ""
    if backend == "tesseract":
        pages, err = _ocr_tif_pages_tesseract(tif_path, hint)
        return pages, err, "tesseract"
    pages, err = _ocr_tif_pages_vision(tif_path)
    return pages, err, "vision"


def _dedupe_amount_hits(
    hits: list[tuple[int, str, Decimal]],
) -> list[tuple[int, str, Decimal]]:
    """Preserve order; drop duplicate (page, raw string, value) tuples."""
    seen: set[tuple[int, str, str]] = set()
    out: list[tuple[int, str, Decimal]] = []
    for pnum, raw, val in hits:
        key = (pnum, raw, str(val))
        if key in seen:
            continue
        seen.add(key)
        out.append((pnum, raw, val))
    return out


def _anchor_regions(page_text: str) -> list[tuple[int, int, str]]:
    """Spans after PAY TO... on one page: (start, end, snippet). Scans every anchor pattern."""
    regions: list[tuple[int, int, str]] = []
    seen_end: set[int] = set()
    anchor_patterns = (_PAY_TO_ANCHOR, _PAY_TO_ANCHOR_SPLIT_LINE)
    if _CHASE_ONLINE_BILL_PAYMENT.search(page_text):
        anchor_patterns = anchor_patterns + (_CHASE_BILL_PAY_ORDER,)
    for rx in anchor_patterns:
        for m in rx.finditer(page_text):
            start = m.end()
            if start in seen_end:
                continue
            seen_end.add(start)
            end = min(len(page_text), start + 800)
            snippet = page_text[start:end]
            regions.append((start, end, snippet))
    return regions


def _match_merchant_against_pages(
    page_texts: list[tuple[int, str, float | None]],
    full_doc: str,
    merchant_raw: str,
    aliases_use: dict[str, list[str]],
) -> tuple[str, list[str], str, int | None]:
    """
    Core payee-matching logic shared by the normal scan and the cross-merchant misroute check.
    Returns (merchant_match 'skipped'/'yes'/'no', notes, match_haystack, matched_page).
    """
    notes: list[str] = []
    if not merchant_raw:
        notes.append("Merchant missing from export; payee match not evaluated.")
        return "skipped", notes, "", None

    needles = merchant_match_needles(merchant_raw, aliases_use)
    merchant_match = "no"
    match_haystack = ""
    matched_page: int | None = None

    matched_anchor = False
    anchor_detail = ""
    for pnum, txt, _ in page_texts:
        for _s, _e, snippet in _anchor_regions(txt):
            if _needle_match_any(needles, snippet):
                matched_anchor = True
                match_haystack = snippet
                matched_page = pnum
                anchor_detail = (
                    f"Payee matched via PAY TO THE ORDER OF anchor region on page {pnum} "
                    f"(all pages scanned)."
                )
                break
        if matched_anchor:
            break
    if matched_anchor:
        merchant_match = "yes"
        notes.append(anchor_detail)
        alias_label = _merchant_match_alias_label(needles, match_haystack)
        if alias_label:
            notes.append(_synonym_match_note(merchant_raw, alias_label, match_haystack))
    elif _needle_match_any(needles, full_doc):
        merchant_match = "yes"
        match_haystack = full_doc
        anchor_hit_pages = [
            pnum
            for pnum, txt, _ in page_texts
            if _needle_match_any(needles, txt) and _pay_anchor_present(txt)
        ]
        if anchor_hit_pages:
            cite_page = anchor_hit_pages[-1]
            match_haystack = next(
                txt for pnum, txt, _ in page_texts if pnum == cite_page
            )
            matched_page = cite_page
            notes.append(
                f"Payee matched on page {cite_page} where OCR shows PAY/order context "
                f"(preferred over earlier pages that only mention the payee)."
            )
        else:
            cite_page = 0
            for pnum, txt, _ in page_texts:
                if _needle_match_any(needles, txt):
                    cite_page = pnum
                    match_haystack = txt
                    break
            matched_page = cite_page
            notes.append(
                f"Payee matched via full-document search (not in PAY TO anchor area) on page {cite_page}."
            )
        alias_label = _merchant_match_alias_label(needles, match_haystack)
        if alias_label:
            notes.append(_synonym_match_note(merchant_raw, alias_label, match_haystack))
    elif _normalize_merchant(merchant_raw).startswith("UOVO"):
        uovo_lockbox_page = 0
        for pnum, txt, _ in page_texts:
            if not _CHASE_ONLINE_BILL_PAYMENT.search(txt):
                continue
            for _s, _e, snippet in _anchor_regions(txt):
                if _uovo_lockbox_payee_region(snippet):
                    uovo_lockbox_page = pnum
                    break
            if uovo_lockbox_page:
                break
        if uovo_lockbox_page:
            merchant_match = "yes"
            matched_page = uovo_lockbox_page
            notes.append(
                f"Payee matched via Chase bill-pay lockbox address on page {uovo_lockbox_page} "
                f"(UOVO LLC absent from OCR but payee block with PO BOX 989746 / WEST SACRAMENTO present)."
            )
        else:
            notes.append("Payee not found in OCR after normalization (anchor and full document).")
    else:
        notes.append("Payee not found in OCR after normalization (anchor and full document).")

    if merchant_match == "yes":
        payee_pages = sorted(
            {pnum for pnum, txt, _ in page_texts if _needle_match_any(needles, txt)}
        )
        if len(payee_pages) > 1:
            notes.append(
                "Payee/export merchant wording matches on OCR page(s): "
                + ", ".join(str(p) for p in payee_pages)
                + "."
            )

    return merchant_match, notes, match_haystack, matched_page


def find_cross_merchant_match(
    page_texts: list[tuple[int, str, float | None]],
    full_doc: str,
    exclude_merchant: str,
    all_merchants: list[str],
    aliases: dict[str, list[str]],
) -> tuple[str, int] | None:
    """
    Look for a DIFFERENT known merchant's name/alias in the OCR text (used to flag checks that
    were possibly routed to the wrong merchant despite already having an invoice number on file).
    PAY TO anchor regions are checked first (highest confidence — it's literally the pay line),
    then the full document. Returns (other_merchant_name, page_num) on the first confident hit,
    or None if no other known merchant is recognized (ambiguous text is intentionally ignored —
    it's more likely an unrecognized alias/DBA than proof of a misroute).
    """
    exclude_norm = _normalize_merchant(exclude_merchant)
    candidates = [
        m for m in all_merchants if m.strip() and _normalize_merchant(m) != exclude_norm
    ]
    for pnum, txt, _ in page_texts:
        for _s, _e, snippet in _anchor_regions(txt):
            for m in candidates:
                if _needle_match_any_strict(merchant_match_needles(m, aliases), snippet):
                    return m, pnum
    for m in candidates:
        needles = merchant_match_needles(m, aliases)
        if _needle_match_any_strict(needles, full_doc):
            for pnum, txt, _ in page_texts:
                if _needle_match_any_strict(needles, txt):
                    return m, pnum
    return None


class MisrouteResult(NamedTuple):
    status: str  # 'yes' | 'no' (confirmed different known merchant) | 'unrecognized' | 'no_legible' | 'skipped'
    matched_other_merchant: str | None
    scan_notes: str
    handwritten_review: bool


def analyze_tif_for_misroute(
    tif_path: Path,
    *,
    merchant_csv: str,
    all_merchants: list[str],
    merchant_aliases: dict[str, list[str]] | None = None,
) -> MisrouteResult:
    """
    For invoice lines that already have an invoice number on file. Only checks whether the OCR
    payee clearly points to a DIFFERENT, already-known merchant (a likely misroute); amount is
    not evaluated here — the invoice number is assumed to already validate amount/application.

    Ambiguous cases (illegible scan, or payee text that matches neither the expected merchant nor
    any other known merchant — most likely just an alias/DBA we haven't catalogued yet) are
    intentionally NOT flagged, to avoid burying real misroutes in noise.
    """
    merchant_raw = (merchant_csv or "").strip()
    aliases_use = (
        merchant_aliases if merchant_aliases is not None else builtin_merchant_aliases()
    )

    if not merchant_raw:
        return MisrouteResult(
            "skipped", None, "Merchant missing from export; misroute check not evaluated.", False
        )
    if not tif_path.is_file():
        return MisrouteResult("skipped", None, "TIF file missing; misroute check not run.", False)

    page_texts, ocr_err, ocr_engine = ocr_tif_pages(tif_path)
    if ocr_err:
        return MisrouteResult("no_legible", None, ocr_err, False)

    page_confs = [c for *_, c in page_texts if c is not None]
    doc_min_conf = min(page_confs) if page_confs else None
    full_doc = "\n".join(t for _, t, _ in page_texts)
    if not full_doc.strip():
        return MisrouteResult(
            "no_legible",
            None,
            "OCR returned empty text for all pages (image quality or engine config).",
            False,
        )

    merchant_match, notes, _match_haystack, _matched_page = _match_merchant_against_pages(
        page_texts, full_doc, merchant_raw, aliases_use
    )

    handwritten_review = bool(
        ocr_engine == "vision" and doc_min_conf is not None and doc_min_conf < 0.58
    )
    if handwritten_review:
        notes.append(
            "Low average OCR confidence on at least one page; likely handwritten or poor print."
        )

    if merchant_match == "yes":
        return MisrouteResult("yes", None, " ".join(notes), handwritten_review)

    other = find_cross_merchant_match(page_texts, full_doc, merchant_raw, all_merchants, aliases_use)
    if other:
        other_name, other_page = other
        notes.append(
            f'Invoice on file, but OCR payee on page {other_page} matches a different known '
            f'merchant: "{other_name}" (expected "{merchant_raw}"). Possible misroute.'
        )
        return MisrouteResult("no", other_name, " ".join(notes), handwritten_review)

    notes.append(
        "Payee not confidently identified: OCR text matches neither the expected merchant nor "
        "any other known merchant (may be an unrecognized alias/DBA). Not flagged."
    )
    return MisrouteResult("unrecognized", None, " ".join(notes), handwritten_review)


# Conservative, curated signals that a TIF holds no real bank-check image at all — a mailed
# notice, legal filing, envelope, internal policy, or vendor letter that ended up in the lockbox
# imaging stream instead of (or alongside) an actual check. Only used together with "no PAY TO
# THE ORDER OF anchor on any page" so a genuinely poor-quality check scan is never misclassified.
_NON_CHECK_DOCUMENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"ADDRESS\s+CORRECTION\s+REQUESTED", re.IGNORECASE),
        "address-correction / bankruptcy court notice",
    ),
    (
        re.compile(
            r"\bCase\s+\d+:\d+-[a-z]{2}-\d+\b|\bDoc\s+\d+\s+Filed\b|"
            r"\bDocument\s+\d+\s+Filed\b|\bFiled\s+in\s+[A-Z]{2,5}\s+on\b",
            re.IGNORECASE,
        ),
        "court filing",
    ),
    (
        re.compile(r"\bNON-NEGOTIABLE\b|\bEFT\s+COPY\b", re.IGNORECASE),
        "EFT/wire transfer confirmation (non-negotiable), not a physical check",
    ),
    (
        re.compile(
            r"\bUS\s+POSTAGE\b|\bFIRST-CLASS\s+MAIL\b|\bPITNEY\s+BOWES\b|\bQUADIENT\b",
            re.IGNORECASE,
        ),
        "envelope scan only, no check content",
    ),
    (
        re.compile(r"\bDear\s+Vendor\b|\bDear\s+Employer\b", re.IGNORECASE),
        "vendor/employer notice letter",
    ),
    (
        re.compile(r"removed\s+from\s+our\s+payment\s+process", re.IGNORECASE),
        "invoice dispute letter",
    ),
    (
        re.compile(r"\bELDER\s+JUSTICE\s+ACT\b", re.IGNORECASE),
        "internal policy document",
    ),
    (
        re.compile(
            r"Occupational\s+Employment\s+and\s+Wage\s+Statistics|"
            r"Department\s+of\s+Job\s+&\s+Family\s+Services",
            re.IGNORECASE,
        ),
        "government survey letter",
    ),
    (
        re.compile(r"\bAvidXchange\b|\bAvidPay\s+Network\b", re.IGNORECASE),
        "AP vendor/payment-provider change notice letter (AvidXchange)",
    ),
    (
        re.compile(r"Notice\s+of\s+Outstanding\s+Check", re.IGNORECASE),
        "outstanding-check notice letter",
    ),
    (
        re.compile(r"\bunclaimed\s+property\b|\bescheatment\b", re.IGNORECASE),
        "unclaimed-property / escheatment notice letter",
    ),
]


def detect_non_check_document(tif_path: Path) -> str | None:
    """
    Best-effort, conservative check for "this TIF contains no real bank check" — used only when
    the CSV export is missing a check amount, to tell "not a check" apart from "check amount
    just wasn't captured". Returns a short reason string when confident, else None (never guess;
    an unmatched/ambiguous document should still fall through to normal human review).
    """
    if not tif_path.is_file():
        return None
    page_texts, ocr_err, _ = ocr_tif_pages(tif_path)
    if ocr_err:
        return None
    if any(_pay_anchor_present(txt) for _, txt, _ in page_texts):
        return None
    full_doc = "\n".join(t for _, t, _ in page_texts)
    if not full_doc.strip():
        return None
    for pattern, label in _NON_CHECK_DOCUMENT_PATTERNS:
        if pattern.search(full_doc):
            return label
    return None


def analyze_tif_against_csv(
    tif_path: Path,
    *,
    merchant_csv: str,
    check_amount_csv: str,
    merchant_aliases: dict[str, list[str]] | None = None,
) -> ScanResult:
    """
    Merchant: normalization + legal-suffix variants + optional aliases; PAY TO region first.
    Amount: numeric match if any candidate on any page equals CSV amount.
    """
    notes: list[str] = []
    merchant_raw = (merchant_csv or "").strip()
    amt_dec = parse_csv_amount_to_decimal(check_amount_csv)
    aliases_use = (
        merchant_aliases if merchant_aliases is not None else builtin_merchant_aliases()
    )

    if not tif_path.is_file():
        return ScanResult("skipped", "skipped", "TIF file missing; scan not run.", False)

    page_texts, ocr_err, ocr_engine = ocr_tif_pages(tif_path)
    if ocr_err:
        return ScanResult("no_legible", "no_legible", ocr_err, False)

    page_confs = [c for *_, c in page_texts if c is not None]
    doc_min_conf = min(page_confs) if page_confs else None

    full_doc = "\n".join(t for _, t, _ in page_texts)
    if not full_doc.strip():
        return ScanResult(
            "no_legible",
            "no_legible",
            "OCR returned empty text for all pages (image quality or engine config).",
            False,
        )

    # --- PAY TO anchor presence (per page; all pages scanned — literal check is often not page 1) ---
    anchor_pages: list[int] = []
    for pnum, txt, _ in page_texts:
        if _pay_anchor_present(txt):
            anchor_pages.append(pnum)
    if anchor_pages:
        pages_s = ", ".join(str(p) for p in anchor_pages)
        notes.append(f"PAY TO THE ORDER OF anchor detected on page(s): {pages_s}.")
    else:
        notes.append("No PAY TO THE ORDER OF anchor detected in OCR (all pages).")

    # --- Merchant ---
    merchant_match, merchant_notes, _match_haystack, _matched_page = _match_merchant_against_pages(
        page_texts, full_doc, merchant_raw, aliases_use
    )
    notes.extend(merchant_notes)

    # --- Amount (always evaluated when CSV has amount, across all pages) ---
    amount_match = "skipped"
    if amt_dec is None:
        notes.append("Check amount missing from export; amount match not evaluated.")
    else:
        found: list[tuple[int, str, Decimal]] = []
        for pnum, txt, _ in page_texts:
            for raw, val in extract_amount_candidates(txt):
                if val == amt_dec:
                    found.append((pnum, raw, val))
        if found:
            amount_match = "yes"
            found_u = _dedupe_amount_hits(found)
            if len(found_u) == 1:
                pnum, raw, val = found_u[0]
                notes.append(
                    f"Amount match: CSV {amt_dec} equals OCR value {val} (raw \"{raw}\" on page {pnum})."
                )
            else:
                parts: list[str] = []
                for pnum, raw, val in found_u[:_MAX_AMOUNT_NOTE_HITS]:
                    parts.append(f'page {pnum}: "{raw}"')
                more = len(found_u) - _MAX_AMOUNT_NOTE_HITS
                suffix = f"; and {more} more occurrence(s)" if more > 0 else ""
                notes.append(
                    f"Amount match: CSV {amt_dec} equals OCR value {amt_dec} in {len(found_u)} "
                    f'OCR reading(s): {"; ".join(parts)}{suffix}.'
                )
        else:
            amount_match = "no"
            notes.append(f"No OCR amount equal to CSV {amt_dec} on any page.")

    handwritten_review = False
    hw_reasons: list[str] = []
    both_matched = merchant_match == "yes" and amount_match == "yes"
    hw_tail = "" if both_matched else " - human review."

    if ocr_engine == "vision" and doc_min_conf is not None and doc_min_conf < 0.58:
        handwritten_review = True
        hw_reasons.append(
            "Low average OCR confidence on at least one page; likely handwritten or poor print"
            f"{hw_tail}"
        )
    if merchant_match == "yes" and amount_match == "no":
        handwritten_review = True
        hw_reasons.append(
            "Payee matched but amount did not; amount may be handwritten or unclear"
            f"{hw_tail}"
        )
    if (
        merchant_match == "no"
        and amount_match == "no"
        and merchant_raw
        and amt_dec is not None
        and ocr_engine == "vision"
        and doc_min_conf is not None
        and doc_min_conf < 0.72
    ):
        handwritten_review = True
        hw_reasons.append(
            "Neither payee nor amount matched and OCR confidence is moderate/low - "
            f"likely handwritten or unclear scan{hw_tail}"
        )
    if handwritten_review and hw_reasons:
        notes.extend(hw_reasons)

    return ScanResult(merchant_match, amount_match, " ".join(notes), handwritten_review)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Debug OCR + match for one .tif")
    ap.add_argument("tif", type=Path)
    ap.add_argument("--merchant", default="")
    ap.add_argument("--amount", default="")
    args = ap.parse_args()
    r = analyze_tif_against_csv(args.tif, merchant_csv=args.merchant, check_amount_csv=args.amount)
    print(r)
