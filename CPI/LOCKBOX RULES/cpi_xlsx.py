"""Shared Excel formatting for CPI reports."""
from __future__ import annotations

from pathlib import Path

from openpyxl.formatting.rule import Rule
from openpyxl.styles import Border, Font, PatternFill
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.utils import get_column_letter
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

# Google Sheets-style fills for Needs Human? (text contains "no" / does not).
_NEEDS_HUMAN_YES_FILL = PatternFill(bgColor="F4C7C3", fill_type="solid")
_NEEDS_HUMAN_NO_FILL = PatternFill(bgColor="B7E1CD", fill_type="solid")


def apply_needs_human_conditional_formatting(ws: Worksheet) -> None:
    """Green if Needs Human? contains 'no'; pink if it does not."""
    headers = [c.value for c in ws[1]]
    if "Needs Human?" not in headers:
        return
    col = get_column_letter(headers.index("Needs Human?") + 1)
    last = max(int(ws.max_row or 1), 2)
    rng = f"{col}2:{col}{last}"
    cell = f"{col}2"
    green = Rule(
        type="containsText",
        operator="containsText",
        text="no",
        dxf=DifferentialStyle(fill=_NEEDS_HUMAN_NO_FILL),
    )
    green.formula = [f'NOT(ISERROR(SEARCH("no",{cell})))']
    pink = Rule(
        type="notContainsText",
        operator="notContains",
        text="no",
        dxf=DifferentialStyle(fill=_NEEDS_HUMAN_YES_FILL),
    )
    pink.formula = [f'ISERROR(SEARCH("no",{cell}))']
    ws.conditional_formatting.add(rng, green)
    ws.conditional_formatting.add(rng, pink)

REPORT_FONT = Font(name="Arial", size=10)


def apply_arial_10(ws: Worksheet) -> None:
    link_font = Font(name="Arial", size=10, color="0563C1", underline="single")
    for row in ws.iter_rows():
        for cell in row:
            cell.font = link_font if cell.hyperlink else REPORT_FONT


def apply_no_cell_borders(ws: Worksheet) -> None:
    none = Border()
    for row in ws.iter_rows():
        for cell in row:
            cell.border = none


def save_workbook(
    wb: Workbook,
    path,
    *,
    show_grid_lines: bool = True,
    cell_borders: bool = True,
) -> None:
    for ws in wb.worksheets:
        apply_arial_10(ws)
        if not cell_borders:
            apply_no_cell_borders(ws)
        ws.sheet_view.showGridLines = show_grid_lines
    wb.save(path)


def write_dict_rows_xlsx(
    path: Path,
    fieldnames: list[str],
    rows: list[dict],
    *,
    sheet_title: str = "Sheet1",
) -> None:
    try:
        import openpyxl
    except ImportError as e:
        raise SystemExit(
            "openpyxl required for .xlsx output: pip install openpyxl"
        ) from e

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for c, col in enumerate(fieldnames, start=1):
        ws.cell(1, c, col)
    for r, row in enumerate(rows, start=2):
        for c, col in enumerate(fieldnames, start=1):
            val = row.get(col, "")
            if col == "Transaction ID" and val != "":
                try:
                    val = int(str(val).strip())
                except ValueError:
                    pass
            elif col in ("Check Amount",) and val != "":
                try:
                    val = float(str(val).replace(",", ""))
                except ValueError:
                    pass
            ws.cell(r, c, val if val != "" else None)
    apply_needs_human_conditional_formatting(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_workbook(wb, path)
