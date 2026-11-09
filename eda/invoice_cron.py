"""Opt-in Eda invoice poller for a single shop; run via cPanel cron, NOT via public HTTP.

Safety:
* Explicit shop ID and activation timestamp required.
* Uses Fresh Core authenticated order API; never trusts a Stripe redirect.
* Calls the SAME Eda issuing routes as staff (no alternative invoice generator).
* 10 candidates per run; locks to prevent concurrent cron invocations.
* Default is DRY RUN. No automatic issue until --apply and env enabled.
"""
import argparse
import fcntl
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from flask import session
from flask_login import login_user

from . import create_app
from .extensions import db
from .models import (Shop, Order, Membership, User, ShopAccountingConnection,
                     EdaInvoiceDocument, EdaAdvanceDocument)
from .services.fresh_core_accounting import FreshCoreAccountingClient
from .services.fresh_core import import_orders
from .invoicing import issue_invoice, issue_advance, republish_invoice, republish_advance


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--shop-id', type=int, required=True)
    parser.add_argument('--not-before', required=True, help='ISO timestamp; only orders newly created after this moment')
    parser.add_argument('--apply', action='store_true', help='Explicitly enable issuance')
    args = parser.parse_args(argv)
    if args.apply and os.environ.get('EDA_AUTOINVOICE_ENABLED') != 'YES':
        parser.error('--apply requires EDA_AUTOINVOICE_ENABLED=YES')
    not_before = datetime.fromisoformat(args.not_before.replace('Z', '+00:00'))
    if not_before.tzinfo:
        not_before = not_before.astimezone(timezone.utc).replace(tzinfo=None)
    app = create_app()
    with app.app_context():
        lockfile = Path(app.instance_path) / ('invoice-cron-shop-%d.lock' % args.shop_id)
        with lockfile.open('a+') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return print('already running; skip')
            shop = db.session.get(Shop, args.shop_id)
            if not shop or not shop.active: raise RuntimeError('Shop not found / inactive')
            conn = ShopAccountingConnection.query.filter_by(shop_id=shop.id, organization_id=shop.organization_id).first()
            if not conn or not conn.encrypted_key: raise RuntimeError('Fresh Core accounting not connected')
            # Existing staff identity is needed for audit; no user passwords or login tokens saved.
            membership = (Membership.query.join(User, User.id == Membership.user_id)
                          .filter(Membership.organization_id == shop.organization_id,
                                  Membership.role.in_(['owner','admin']), User.is_active_user.is_(True))
                          .order_by(Membership.id).first())
            if not membership: raise RuntimeError('No active authorized owner/admin for audit')
            user = db.session.get(User, membership.user_id)
            api = FreshCoreAccountingClient(conn)
            # A single page keeps cron cheap; the most recent orders need to come first.
            payload = api.orders_page(page=1, per_page=200)
            rows = payload.get('orders') or []
            if args.apply:
                import_orders(shop.organization_id, shop, rows, source='invoice_cron')
            seen = 0
            for src in rows:
                if seen >= 10: break
                if not isinstance(src, dict): continue
                external = str(src.get('id') or '')
                if not external.isdecimal(): continue
                created = str(src.get('date_created') or '').replace('Z', '+00:00')
                try: when = datetime.fromisoformat(created)
                except ValueError: continue
                if when.tzinfo: when = when.astimezone(timezone.utc).replace(tzinfo=None)
                if when < not_before: continue
                # All decisions use FULL, LATEST remote order details.
                data = api.order_detail(external)
                status = str(data.get('status') or '').lower()
                method = str(data.get('payment_method') or '').lower()
                paid = bool(data.get('is_paid'))
                action = None
                inv = None; adv = None
                if args.apply:
                    order = Order.query.filter_by(shop_id=shop.id, external_id=external).first()
                    if order is None: continue
                    inv = EdaInvoiceDocument.query.filter_by(shop_id=shop.id, order_id=order.id).first()
                    adv = EdaAdvanceDocument.query.filter_by(shop_id=shop.id, order_id=order.id).first()
                if paid and status in ('processing','completed'):
                    action = 'republish_invoice' if inv and inv.publish_status != 'published' and not inv.revoked else ('issue_invoice' if not inv else None)
                elif method == 'bacs' and not paid and status in ('pending','on-hold'):
                    action = 'republish_advance' if adv and adv.publish_status != 'published' and not adv.revoked else ('issue_advance' if not adv else None)
                if action and any(str(c.get('code') or '').lower().startswith('fresh-credit-') for c in data.get('coupons',[]) if isinstance(c,dict)):
                    from .services.eda_invoice_pdf import verified_affiliate_credit, InvoiceDataError
                    try:
                        verified_affiliate_credit(data)
                    except InvoiceDataError as exc:
                        print('order=%s BLOCKED: %s' % (external, exc))
                        continue
                if not action: continue
                print('order=%s action=%s %s' % (external, action, 'APPLY' if args.apply else 'DRY_RUN'), flush=True)
                seen += 1
                if not args.apply: continue
                # Existing route logic is the only issuing authority. Reuse its validation,
                # invoice series, immutable source snapshot and duplicate guards.
                with app.test_request_context('/_internal/invoice-cron', method='POST'):
                    login_user(user)
                    session['organization_id'] = shop.organization_id
                    funcs = {'issue_invoice': issue_invoice, 'issue_advance': issue_advance,
                             'republish_invoice': republish_invoice, 'republish_advance': republish_advance}
                    funcs[action](shop.id, order.id)
                    db.session.remove()
            print('candidates=%d mode=%s' % (seen, 'APPLY' if args.apply else 'DRY_RUN'))


if __name__ == '__main__':
    main()
