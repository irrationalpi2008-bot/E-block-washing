"""
pdf_export.py - Zero-dependency PDF generator for E Block Laundry Allocations.
Generates a clean, print-ready single-page A4 landscape weekly timetable matching the resident portal.
"""

from datetime import datetime
import re

# Standard A4 Landscape dimensions in points (72 points = 1 inch)
A4_LANDSCAPE_W = 841.89
A4_LANDSCAPE_H = 595.28

TIMES = [
    ("00:00", "01:30"), ("01:30", "03:00"), ("03:00", "04:30"), ("04:30", "06:00"),
    ("06:00", "07:30"), ("07:30", "09:00"), ("09:00", "10:30"), ("10:30", "12:00"),
    ("12:00", "13:30"), ("13:30", "15:00"), ("15:00", "16:30"), ("16:30", "18:00"),
    ("18:00", "19:30"), ("19:30", "21:00"), ("21:00", "22:30"), ("22:30", "00:00"),
]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def escape_pdf_str(s):
    """Sanitize and escape a string for PDF Type 1 fonts (ASCII/Latin-1 safe)."""
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\u2013", "-").replace("\u2014", "--")
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2022", "*").replace("\u00a0", " ")
    
    cleaned = []
    for ch in s:
        code = ord(ch)
        if 32 <= code <= 126:
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    s = "".join(cleaned)
    s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return s


class PDFCanvas:
    """Low-level stream builder for a single PDF page."""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.stream = []

    def set_fill_color(self, r, g, b):
        self.stream.append(f"{r:.3f} {g:.3f} {b:.3f} rg")

    def set_stroke_color(self, r, g, b):
        self.stream.append(f"{r:.3f} {g:.3f} {b:.3f} RG")

    def set_line_width(self, width):
        self.stream.append(f"{width:.2f} w")

    def rect(self, x, y, w, h, fill=False, stroke=False):
        self.stream.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re")
        if fill and stroke:
            self.stream.append("B")
        elif fill:
            self.stream.append("f")
        elif stroke:
            self.stream.append("S")

    def line(self, x1, y1, x2, y2):
        self.stream.append(f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def text(self, x, y, text_str, font="F1", size=10, r=0, g=0, b=0):
        safe_str = escape_pdf_str(text_str)
        self.stream.append("q")
        self.set_fill_color(r, g, b)
        self.stream.append(f"BT /{font} {size:.2f} Tf {x:.2f} {y:.2f} Td ({safe_str}) Tj ET")
        self.stream.append("Q")

    def text_right(self, x_right, y, text_str, font="F1", size=10, r=0, g=0, b=0, approx_char_w=None):
        safe_str = escape_pdf_str(text_str)
        if approx_char_w is None:
            approx_char_w = size * (0.60 if font == "F2" else 0.52)
        w = len(safe_str) * approx_char_w
        x = x_right - w
        self.text(x, y, text_str, font=font, size=size, r=r, g=g, b=b)

    def text_center(self, x_center, y, text_str, font="F1", size=10, r=0, g=0, b=0, approx_char_w=None):
        safe_str = escape_pdf_str(text_str)
        if approx_char_w is None:
            approx_char_w = size * (0.60 if font == "F2" else 0.52)
        w = len(safe_str) * approx_char_w
        x = x_center - (w / 2.0)
        self.text(x, y, text_str, font=font, size=size, r=r, g=g, b=b)

    def to_stream_bytes(self):
        content = "\n".join(self.stream).encode("ascii")
        return content


class PDFDocument:
    """PDF 1.4 document builder."""

    def __init__(self):
        self.pages = []

    def add_page(self, width=A4_LANDSCAPE_W, height=A4_LANDSCAPE_H):
        page = PDFCanvas(width, height)
        self.pages.append(page)
        return page

    def build(self):
        objects = []
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        
        page_count = len(self.pages)
        kids = []
        
        f1_num = 3
        f2_num = 4
        f3_num = 5
        
        objects.append(None) # placeholder for Pages at index 1
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>")
        
        for i, page in enumerate(self.pages):
            p_num = 6 + 2 * i
            c_num = 7 + 2 * i
            kids.append(f"{p_num} 0 R")
            
            page_dict = (
                f"<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 {page.width:.2f} {page.height:.2f}] "
                f"/Contents {c_num} 0 R "
                f"/Resources << /Font << /F1 {f1_num} 0 R /F2 {f2_num} 0 R /F3 {f3_num} 0 R >> >> >>"
            ).encode("ascii")
            objects.append(page_dict)
            
            stream_data = page.to_stream_bytes()
            content_dict = (
                f"<< /Length {len(stream_data)} >>\nstream\n"
            ).encode("ascii") + stream_data + b"\nendstream"
            objects.append(content_dict)
            
        kids_str = " ".join(kids)
        pages_dict = f"<< /Type /Pages /Kids [{kids_str}] /Count {page_count} >>".encode("ascii")
        objects[1] = pages_dict
        
        out = bytearray()
        out.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        
        offsets = [0]
        for i, obj_content in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{i} 0 obj\n".encode("ascii"))
            out.extend(obj_content)
            out.extend(b"\nendobj\n")
            
        xref_pos = len(out)
        out.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
        out.extend(b"0000000000 65535 f \n")
        for pos in offsets[1:]:
            out.extend(f"{pos:010d} 00000 n \n".encode("ascii"))
            
        out.extend(
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
        )
        return bytes(out)


def shorten_resident_name(name, max_len=14):
    """Cleanly shorten long names to fit nicely inside portal-style slot pills."""
    nm = str(name or '').strip()
    if len(nm) <= max_len:
        return nm
    parts = nm.split()
    if len(parts) > 1:
        # First name + last initial
        nm = f"{parts[0]} {parts[-1][0]}."
    if len(nm) > max_len:
        nm = nm[:max_len - 2] + ".."
    return nm


def generate_allocations_pdf(assignment_data, view="all", is_preview=False):
    """
    Generate print-ready single-page weekly timetable PDF matching the resident portal.
    Does not display machine numbers.
    """
    doc = PDFDocument()
    slots_map = assignment_data.get("slots", {}) or {}
    
    page_w = A4_LANDSCAPE_W
    page_h = A4_LANDSCAPE_H
    canvas = doc.add_page(width=page_w, height=page_h)
    
    margin_x = 20.0
    margin_top = 18.0
    margin_bot = 14.0
    
    # -------------------------------------------------------------------------
    # TOP BANNER
    # -------------------------------------------------------------------------
    banner_h = 24.0
    canvas.set_fill_color(0.08, 0.12, 0.16)
    canvas.rect(margin_x, page_h - margin_top - banner_h, page_w - 2 * margin_x, banner_h, fill=True)
    
    title = (
        "E BLOCK LAUNDRY  -  SIMULATED ALLOTMENT TIMETABLE"
        if is_preview else
        "E BLOCK LAUNDRY  -  WEEKLY ALLOTMENT TIMETABLE"
    )
    canvas.text(margin_x + 12, page_h - margin_top - 16.5, title, font="F2", size=11, r=1, g=1, b=1)
    
    subtitle = (
        "Live Simulation Only (Database Unchanged)  *  1.5h Cycles (24/7)"
        if is_preview else
        "Weekly Timetable  *  1.5h Cycles (24/7)  *  Official Schedule"
    )
    canvas.text_right(page_w - margin_x - 12, page_h - margin_top - 16.5, subtitle, font="F1", size=8.5, r=0.75, g=0.85, b=0.92)
    
    # -------------------------------------------------------------------------
    # TIMETABLE GRID DIMENSIONS
    # -------------------------------------------------------------------------
    grid_top = page_h - margin_top - banner_h - 6.0
    time_col_w = 76.0
    day_col_w = (page_w - 2 * margin_x - time_col_w) / 7.0
    header_h = 18.0
    row_h = (grid_top - header_h - margin_bot) / 16.0
    
    # Table Header Row
    canvas.set_fill_color(0.15, 0.22, 0.30)
    canvas.rect(margin_x, grid_top - header_h, page_w - 2 * margin_x, header_h, fill=True)
    canvas.text_center(margin_x + time_col_w / 2.0, grid_top - 12.5, "TIME (24/7)", font="F2", size=8, r=1, g=1, b=1)
    
    for d_idx, day in enumerate(DAYS):
        col_x = margin_x + time_col_w + d_idx * day_col_w
        canvas.text_center(col_x + day_col_w / 2.0, grid_top - 12.5, day.upper(), font="F2", size=9, r=1, g=1, b=1)
        
    # -------------------------------------------------------------------------
    # 16 TIMESLOT ROWS
    # -------------------------------------------------------------------------
    for ti, (t_start, t_end) in enumerate(TIMES):
        row_y = grid_top - header_h - ti * row_h
        
        # Time column cell
        canvas.set_fill_color(0.93, 0.95, 0.97)
        canvas.rect(margin_x, row_y - row_h, time_col_w, row_h, fill=True)
        canvas.set_stroke_color(0.80, 0.84, 0.88)
        canvas.set_line_width(0.4)
        canvas.rect(margin_x, row_y - row_h, time_col_w, row_h, stroke=True)
        
        time_label = f"{t_start} - {t_end}"
        canvas.text_center(margin_x + time_col_w / 2.0, row_y - row_h / 2.0 - 2.5, time_label, font="F2", size=7.5, r=0.10, g=0.20, b=0.30)
        
        # 7 Day cells
        for d_idx, day in enumerate(DAYS):
            col_x = margin_x + time_col_w + d_idx * day_col_w
            sid = f"{day}-{ti}"
            peeps = slots_map.get(sid, [])
            
            # Subtle alternating background for rows
            if ti % 2 == 1:
                canvas.set_fill_color(0.985, 0.988, 0.992)
                canvas.rect(col_x, row_y - row_h, day_col_w, row_h, fill=True)
                
            canvas.set_stroke_color(0.82, 0.85, 0.89)
            canvas.set_line_width(0.4)
            canvas.rect(col_x, row_y - row_h, day_col_w, row_h, stroke=True)
            
            # Render up to 3 allocated residents as portal-style pills
            pill_margin = 2.0
            pill_w = day_col_w - 2 * pill_margin
            pill_h = 9.2
            pill_gap = 1.0
            
            for p_idx, p in enumerate(peeps[:3]):
                py = row_y - pill_margin - p_idx * (pill_h + pill_gap)
                
                # Pill background
                canvas.set_fill_color(0.93, 0.95, 0.98)
                canvas.set_stroke_color(0.78, 0.83, 0.89)
                canvas.set_line_width(0.3)
                canvas.rect(col_x + pill_margin, py - pill_h, pill_w, pill_h, fill=True, stroke=True)
                
                rm_str = str(p.get("room", "")).strip()
                nm_str = shorten_resident_name(p.get("name", ""), max_len=14)
                p_awarded = p.get("priority_awarded")
                p_badge = f"P{p_awarded}" if p_awarded else "F"
                
                # Room number (Bold)
                rx = col_x + pill_margin + 3.0
                canvas.text(rx, py - 7.0, rm_str, font="F2", size=6.8, r=0.05, g=0.10, b=0.20)
                
                # Name (Regular) - spaced appropriately based on room digits
                rm_w = len(rm_str) * 4.4 + 4.0
                canvas.text(rx + rm_w, py - 7.0, nm_str, font="F1", size=6.6, r=0.15, g=0.20, b=0.25)
                
                # Priority badge (Bold, right-aligned)
                canvas.text_right(col_x + pill_margin + pill_w - 3.0, py - 7.0, p_badge, font="F2", size=5.8, r=0.35, g=0.42, b=0.52)
                
    return doc.build()
