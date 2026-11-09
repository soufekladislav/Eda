"""Czech invoice PDF from an immutable invoice snapshot; files stay in Eda instance/."""
import os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape


INVOICE_LOGIC_VERSION = "0.7.9"


class InvoiceDataError(ValueError):
    pass


def money(value):
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise InvoiceDataError("Neplatná částka na objednávce.") from exc


def _money_text(value):
    return f"{money(value):,.2f}".replace(",", " ").replace(".", ",")


def _rows(data):
    rows = []
    for field, title in (("items", "Zboží"), ("shipping", "Doprava"), ("fees", "Poplatek")):
        for item in (data.get(field) or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or title).strip()
            qty = money(item.get("quantity") or 1) if field == "items" else Decimal("1")
            line = money(item.get("total")) + money(item.get("total_tax"))
            rows.append((name, qty, line))
    return rows


def verified_affiliate_credit(data):
    """Fail closed for any fresh-credit coupon, unless Fresh Core verified wallet DB."""
    coupons = data.get("coupons") or []
    credit_coupons = [c for c in coupons if isinstance(c, dict)
                      and str(c.get("code") or "").lower().startswith("fresh-credit-")]
    proof = data.get("affiliate_credit_accounting")
    if not credit_coupons:
        if isinstance(proof, dict) and proof.get("present"):
            raise InvoiceDataError("Objednávka má metadata kreditu bez odpovídajícího kupónu.")
        return Decimal("0.00")
    if len(credit_coupons) != 1 or not isinstance(proof, dict) or not proof.get("present") or proof.get("verified") is not True:
        raise InvoiceDataError("Provizní kredit není ověřen proti transakci Fresh Affiliate.")
    status = str(proof.get("status") or "")
    if status not in {"reserved", "completed"} or (data.get("is_paid") and status != "completed"):
        raise InvoiceDataError("Stav provizní transakce neodpovídá stavu platby.")
    amount = money(proof.get("amount"))
    raw_coupon = money(credit_coupons[0].get("discount")) + money(credit_coupons[0].get("discount_tax"))
    if amount <= 0 or abs(amount - raw_coupon) > Decimal("0.02"):
        raise InvoiceDataError("Nesouhlasí výše kupónu a ověřený provizní kredit.")
    if not str(proof.get("wallet_transaction_id") or "").isdigit():
        raise InvoiceDataError("Chybí ID ověřené transakce provizního kreditu.")
    return amount


def commercial_total(data):
    """Woo total is final payment amount; verified credit is separate settlement."""
    return money(data.get("total")) + verified_affiliate_credit(data)


def woo_order_reconciliation(data):
    """Read-only difference between WooCommerce order total and stored order-item totals.

    WooCommerce is authoritative for e-shop pricing. Some plugins store a discount
    in coupon/order totals without distributing it to every line. An invoice must
    disclose that amount, not block on an independent reconstruction of pricing.
    """
    line_total = sum((row[2] for row in _rows(data)), Decimal("0.00"))
    order_total = money(data.get("total"))
    return (order_total - line_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def validate_order(data, nonvat=True):
    """Validate provenance, customer, settlement and existence of invoice lines.

    E-commerce prices and discounts are never recalculated. A mismatch between
    line sums and the Woo order total becomes a visible reconciliation line.
    Full source evidence stays in the immutable encrypted invoice snapshot.
    """
    if not isinstance(data, dict) or not isinstance(data.get("billing"), dict):
        raise InvoiceDataError("Chybí bezpečně načtené fakturační údaje objednávky.")
    if data.get("refunds"):
        raise InvoiceDataError("Objednávka obsahuje refundaci; fakturu vystav samostatným postupem.")
    if str(data.get("status") or "").lower() in {"cancelled", "refunded", "failed", "trash"}:
        raise InvoiceDataError("K této stornované nebo vrácené objednávce nelze vystavit běžnou fakturu.")
    if str(data.get("currency") or "CZK") != "CZK":
        raise InvoiceDataError("První verze podporuje pouze faktury v CZK.")
    credit = verified_affiliate_credit(data)
    rows = _rows(data)
    order_total = money(data.get("total"))
    if not rows or order_total < 0:
        raise InvoiceDataError("Objednávka neobsahuje použitelné položky nebo má záporný součet.")
    # Only verify provenance and amounts; the product list never contains
    # fake credit/rounding lines. WooCommerce supplied totals are authoritative.
    if not data["billing"].get("company") and not (data["billing"].get("first_name") or data["billing"].get("last_name")):
        raise InvoiceDataError("Chybí jméno nebo firma odběratele.")
    if not data["billing"].get("address_1") or not data["billing"].get("city"):
        raise InvoiceDataError("Chybí adresa odběratele.")
    if credit > money(data.get("discount_total")) + money(data.get("discount_tax")) + Decimal("0.02"):
        raise InvoiceDataError("Provizní kredit přesahuje evidované WooCommerce slevy.")
    return rows


def invoice_breakdown(data, *, paid_advance_amount=Decimal("0"), is_paid=False):
    """Display-only allocation of the immutable WooCommerce accounting snapshot.

    Nothing here applies coupons or recalculates the store's selling prices.
    Credit is payment settlement, never a sale item or a regular discount.
    """
    validate_order(data)
    credit = verified_affiliate_credit(data)
    discount = money(data.get("discount_total")) + money(data.get("discount_tax"))
    ordinary = discount - credit
    if ordinary < Decimal("-0.02"):
        raise InvoiceDataError("Nesouhlasí provizní kredit a WooCommerce slevy.")
    ordinary = max(ordinary, Decimal("0"))
    payment = money(data.get("total"))
    sale = payment + credit
    before = sale + ordinary
    advance = money(paid_advance_amount)
    if advance < 0 or advance > payment:
        raise InvoiceDataError("Započtená záloha převyšuje bankovní doplatek objednávky.")
    # Post-credit WooCommerce amounts are sums for display, not a price engine.
    rows = _rows(data)
    components = sum((row[2] for row in rows), Decimal("0"))
    discrepancy = before - sum((money(x.get("subtotal")) + money(x.get("subtotal_tax")) for x in data.get("items") or [] if isinstance(x,dict)), Decimal("0")) - sum((money(x.get("total")) + money(x.get("total_tax")) for part in ("shipping", "fees") for x in data.get(part) or [] if isinstance(x,dict)), Decimal("0"))
    # Explicitly flag unexplained differences: do not pretend they are goods.
    return {
        "ordinary_discount": ordinary, "credit": credit, "payment": payment,
        "sale": sale, "before": before, "advance": advance,
        "bank_payment": payment - advance if is_paid else Decimal("0"),
        "remaining": Decimal("0") if is_paid else payment - advance,
        "discrepancy": discrepancy,
    }


def woocommerce_tax_audit(data):
    """Minimal, non-PII diagnostic of Woo tax amounts in the original order.

    Used only for private audit/UI. Never appears as a VAT breakdown on a PDF
    issued by a non-VAT-registered seller.
    """
    gross = sum((money(row.get("total")) + money(row.get("total_tax"))
                 for part in ("items", "shipping", "fees")
                 for row in (data.get(part) or []) if isinstance(row, dict)), Decimal("0"))
    return {
        "woocommerce_tax_recorded": any(
            money(row.get("total_tax")) != 0
            for part in ("items", "shipping", "fees")
            for row in (data.get(part) or []) if isinstance(row, dict)
        ) or money(data.get("total_tax")) != 0,
        "woocommerce_order_tax": str(money(data.get("total_tax"))),
        "order_total": str(money(data.get("total"))),
        "gross_line_sum": str(money(gross)),
        "woo_reconciliation": str(woo_order_reconciliation(data)),
        "affiliate_credit": str(verified_affiliate_credit(data)),
        "commercial_total": str(commercial_total(data)),
        "invoice_vat_mode": "nonvat",
    }


def _find_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    supplied = os.getenv("EDA_INVOICE_FONT_PATH", "").strip()
    paths = [supplied] if supplied else []
    paths += ["/home2/lauhannq/finance.freshcrackers.cz/fonts/Poppins-Regular.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    for p in paths:
        if p and Path(p).is_file():
            pdfmetrics.registerFont(TTFont("EdaInvoiceCZ", p))
            return "EdaInvoiceCZ"
    raise InvoiceDataError("Na serveru Edy chybí font pro české PDF. Nastav EDA_INVOICE_FONT_PATH na existující český TTF font.")


def generate_invoice_pdf(path, issuer, data, invoice_number, issued_on, due_on, *, is_paid, kind="final", paid_advance_amount=Decimal("0")):
    """Readable Czech non-VAT invoice, ABRA-inspired two-column layout (not a pixel copy).

    The immutable WooCommerce snapshot is authoritative. Discounts are already
    reflected in WC line totals; never subtract a coupon twice.
    """
    if kind not in {"final", "advance"}:
        raise InvoiceDataError("Neznámý typ dokladu.")
    if paid_advance_amount < 0 or paid_advance_amount > commercial_total(data):
        raise InvoiceDataError("Započtená záloha je mimo hodnotu objednávky.")
    from datetime import date as _date
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    rows = validate_order(data, nonvat=True)
    breakdown = invoice_breakdown(data, paid_advance_amount=paid_advance_amount, is_paid=is_paid)
    credit_amount = breakdown["credit"]
    regular = _find_font()
    bold = regular
    try:
        from reportlab.pdfbase.ttfonts import TTFont
        bold_path = os.getenv('EDA_INVOICE_BOLD_FONT_PATH', '').strip()
        if bold_path and Path(bold_path).is_file():
            pdfmetrics.registerFont(TTFont('EdaInvoiceCZBold', bold_path))
            bold = 'EdaInvoiceCZBold'
        elif os.getenv('EDA_INVOICE_FONT_PATH', '').endswith('Poppins-Regular.ttf'):
            alt = Path(os.getenv('EDA_INVOICE_FONT_PATH')).with_name('Poppins-SemiBold.ttf')
            if alt.is_file():
                pdfmetrics.registerFont(TTFont('EdaInvoiceCZBold', str(alt)))
                bold = 'EdaInvoiceCZBold'
    except Exception:
        pass
    W, H = A4
    c = canvas.Canvas(str(Path(path)), pagesize=A4, pageCompression=1)
    c.setTitle(('Zálohová výzva ' if kind == 'advance' else 'Faktura ') + str(invoice_number))
    black = colors.HexColor('#202124')
    grey = colors.HexColor('#5F6368')
    line_color = colors.HexColor('#DCE1E5')
    accent = colors.HexColor('#A5C900')
    light = colors.HexColor('#F4F6F7')
    L, R = 40, W - 40
    text_width = R - L

    def fmt_date(obj):
        return _date.fromisoformat(str(obj)).strftime('%d.%m.%Y')

    def txt(x, y, s, size=9, face=None, color=None, align='left'):
        c.setFont(face or regular, size)
        c.setFillColor(color or black)
        value = str(s or '')
        if align == 'right': c.drawRightString(x, y, value)
        elif align == 'center': c.drawCentredString(x, y, value)
        else: c.drawString(x, y, value)

    def wrapped(x, y, words, maxw, size=9, leading=12, face=None, color=None, maxlines=10):
        face = face or regular
        result = []
        for para in str(words or '').split('\n'):
            current = ''
            for word in para.split():
                candidate = (current + ' ' + word).strip()
                if current and pdfmetrics.stringWidth(candidate, face, size) > maxw:
                    result.append(current); current = word
                else: current = candidate
            result.append(current)
        for line in result[:maxlines]:
            txt(x, y, line, size, face, color); y -= leading
        return y

    def hr(y):
        c.setStrokeColor(line_color); c.setLineWidth(.7); c.line(L, y, R, y)

    def header():
        c.setFillColor(accent); c.rect(0, H-7, W, 7, fill=1, stroke=0)
        txt(L, H-43, 'ZÁLOHOVÁ VÝZVA' if kind == 'advance' else 'FAKTURA', 19, bold)
        txt(R, H-38, invoice_number, 12, bold, align='right')
        txt(R, H-53, 'Není daňovým dokladem' if kind == 'advance' else 'Nejsme plátci DPH', 8, color=grey, align='right')
        # Supplier and customer panels
        top = H-69
        bottom = H-225
        mid = 303
        c.setStrokeColor(line_color)
        c.roundRect(L, bottom, mid-L-6, top-bottom, 6, fill=0, stroke=1)
        c.roundRect(mid+6, bottom, R-mid-6, top-bottom, 6, fill=0, stroke=1)
        txt(L+12, top-19, 'DODAVATEL', 8, bold, grey)
        txt(mid+18, top-19, 'ODBĚRATEL', 8, bold, grey)
        logo_path = issuer.get('logo_path')
        company_x = L+13
        if logo_path and Path(logo_path).is_file():
            try:
                from reportlab.lib.utils import ImageReader
                im = ImageReader(str(logo_path))
                iw, ih = im.getSize()
                ratio = min(67/max(iw,1), 60/max(ih,1))
                c.drawImage(im, L+12, top-94, iw*ratio, ih*ratio, mask='auto', preserveAspectRatio=True)
                company_x = L+83
            except Exception: pass
        company_max = mid - company_x - 15
        y = wrapped(company_x, top-43, issuer.get('company_name'), company_max, 10, 14, bold, maxlines=3)
        y = wrapped(company_x, y-3, issuer.get('address'), company_max, 8.3, 12, maxlines=5)
        txt(L+12, bottom+33, 'IČO: ' + str(issuer.get('ico') or ''), 8.4)
        if issuer.get('dic'):
            txt(L+12, bottom+20, 'DIČ: ' + str(issuer['dic']), 8.4)
        else:
            txt(L+12, bottom+20, 'Nejsme plátci DPH.', 8.4, color=grey)
        billing = data['billing']
        name = billing.get('company') or (' '.join(filter(None, [billing.get('first_name'), billing.get('last_name')]))).strip()
        x = mid+18; width = R-x-10
        by = wrapped(x, top-43, name, width, 10, 14, bold, maxlines=3)
        by = wrapped(x, by-2, ' '.join(filter(None, [billing.get('address_1'), billing.get('address_2')])), width, 8.4, 12, maxlines=3)
        by = wrapped(x, by, ' '.join(filter(None, [billing.get('postcode'), billing.get('city'), billing.get('country')])), width, 8.4, 12, maxlines=3)
        if billing.get('ico'): txt(x, bottom+33, 'IČO: ' + str(billing['ico']), 8.4)
        if billing.get('dic'): txt(x, bottom+20, 'DIČ: ' + str(billing['dic']), 8.4)
        # Payment box across full width
        btop = bottom-13
        bbot = H-358
        c.setFillColor(light); c.roundRect(L, bbot, R-L, btop-bbot, 6, stroke=0, fill=1)
        txt(L+12, btop-18, 'PLATEBNÍ ÚDAJE', 8, bold, grey)
        left_lines = [
            'Účet: ' + str(issuer.get('bank_account') or ''),
            'IBAN: ' + str(issuer.get('iban') or ''),
            'BIC: ' + str(issuer.get('swift') or ''),
            'Variabilní symbol: ' + str(data.get('number') or data.get('id') or '')
        ]
        for idx, value in enumerate(left_lines): txt(L+12, btop-36-15*idx, value, 8.6)
        meta_x = mid+5
        txt(meta_x, btop-37, 'Vystaveno: ' + fmt_date(issued_on), 8.5)
        txt(meta_x, btop-53, 'Splatnost: ' + fmt_date(due_on), 8.5)
        txt(meta_x, btop-69, 'Platba: ' + str(data.get('payment_method_title') or 'Dle objednávky')[:32], 8.3)
        txt(meta_x, btop-85, 'Stav: ' + ('K ÚHRADĚ' if kind == 'advance' else ('UHRAZENO' if is_paid else 'K ÚHRADĚ')), 8.5, bold)
        # Compact SPAYD QR on proforma only: avoid producing QR for an already-paid invoice.
        if kind == 'advance' or (kind == 'final' and not is_paid):
            iban = str(issuer.get('iban') or '').replace(' ','').upper()
            if iban.startswith('CZ') and len(iban) == 24:
                try:
                    import qrcode
                    from reportlab.lib.utils import ImageReader
                    vs = str(data.get('number') or data.get('id') or '')
                    msg = 'SPD*1.0*ACC:' + iban + '*AM:' + format(breakdown['payment'] if kind == 'advance' else breakdown['remaining'], '.2f') + '*CC:CZK'
                    if vs.isdigit() and len(vs) <= 10: msg += '*X-VS:' + vs
                    qr = qrcode.QRCode(box_size=2, border=1)
                    qr.add_data(msg); qr.make(fit=True)
                    c.drawImage(ImageReader(qr.make_image(fill_color='black', back_color='white').convert('RGB')),
                                R-75, bbot+20, 62, 62)
                except ImportError:
                    pass
        return bbot-24

    def section_heading(y, title):
        if y < 165:
            c.showPage()
            c.setFillColor(accent); c.rect(0, H-7, W, 7, fill=1, stroke=0)
            txt(L,H-40,invoice_number+' – pokračování',11,bold)
            y=H-70
        c.setFillColor(light); c.roundRect(L,y-16,text_width,28,4,fill=1,stroke=0)
        txt(L+9,y-4,title,9,bold)
        return y-30

    def product_heading(y):
        txt(L+7,y,'Položka',8,bold)
        txt(R-145,y,'Množ.',8,bold,align='right')
        txt(R-72,y,'Cena/ks',8,bold,align='right')
        txt(R-7,y,'Celkem',8,bold,align='right')
        return y-21

    def product_row(y, name, qty, value):
        width=text_width-211
        words=str(name).split()
        lines=[]; current=''
        for word in words:
            candidate=(current+' '+word).strip()
            if current and pdfmetrics.stringWidth(candidate,regular,8.0)>width:
                lines.append(current);current=word
            else: current=candidate
        lines.append(current)
        h=max(23,len(lines)*12+9)
        if y-h < 158:
            c.showPage(); c.setFillColor(accent);c.rect(0,H-7,W,7,fill=1,stroke=0)
            txt(L,H-40,invoice_number+' – pokračování',11,bold)
            y=H-70
            y=product_heading(y)
        hr(y+7)
        for i,line in enumerate(lines): txt(L+7,y-i*12,line,8)
        unit=(value/qty).quantize(Decimal('.01'),rounding=ROUND_HALF_UP) if qty else value
        txt(R-145,y,format(qty.normalize(),'f'),8,align='right')
        txt(R-72,y,_money_text(unit),8,align='right')
        txt(R-7,y,_money_text(value),8,bold,align='right')
        return y-h

    def summary_row(y, label, amount, bold_row=False):
        if y<93:
            c.showPage();c.setFillColor(accent);c.rect(0,H-7,W,7,fill=1,stroke=0)
            txt(L,H-40,invoice_number+' – pokračování',11,bold)
            y=H-75
        txt(L+9,y,label,8.6,bold if bold_row else regular)
        txt(R-9,y,_money_text(amount)+' Kč',9.1,bold if bold_row else regular,align='right')
        return y-19

    y=header()
    y=section_heading(y,'ZBOŽÍ')
    y=product_heading(y)
    for item in data.get('items') or []:
        if not isinstance(item,dict):continue
        qty=money(item.get('quantity') or 1)
        # Product rows show original WC subtotal, before regular discounts and
        # credit; do not reconstruct it from a unit catalog price.
        subtotal=money(item.get('subtotal'))+money(item.get('subtotal_tax'))
        y=product_row(y,item.get('name') or 'Zboží',qty,subtotal)
    extra=[]
    for group,label in (('shipping','DOPRAVA'),('fees','POPLATKY')):
        group_rows=[r for r in data.get(group) or [] if isinstance(r,dict)]
        if group_rows:
            y-=7; y=section_heading(y,label)
            y=product_heading(y)
            for item in group_rows:
                y=product_row(y,item.get('name') or label,Decimal('1'),money(item.get('total'))+money(item.get('total_tax')))
    y-=8
    y=section_heading(y,'SLEVY A VYPOŘÁDÁNÍ')
    y=summary_row(y,'Běžné obchodní slevy (WooCommerce)',-breakdown['ordinary_discount'])
    if breakdown['credit']:
        y=summary_row(y,'Provizní kredit Fresh Affiliate – úhrada',-breakdown['credit'])
    if breakdown['discrepancy']:
        # WooCommerce is authoritative; display discrepancy without inventing
        # an invoice product. A mismatch above 2 haléře warrants review.
        y=summary_row(y,'Nevysvětlený rozdíl vůči WooCommerce',breakdown['discrepancy'])
    y-=7
    if y < 179:
        c.showPage(); c.setFillColor(accent); c.rect(0,H-7,W,7,fill=1,stroke=0)
        txt(L,H-40,invoice_number+' – pokračování',11,bold)
        y=H-70
    y=section_heading(y,'REKAPITULACE OBJEDNÁVKY')
    # Two compact columns keep ordinary one/two-item invoices on one page.
    leftx=L+9; midx=L+260; rightx=R-8
    def mini_label(x, yy, label, amount, dest, face=None):
        txt(x,yy,label,8.2,face or regular)
        txt(dest,yy,_money_text(amount)+' Kč',8.3,face or regular,align='right')
    ly=y
    mini_label(leftx,ly,'Cena před slevami',breakdown['before'],midx-12);ly-=18
    mini_label(leftx,ly,'Obchodní slevy',-breakdown['ordinary_discount'],midx-12);ly-=18
    mini_label(leftx,ly,'Cena po slevách',breakdown['sale'],midx-12,bold);ly-=18
    mini_label(leftx,ly,'Provizní kredit',-breakdown['credit'],midx-12);ly-=18
    mini_label(leftx,ly,'K úhradě dle Woo',breakdown['payment'],midx-12,bold)
    ry=y
    if kind == 'final':
        if breakdown['advance']:
            mini_label(midx,ry,'Zaplacená záloha',-breakdown['advance'],rightx);ry-=18
        if is_paid:
            mini_label(midx,ry,'Bankou / bránou uhrazeno',-breakdown['bank_payment'],rightx);ry-=18
        mini_label(midx,ry,'Zbývá uhradit',breakdown['remaining'],rightx,bold)
    else:
        mini_label(midx,ry,'Zbývá uhradit',breakdown['payment'],rightx,bold)
    y=min(ly,ry)-24
    if y<52:
        c.showPage();y=H-72
    txt(L,y,'Nejsme plátci DPH.' if kind=='final' else 'Zálohová výzva není fakturou ani daňovým dokladem.',8.2,bold)
    txt(L,39,'Doklad vytvořen systémem Eda · Fresh Crackers',7.2,color=grey)
    txt(R,39,'Strana '+str(c.getPageNumber()),7.2,color=grey,align='right')
    c.save()
