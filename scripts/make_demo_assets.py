"""Generate the demo order image + an ANNOTATED screenshot for the README.

The annotated screenshot maps every region of the source order to the flow step
that consumes it (the assignment requires "Annotated screenshots or a short
recording"). The image is synthetic so the annotations are exact, deterministic
and re-runnable offline.

Outputs:
    assets/sample_order/order.png           plain demo order
    assets/annotated/order_annotated.png    annotated screenshot
    assets/sample_order/order.json          mock-LLM sidecar (ExtractedOrder)
    assets/sample_order/order.txt           mock-OCR sidecar (raw text)

Run:  python scripts/make_demo_assets.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SAMPLE = ASSETS / "sample_order"
ANNOTATED = ASSETS / "annotated"

# --- canvas ---------------------------------------------------------------
W, H = 860, 620
WHITE = (255, 255, 255)
INK = (30, 30, 30)
GRAY = (110, 110, 110)
LIGHT = (243, 245, 249)
BORDER = (210, 215, 222)
STEP_DEBTOR = (205, 44, 44)       # red   -> STEP 2 debtor
STEP_ITEMS = (35, 96, 183)        # blue  -> STEP 3 products/lines
STEP_TOTALS = (38, 148, 92)       # green -> STEP 4 reconciliation
STEP_PAYMENT = (142, 68, 173)     # purple-> STEP 5 invoice/payment


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/ariali.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _draw_table(img: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int) -> None:
    img.rectangle([x0, y0, x1, y1], fill=LIGHT, outline=BORDER, width=1)
    for row in range(1, 4):
        yy = y0 + int((y1 - y0) * row / 4)
        img.line([x0, yy, x1, yy], fill=BORDER, width=1)


def build_order_image() -> Path:
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    f_title = _font(24, bold=True)
    f_head = _font(16, bold=True)
    f_body = _font(14)
    f_small = _font(11)

    # Header band
    d.rectangle([0, 0, W, 64], fill=(236, 241, 248), outline=None)
    d.text((24, 14), "ORDER  #ORD-2026-0142", font=f_title, fill=INK)
    d.text((24, 46), "Order date: 18.03.2026        Cust.Ref: ORD-2026-0142", font=f_head, fill=GRAY)

    # Debtor block
    d.text((24, 84), "Bill To / Deliver To", font=f_head, fill=INK)
    d.text((24, 112), "Acme GmbH", font=f_body, fill=INK)
    d.text((24, 134), "Max Mustermann", font=f_body, fill=INK)
    d.text((24, 156), "Industriestr. 12", font=f_body, fill=INK)
    d.text((24, 178), "60327 Frankfurt am Main, Germany", font=f_body, fill=INK)
    d.text((24, 200), "invoice@acme.example  +49 69 1234 5678", font=f_small, fill=GRAY)

    # Items table
    d.text((24, 240), "Items", font=f_head, fill=INK)
    _draw_table(d, 24, 266, W - 24, 406)
    headers = ["SKU", "Description", "Qty", "U.Price", "VAT", "Disc", "Price"]
    cols = [36, 130, 320, 400, 470, 540, 640]
    d.text((cols[0], 272), headers[0], font=f_head, fill=GRAY)
    for i, hx in enumerate(headers[1:], start=1):
        d.text((cols[i], 272), hx, font=f_head, fill=GRAY)
    rows = [
        ("SKU-1001", "Heavy-duty cable, 3m", "2", "59.90", "19%", "0%", "119.80"),
        ("SKU-1002", "HDMI adapter", "3", "9.90", "19%", "10%", "26.73"),
    ]
    for r, (sku, desc, qty, up, vat, disc, price) in enumerate(rows):
        yy = 302 + r * 48
        d.text((cols[0], yy), sku, font=f_body, fill=INK)
        d.text((cols[1], yy), desc, font=f_body, fill=INK)
        d.text((cols[2], yy), qty, font=f_body, fill=INK)
        d.text((cols[3], yy), up, font=f_body, fill=INK)
        d.text((cols[4], yy), vat, font=f_body, fill=INK)
        d.text((cols[5], yy), disc, font=f_body, fill=INK)
        d.text((cols[6], yy), price, font=f_body, fill=INK)

    # Totals
    d.text((560, 432), "Total net", font=f_body, fill=INK)
    d.text((730, 432), "146.53 EUR", font=f_body, fill=INK)
    d.text((560, 458), "VAT (19%)", font=f_body, fill=INK)
    d.text((730, 458), "27.84 EUR", font=f_body, fill=INK)
    d.text((560, 484), "TOTAL", font=f_head, fill=INK)
    d.text((730, 484), "174.37 EUR", font=f_head, fill=INK)
    d.text((24, 432), "Shipping: free of charge", font=f_small, fill=GRAY)

    # Payment
    d.text((24, 518), "Payment", font=f_head, fill=INK)
    d.text((24, 544), "Bank Transfer  |  status: PAID  |  received 18.03.2026", font=f_body, fill=INK)

    path = SAMPLE / "order.png"
    img.save(path)
    return path


def annotate(image: Path) -> Path:
    img = Image.open(image).convert("RGB")
    d = ImageDraw.Draw(img)
    f_lab = _font(12, bold=True)
    f_leg = _font(13, bold=True)

    # Region boxes (top-left, bottom-right)
    boxes = [
        ((18, 78), (470, 216), STEP_DEBTOR, "STEP 2 - Debtor (select-or-create)"),
        ((18, 260), (836, 410), STEP_ITEMS, "STEP 3 - Items -> Product + line"),
        ((554, 426), (836, 506), STEP_TOTALS, "STEP 4 - Totals (reconciliation gate)"),
        ((18, 512), (560, 570), STEP_PAYMENT, "STEP 5 - Invoice payment status"),
    ]
    for (x0, y0), (x1, y1), color, label in boxes:
        d.rectangle([x0, y0, x1, y1], outline=color, width=3)
        d.rectangle([x0 + 4, y0 + 4, x0 + 10, y0 + 10], fill=color, outline=color)
        d.text((x0 + 6, y0 + 8), label, font=f_lab, fill=color)

    # Legend
    ly = 24
    d.rectangle([600, ly, 836, ly + 34], fill=(240, 240, 240), outline=BORDER)
    d.text((608, ly + 9), "Flow: extract -> order -> verify", font=f_leg, fill=INK)

    ANNOTATED.mkdir(parents=True, exist_ok=True)
    out = ANNOTATED / "order_annotated.png"
    img.save(out)
    return out


def write_sidecars() -> None:
    payload = {
        "header": {
            "order_date": "2026-03-18",
            "external_reference": "ORD-2026-0142",
            "price_mode": "Net",
            "vat_mode": "With VAT",
            "overall_discount_percent": 0,
            "shipping_amount": 0,
            "shipping_is_free": True,
        },
        "debtor": {
            "company": "Acme GmbH",
            "first_name": "Max",
            "last_name": "Mustermann",
            "salutation": "---",
            "alias": "Acme GmbH",
            "billing_address": {
                "street": "Industriestr. 12",
                "zip_code": "60327",
                "city": "Frankfurt am Main",
                "country": "Germany",
                "email": "invoice@acme.example",
                "telephone": "+49 69 1234 5678",
            },
            "delivery_address": {
                "street": "Industriestr. 12",
                "zip_code": "60327",
                "city": "Frankfurt am Main",
                "country": "Germany",
                "email": "invoice@acme.example",
                "telephone": "+49 69 1234 5678",
            },
            "same_delivery_address": True,
            "payment_method": "Bank Transfer",
            "price_mode": "Net",
            "discount_percent": 0,
        },
        "items": [
            {
                "sku": "SKU-1001",
                "description": "Heavy-duty cable, 3m",
                "quantity": 2,
                "unit_net_price": 59.90,
                "vat_percent": 19,
                "discount_percent": 0,
            },
            {
                "sku": "SKU-1002",
                "description": "HDMI adapter",
                "quantity": 3,
                "unit_net_price": 9.90,
                "vat_percent": 19,
                "discount_percent": 10,
            },
        ],
        "totals": {"total_net": 146.53, "total_vat": 27.84, "total_gross": 174.37, "currency": "EUR"},
        "payment": {
            "paid_status": "Paid",
            "payment_date": "2026-03-18",
            "payment_method": "Bank Transfer",
        },
    }
    SAMPLE.mkdir(parents=True, exist_ok=True)
    (SAMPLE / "order.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (SAMPLE / "order.txt").write_text(
        "ORDER ORD-2026-0142\nOrder date 18.03.2026\nAcme GmbH\nMax Mustermann\n"
        "Industriestr. 12\n60327 Frankfurt am Main\nSKU-1001 Heavy-duty cable 3m 2 59.90 19% 0%\n"
        "SKU-1002 HDMI adapter 3 9.90 19% 10%\nNet 146.53 VAT 27.84 TOTAL 174.37\n"
        "Payment Bank Transfer PAID 18.03.2026\n",
        encoding="utf-8",
    )


def main() -> int:
    image = build_order_image()
    annotated = annotate(image)
    write_sidecars()
    print(f"demo image     : {image}")
    print(f"annotated      : {annotated}")
    print("sidecars       : order.json + order.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())