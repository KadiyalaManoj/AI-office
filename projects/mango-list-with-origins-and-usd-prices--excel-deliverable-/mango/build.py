import argparse
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from mango.validate import ValidationError, load_rows

ROOT = Path(__file__).resolve().parent.parent
HEADERS = ["Variety", "Origin country", "Origin region", "Price (USD/kg)",
           "Price basis", "Source / date checked"]
PRICE_FORMAT = "$#,##0.00"
MAX_WIDTH = 60


def build_notes(rows, notes):
    for ln in notes:
        if "TBC" in ln.upper() or "TODO" in ln.upper():
            raise ValidationError(f"notes contain unfinished text: {ln}")
    out = ["Prices are indicative only and vary by season, market and retailer.",
           f"Date checked: latest {max(r.date_checked for r in rows).isoformat()} "
           "(per-row dates in the data sheet).",
           "Price basis used: " + "; ".join(sorted({r.price_basis for r in rows})),
           "Sources: " + "; ".join(sorted({r.source for r in rows}))]
    out += [f"Origin assumption - {r.variety}: {r.origin_note}"
            for r in sorted(rows, key=lambda r: r.variety.casefold()) if r.origin_note]
    return out + list(notes)


def build_workbook(rows, notes):
    wb = Workbook()
    ws = wb.active
    ws.title = "Mangoes"
    ws.append(HEADERS)
    for r in sorted(rows, key=lambda r: r.variety.casefold()):
        ws.append([r.variety, r.origin_country, r.origin_region, r.price_usd_kg,
                   r.price_basis, f"{r.source}, checked {r.date_checked.isoformat()}"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for (cell,) in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        cell.number_format = PRICE_FORMAT
        cell.alignment = Alignment(horizontal="right")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:F{ws.max_row}"
    for i, col in enumerate(ws.columns, start=1):
        shown = [f"${c.value:,.2f}" if i == 4 and c.row > 1 else str(c.value or "")
                 for c in col]
        width = min(max(len(s) for s in shown) + 2, MAX_WIDTH)
        ws.column_dimensions[get_column_letter(i)].width = width
    nt = wb.create_sheet("Notes")
    nt.column_dimensions["A"].width = 110
    for line in build_notes(rows, notes):
        nt.append([line])
    for (c,) in nt.iter_rows():
        c.alignment = Alignment(wrap_text=True, vertical="top")
    nt["A1"].font = Font(bold=True)
    return wb


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the mango list .xlsx")
    p.add_argument("--data", default=str(ROOT / "data/mangoes.csv"))
    p.add_argument("--notes", default=str(ROOT / "data/notes.txt"))
    p.add_argument("--out", default="mangoes.xlsx")
    a = p.parse_args(argv)
    rows = load_rows(a.data)  # raises ValidationError naming the row
    notes = [ln.strip() for ln in Path(a.notes).read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    build_workbook(rows, notes).save(a.out)


if __name__ == "__main__":
    main()
