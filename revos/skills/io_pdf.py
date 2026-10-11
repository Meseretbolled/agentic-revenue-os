"""Render the IO to a PDF with fpdf2 (pure Python, no secrets, no network)."""

import re

from fpdf import FPDF

_LATIN = {"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
          "…": "...", " ": " "}


def _clean(text: str) -> str:
    for char, plain in _LATIN.items():
        text = text.replace(char, plain)
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).encode("latin-1", "replace").decode("latin-1")


def render_io_pdf(document: str, *, test_label: str = "TEST MODE: not legally binding") -> bytes:
    """The IO's markdown as a plain PDF: a title, the terms, the line-item table, the SAMPLE labels."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(160, 0, 0)
    pdf.cell(0, 6, _clean(test_label), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    for raw in document.splitlines():
        line = raw.rstrip()
        if not line:
            pdf.ln(3)
        elif line.startswith("#"):
            pdf.set_font("Helvetica", "B", 15)
            pdf.multi_cell(0, 8, _clean(line.lstrip("# ")), new_x="LMARGIN", new_y="NEXT")
        elif line.startswith("|---"):
            continue
        elif line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            pdf.set_font("Helvetica", "B" if cells and cells[0] == "Item" else "", 9)
            width = (pdf.w - pdf.l_margin - pdf.r_margin) / max(len(cells), 1)
            for cell in cells:
                pdf.cell(width, 6, _clean(cell)[:34], border=1)
            pdf.ln(6)
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5.5, _clean(line), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
