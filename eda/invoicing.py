"""Eda-native invoice issuing, private archival and token-based PDF downloads."""
import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for, make_response
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .helpers import current_organization
from .models import (Membership, OrganizationProfile, Shop, Order, Invoice,
    OrderAccountingSnapshot, ShopAccountingConnection, InvoiceIssuerSettings,
    InvoiceSequence, ShopInvoicePublisher, EdaInvoiceDocument, EdaAdvanceDocument, EdaAdvanceSequence, AuditLog)
from .services.secrets import encrypt_text, decrypt_text
from .services.eda_invoice_pdf import generate_invoice_pdf, validate_order, woocommerce_tax_audit, InvoiceDataError, money, INVOICE_LOGIC_VERSION, commercial_total, verified_affiliate_credit, woo_order_reconciliation
from .services.fresh_core_accounting import FreshCoreAccountingClient, FreshCoreAccountingError

invoice_bp = Blueprint('eda_invoicing', __name__)


def _company_logo_storage():
    """Private branding outside the deployable application directory."""
    return Path(current_app.root_path).resolve().parent.parent / 'eda-private-branding'


def _company_logo_path(org_id):
    """Locate persistent logo, and migrate from legacy application storage."""
    import shutil
    filename = 'company-' + str(int(org_id)) + '.png'
    persistent = _company_logo_storage() / filename
    if persistent.is_file():
        return persistent
    legacy = Path(current_app.instance_path) / 'invoices' / 'branding' / filename
    if legacy.is_file():
        try:
            persistent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(legacy, persistent)
            persistent.chmod(0o600)
            return persistent
        except OSError:
            return legacy
    return None


@invoice_bp.get('/shops/<int:shop_id>/invoice-preview')
@login_required
def invoice_preview(shop_id):
    """Sample document only; does not consume an invoice number or save an accounting record."""
    org = _org()
    Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    issuer = InvoiceIssuerSettings.query.filter_by(organization_id=org.id).first()
    if not issuer or issuer.vat_mode != 'nonvat':
        abort(409, 'Nejdřív zvol režim neplátce DPH.')
    from tempfile import NamedTemporaryFile
    from datetime import date, timedelta
    sample = {
      'id': 12345, 'number': '12345', 'status': 'processing', 'currency': 'CZK',
      'total': '350.00', 'payment_method_title': 'Bankovní převod',
      'billing': {'company': 'Ukázková společnost s.r.o.', 'first_name': 'Jan',
                  'last_name': 'Novák', 'address_1': 'Ukázková 123', 'city': 'Praha',
                  'postcode': '110 00', 'country': 'CZ', 'ico': '12345678'},
      'items': [{'name': 'Ukázkový sportovní produkt', 'quantity': 1,
                 'total': '350.00', 'total_tax': '0.00'}],
      'shipping': [], 'fees': [], 'coupons': [], 'refunds': [],
    }
    from .services.eda_invoice_pdf import generate_invoice_pdf
    with NamedTemporaryFile(suffix='.pdf') as tmp:
        generate_invoice_pdf(tmp.name, dict(company_name=issuer.company_name, ico=issuer.ico,
            dic=issuer.dic, address=issuer.address, bank_account=issuer.bank_account,
            iban=issuer.iban, swift=issuer.swift, **_issuer_logo_dict(issuer)),
            sample, 'NÁHLED-2026-0000', date.today(), date.today() + timedelta(days=14),
            is_paid=False, kind='advance')
        tmp.seek(0)
        data = tmp.read()
    response = make_response(data)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = 'inline; filename="Náhled-zálohové-výzvy.pdf"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@invoice_bp.get('/shops/<int:shop_id>/invoice-logo')
@login_required
def company_logo(shop_id):
    org = _org()
    _shop_order_logo_check = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    logo = _company_logo_path(org.id)
    if logo is None:
        abort(404)
    response = send_file(str(logo), mimetype='image/png')
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def _issuer_logo_dict(issuer):
    return {'logo_path': str(_company_logo_path(issuer.organization_id)) if _company_logo_path(issuer.organization_id) else None}



def _org():
    org = current_organization()
    if not org:
        abort(403)
    membership = Membership.query.filter_by(organization_id=org.id, user_id=current_user.id).first()
    if not current_user.is_admin and (not membership or membership.role not in {'owner', 'admin'}):
        abort(403)
    return org


def _shop_order(org, shop_id, order_id):
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    order = Order.query.filter_by(id=order_id, shop_id=shop.id, organization_id=org.id).first_or_404()
    return shop, order


def _public_url(token, *, advance=False):
    base = (current_app.config.get('APP_URL') or '').rstrip('/')
    if not base:
        return url_for('eda_invoicing.download_advance' if advance else 'eda_invoicing.download_public', token=token, _external=True)
    if not base.startswith('https://'):
        raise InvoiceDataError('APP_URL musí být https://finance.freshcrackers.cz pro bezpečné odkazy faktur.')
    return base + url_for('eda_invoicing.download_advance' if advance else 'eda_invoicing.download_public', token=token)


def _invoice_file(doc):
    return Path(current_app.instance_path) / 'invoices' / str(doc.organization_id) / doc.pdf_file


def _public_doc_url(doc):
    return _public_url(decrypt_text(doc.encrypted_token), advance=isinstance(doc,EdaAdvanceDocument))


def _publish(doc, shop, order):
    publisher = ShopInvoicePublisher.query.filter_by(shop_id=shop.id, organization_id=shop.organization_id).first()
    connection = ShopAccountingConnection.query.filter_by(shop_id=shop.id, organization_id=shop.organization_id).first()
    if doc.revoked:
        raise RuntimeError('Zneplatněný odkaz nelze znovu publikovat.')
    if not publisher or not publisher.encrypted_write_key or not connection or not connection.orders_endpoint:
        raise RuntimeError('Chybí samostatný Invoice write key nebo Accounting API v nastavení e-shopu.')
    url = connection.orders_endpoint.rstrip('/') + '/' + str(order.external_id) + '/invoice-link'
    if not url.startswith('https://') or not (shop.site_url or '').startswith('https://'):
        raise RuntimeError('Zápis faktury vyžaduje HTTPS adresu e-shopu.')
    # Do not let a misconfigured endpoint send the write key to an unrelated server.
    from urllib.parse import urlparse
    if urlparse(url).hostname != urlparse(shop.site_url).hostname:
        raise RuntimeError('Doména Accounting API nesouhlasí s doménou e-shopu.')
    payload = {'number': doc.invoice.number, 'url': _public_doc_url(doc),
               'invoice_id': str(doc.invoice.id), 'total': str(doc.invoice.total),
               'currency': doc.invoice.currency, 'issued_on': doc.invoice.issued_on.isoformat()}
    resp = requests.post(url, json=payload, timeout=25, allow_redirects=False, headers={
        'X-Fresh-Core-Invoice-Write-Key': decrypt_text(publisher.encrypted_write_key),
        'Accept': 'application/json', 'User-Agent': 'Eda-Invoice/0.7.1'})
    if resp.status_code != 200:
        raise RuntimeError(f'Fresh Core publikace vrátila HTTP {resp.status_code}: {resp.text[:180]}')
    doc.publish_status = 'published'
    doc.publish_error = None
    doc.published_at = datetime.utcnow()
    db.session.commit()


@invoice_bp.route('/faktura/<token>')
def download_public(token):
    # The URL token is a bearer credential. Store only its hash for lookups.
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,100}', token or ''):
        abort(404)
    digest = hashlib.sha256(token.encode('ascii')).hexdigest()
    doc = EdaInvoiceDocument.query.filter_by(token_hash=digest, revoked=False).first()
    if not doc or not _invoice_file(doc).is_file():
        abort(404)
    response = send_file(str(_invoice_file(doc)), mimetype='application/pdf', as_attachment=True,
                         download_name='Faktura-' + re.sub(r'[^A-Za-z0-9_-]', '-', doc.invoice.number) + '.pdf')
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@invoice_bp.route('/zaloha/<token>')
def download_advance(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,100}', token or ''):
        abort(404)
    digest = hashlib.sha256(token.encode('ascii')).hexdigest()
    doc = EdaAdvanceDocument.query.filter_by(token_hash=digest, revoked=False).first()
    if not doc or not _invoice_file(doc).is_file():
        abort(404)
    response = send_file(str(_invoice_file(doc)), mimetype='application/pdf', as_attachment=True,
                         download_name='Zaloha-'+re.sub(r'[^A-Za-z0-9_-]', '-',doc.number)+'.pdf')
    response.headers['Cache-Control']='private, no-store, max-age=0'
    response.headers['X-Robots-Tag']='noindex, nofollow, noarchive'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Content-Type-Options']='nosniff'
    return response


def _advance_publish(doc, shop, order):
    if doc.revoked:
        raise RuntimeError('Zneplatněný odkaz na zálohu nelze znovu publikovat.')
    publisher = ShopInvoicePublisher.query.filter_by(shop_id=shop.id, organization_id=shop.organization_id).first()
    connection = ShopAccountingConnection.query.filter_by(shop_id=shop.id, organization_id=shop.organization_id).first()
    if not publisher or not publisher.encrypted_write_key or not connection or not connection.orders_endpoint:
        raise RuntimeError('Chybí Invoice write key nebo Accounting API v nastavení e-shopu.')
    from urllib.parse import urlsplit
    url = connection.orders_endpoint.rstrip('/') + '/' + str(order.external_id) + '/advance-link'
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != urlsplit(shop.site_url or '').hostname:
        raise RuntimeError('Nesouhlasí doména Accounting API s doménou e-shopu.')
    resp = requests.post(url, json={'number':doc.number,'url':_public_doc_url(doc),
        'total':str(doc.amount),'currency':doc.currency,'issued_on':doc.issued_on.isoformat()},
        timeout=25, allow_redirects=False, headers={
        'X-Fresh-Core-Invoice-Write-Key':decrypt_text(publisher.encrypted_write_key),
        'Accept':'application/json','User-Agent':'Eda-Advance/0.7.2'})
    if resp.status_code != 200:
        raise RuntimeError(f'Fresh Core zveřejnění zálohy vrátilo HTTP {resp.status_code}: {resp.text[:160]}')
    doc.publish_status='published'; doc.publish_error=None; doc.published_at=datetime.utcnow()
    db.session.commit()


@invoice_bp.post('/shops/<int:shop_id>/orders/<int:order_id>/advance/issue-eda')
@login_required
def issue_advance(shop_id,order_id):
    if not current_app.config.get('EDA_INVOICING_READY',False):
        abort(503,'Chybí databázové tabulky fakturace.')
    org=_org()
    shop,order=_shop_order(org,shop_id,order_id)
    back=url_for('web.shop_order_detail',shop_id=shop.id,order_id=order.id)
    if EdaAdvanceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first():
        flash('Zálohová výzva již byla vystavena.','warning'); return redirect(back)
    issuer=InvoiceIssuerSettings.query.filter_by(organization_id=org.id).first()
    if not issuer or issuer.vat_mode != 'nonvat' or not all((issuer.company_name,issuer.ico,issuer.address,issuer.bank_account)):
        flash('Vyplň firemní údaje a ověř režim neplátce DPH v nastavení fakturace.','danger')
        return redirect(url_for('eda_invoicing.invoice_setup',shop_id=shop.id))
    filepath=None
    try:
        connection=ShopAccountingConnection.query.filter_by(shop_id=shop.id,organization_id=org.id).first()
        if not connection or not connection.encrypted_key:
            raise InvoiceDataError('Accounting API není připojené.')
        data=FreshCoreAccountingClient(connection).order_detail(order.external_id)
        if str(data.get('id')) != str(order.external_id) or abs(money(data.get('total'))-money(order.total))>Decimal('0.02'):
            raise InvoiceDataError('Údaje objednávky se změnily, znovu proveď synchronizaci.')
        if data.get('payment_method') != 'bacs' or data.get('status') not in {'pending','on-hold'} or data.get('is_paid'):
            raise InvoiceDataError('Zálohovou výzvu lze vystavit jen pro nezaplacený bankovní převod.')
        validate_order(data,nonvat=True)
        tax_audit = woocommerce_tax_audit(data)
        issued=datetime.now(ZoneInfo('Europe/Prague')).date()
        due=issued+timedelta(days=issuer.due_days)
        sequence=EdaAdvanceSequence.query.filter_by(organization_id=org.id,year=issued.year).with_for_update().first()
        if sequence is None:
            sequence=EdaAdvanceSequence(organization_id=org.id,year=issued.year,next_number=1)
            db.session.add(sequence); db.session.flush()
        serial=sequence.next_number; sequence.next_number+=1
        num=f'{issuer.series_prefix}-Z-{issued.year}-{serial:04d}'
        token=secrets.token_urlsafe(32)
        folder=Path(current_app.instance_path)/'invoices'/str(org.id)
        folder.mkdir(parents=True,exist_ok=True)
        # Random file name avoids exposure before the DB transaction is committed.
        name='advance-'+secrets.token_hex(16)+'.pdf'
        filepath=folder/name
        generate_invoice_pdf(filepath,dict(company_name=issuer.company_name,ico=issuer.ico,
            dic=issuer.dic,address=issuer.address,bank_account=issuer.bank_account,
            iban=issuer.iban,swift=issuer.swift, **_issuer_logo_dict(issuer)),data,num,issued,due,is_paid=False,kind='advance')
        doc=EdaAdvanceDocument(organization_id=org.id,shop_id=shop.id,order_id=order.id,
            number=num,amount=money(data.get('total')),currency=order.currency,issued_on=issued,
            pdf_file=name,token_hash=hashlib.sha256(token.encode()).hexdigest(),
            encrypted_token=encrypt_text(token),snapshot_encrypted=encrypt_text(json.dumps(data,ensure_ascii=False)))
        db.session.add(doc)
        db.session.add(AuditLog(organization_id=org.id,user_id=current_user.id,
            category='invoicing',action='advance_request_issued',entity_type='order',entity_id=str(order.id),
            detail_json=json.dumps({'number':num,'woocommerce_tax_audit':tax_audit},ensure_ascii=False)))
        db.session.commit()
    except (InvoiceDataError,FreshCoreAccountingError,ValueError,IntegrityError,OSError,ImportError,RuntimeError) as exc:
        db.session.rollback()
        if filepath is not None and filepath.exists(): filepath.unlink()
        flash('Zálohovou výzvu nelze vystavit: '+str(exc)[:400],'danger')
        return redirect(back)
    try:
        _advance_publish(doc,shop,order)
        flash(f'Zálohová výzva {num} byla uložena a zveřejněna v detailu objednávky.','success')
    except Exception as exc:
        db.session.rollback()
        doc=EdaAdvanceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first()
        doc.publish_status='error'; doc.publish_error=str(exc)[:500]
        db.session.commit()
        flash('Výzva byla vytvořena v Edovi, ale zveřejnění ve WordPressu se nepodařilo.','warning')
    return redirect(back)


@invoice_bp.post('/shops/<int:shop_id>/orders/<int:order_id>/advance/publish-eda')
@login_required
def republish_advance(shop_id,order_id):
    org=_org(); shop,order=_shop_order(org,shop_id,order_id)
    doc=EdaAdvanceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first_or_404()
    try:
        _advance_publish(doc,shop,order); flash('Zálohová výzva byla zveřejněna.','success')
    except Exception as exc:
        db.session.rollback(); doc.publish_status='error'; doc.publish_error=str(exc)[:500]; db.session.commit()
        flash('Zveřejnění selhalo, ověř Invoice write key a Fresh Core.','danger')
    return redirect(url_for('web.shop_order_detail',shop_id=shop.id,order_id=order.id))


@invoice_bp.post('/shops/<int:shop_id>/orders/<int:order_id>/advance/revoke-eda')
@login_required
def revoke_advance(shop_id,order_id):
    org=_org(); shop,order=_shop_order(org,shop_id,order_id)
    back=url_for('web.shop_order_detail',shop_id=shop.id,order_id=order.id)
    doc=EdaAdvanceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first_or_404()
    if doc.revoked:
        flash('Odkaz na zálohu už byl zneplatněn.','warning'); return redirect(back)
    connection=ShopAccountingConnection.query.filter_by(shop_id=shop.id,organization_id=org.id).first()
    publisher=ShopInvoicePublisher.query.filter_by(shop_id=shop.id,organization_id=org.id).first()
    if not connection or not connection.orders_endpoint or not publisher or not publisher.encrypted_write_key:
        flash('Chybí klíč pro předání změny do WooCommerce.','danger'); return redirect(back)
    from urllib.parse import urlsplit
    url=connection.orders_endpoint.rstrip('/')+'/'+str(order.external_id)+'/advance-link/revoke'
    if urlsplit(url).scheme!='https' or urlsplit(url).hostname != urlsplit(shop.site_url or '').hostname:
        flash('Nesouhlasí bezpečná doména účetního API.','danger'); return redirect(back)
    try:
        response=requests.post(url,json={'number':doc.number},timeout=25,allow_redirects=False,
            headers={'X-Fresh-Core-Invoice-Write-Key':decrypt_text(publisher.encrypted_write_key),
                     'User-Agent':'Eda-Advance/0.7.2','Accept':'application/json'})
        if response.status_code != 200:
            raise RuntimeError(f'Fresh Core HTTP {response.status_code}')
        doc.revoked=True
        db.session.add(AuditLog(organization_id=org.id,user_id=current_user.id,category='invoicing',
            action='advance_link_revoked',entity_type='order',entity_id=str(order.id)))
        db.session.commit()
        flash('Odkaz na zálohovou výzvu byl zneplatněn v Edovi i WooCommerce.','success')
    except Exception as exc:
        db.session.rollback()
        flash('Odkaz se nepodařilo zneplatnit: '+str(exc)[:200],'danger')
    return redirect(back)


@invoice_bp.route('/shops/<int:shop_id>/invoice-setup', methods=['GET', 'POST'])
@login_required
def invoice_setup(shop_id):
    if not current_app.config.get('EDA_INVOICING_READY', False):
        abort(503, 'Chybí tabulky fakturace Edy. Je nutná databázová migrace.')
    org = _org()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    from .models import EdaShopEventSettings
    profile = OrganizationProfile.query.filter_by(organization_id=org.id).first()
    event_settings = (EdaShopEventSettings.query.filter_by(shop_id=shop.id, organization_id=org.id).first()
                      if current_app.config.get("EDA_SHOP_EVENTS_READY", False) else None)
    config = InvoiceIssuerSettings.query.filter_by(organization_id=org.id).first()
    publisher = ShopInvoicePublisher.query.filter_by(shop_id=shop.id, organization_id=org.id).first()
    if request.method == 'POST':
        logo_file = request.files.get('company_logo')
        if logo_file and logo_file.filename:
            try:
                from PIL import Image
                import io
                raw = logo_file.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('Logo nesmí překročit 2 MB.')
                with Image.open(io.BytesIO(raw)) as src:
                    if src.format not in {'PNG', 'JPEG', 'WEBP'}:
                        raise ValueError('Povolené logo: PNG, JPG nebo WebP.')
                    src.load()
                    if src.width > 3000 or src.height > 3000:
                        raise ValueError('Logo má příliš velké rozměry.')
                    dst = src.convert('RGBA')
                    dst.thumbnail((800, 800))
                target = _company_logo_storage()
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                import os
                import tempfile
                fd, temp_name = tempfile.mkstemp(prefix='.logo-', suffix='.png', dir=str(target))
                os.close(fd)
                try:
                    dst.save(temp_name, 'PNG')
                    os.replace(temp_name, str(target / ('company-' + str(org.id) + '.png')))
                finally:
                    if os.path.exists(temp_name): os.unlink(temp_name)
            except (ImportError, ValueError, OSError) as exc:
                flash('Logo nebylo uloženo: ' + str(exc), 'danger')
                return redirect(url_for('eda_invoicing.invoice_setup', shop_id=shop.id))
        if not config:
            config = InvoiceIssuerSettings(organization_id=org.id)
            db.session.add(config)
        config.company_name = (request.form.get('company_name') or '').strip()[:180]
        config.ico = (request.form.get('ico') or '').strip()[:32]
        config.dic = (request.form.get('dic') or '').strip()[:32]
        config.address = (request.form.get('address') or '').strip()[:500]
        config.bank_account = (request.form.get('bank_account') or '').strip()[:120]
        config.iban = (request.form.get('iban') or '').strip()[:120]
        config.swift = (request.form.get('swift') or '').strip()[:32]
        selected_mode = (request.form.get('vat_mode') or 'unknown').strip()
        if selected_mode not in {'unknown','nonvat','vat'}:
            db.session.rollback()
            flash('Neplatný režim DPH.', 'danger')
            return redirect(url_for('eda_invoicing.invoice_setup',shop_id=shop.id))
        config.vat_mode = selected_mode
        # The firm profile and invoice identity are one legal entity.
        # Reflect an invoice setup edit in the company section immediately.
        if profile is None:
            profile = OrganizationProfile(organization_id=org.id)
            db.session.add(profile)
        changed_identity = (org.name != config.company_name or (org.ico or '') != config.ico
                            or (profile.address or '') != config.address or (profile.dic or '') != config.dic)
        org.name = config.company_name
        org.ico = config.ico
        profile.address = config.address
        profile.dic = config.dic or None
        if changed_identity and profile.ares_status == 'verified':
            profile.ares_status = 'modified'
        prefix = (request.form.get('series_prefix') or 'AR').strip().upper()
        if not re.fullmatch(r'[A-Z0-9]{2,12}', prefix):
            flash('Předpona číselné řady musí obsahovat 2–12 velkých písmen nebo číslic.', 'danger')
            db.session.rollback()
            return redirect(url_for('eda_invoicing.invoice_setup', shop_id=shop.id))
        proposed_prefix = prefix
        if config.id and config.series_prefix != proposed_prefix and Invoice.query.filter_by(organization_id=org.id, source='eda', direction='issued').first():
            flash('Předponu číselné řady nelze změnit po vystavení prvních faktur.', 'danger')
            db.session.rollback()
            return redirect(url_for('eda_invoicing.invoice_setup', shop_id=shop.id))
        config.series_prefix = proposed_prefix
        config.due_days = max(0, min(90, int(request.form.get('due_days') or 14)))
        if not publisher:
            publisher = ShopInvoicePublisher(organization_id=org.id, shop_id=shop.id)
            db.session.add(publisher)
        submitted = (request.form.get('invoice_write_key') or '').strip()
        if submitted:
            if len(submitted) < 32:
                flash('Invoice write key musí mít alespoň 32 znaků.', 'danger')
                return redirect(url_for('eda_invoicing.invoice_setup', shop_id=shop.id))
            publisher.encrypted_write_key = encrypt_text(submitted)
        if not current_app.config.get("EDA_SHOP_EVENTS_READY", False):
            db.session.rollback()
            flash("Online automatizace není připravena: zkontroluj tabulky událostí a log aplikace.", "danger")
            return redirect(url_for("eda_invoicing.invoice_setup", shop_id=shop.id))
        if not event_settings:
            event_settings = EdaShopEventSettings(shop_id=shop.id, organization_id=org.id)
            db.session.add(event_settings)
        old_enabled=bool(event_settings.automatic_enabled)
        event_settings.automatic_enabled=request.form.get('event_automatic_enabled') == 'yes'
        event_settings.auto_advance=request.form.get('event_auto_advance') == 'yes'
        event_settings.auto_invoice=request.form.get('event_auto_invoice') == 'yes'
        if event_settings.automatic_enabled and not old_enabled:
            event_settings.started_at=datetime.utcnow()
        if not event_settings.automatic_enabled:
            event_settings.started_at=None
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id,
            category='invoicing', action='issuer_settings_updated', entity_type='shop', entity_id=str(shop.id)))
        db.session.commit()
        flash('Nastavení fakturace uloženo.', 'success')
        return redirect(url_for('eda_invoicing.invoice_setup', shop_id=shop.id))
    return render_template('invoice_setup.html', org=org, shop=shop, issuer=config,
                           profile=profile, publisher=publisher, event_settings=event_settings)


@invoice_bp.post('/shops/<int:shop_id>/orders/<int:order_id>/invoice/check-eda')
@login_required
def check_invoice(shop_id, order_id):
    """Read-only preflight. No invoice number, PDF, reservation, or Woo write."""
    org = _org()
    shop, order = _shop_order(org, shop_id, order_id)
    back = url_for('web.shop_order_detail', shop_id=shop.id, order_id=order.id)
    try:
        if not current_app.config.get('EDA_INVOICING_READY', False):
            raise InvoiceDataError('Tabulky fakturace nejsou připravené.')
        issuer = InvoiceIssuerSettings.query.filter_by(organization_id=org.id).first()
        if not issuer or issuer.vat_mode != 'nonvat' or not all((issuer.company_name, issuer.ico, issuer.address, issuer.bank_account)):
            raise InvoiceDataError('Nejsou kompletní údaje dodavatele nebo není zvolen režim neplátce DPH.')
        if EdaInvoiceDocument.query.filter_by(shop_id=shop.id, order_id=order.id).first():
            raise InvoiceDataError('Faktura už byla vystavena; druhou nevystavuj.')
        connection = ShopAccountingConnection.query.filter_by(shop_id=shop.id, organization_id=org.id).first()
        if not connection or not connection.encrypted_key:
            raise InvoiceDataError('Accounting API není připojené.')
        data = FreshCoreAccountingClient(connection).order_detail(order.external_id)
        if str(data.get('status') or '').lower() not in {'processing', 'completed'} or not bool(data.get('is_paid')):
            raise InvoiceDataError('Úhrada dosud není potvrzená nebo stav není processing/completed.')
        validate_order(data, nonvat=True)
        if str(data.get('id')) != str(order.external_id) or abs(money(data.get('total')) - money(order.total)) > Decimal('0.02'):
            raise InvoiceDataError('Účetní detail neodpovídá synchronizované objednávce.')
        vs = str(data.get('number') or order.external_id)
        if Invoice.query.filter_by(organization_id=org.id, direction='issued', variable_symbol=vs).first():
            raise InvoiceDataError('Pro tento variabilní symbol už existuje vydaná faktura.')
        audit = woocommerce_tax_audit(data)
        suffix = ('Konečná cena převzata z WooCommerce; rozdíl proti součtu řádků: ' + str(audit['woo_reconciliation']) + ' Kč. Kredit: ' + str(audit['affiliate_credit']) + ' Kč. Obchodní hodnota: ' + str(audit['commercial_total']) + ' Kč.')
        flash(f'Kontrola fakturace {INVOICE_LOGIC_VERSION} prošla. Doklad NEBYL vystaven. Částka {money(data.get("total"))} {data.get("currency") or "CZK"}. {suffix}', 'success')
    except (InvoiceDataError, FreshCoreAccountingError, ValueError, RuntimeError) as exc:
        flash(f'Kontrola fakturace {INVOICE_LOGIC_VERSION}: ' + str(exc)[:340], 'danger')
    return redirect(back)


@invoice_bp.post('/shops/<int:shop_id>/orders/<int:order_id>/invoice/issue-eda')
@login_required
def issue_invoice(shop_id, order_id):
    if not current_app.config.get('EDA_INVOICING_READY', False):
        abort(503, 'Chybí tabulky fakturace Edy.')
    org = _org()
    shop, order = _shop_order(org, shop_id, order_id)
    back = url_for('web.shop_order_detail', shop_id=shop.id, order_id=order.id)
    existing = EdaInvoiceDocument.query.filter_by(shop_id=shop.id, order_id=order.id).first()
    if existing:
        flash('Faktura už byla vystavena; nelze ji vytvořit pod jiným číslem.', 'warning')
        return redirect(back)
    issuer = InvoiceIssuerSettings.query.filter_by(organization_id=org.id).first()
    if not issuer or not issuer.company_name or not issuer.ico or not issuer.address or not issuer.bank_account:
        flash('Nejdřív doplň úplné údaje dodavatele a bankovní účet v nastavení fakturace.', 'warning')
        return redirect(url_for('eda_invoicing.invoice_setup', shop_id=shop.id))
    if issuer.vat_mode != 'nonvat':
        flash('V této etapě lze vystavovat pouze faktury neplátců DPH.', 'danger')
        return redirect(back)
    snap = OrderAccountingSnapshot.query.filter_by(organization_id=org.id, shop_id=shop.id, order_id=order.id).first()
    # Direct issue from the order list: always fetch latest private data from Accounting API.
    # Do not require a separately stored snapshot, but preserve it for auditing.
    if not snap:
        snap = OrderAccountingSnapshot(organization_id=org.id, shop_id=shop.id,
            order_id=order.id, project_id=shop.project_id, status='not_loaded')
        db.session.add(snap)
    try:
        # An invoice must be issued from the latest WooCommerce order, not a stale snapshot.
        connection = ShopAccountingConnection.query.filter_by(shop_id=shop.id, organization_id=org.id).first()
        if not connection or not connection.encrypted_key:
            raise InvoiceDataError('Accounting API není připojeno; aktuální objednávku nelze ověřit.')
        data = FreshCoreAccountingClient(connection).order_detail(order.external_id)
        if str(data.get('status') or '').lower() not in {'processing','completed'} or not bool(data.get('is_paid')):
            raise InvoiceDataError('Konečnou fakturu vystavujeme až po potvrzení úhrady (processing nebo completed). Pro bankovní převod použij zálohovou výzvu.')
        validate_order(data, nonvat=True)
        tax_audit = woocommerce_tax_audit(data)
        vs = str(data.get('number') or order.external_id)
        existing_doc = Invoice.query.filter_by(organization_id=org.id, direction='issued', variable_symbol=vs).first()
        if existing_doc:
            raise InvoiceDataError('Pro tento variabilní symbol již existuje vydaná faktura v Edovi nebo Flexi; ověř duplicitu.')
        if str(data.get('id')) != str(order.external_id) or abs(money(data.get('total')) - money(order.total)) > Decimal('0.02'):
            raise InvoiceDataError('Účetní detail neodpovídá aktuální objednávce. Znovu načti účetní data.')
        issued = datetime.now(ZoneInfo('Europe/Prague')).date()
        due = issued + timedelta(days=issuer.due_days)
        sequence = InvoiceSequence.query.filter_by(organization_id=org.id, year=issued.year).with_for_update().first()
        if not sequence:
            # First invoice of the year. Unique DB constraint protects a concurrent attempt.
            sequence = InvoiceSequence(organization_id=org.id, year=issued.year, next_number=1)
            db.session.add(sequence)
            db.session.flush()
        serial = sequence.next_number
        sequence.next_number += 1
        invoice_number = f'{issuer.series_prefix}-{issued.year}-{serial:04d}'
        token = secrets.token_urlsafe(32)
        invoice = Invoice(organization_id=org.id, project_id=snap.project_id or shop.project_id,
            direction='issued', external_id=f'EDA:SHOP:{shop.id}:ORDER:{order.external_id}',
            number=invoice_number, variable_symbol=str(data.get('number') or order.external_id),
            issued_on=issued, due_on=due, total=commercial_total(data),
            remaining=Decimal('0') if data.get('is_paid') else commercial_total(data),
            currency=order.currency, paid=bool(data.get('is_paid')), source='eda',
            raw_json=json.dumps({'shop_id': shop.id, 'order_id': order.id, 'kind': 'nonvat', 'woocommerce_tax_audit': tax_audit, 'affiliate_credit': str(verified_affiliate_credit(data)), 'wallet_transaction_id': (data.get('affiliate_credit_accounting') or {}).get('wallet_transaction_id')}, ensure_ascii=False))
        db.session.add(invoice); db.session.flush()
        folder = Path(current_app.instance_path) / 'invoices' / str(org.id)
        folder.mkdir(parents=True, exist_ok=True)
        filename = f'{invoice.id}.pdf'
        filepath = folder / filename
        advance_doc=EdaAdvanceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first()
        if advance_doc and money(advance_doc.amount) != money(data.get('total')):
            raise InvoiceDataError('Hodnota objednávky se po vystavení zálohové výzvy změnila. Před konečnou fakturou zkontroluj skutečně přijatou zálohu.')
        paid_advance=money(advance_doc.amount) if advance_doc and bool(data.get('is_paid')) else Decimal('0')
        generate_invoice_pdf(filepath, dict(company_name=issuer.company_name, ico=issuer.ico,
            dic=issuer.dic, address=issuer.address, bank_account=issuer.bank_account,
            iban=issuer.iban, swift=issuer.swift, **_issuer_logo_dict(issuer)), data, invoice_number, issued, due,
            is_paid=invoice.paid, kind="final", paid_advance_amount=paid_advance)
        digest = hashlib.sha256(filepath.read_bytes()).hexdigest()
        doc = EdaInvoiceDocument(organization_id=org.id, shop_id=shop.id, order_id=order.id,
            invoice_id=invoice.id, pdf_file=filename, token_hash=hashlib.sha256(token.encode()).hexdigest(),
            encrypted_token=encrypt_text(token), pdf_sha256=digest, snapshot_encrypted=encrypt_text(json.dumps(data, ensure_ascii=False)))
        db.session.add(doc)
        # Eda's own invoice is the authoritative accounting record, also for project reports.
        snap.invoice_external_ref = invoice.external_id
        snap.status = 'invoiced'
        snap.encrypted_payload = encrypt_text(json.dumps(data, ensure_ascii=False))
        snap.fetched_at = datetime.utcnow()
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id,
            category='invoicing', action='invoice_issued', entity_type='invoice', entity_id=str(invoice.id),
            detail_json=json.dumps({'number': invoice.number, 'order_id': order.external_id, 'woocommerce_tax_audit': tax_audit}, ensure_ascii=False)))
        db.session.commit()
    except (InvoiceDataError, FreshCoreAccountingError, ValueError, IntegrityError, OSError, ImportError, RuntimeError) as exc:
        db.session.rollback()
        if 'filepath' in locals() and filepath.exists():
            filepath.unlink()
        flash(f'Fakturu nelze vystavit (modul {INVOICE_LOGIC_VERSION}): ' + str(exc)[:400], 'danger')
        return redirect(back)
    try:
        _publish(doc, shop, order)
        flash(f'Faktura {invoice.number} byla vystavena a odkaz byl předán do WooCommerce.', 'success')
    except Exception as exc:
        db.session.rollback()
        doc = EdaInvoiceDocument.query.filter_by(shop_id=shop.id, order_id=order.id).first()
        doc.publish_status = 'error'
        doc.publish_error = str(exc)[:500]
        db.session.commit()
        flash('Faktura byla vystavena a bezpečně uložena, ale odkaz se nepodařilo publikovat do WooCommerce. Zkus opakovat předání.', 'warning')
    return redirect(back)


@invoice_bp.post('/shops/<int:shop_id>/orders/<int:order_id>/invoice/publish-eda')
@login_required
def republish_invoice(shop_id, order_id):
    org = _org()
    shop, order = _shop_order(org, shop_id, order_id)
    doc = EdaInvoiceDocument.query.filter_by(shop_id=shop.id, order_id=order.id).first_or_404()
    try:
        _publish(doc, shop, order)
        flash('Odkaz na již vystavenou fakturu byl znovu předán do WooCommerce.', 'success')
    except Exception as exc:
        db.session.rollback()
        doc.publish_status = 'error'; doc.publish_error = str(exc)[:500]
        db.session.commit()
        flash('Předání faktury selhalo; ověř Invoice write key a spojení s Fresh Core.', 'danger')
    return redirect(url_for('web.shop_order_detail', shop_id=shop.id, order_id=order.id))


def _revoke_remote(doc, shop, order):
    publisher = ShopInvoicePublisher.query.filter_by(shop_id=shop.id, organization_id=shop.organization_id).first()
    connection = ShopAccountingConnection.query.filter_by(shop_id=shop.id, organization_id=shop.organization_id).first()
    if not publisher or not publisher.encrypted_write_key or not connection or not connection.orders_endpoint:
        raise RuntimeError('Chybí Invoice write key nebo Accounting API.')
    from urllib.parse import urlparse
    endpoint = connection.orders_endpoint.rstrip('/') + '/' + str(order.external_id) + '/invoice-link/revoke'
    if not endpoint.startswith('https://') or urlparse(endpoint).hostname != urlparse(shop.site_url or '').hostname:
        raise RuntimeError('Nesouhlasí doména Accounting API a e-shopu.')
    result = requests.post(endpoint, json={'number': doc.invoice.number}, timeout=25, allow_redirects=False,
                           headers={'X-Fresh-Core-Invoice-Write-Key': decrypt_text(publisher.encrypted_write_key),
                                    'User-Agent': 'Eda-Invoice/0.7.1', 'Accept': 'application/json'})
    if result.status_code != 200:
        raise RuntimeError(f'Fresh Core vrátil HTTP {result.status_code}')


@invoice_bp.post('/shops/<int:shop_id>/orders/<int:order_id>/invoice/revoke-eda')
@login_required
def revoke_invoice_link(shop_id, order_id):
    org = _org()
    shop, order = _shop_order(org, shop_id, order_id)
    doc = EdaInvoiceDocument.query.filter_by(shop_id=shop.id, order_id=order.id).first_or_404()
    try:
        _revoke_remote(doc, shop, order)
    except Exception:
        flash('Odkaz nelze zneplatnit ve WordPressu. Zkontroluj spojení a opakuj.', 'danger')
        return redirect(url_for('web.shop_order_detail', shop_id=shop.id, order_id=order.id))
    doc.revoked = True
    db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id,
        category='invoicing', action='invoice_link_revoked', entity_type='invoice', entity_id=str(doc.invoice_id)))
    db.session.commit()
    flash('Veřejný odkaz byl zneplatněn. Doklad v Edovi zůstává archivován. Nový odkaz zatím vystavujeme ručně.', 'success')
    return redirect(url_for('web.shop_order_detail', shop_id=shop.id, order_id=order.id))
