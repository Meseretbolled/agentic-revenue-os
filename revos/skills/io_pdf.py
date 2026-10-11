"""Render the IO to a PDF with fpdf2 (pure Python, no secrets, no network)."""

import re

from fpdf import FPDF

_LATIN = {"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
          "…": "...", " ": " "}


def _clean(text: str) -> str:
    for char, plain in _LATIN.items():
        text = text.replace(char, plain)
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).encode("latin-1", "replace").decode("latin-1")


_WIDTHS = (62, 52, 20, 26, 30)             # mm: item, unit, quantity, price, amount (190)


def _table(pdf: FPDF, rows: list[str]) -> None:
    """The line-item table with wrapped cells, so a long item or unit name grows the row instead of running
    into the next column. Numbers are right-aligned."""
    pdf.set_font("Helvetica", "", 9)
    with pdf.table(col_widths=_WIDTHS, text_align=("LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT"),
                   line_height=5.5, first_row_as_headings=True) as table:
        for raw in rows:
            row = table.row()
            for cell in [c.strip() for c in raw.strip("|").split("|")]:
                row.cell(_clean(cell))
    pdf.ln(2)


def render_io_pdf(document: str, *, test_label: str = "TEST MODE: not legally binding") -> bytes:
    """The IO's markdown as a plain PDF: a title, the terms, the line-item table, the SAMPLE labels."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(160, 0, 0)
    pdf.cell(0, 6, _clean(test_label), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    lines = document.splitlines()
    table_done = False
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("|") and not table_done:
            _table(pdf, [row for row in lines if row.startswith("|") and not row.startswith("|---")])
            table_done = True
            continue
        if not line:
            pdf.ln(3)
        elif line.startswith("#"):
            pdf.set_font("Helvetica", "B", 15)
            pdf.multi_cell(0, 8, _clean(line.lstrip("# ")), new_x="LMARGIN", new_y="NEXT")
        elif line.startswith("|"):
            continue                                # table rows are drawn together, below
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5.5, _clean(line), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
