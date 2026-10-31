"""Builds the demo knowledge base in seed/kb/ from the content modules (reproducible, no network).

    docker compose run --rm --no-deps -v ./backend:/app api python seed/build_kb.py

Outputs: ~80 HTML help-center articles (EN + DE + NL), 4 PDFs, 4 CSV size tables and 1 DOCX care guide.
"""

import csv
import html
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kb_content_de_nl import DE, NL  # noqa: E402
from kb_content_en import EN  # noqa: E402

OUT = Path(__file__).parent / "kb"
SHOP = "Nordvik Outdoor"
NAV = {"en": "Help Center", "de": "Hilfe-Center", "nl": "Helpcentrum"}


def render_html(lang: str, title: str, section: str, blocks: list) -> str:
    body: list[str] = []
    for kind, content in blocks:
        if kind == "h2":
            body.append(f"<h2>{html.escape(content)}</h2>")
        elif kind == "p":
            body.append(f"<p>{html.escape(content)}</p>")
        elif kind in {"ul", "ol"}:
            body.append(f"<{kind}>" + "".join(f"<li>{html.escape(i)}</li>" for i in content) + f"</{kind}>")
    return (
        f'<!doctype html>\n<html lang="{lang}"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f'<meta name="category" content="{section}"></head><body>\n'
        f'<header><a href="/">{SHOP}</a> | <a href="/help">{NAV[lang]}</a> | Cart | Account</header>\n'
        f'<nav>Orders | Shipping | Returns | Payment | Products | Warranty</nav>\n'
        f'<main><article>\n<h1>{html.escape(title)}</h1>\n' + "\n".join(body) + "\n</article></main>\n"
        f'<aside>Was this article helpful? Yes / No. Related articles.</aside>\n'
        f'<footer>&copy; {SHOP} B.V. | Imprint | Privacy | Terms | Cookie settings</footer>\n</body></html>\n'
    )


# ------------------------------------------------------------------ minimal multi-page PDF writer (stdlib only)

def _pdf_escape(text: str) -> bytes:
    out = bytearray()
    for ch in text:
        b = ch.encode("cp1252", errors="replace")
        if b in (b"(", b")", b"\\"):
            out += b"\\" + b
        elif b[0] < 32 or b[0] > 126:
            out += b"\\%03o" % b[0]
        else:
            out += b
    return bytes(out)


def make_pdf(title: str, items: list[tuple[str, str]]) -> bytes:
    """items: ('h', text) heading | ('p', text) paragraph. Helvetica, A4, 10 pt, wrapped at 95 chars."""
    lines: list[tuple[str, str]] = [("h1", title), ("sp", "")]
    for kind, text in items:
        if kind == "h":
            lines += [("sp", ""), ("h2", text)]
        else:
            lines += [("p", ln) for ln in textwrap.wrap(text, 95)] + [("sp", "")]
    pages, cur, y = [], [], 790
    for kind, text in lines:
        size, step = {"h1": (16, 26), "h2": (12, 20), "p": (10, 14), "sp": (10, 8)}[kind]
        if y - step < 60:
            pages.append(cur)
            cur, y = [], 790
        if kind != "sp":
            cur.append((72, y, size, kind != "p", text))
        y -= step
    pages.append(cur)

    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # pages placeholder (obj 2)
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    for page in pages:
        stream = b"".join(
            b"BT /%s %d Tf %d %d Td (" % (b"F2" if bold else b"F1", size, x, y_) + _pdf_escape(text) + b") Tj ET\n"
            for x, y_, size, bold, text in page
        )
        c = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        kids.append(add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>" % (c, f1, f2)))
    objs[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    info = add(b"<< /Title (" + _pdf_escape(title) + b") /Producer (Nordvik Outdoor) >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, info, xref)
    return bytes(out)


PDFS = {
    "terms-and-conditions.pdf": ("Terms and Conditions of Nordvik Outdoor B.V.", [
        ("h", "1. Who we are"),
        ("p", "These terms apply to all purchases in the online shop of Nordvik Outdoor B.V., Keizersgracht 100, 1015 AA Amsterdam, the Netherlands (Chamber of Commerce number 99887766, VAT NL999999999B01). Contact: support@nordvik-outdoor.example."),
        ("h", "2. Contract"),
        ("p", "The contract is concluded when we confirm your order by email. We may refuse an order for a justified reason, for example a pricing error that is obvious to a reasonable consumer or suspected fraud. We store your order data; you receive the contract terms with the order confirmation."),
        ("h", "3. Prices and payment"),
        ("p", "All prices are in euro and include VAT at the rate of the country of delivery. Shipping costs are shown before you pay. Accepted payment methods are listed in the Help Center article Payment methods. Klarna pay later is payable within 30 days after dispatch; SEPA direct debit is debited within 7 days after dispatch. Ownership of the goods passes to you on full payment."),
        ("h", "4. Delivery"),
        ("p", "We deliver within the European Union. Orders placed on working days before 14:00 CET are dispatched the same day. Delivery times are indications and not guaranteed dates; if we cannot deliver within 30 days after the order, you may cancel the contract and we refund payments within 14 days."),
        ("h", "5. Right of withdrawal"),
        ("p", "You have the right to withdraw from this contract within 14 days without giving any reason. The withdrawal period expires 14 days after the day on which you or a third party other than the carrier physically take possession of the goods. To exercise the right, inform us clearly, for example by email or with the returns portal. We reimburse all payments, including standard shipping costs, without undue delay and at the latest within 14 days of the day we are informed of your decision. We may withhold reimbursement until we have received the goods or you have provided evidence of having sent them back."),
        ("p", "In addition to this legal right we grant a voluntary return period of 30 days after delivery. Returns from the Netherlands, Belgium and Germany are free of charge; from all other EU countries EUR 6.90 is deducted from the refund. You are only liable for diminished value of the goods resulting from handling beyond what is necessary to establish their nature, characteristics and functioning."),
        ("h", "6. Legal guarantee and complaints"),
        ("p", "The legal guarantee of 2 years from delivery applies to all goods. Defects that become apparent within one year after delivery are presumed to have existed at the time of delivery. We respond to complaints within 3 working days. Our own-brand Nordvik jackets additionally have a 5-year guarantee on seams and zips."),
        ("h", "7. Liability"),
        ("p", "We are liable for damage caused by intent or gross negligence and in cases required by mandatory law. Our products are not certified personal protective equipment."),
        ("h", "8. Applicable law"),
        ("p", "Dutch law applies. Mandatory consumer protection rules of your country of residence remain unaffected."),
    ]),
    "returns-and-withdrawal-policy.pdf": ("Returns and Withdrawal Policy", [
        ("h", "Return period"),
        ("p", "You can return items within 30 days after delivery. The statutory right of withdrawal of 14 days remains unaffected. Items must be unworn, unwashed and have all tags attached. Footwear may only be tried on indoors."),
        ("h", "How to return"),
        ("p", "Open the returns portal, enter the order number and email address, select the items and the reason, print the prepaid label (or show the QR code at a PostNL or DHL point) and hand over the parcel within 7 days."),
        ("h", "Costs"),
        ("p", "Returns from the Netherlands, Belgium and Germany are free. Returns from other EU countries cost EUR 6.90, deducted from the refund. Exchanges and returns of defective or wrong items are always free."),
        ("h", "Refund"),
        ("p", "After the parcel arrives we inspect it within 2 working days. The refund is paid within 5 working days to the original payment method. Banks may need 3 to 5 further working days. The standard shipping costs of the original order are refunded when the whole order is returned."),
        ("h", "Exclusions"),
        ("p", "For hygiene reasons socks, underwear and opened neck tubes are excluded, as are gift cards and items marked Final sale. This does not affect your rights for defective goods."),
        ("h", "Model withdrawal form"),
        ("p", "(Complete and return this form only if you wish to withdraw from the contract.) To Nordvik Outdoor B.V., Keizersgracht 100, 1015 AA Amsterdam, support@nordvik-outdoor.example: I/We hereby give notice that I/We withdraw from my/our contract of sale of the following goods: ____ . Ordered on / received on: ____ . Name of consumer(s): ____ . Address of consumer(s): ____ . Signature (only if this form is notified on paper): ____ . Date: ____"),
    ]),
    "shipping-policy.pdf": ("Shipping Policy", [
        ("h", "Dispatch"),
        ("p", "Orders placed Monday to Friday before 14:00 CET are dispatched the same working day from our warehouse in Venlo. Orders placed later or at the weekend are dispatched the next working day. We do not dispatch on Sundays or Dutch public holidays."),
        ("h", "Delivery times"),
        ("p", "Netherlands, Belgium and Luxembourg: 1 to 2 working days. Germany, Austria and France: 2 to 4 working days. All other EU countries: 4 to 7 working days. Delivery to islands can take 1 to 3 additional working days."),
        ("h", "Costs"),
        ("p", "Netherlands, Belgium and Germany: EUR 4.90, free from EUR 60. Austria, France and Luxembourg: EUR 6.90, free from EUR 80. All other EU countries: EUR 9.90, free from EUR 120."),
        ("h", "Carriers and tracking"),
        ("p", "PostNL delivers to the Netherlands and Belgium, DHL to Germany, Austria and Luxembourg, DPD to France and all other EU countries. A tracking link is sent when your order is dispatched. Tracking data can take up to 24 hours to appear."),
        ("h", "Failed delivery and lost parcels"),
        ("p", "After a failed delivery the carrier tries again on the next working day and then holds the parcel at a pickup point for 7 days. If a parcel has not arrived 5 working days after the latest estimated delivery date, contact us; we start a trace with the carrier, which takes up to 7 working days, and then send a replacement or refund."),
        ("h", "Countries"),
        ("p", "We deliver to the 27 EU member states. We do not deliver to the United Kingdom, Switzerland, Norway or other countries outside the EU, and not to the Canary Islands, Madeira or the Azores."),
    ]),
    "privacy-notice.pdf": ("Privacy Notice", [
        ("h", "Controller"),
        ("p", "Nordvik Outdoor B.V., Keizersgracht 100, 1015 AA Amsterdam, the Netherlands. Data protection contact: privacy@nordvik-outdoor.example."),
        ("h", "Which data we process and why"),
        ("p", "Order and account data (name, address, email, order history) to perform the contract. Payment data is processed by our payment providers. Support conversations, including conversations with our AI assistant, to answer your requests. Newsletter data only with your consent (double opt-in)."),
        ("h", "Retention"),
        ("p", "Invoices and accounting records are kept as long as tax law requires, up to 10 years. Account data is deleted within 30 days after you delete your account. Chat conversations with the AI assistant are deleted automatically after 90 days. Newsletter data is deleted when you unsubscribe."),
        ("h", "Processors and location"),
        ("p", "We use hosting in the European Union, a payment provider, the carriers PostNL, DHL and DPD, a helpdesk provider, and an AI language model provider that processes chat messages on our behalf under a data processing agreement. Personal data is stored in the EU."),
        ("h", "Your rights"),
        ("p", "You have the right of access, rectification, erasure, restriction, data portability and objection. Write to privacy@nordvik-outdoor.example; we respond within 30 days. You may also lodge a complaint with the Dutch Data Protection Authority (Autoriteit Persoonsgegevens)."),
    ]),
}

SIZE_TABLES = {
    "size-chart-footwear.csv": (
        ["EU", "UK", "US men", "US women", "Foot length cm"],
        [(36, 3.5, 4.5, 5.5, 22.5), (37, 4, 5, 6, 23.2), (38, 5, 6, 7, 23.8), (39, 6, 7, 8, 24.5), (40, 6.5, 7.5, 8.5, 25.2), (41, 7.5, 8.5, 9.5, 25.8),
         (42, 8, 9, 10, 26.5), (43, 9, 10, 11, 27.2), (44, 9.5, 10.5, 11.5, 27.8), (45, 10.5, 11.5, 12.5, 28.5), (46, 11, 12, 13, 29.2), (47, 12, 13, 14, 29.8)],
    ),
    "size-chart-clothing-men.csv": (
        ["Size", "Chest cm", "Waist cm", "Hip cm"],
        [("XS", "84-88", "72-76", "88-92"), ("S", "88-96", "76-84", "92-100"), ("M", "96-104", "84-92", "100-108"), ("L", "104-112", "92-100", "108-116"),
         ("XL", "112-120", "100-108", "116-124"), ("XXL", "120-128", "108-116", "124-132"), ("3XL", "128-136", "116-124", "132-140")],
    ),
    "size-chart-clothing-women.csv": (
        ["Size", "Chest cm", "Waist cm", "Hip cm"],
        [("XS", "80-84", "62-66", "86-90"), ("S", "84-88", "66-70", "90-94"), ("M", "88-94", "70-76", "94-100"), ("L", "94-100", "76-82", "100-106"), ("XL", "100-106", "82-88", "106-112")],
    ),
    "size-chart-kids.csv": (
        ["Size", "Height cm", "Approx age", "Kids shoe EU", "Kids foot length cm"],
        [(92, "87-92", "2 years", "EU 28", 17.2), (104, "99-104", "4 years", "EU 29-30", 17.8), (116, "111-116", "6 years", "EU 31-32", 19.2),
         (128, "123-128", "8 years", "EU 33", 20.5), (140, "135-140", "10 years", "EU 34", 21.2), (152, "147-152", "12 years", "EU 35", 21.8)],
    ),
}


def build_docx(path: Path) -> None:
    import docx

    d = docx.Document()
    d.core_properties.title = "Nordvik Outdoor product care guide"
    d.add_heading("Product care guide", level=1)
    sections = [
        ("Waterproof jackets", "Wash at 30 degrees with mild liquid detergent, no fabric softener. Tumble dry on low heat for 20 minutes to reactivate the water-repellent finish. Re-proof about every 20 washes with a PFC-free product."),
        ("Down jackets", "Wash at 30 degrees with special down detergent on a gentle cycle and an extra rinse. Dry in the tumble dryer on low heat with 2 or 3 tennis balls, shaking the jacket every 30 minutes. Make sure it is completely dry before storing."),
        ("Hiking boots", "Brush off dirt, rinse with lukewarm water, dry at room temperature and never on a radiator. Wax leather boots every 2 to 3 months."),
        ("Merino wool", "Wash at 30 degrees on the wool cycle, inside out. Lay flat to dry. Do not tumble dry."),
        ("Backpacks", "Spot clean with lukewarm water and mild soap. Never machine wash. Air dry open and away from the sun."),
    ]
    for head, text in sections:
        d.add_heading(head, level=2)
        d.add_paragraph(text)
    table = d.add_table(rows=1, cols=3)
    for cell, label in zip(table.rows[0].cells, ["Item", "Max temperature", "Tumble dryer"], strict=True):
        cell.text = label
    for row in [("Waterproof jacket", "30 C", "Low heat, 20 min"), ("Down jacket", "30 C", "Low heat with tennis balls"), ("Merino", "30 C", "No"), ("Fleece", "30 C", "Low heat"), ("Kids fleece", "40 C", "Low heat")]:
        cells = table.add_row().cells
        for c, t in zip(cells, row, strict=True):
            c.text = t
    d.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.iterdir():
        if old.is_file():
            old.unlink()
    n = 0
    for lang, articles in (("en", EN), ("de", DE), ("nl", NL)):
        for slug, (title, section, blocks) in articles.items():
            (OUT / f"{lang}-{slug}.html").write_text(render_html(lang, title, section, blocks), encoding="utf-8")
            n += 1
    for name, (title, items) in PDFS.items():
        (OUT / name).write_bytes(make_pdf(title, items))
    for name, (header, rows) in SIZE_TABLES.items():
        with (OUT / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    build_docx(OUT / "product-care-guide.docx")
    print(f"{n} HTML articles, {len(PDFS)} PDFs, {len(SIZE_TABLES)} CSV tables, 1 DOCX -> {OUT}")


if __name__ == "__main__":
    main()
