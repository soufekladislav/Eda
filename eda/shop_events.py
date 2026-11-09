"""Signed one-order events from Fresh Core, no client details in webhook body.

The event is sent by WooCommerce Action Scheduler, not on a checkout request.
The receiver independently retrieves the latest authorized order detail from Fresh Core.
"""
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request, session
from flask_login import login_user
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import (Shop, ShopAccountingConnection, Order, OrderAccountingSnapshot,
    EdaShopEvent, EdaShopEventSettings, EdaInvoiceDocument, EdaAdvanceDocument,
    Membership, User)
from .services.secrets import encrypt_text, decrypt_text
from .services.fresh_core_accounting import FreshCoreAccountingClient
from .services.eda_invoice_pdf import verified_affiliate_credit, InvoiceDataError

shop_events_bp = Blueprint('eda_shop_events', __name__)


def _fresh_order(shop, data):
    """Use the WooCommerce payload as-is; never recalculate totals."""
    external=str(data.get('id') or '')
    item=Order.query.filter_by(shop_id=shop.id,external_id=external).first()
    if item is None:
        item=Order(organization_id=shop.organization_id,shop_id=shop.id,external_id=external)
        db.session.add(item)
        db.session.flush()
    item.status=str(data.get('status') or '')[:80]
    item.total=Decimal(str(data.get('total') or '0'))
    item.currency=str(data.get('currency') or 'CZK')[:8]
    item.customer_email=str((data.get('billing') or {}).get('email') or '')[:255] or None
    created=str(data.get('date_created') or '').replace('Z','+00:00')
    if created:
        try:
            dt=datetime.fromisoformat(created)
            if dt.tzinfo: dt=dt.astimezone(timezone.utc).replace(tzinfo=None)
            item.ordered_at=dt
        except ValueError: pass
    # The basic Order raw_json is deliberately PII-free. Full billing data is encrypted below.
    item.raw_json=json.dumps({'id':external,'status':item.status,'total':str(item.total),
      'currency':item.currency,'is_paid':bool(data.get('is_paid'))},ensure_ascii=False)
    snap=OrderAccountingSnapshot.query.filter_by(organization_id=shop.organization_id,
        shop_id=shop.id,order_id=item.id).first()
    if snap is None:
        snap=OrderAccountingSnapshot(organization_id=shop.organization_id,shop_id=shop.id,
             order_id=item.id,project_id=shop.project_id)
        db.session.add(snap)
    snap.encrypted_payload=encrypt_text(json.dumps(data,ensure_ascii=False,default=str))
    snap.fetched_at=datetime.utcnow()
    # Do not reset a previously issued/final state on a status update.
    if snap.status not in ('invoiced','invoice_prepared'):
        snap.status='loaded'
    shop.last_sync_at=datetime.utcnow()
    db.session.flush()
    return item


def _staff_for_audit(org_id):
    membership=(Membership.query.join(User,User.id==Membership.user_id)
       .filter(Membership.organization_id==org_id,Membership.role.in_(['owner','admin']),
               User.is_active_user.is_(True)).order_by(Membership.id).first())
    return db.session.get(User,membership.user_id) if membership else None


def _action(shop,order,data,settings):
    if not settings or not settings.automatic_enabled or not settings.started_at:
        return 'synced'
    # Never retroactively issue invoices for pre-activation orders.
    if not order.ordered_at or order.ordered_at < settings.started_at:
        return 'synced_legacy'
    status=str(data.get('status') or '').lower()
    paid=bool(data.get('is_paid'))
    method=str(data.get('payment_method') or '').lower()
    action=None
    invoice=EdaInvoiceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first()
    advance=EdaAdvanceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first()
    if paid and status in ('processing','completed') and settings.auto_invoice:
        if invoice:
            if not invoice.revoked and invoice.publish_status!='published': action='republish_invoice'
        else: action='issue_invoice'
    elif not paid and method=='bacs' and status in ('pending','on-hold') and settings.auto_advance:
        if advance:
            if not advance.revoked and advance.publish_status!='published': action='republish_advance'
        else: action='issue_advance'
    if action is None: return 'synced'
    # Credit transactions must be verified before any financial document.
    if any(str(c.get('code') or '').lower().startswith('fresh-credit-') for c in data.get('coupons',[]) if isinstance(c,dict)):
        verified_affiliate_credit(data)
    user=_staff_for_audit(shop.organization_id)
    if not user: raise RuntimeError('No active owner/admin to attribute automated action')
    from .invoicing import issue_invoice,issue_advance,republish_invoice,republish_advance
    func={'issue_invoice':issue_invoice,'issue_advance':issue_advance,
          'republish_invoice':republish_invoice,'republish_advance':republish_advance}[action]
    # Use the existing invoice routes and their validations, not a second invoicing engine.
    with current_app.test_request_context('/_internal/eda-shop-event',method='POST'):
        login_user(user)
        session['organization_id']=shop.organization_id
        func(shop.id,order.id)
    db.session.expire_all()
    invoice=EdaInvoiceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first()
    advance=EdaAdvanceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first()
    doc=invoice if 'invoice' in action else advance
    if not doc or doc.revoked:
        raise RuntimeError('Invoice action did not create/preserve a valid document; review Eda order')
    if doc.publish_status!='published':
        raise RuntimeError('Document exists but publishing failed; retry will republish, not duplicate')
    return action


@shop_events_bp.post('/shop-events')
def receive_shop_event():
    timestamp=request.headers.get('X-Eda-Event-Timestamp','')
    signature=request.headers.get('X-Eda-Event-Signature','')
    body=request.get_data(cache=True)
    if len(body)>4096 or not timestamp.isdigit() or abs(time.time()-int(timestamp))>300:
        return jsonify(ok=False,error='unauthorized'),403
    try: data=json.loads(body)
    except ValueError: return jsonify(ok=False,error='invalid_event'),400
    if not isinstance(data,dict): return jsonify(ok=False,error='invalid_event'),400
    eid=str(data.get('event_id') or '')
    oid=str(data.get('order_id') or '')
    source=str(data.get('shop_url') or '')
    if not re.fullmatch(r'[a-fA-F0-9-]{36}',eid) or not oid.isdigit() or not 1<=int(oid)<=2**63-1:
        return jsonify(ok=False,error='invalid_ids'),400
    parsed=urlsplit(source)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return jsonify(ok=False,error='invalid_source'),400
    matches=[s for s in Shop.query.filter_by(active=True).all() if
        urlsplit(s.site_url or '').hostname==parsed.hostname]
    if len(matches)!=1: return jsonify(ok=False,error='unknown_or_ambiguous_shop'),403
    shop=matches[0]
    conn=ShopAccountingConnection.query.filter_by(shop_id=shop.id,organization_id=shop.organization_id).first()
    if not conn or not conn.encrypted_key: return jsonify(ok=False,error='accounting_not_configured'),403
    try:
        accounting_key=decrypt_text(conn.encrypted_key)
    except Exception:
        return jsonify(ok=False,error='accounting_not_configured'),403
    if len(accounting_key)<32: return jsonify(ok=False,error='accounting_not_configured'),403
    expected='sha256='+hmac.new(accounting_key.encode(),timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature,expected):
        return jsonify(ok=False,error='unauthorized'),403
    # Store ID before processing, to prevent concurrent duplicate deliveries.
    if not current_app.config.get('EDA_SHOP_EVENTS_READY',False):
        return jsonify(ok=False,error='events_not_ready'),503
    event=EdaShopEvent.query.filter_by(event_id=eid).first()
    if event and event.status=='done': return jsonify(ok=True,duplicate=True),200
    if event is None:
        try:
            event=EdaShopEvent(event_id=eid,shop_id=shop.id,external_order_id=oid,status='received')
            db.session.add(event);db.session.commit()
        except IntegrityError:
            db.session.rollback()
            event=EdaShopEvent.query.filter_by(event_id=eid).first()
            if event and event.status=='done': return jsonify(ok=True,duplicate=True),200
    try:
        conn=ShopAccountingConnection.query.filter_by(shop_id=shop.id,organization_id=shop.organization_id).first()
        if not conn or not conn.encrypted_key: raise RuntimeError('Fresh Core read connection is not configured')
        detail=FreshCoreAccountingClient(conn).order_detail(oid)
        if str(detail.get('id'))!=oid: raise RuntimeError('Remote order ID mismatch')
        order=_fresh_order(shop,detail)
        db.session.commit()
        settings=EdaShopEventSettings.query.filter_by(shop_id=shop.id).first()
        act=_action(shop,order,detail,settings)
        event=EdaShopEvent.query.filter_by(event_id=eid).first()
        event.status='done';event.action=act;event.error=None
        (EdaShopEvent.query.filter_by(shop_id=shop.id,external_order_id=oid,status='error')
            .update({'status':'superseded'},synchronize_session=False))
        db.session.commit()
        return jsonify(ok=True,action=act,order_id=oid),200
    except Exception as exc:
        db.session.rollback()
        event=EdaShopEvent.query.filter_by(event_id=eid).first()
        if event:
            event.status='error';event.error=f'{type(exc).__name__}: {str(exc)}'[:400]
            db.session.commit()
        current_app.logger.warning('Eda shop event %s failed: %s',eid,type(exc).__name__)
        return jsonify(ok=False,error='processing_failed'),503
