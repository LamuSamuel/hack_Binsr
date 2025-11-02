#!/usr/bin/env python3
"""
Fast TREC PDF filler (overlay method)
- Reads: inspection.json, dynamic_fields.json, TREC_Template_Blank.pdf
- Writes: output_pdf.pdf
Usage:
  python fill_trec.py --template TREC_Template_Blank.pdf --json inspection.json --spec dynamic_fields.json --out output_pdf.pdf [--debug]
"""
import argparse, json, io, tempfile, time, math, threading
from datetime import datetime , timezone
from collections import defaultdict
from typing import Dict, Any, Tuple, List
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import red, black, blue
import requests
from PIL import Image
import hashlib

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def denorm(box, w, h):
    x0, y0, x1, y1 = box
    return (x0*w, y0*h, x1*w, y1*h)

def wrap_text(text, font_name, font_size, max_width):
    words = (text or "").split()
    lines, line = [], ""
    for w in words:
        cand = (line + " " + w).strip()
        if stringWidth(cand, font_name, font_size) <= max_width:
            line = cand
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines

def draw_text_in_box(c, text, box_pts, font="Helvetica", size=9, color=black, valign="top", debug=False, name=None):
    x0, y0, x1, y1 = box_pts
    pad = 2
    max_w = max(0, (x1-x0) - 2*pad)
    c.setFillColor(color)
    c.setFont(font, size)
    lines = wrap_text(text, font, size, max_w)
    line_h = size * 1.2
    if valign == "top":
        y = y1 - pad - size
    elif valign == "middle":
        total_h = line_h * len(lines)
        y = y0 + (y1 - y0 - total_h)/2 + (len(lines)-1)*line_h
    else:
        y = y0 + pad
    for ln in lines:
        c.drawString(x0 + pad, y, ln)
        y -= line_h
    if debug:
        c.setStrokeColor(red)
        c.rect(x0, y0, x1-x0, y1-y0, stroke=1, fill=0)
        if name:
            c.setFont("Helvetica", 6)
            c.setFillColor(red)
            c.drawString(x0+2, y1-8, name)
            c.setFillColor(black)

def draw_checkbox(c, checked, box_pts, debug=False, name=None):
    x0, y0, x1, y1 = box_pts
    size = min(x1-x0, y1-y0)
    c.setStrokeColor(black)
    c.rect(x0, y0, size, size, stroke=1, fill=0)
    if checked:
        c.setLineWidth(2)
        c.line(x0+2, y0+2, x0+size-2, y0+size-2)
        c.line(x0+2, y0+size-2, x0+size-2, y0+2)
        c.setLineWidth(1)
    if debug:
        c.setStrokeColor(red)
        c.rect(x0, y0, x1-x0, y1-y0, stroke=1, fill=0)
        if name:
            c.setFont("Helvetica", 6)
            c.setFillColor(red)
            c.drawString(x0+2, y1-8, name)
            c.setFillColor(black)

def fetch_image(url, timeout=5):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None

def draw_image_url(c, url, box_pts, fit="contain", debug=False, name=None):
    img = fetch_image(url)
    x0, y0, x1, y1 = box_pts
    if img is None:
        c.setStrokeColor(blue)
        c.rect(x0, y0, x1-x0, y1-y0, stroke=1, fill=0)
        draw_text_in_box(c, "Image unavailable", box_pts, size=8, color=blue, valign="middle", debug=False)
        return
    bw, bh = (x1-x0), (y1-y0)
    iw, ih = img.size
    if fit == "cover":
        scale = max(bw/iw, bh/ih)
    else:
        scale = min(bw/iw, bh/ih)
    nw, nh = iw*scale, ih*scale
    ox, oy = x0 + (bw-nw)/2, y0 + (bh-nh)/2
    c.drawImage(ImageReader(img), ox, oy, width=nw, height=nh, preserveAspectRatio=True, mask='auto')
    if debug:
        c.setStrokeColor(red)
        c.rect(x0, y0, x1-x0, y1-y0, stroke=1, fill=0)
        if name:
            c.setFont("Helvetica", 6)
            c.setFillColor(red)
            c.drawString(x0+2, y1-8, name)
            c.setFillColor(black)

def ms_to_local_date(ms):
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %I:%M %p UTC")
    except Exception:
        return "Data not found in test data"

def shape_values(inspection):
    out = {}
    out["client_name"] = (inspection.get("clientInfo", {}).get("name")
                          or inspection.get("client", {}).get("name")
                          or "Data not found in test data")
    ms = (inspection.get("schedule", {}).get("date")
          or inspection.get("inspection", {}).get("date"))
    out["date"] = ms_to_local_date(ms) if ms else "Data not found in test data"
    out["address"] = (inspection.get("address", {}).get("fullAddress")
                      or inspection.get("property", {}).get("address")
                      or "Data not found in test data")
    out["inspector"] = (inspection.get("inspector", {}).get("name")
                        or "Data not found in test data")
    out["trec_license"] = (inspection.get("inspector", {}).get("licenseNumber")
                           or "Data not found in test data")

    systems = inspection.get("sections", []) or inspection.get("systems", [])
    for sec in systems:
        sec_name = sec.get("title") or sec.get("name") or "section"
        line_items = sec.get("lineItems", []) or sec.get("items", [])
        flags = {"I": False, "NI": False, "NP": False, "D": False}
        comments_text, photos = [], []
        for li in line_items:
            status = (li.get("inspectionStatus") or "").upper()
            if status in flags:
                flags[status] = True
            for cm in li.get("comments", []):
                label = cm.get("label") or cm.get("title") or ""
                text = cm.get("text") or cm.get("content") or ""
                if label or text:
                    comments_text.append((label + ": " if label else "") + text)
                for p in cm.get("photos", []):
                    if isinstance(p, str):
                        photos.append(p)
                    elif isinstance(p, dict) and p.get("url"):
                        photos.append(p["url"])
        key_base = sec_name.lower().replace(" ", "_")[:30]
        out[f"{key_base}_I"]  = flags["I"]
        out[f"{key_base}_NI"] = flags["NI"]
        out[f"{key_base}_NP"] = flags["NP"]
        out[f"{key_base}_D"]  = flags["D"]
        out[f"{key_base}_notes"] = "\n".join(comments_text[:6]) if comments_text else "No comments"
        if photos:
            out[f"{key_base}_photo1"] = photos[0]
    return out

def render(template_path, spec_path, values, out_path, debug=False):
    with open(spec_path, 'r') as f:
        spec = json.load(f)
    if "template_sha256" in spec:
        current = sha256_file(template_path)
        if current != spec["template_sha256"]:
            print("[WARN] Template hash mismatch. Mapping may be off.")

    reader = PdfReader(template_path)
    writer = PdfWriter()

    fields_by_page = {}
    for fld in spec["fields"]:
        fields_by_page.setdefault(fld["page"], []).append(fld)

    for pi, page in enumerate(reader.pages, start=1):
        mb = page.mediabox
        w, h = float(mb.width), float(mb.height)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(w, h))

        for fld in fields_by_page.get(pi, []):
            box = denorm(fld["box"], w, h)
            ftype = fld.get("type", "text")
            name  = fld.get("name", "")
            font  = fld.get("font", "Helvetica")
            size  = fld.get("size", 9)
            color = fld.get("color", "black")
            if name.endswith("_status_label") or name.endswith("_lbl") or ftype == "label":
                continue
            from reportlab.lib import colors as RLcolors
            col = getattr(RLcolors, color, RLcolors.black)

            val = values.get(name, fld.get("placeholder", "Data not found in test data"))
            if ftype in ("text", "multiline"):
                draw_text_in_box(c, str(val), box, font=font, size=size, color=col,
                                 valign=fld.get("valign","top"), debug=debug, name=name)
            elif ftype == "checkbox":
                draw_checkbox(c, bool(val), box, debug=debug, name=name)
            elif ftype == "image":
                draw_image_url(c, val, box, fit=fld.get("fit","contain"), debug=debug, name=name)
            elif ftype == "link":
                draw_text_in_box(c, str(val), box, font=font, size=size, color=col,
                                 valign="middle", debug=debug, name=name)

        c.save()
        packet.seek(0)
        data = packet.getvalue()
        if not data:
            # No overlay content — just keep the base page
            writer.add_page(page)
            continue

        try:
            overlay_pdf = PdfReader(io.BytesIO(data))
            if len(overlay_pdf.pages) > 0:
                page.merge_page(overlay_pdf.pages[0])  # PyPDF2 2.x/3.x OK
            # else: no overlay page, keep base as-is
        except Exception as e:
            # If anything goes wrong reading overlay, keep base page
            # print(f"[WARN] Overlay read error on page {pi}: {e}")
            pass

        writer.add_page(page)

    with open(out_path, "wb") as f:
        writer.write(f)

def main():
    import argparse, time, json
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--json", required=True, help="inspection.json")
    ap.add_argument("--spec", required=True, help="dynamic_fields.json")
    ap.add_argument("--out", default="output_pdf.pdf")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    with open(args.json, "r") as f:
        payload = json.load(f)
    inspection = payload.get("inspection", payload)

    values = shape_values(inspection)
    render(args.template, args.spec, values, args.out, debug=args.debug)

    print(f"Done: {args.out}  (elapsed: {time.time()-t0:.2f}s)")

if __name__ == "__main__":
    import time, json, os
    t0 = time.time()

    template = "TREC_Template_Blank.pdf"
    json_path = "inspection.json"
    spec_path = "dynamic_fields_trec.json"
    out_path = "output_pdf.pdf"

    # Make sure files exist
    for f in [template, json_path, spec_path]:
        if not os.path.exists(f):
            raise FileNotFoundError(f"Missing required file: {f}")

    with open(json_path, "r") as f:
        payload = json.load(f)
    inspection = payload.get("inspection", payload)

    values = shape_values(inspection)
    render(template, spec_path, values, out_path, debug=False)

    print(f"✅ PDF generated: {out_path}  (elapsed: {time.time()-t0:.2f}s)")