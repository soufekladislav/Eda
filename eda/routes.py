from collections import defaultdict
import requests
import shutil
import hashlib
import hmac
import secrets
import json
from pathlib import Path
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, send_from_directory, jsonify, abort
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import func, case, or_
from .extensions import db
from .helpers import current_organization
from .models import User, Membership, Organization, OrganizationProfile, Task, TaskSchedule, Transaction, Project, ProjectMember, Shop, Order, Invoice, Integration, AgentJob, AgentAction, InboxDocument, AuditLog, DiagnosticEvent, SyncLog, ChatConversation, ChatMessage, ChatAttachment, AccountingStandard, ShopAccountingConnection, OrderAccountingSnapshot, EdaInvoiceDocument

from werkzeug.utils import secure_filename
from .services.worker import PRESETS, create_job, execute_job
from .services.flexibee import FlexiBeeClient
from .services.ai_engine import run_chat, AIEngineError, analyze_inbox_document
from .services.document_reader import read_document
from .services.ares import get_company as ares_get_company, AresError
from .services.scheduler import ensure_scheduled_tasks, reminder_items
from .services.standards import review_standard
from .services.fresh_core import test_connection as test_fresh_core_connection, sync_orders as sync_fresh_core_orders, FreshCoreError
from .services.fresh_core_accounting import FreshCoreAccountingClient, FreshCoreAccountingError, discover_from_manifest, serialize_private_order
from .services.secrets import encrypt_text, decrypt_text, mask_secret
from .services import fresh_key

web_bp = Blueprint("web", __name__)


@web_bp.before_request
def _restrict_project_only_members():
    """Company members with 'member' role are project-scoped, never org accountants."""
    if not current_user.is_authenticated or current_user.is_admin:
        return None
    org = current_organization()
    if not org:
        return None
    membership = Membership.query.filter_by(user_id=current_user.id, organization_id=org.id).first()
    if not membership or membership.role != 'member':
        return None
    # Project detail is checked by _project_for_current_user.
    allowed = {'web.projects', 'web.project_detail', 'web.logout', 'web.organization_switch'}
    if request.endpoint == 'web.dashboard':
        return redirect(url_for('web.projects'))
    if request.endpoint not in allowed:
        abort(403)
    return None


def _project_members_ready():
    return bool(current_app.config.get("FRESH_KEY_PROJECT_MEMBERS_READY", False))


def _project_members_migration_error():
    return str(current_app.config.get("FRESH_KEY_PROJECT_MEMBERS_ERROR") or "")


def _project_members_fail(exc):
    """Disable only the optional project-sharing ACL for this worker.

    Eda core projects/finance must stay available even if an additive Fresh Key
    table is missing or partially migrated.
    """
    try:
        db.session.rollback()
    except Exception:
        pass
    current_app.config["FRESH_KEY_PROJECT_MEMBERS_READY"] = False
    current_app.config["FRESH_KEY_PROJECT_MEMBERS_ERROR"] = f"{type(exc).__name__}: {exc}"[:500]



def _org_or_redirect():
    org = current_organization()
    if not org:
        flash("K účtu není přiřazena žádná firma.", "danger")
        return None
    return org


def _ensure_org_profile(org):
    profile = OrganizationProfile.query.filter_by(organization_id=org.id).first()
    if not profile:
        profile = OrganizationProfile(organization_id=org.id)
        db.session.add(profile)
        db.session.flush()
    return profile


def _can_edit_org(org):
    membership = Membership.query.filter_by(user_id=current_user.id, organization_id=org.id).first()
    return bool(current_user.is_admin or (membership and membership.role in {"owner", "admin"}))


def _project_member_for_user(project_id, user=None, *, activate=True):
    user = user or current_user
    if not getattr(user, "is_authenticated", False) or not _project_members_ready():
        return None
    email = str(getattr(user, "email", "") or "").strip().lower()
    try:
        row = ProjectMember.query.filter(
            ProjectMember.project_id == project_id,
            or_(ProjectMember.user_id == user.id, func.lower(ProjectMember.email) == email),
        ).first()
    except Exception as exc:
        _project_members_fail(exc)
        return None
    if row and activate and row.status in {"pending", "active"}:
        changed = False
        if not row.user_id:
            row.user_id = user.id; changed = True
        if row.status != "active":
            row.status = "active"; changed = True
        if row.status == 'active' and not Membership.query.filter_by(user_id=user.id,organization_id=row.project.organization_id).first():
            db.session.add(Membership(user_id=user.id,organization_id=row.project.organization_id,role='member'))
            changed = True
        if changed:
            db.session.commit()
    return row


def _project_for_current_user(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    if current_user.is_admin:
        return project
    membership = Membership.query.filter_by(user_id=current_user.id, organization_id=project.organization_id).first()
    if membership and membership.role in {"owner", "admin"}:
        return project
    access = _project_member_for_user(project.id)
    if access and access.status == "active":
        return project
    abort(404)


def _normalize_access_role(value):
    role = str(value or "").strip().lower()
    aliases = {"member": "editor", "client": "editor", "agency": "editor", "visitor": "viewer", "reader": "viewer"}
    role = aliases.get(role, role)
    return role if role in {"owner", "manager", "editor", "viewer"} else "viewer"


def _normalize_relationship(value):
    rel = str(value or "member").strip().lower()
    return rel if rel in {"member", "client", "agency"} else "member"


def _role_label(value):
    return {"owner":"Vlastník", "manager":"Správce", "editor":"Editor", "viewer":"Čtenář"}.get(_normalize_access_role(value), "Čtenář")


def _relationship_label(value):
    return {"member":"Člen týmu", "client":"Klient", "agency":"Agentura"}.get(_normalize_relationship(value), "Člen týmu")


def _project_access_role(project):
    if current_user.is_admin:
        return "manager"
    membership = Membership.query.filter_by(user_id=current_user.id, organization_id=project.organization_id).first()
    if membership and membership.role in {"owner", "admin"}:
        return "owner" if membership.role == "owner" else "manager"
    access = _project_member_for_user(project.id)
    return _normalize_access_role(access.role) if access and access.status == "active" else ""


def _can_manage_project(project):
    return _project_access_role(project) in {"manager", "owner"}


def _upsert_project_member_local(project, email, role, relationship, invited_by):
    if not _project_members_ready():
        raise RuntimeError("Sdílení projektu v Edovi čeká na databázovou opravu project_members. Základní projekty zůstávají dostupné.")
    email = str(email or "").strip().lower()
    if not email:
        return None
    user = User.query.filter(func.lower(User.email) == email).first()
    row = ProjectMember.query.filter_by(project_id=project.id, email=email).first()
    local_role = _normalize_access_role(role)
    if local_role == "owner":
        local_role = "manager"
    local_relationship = _normalize_relationship(relationship)
    if not row:
        row = ProjectMember(project_id=project.id, email=email, role=local_role, relationship=local_relationship,
                            status="active" if user else "pending", user_id=user.id if user else None, invited_by=invited_by)
        db.session.add(row)
    else:
        row.role = local_role
        row.relationship = local_relationship
        row.invited_by = invited_by
        if user:
            row.user_id = user.id; row.status = "active"
    db.session.commit()
    return row


def _project_people_local(project):
    people=[]; seen=set()
    org_members = (db.session.query(Membership, User)
                   .join(User, User.id == Membership.user_id)
                   .filter(Membership.organization_id == project.organization_id)
                   .all())
    for membership, user in org_members:
        email=str(user.email or "").strip().lower()
        if not email or email in seen: continue
        seen.add(email)
        role = "owner" if membership.role == "owner" else ("manager" if membership.role == "admin" else "editor")
        people.append({"kind":"organization","name":user.name or email,"email":email,"role":role,"role_label":_role_label(role),
                       "relationship":"member","relationship_label":_relationship_label("member"),"status":"active","organization_member":True})
    if _project_members_ready():
        try:
            optional_rows = ProjectMember.query.filter_by(project_id=project.id).order_by(ProjectMember.email.asc()).all()
        except Exception as exc:
            _project_members_fail(exc)
            optional_rows = []
        for row in optional_rows:
            email=str(row.email or "").strip().lower()
            if not email or email in seen: continue
            seen.add(email)
            role=_normalize_access_role(row.role); relationship=_normalize_relationship(getattr(row,"relationship", "member"))
            people.append({"kind":"project","name":(row.user.name if row.user else email),"email":email,"role":role,"role_label":_role_label(role),
                           "relationship":relationship,"relationship_label":_relationship_label(relationship),
                           "status":row.status or "pending","organization_member":False})
    return people


def _fresh_link_for_project(project):
    if not fresh_key.configured(): return None
    try:
        website=next((x.site_url for x in project.shops if x.site_url), "")
        summary=fresh_key.project_summary(current_user.id,project,website_url=website,role=_project_access_role(project) or "viewer")
        return summary if summary.get("linked") else None
    except Exception:
        return None


def _notify_local_project_share(project,email,role):
    action=(current_app.config.get("APP_URL") or "https://finance.freshcrackers.cz").rstrip("/")+url_for("web.login")
    try:
        return fresh_key.notify_project_share(current_user.id,email,project.name,role,action)
    except Exception:
        return {"email_sent":False,"recipient_status":""}


def _apply_ares_company(org, company):
    profile = _ensure_org_profile(org)
    org.ico = company.ico
    if company.name:
        org.name = company.name
    profile.dic = company.dic
    profile.address = company.address
    profile.legal_form = company.legal_form
    profile.legal_form_code = company.legal_form_code
    profile.nace = company.nace
    profile.founded_on = company.founded_on
    profile.ares_status = "verified"
    profile.ares_verified_at = datetime.utcnow()
    profile.ares_raw_json = json.dumps(company.raw, ensure_ascii=False, default=str)
    return profile


def _organization_guide_state(org):
    profile = OrganizationProfile.query.filter_by(organization_id=org.id).first()
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name.asc()).all()
    shops = Shop.query.filter_by(organization_id=org.id, active=True).count()
    flexi = FlexiBeeClient(org.id).configured
    reminders = reminder_items(org.id, limit=6)
    unassigned_documents = InboxDocument.query.filter_by(organization_id=org.id, project_id=None).count()
    standards = AccountingStandard.query.filter_by(active=True).order_by(AccountingStandard.category, AccountingStandard.code).limit(12).all()
    return {
        "project_count": len(projects),
        "projects": [{"id": p.id, "name": p.name, "type": p.project_type} for p in projects[:20]],
        "company_ares_verified": bool(profile and profile.ares_status == "verified"),
        "company_profile_complete": bool(org.ico and profile and profile.address),
        "shop_count": shops,
        "flexibee_configured": flexi,
        "unassigned_documents": unassigned_documents,
        "upcoming_reminders": [
            {"title": x["task"].title, "due_date": x["task"].due_date.isoformat(), "days": x["days"]}
            for x in reminders
        ],
        "accounting_standards": [
            {"id": s.id, "code": s.code, "title": s.title, "category": s.category, "content": (s.content or "")[:1200], "source": s.source_name, "review_status": s.review_status}
            for s in standards
        ],
    }


def _json_load(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _order_is_paid(order):
    """Return payment state from the sanitized Fresh Core order snapshot."""
    raw = _json_load(order.raw_json, {})
    if raw.get("is_paid") is True or raw.get("date_paid"):
        return True
    if raw.get("needs_payment") is False and (order.status or "").lower() in {"processing", "completed"}:
        return True
    return False


def _project_financial_state(org_id, project, transactions=None, invoices=None, documents=None, orders=None):
    """Build a management view without mixing commercial and accounting reality.

    Orders are commercial value immediately. Issued invoices are accounting value.
    Pending documents are known costs even before posting. This keeps the project
    useful before ABRA is fully synchronized while preserving clear statuses.
    """
    transactions = transactions if transactions is not None else Transaction.query.filter_by(organization_id=org_id, project_id=project.id).all()
    invoices = invoices if invoices is not None else Invoice.query.filter_by(organization_id=org_id, project_id=project.id).all()
    documents = documents if documents is not None else InboxDocument.query.filter_by(organization_id=org_id, project_id=project.id).all()
    if orders is None:
        shop_ids = [x.id for x in Shop.query.filter_by(organization_id=org_id, project_id=project.id).all()]
        orders = Order.query.filter(Order.organization_id == org_id, Order.shop_id.in_(shop_ids)).all() if shop_ids else []

    income = sum((Decimal(x.amount or 0) for x in transactions if x.kind == "income"), Decimal("0"))
    expense = sum((Decimal(x.amount or 0) for x in transactions if x.kind == "expense"), Decimal("0"))
    order_total = sum((Decimal(x.total or 0) for x in orders), Decimal("0"))
    paid_order_total = sum((Decimal(x.total or 0) for x in orders if _order_is_paid(x)), Decimal("0"))

    pending_doc_total = Decimal("0")
    for d in documents:
        proposal = _json_load(d.proposed_json, {})
        try:
            if d.status not in {"booked"} and proposal.get("total") not in (None, ""):
                pending_doc_total += Decimal(str(proposal.get("total")))
        except (InvalidOperation, ValueError, TypeError):
            pass

    issued_invoices = [x for x in invoices if x.direction == "issued"]
    received_invoices = [x for x in invoices if x.direction == "received"]
    transaction_expense_refs = {str(x.external_ref) for x in transactions if x.kind == "expense" and x.external_ref}
    unbooked_received_total = sum(
        (Decimal(x.total or 0) for x in received_invoices if str(x.external_id) not in transaction_expense_refs),
        Decimal("0"),
    )
    known_cost_total = expense + unbooked_received_total + pending_doc_total

    order_ids = [x.id for x in orders]
    snapshots = OrderAccountingSnapshot.query.filter(
        OrderAccountingSnapshot.organization_id == org_id,
        OrderAccountingSnapshot.order_id.in_(order_ids),
    ).all() if order_ids else []
    snapshot_map = {x.order_id: x for x in snapshots}

    invoice_by_external = {str(x.external_id): x for x in issued_invoices if x.external_id}
    invoice_by_vs = {str(x.variable_symbol): x for x in issued_invoices if x.variable_symbol}
    linked_invoice_ids = set()
    order_rows = []
    invoiced_total = Decimal("0")
    uninvoiced_total = Decimal("0")

    for order in orders:
        snapshot = snapshot_map.get(order.id)
        linked = None
        if snapshot and snapshot.invoice_external_ref:
            linked = invoice_by_external.get(str(snapshot.invoice_external_ref))
        if not linked:
            linked = invoice_by_vs.get(str(order.external_id))

        written = bool(snapshot and snapshot.flexi_written_at)
        prepared = bool(snapshot and snapshot.status in {"invoice_prepared", "dry_run_ok", "dry_run_validated", "waiting_approval"})
        invoiced = bool(linked or written)
        if linked:
            linked_invoice_ids.add(linked.id)
            invoiced_value = Decimal(linked.total or 0)
        elif written:
            invoiced_value = Decimal(order.total or 0)
        else:
            invoiced_value = Decimal("0")

        if invoiced:
            invoiced_total += invoiced_value
            invoice_state = "Vyfakturováno"
            invoice_class = "done"
        elif prepared:
            invoice_state = "Připraveno"
            invoice_class = "waiting"
            uninvoiced_total += Decimal(order.total or 0)
        else:
            invoice_state = "Nevyfakturováno"
            invoice_class = "missing"
            uninvoiced_total += Decimal(order.total or 0)

        paid = bool((linked and linked.paid) or _order_is_paid(order))
        order_rows.append({
            "order": order,
            "invoice_state": invoice_state,
            "invoice_class": invoice_class,
            "paid": paid,
            "payment_state": "Uhrazeno" if paid else "Neuhrazeno",
        })

    # Include issued project invoices that are not linked to a known order.
    invoiced_total += sum((Decimal(x.total or 0) for x in issued_invoices if x.id not in linked_invoice_ids), Decimal("0"))

    return {
        "income": income,
        "expense": expense,
        "order_total": order_total,
        "paid_order_total": paid_order_total,
        "invoiced_total": invoiced_total,
        "uninvoiced_total": uninvoiced_total,
        "pending_doc_total": pending_doc_total,
        "unbooked_received_total": unbooked_received_total,
        "known_cost_total": known_cost_total,
        "expected_profit": order_total - known_cost_total,
        "accounting_profit": income - expense,
        "order_rows": order_rows,
    }


def _shop_connection(org_id, shop_id, create=False):
    name = f"shop:{shop_id}"
    row = Integration.query.filter_by(organization_id=org_id, kind="fresh_core_shop", name=name).first()
    if not row and create:
        row = Integration(organization_id=org_id, kind="fresh_core_shop", name=name, status="not_configured")
        db.session.add(row)
        db.session.flush()
    return row


def _shop_accounting_connection(org_id, shop_id, create=False):
    row = ShopAccountingConnection.query.filter_by(organization_id=org_id, shop_id=shop_id).first()
    if not row and create:
        row = ShopAccountingConnection(organization_id=org_id, shop_id=shop_id)
        db.session.add(row)
        db.session.flush()
    return row


def _order_snapshot(org_id, shop_id, order_id, create=False):
    row = OrderAccountingSnapshot.query.filter_by(organization_id=org_id, shop_id=shop_id, order_id=order_id).first()
    if not row and create:
        row = OrderAccountingSnapshot(organization_id=org_id, shop_id=shop_id, order_id=order_id)
        db.session.add(row)
        db.session.flush()
    return row


def _private_order_payload(snapshot):
    if not snapshot or not snapshot.encrypted_payload:
        return None
    try:
        return json.loads(decrypt_text(snapshot.encrypted_payload))
    except Exception:
        return None


def _invoice_payload_from_order(shop, order, private_order, type_code=None, due_on=None):
    billing = private_order.get("billing") if isinstance(private_order.get("billing"), dict) else {}
    payment_reference = private_order.get("payment_reference") if isinstance(private_order.get("payment_reference"), dict) else {}
    variable_symbol = str(payment_reference.get("explicit") or payment_reference.get("suggested_variable_symbol") or private_order.get("number") or order.external_id or "").strip()
    issued_on = str(private_order.get("date_created") or "")[:10] or date.today().isoformat()
    if not due_on:
        try:
            base_date = date.fromisoformat(issued_on)
        except ValueError:
            base_date = date.today()
        due_on = (base_date + timedelta(days=int(current_app.config.get("FLEXIBEE_DEFAULT_DUE_DAYS", 14)))).isoformat()
    items = []
    for source in private_order.get("items") or []:
        if not isinstance(source, dict):
            continue
        qty = Decimal(str(source.get("quantity") or 1))
        line_total = Decimal(str(source.get("total") or 0))
        line_tax = Decimal(str(source.get("total_tax") or 0))
        unit = line_total / qty if qty else line_total
        item = {
            "nazev": str(source.get("name") or "Položka objednávky"),
            "mnozMj": str(qty),
            "cenaMj": str(unit),
        }
        if line_total > 0 and line_tax >= 0:
            rate = (line_tax / line_total * Decimal("100")).quantize(Decimal("0.01"))
            item["szbDph"] = str(rate)
        if source.get("sku"):
            item["popis"] = f"SKU: {source.get('sku')}"
        items.append(item)
    for source in private_order.get("shipping") or []:
        if not isinstance(source, dict):
            continue
        total = Decimal(str(source.get("total") or 0))
        tax = Decimal(str(source.get("total_tax") or 0))
        item = {"nazev": str(source.get("name") or "Doprava"), "mnozMj": "1", "cenaMj": str(total)}
        if total > 0 and tax >= 0:
            item["szbDph"] = str((tax / total * Decimal("100")).quantize(Decimal("0.01")))
        items.append(item)
    for source in private_order.get("fees") or []:
        if not isinstance(source, dict):
            continue
        total = Decimal(str(source.get("total") or 0))
        tax = Decimal(str(source.get("total_tax") or 0))
        item = {"nazev": str(source.get("name") or "Poplatek"), "mnozMj": "1", "cenaMj": str(total)}
        if total > 0 and tax >= 0:
            item["szbDph"] = str((tax / total * Decimal("100")).quantize(Decimal("0.01")))
        items.append(item)
    return {
        "external_id": f"SHOP:{shop.id}:ORDER:{order.external_id}",
        "type_code": type_code or current_app.config.get("FLEXIBEE_INVOICE_TYPE_CODE", "FAKTURA"),
        "partner_code": "",
        "variable_symbol": variable_symbol,
        "description": f"Objednávka #{private_order.get('number') or order.external_id} · {shop.name}",
        "issued_on": issued_on,
        "due_on": due_on,
        "currency": str(private_order.get("currency") or order.currency or "CZK"),
        "amount": str(private_order.get("total") or order.total or 0),
        "billing": billing,
        "items": items,
        "source_order_id": order.id,
        "source_shop_id": shop.id,
    }


def _money(v):
    return f"{float(v or 0):,.0f}".replace(",", " ") + " Kč"


@web_bp.app_template_filter("money")
def money_filter(value):
    return _money(value)


@web_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))
    return redirect(url_for("web.login"))


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter(func.lower(User.email) == email).first()
        if user and user.check_password(password) and user.is_active:
            login_user(user, remember=bool(request.form.get("remember")))
            membership = Membership.query.filter_by(user_id=user.id).first()
            if membership:
                session["organization_id"] = membership.organization_id
                target = url_for("web.dashboard")
            else:
                shared = ProjectMember.query.filter(func.lower(ProjectMember.email) == email).first() if _project_members_ready() else None
                target = url_for("web.projects") if shared else url_for("web.dashboard")
            return redirect(request.args.get("next") or target)
        flash("Nesprávný e-mail nebo heslo.", "danger")
    return render_template("login.html", sso_url=current_app.config.get("FRESH_CRACKERS_SSO_URL"))


@web_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("web.login"))


@web_bp.route("/dashboard")
@login_required
def dashboard():
    org = _org_or_redirect()
    if not org:
        shared = (ProjectMember.query.filter(
            or_(ProjectMember.user_id == current_user.id, func.lower(ProjectMember.email) == str(current_user.email or "").lower())
        ).first() if _project_members_ready() else None)
        if shared:
            return redirect(url_for("web.projects"))
        logout_user()
        session.clear()
        return redirect(url_for("web.login"))

    today = date.today()
    ensure_scheduled_tasks(org.id, today)
    month_start = today.replace(day=1)
    income = db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.organization_id == org.id, Transaction.kind == "income", Transaction.booked_on >= month_start
    ).scalar() or 0
    expense = db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.organization_id == org.id, Transaction.kind == "expense", Transaction.booked_on >= month_start
    ).scalar() or 0
    profit = Decimal(income) - Decimal(expense)

    overdue_invoices = Invoice.query.filter(
        Invoice.organization_id == org.id, Invoice.direction == "issued", Invoice.paid.is_(False), Invoice.due_on < today
    ).order_by(Invoice.due_on.asc()).limit(6).all()
    due_tasks = Task.query.filter(
        Task.organization_id == org.id, Task.status != "done", Task.due_date.isnot(None), Task.due_date <= today + timedelta(days=14)
    ).order_by(Task.due_date.asc()).limit(8).all()
    attention_count = len(overdue_invoices) + Task.query.filter(
        Task.organization_id == org.id, Task.status != "done", Task.due_date.isnot(None), Task.due_date <= today
    ).count()

    # Last 6 months cashflow aggregated in Python for DB portability.
    months = []
    cursor = month_start
    for _ in range(5):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        months.append(cursor)
    months = list(reversed(months)) + [month_start]
    chart = []
    for start in months:
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        rows = db.session.query(
            Transaction.kind, func.coalesce(func.sum(Transaction.amount), 0)
        ).filter(
            Transaction.organization_id == org.id, Transaction.booked_on >= start, Transaction.booked_on < end
        ).group_by(Transaction.kind).all()
        values = {k: float(v or 0) for k, v in rows}
        chart.append({"label": start.strftime("%m/%Y"), "income": values.get("income", 0), "expense": values.get("expense", 0)})

    flexi_ok = FlexiBeeClient(org.id).configured
    core_ok = bool(current_app.config.get("FRESH_CORE_SHARED_SECRET"))
    shop_count = Shop.query.filter_by(organization_id=org.id, active=True).count()

    guide_state = _organization_guide_state(org)
    recent_jobs = AgentJob.query.filter_by(organization_id=org.id).order_by(AgentJob.id.desc()).limit(5).all()
    return render_template("dashboard.html", org=org, income=income, expense=expense, profit=profit,
                           attention_count=attention_count, overdue_invoices=overdue_invoices, due_tasks=due_tasks,
                           chart=chart, flexi_ok=flexi_ok, core_ok=core_ok, shop_count=shop_count, today=today,
                           guide_state=guide_state, recent_jobs=recent_jobs)


@web_bp.route("/tasks", methods=["GET", "POST"])
@login_required
def tasks():
    org = _org_or_redirect()
    ensure_scheduled_tasks(org.id)
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("Název úkolu je povinný.", "danger")
        else:
            due = request.form.get("due_date")
            task = Task(organization_id=org.id, title=title, task_type=request.form.get("task_type") or "general",
                        due_date=datetime.strptime(due, "%Y-%m-%d").date() if due else None,
                        priority=request.form.get("priority") or "normal", project_id=request.form.get("project_id") or None)
            db.session.add(task); db.session.commit(); flash("Úkol byl přidán.", "success")
            return redirect(url_for("web.tasks"))
    # MySQL does not support the SQL NULLS LAST syntax generated by nullslast().
    # Keep null dates behind dated tasks with a portable CASE expression.
    items = Task.query.filter_by(organization_id=org.id).order_by(
        Task.status.asc(),
        case((Task.due_date.is_(None), 1), else_=0).asc(),
        Task.due_date.asc(),
        Task.id.desc(),
    ).all()
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name).all()
    return render_template("tasks.html", items=items, projects=projects, org=org)


@web_bp.route("/tasks/<int:task_id>/toggle", methods=["POST"])
@login_required
def task_toggle(task_id):
    org = _org_or_redirect()
    task = Task.query.filter_by(id=task_id, organization_id=org.id).first_or_404()
    task.status = "open" if task.status == "done" else "done"
    db.session.commit()
    return redirect(request.referrer or url_for("web.tasks"))


@web_bp.route("/planner", methods=["GET", "POST"])
@login_required
def planner():
    org = _org_or_redirect()
    if request.method == "POST":
        action = request.form.get("action") or "create"
        if action == "create":
            title = (request.form.get("title") or "").strip()
            frequency = (request.form.get("frequency") or "monthly").strip()
            if not title:
                flash("Název plánované povinnosti je povinný.", "danger")
                return redirect(url_for("web.planner"))
            try:
                day = int(request.form.get("day_of_month") or 25) if frequency in {"monthly", "annual"} else None
                month = int(request.form.get("month_of_year") or 1) if frequency == "annual" else None
                reminder_days = max(0, min(int(request.form.get("reminder_days") or 3), 30))
            except ValueError:
                flash("Termín plánované povinnosti není platný.", "danger")
                return redirect(url_for("web.planner"))
            once = None
            if frequency == "once" and request.form.get("one_time_due_on"):
                try:
                    once = date.fromisoformat(request.form.get("one_time_due_on"))
                except ValueError:
                    flash("Jednorázové datum není platné.", "danger")
                    return redirect(url_for("web.planner"))
            schedule = TaskSchedule(
                organization_id=org.id,
                project_id=request.form.get("project_id") or None,
                title=title,
                task_type=request.form.get("task_type") or "tax",
                frequency=frequency,
                day_of_month=day,
                month_of_year=month,
                one_time_due_on=once,
                reminder_days=reminder_days,
                priority=request.form.get("priority") or "high",
                note=(request.form.get("note") or "").strip() or None,
                enabled=True,
            )
            db.session.add(schedule)
            db.session.commit()
            ensure_scheduled_tasks(org.id)
            flash("Plánovaná povinnost byla vytvořena.", "success")
            return redirect(url_for("web.planner"))
        if action in {"toggle", "delete"}:
            schedule = TaskSchedule.query.filter_by(id=request.form.get("schedule_id", type=int), organization_id=org.id).first_or_404()
            if action == "toggle":
                schedule.enabled = not schedule.enabled
                db.session.commit()
                flash("Plán byl aktualizován.", "success")
            else:
                db.session.delete(schedule)
                db.session.commit()
                flash("Plánovaná povinnost byla smazána. Již vytvořené úkoly zůstaly v historii.", "success")
            return redirect(url_for("web.planner"))
    ensure_scheduled_tasks(org.id)
    schedules = TaskSchedule.query.filter_by(organization_id=org.id).order_by(TaskSchedule.enabled.desc(), TaskSchedule.title.asc()).all()
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name.asc()).all()
    upcoming = reminder_items(org.id, limit=20)
    return render_template("planner.html", org=org, schedules=schedules, projects=projects, upcoming=upcoming, today=date.today())


@web_bp.route("/finance", methods=["GET", "POST"])
@login_required
def finance():
    org = _org_or_redirect()
    if request.method == "POST":
        try:
            amount = Decimal((request.form.get("amount") or "0").replace(" ", "").replace(",", "."))
        except InvalidOperation:
            flash("Částka není platná.", "danger")
            return redirect(url_for("web.finance"))
        booked = request.form.get("booked_on")
        tx = Transaction(organization_id=org.id, kind=request.form.get("kind") or "expense", amount=amount,
                         booked_on=datetime.strptime(booked, "%Y-%m-%d").date() if booked else date.today(),
                         category=(request.form.get("category") or "").strip() or None,
                         description=(request.form.get("description") or "").strip() or None,
                         project_id=request.form.get("project_id") or None)
        db.session.add(tx); db.session.commit(); flash("Položka byla uložena.", "success")
        return redirect(url_for("web.finance"))
    items = Transaction.query.filter_by(organization_id=org.id).order_by(Transaction.booked_on.desc(), Transaction.id.desc()).limit(100).all()
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name).all()
    invoices = Invoice.query.filter_by(organization_id=org.id).all()
    incoming = sum((Decimal(i.total or 0) for i in invoices if i.direction == 'received'), Decimal('0'))
    issued = sum((Decimal(i.total or 0) for i in invoices if i.direction == 'issued'), Decimal('0'))
    receivable = sum((max(Decimal(i.remaining or 0), Decimal('0')) for i in invoices if i.direction == 'issued' and not i.paid), Decimal('0'))
    marked_paid = sum((Decimal(i.total or 0) for i in invoices if i.direction == 'issued' and i.paid), Decimal('0'))
    actual_income = sum((Decimal(i.amount or 0) for i in items if i.kind == 'income'), Decimal('0'))
    actual_expense = sum((Decimal(i.amount or 0) for i in items if i.kind == 'expense'), Decimal('0'))
    finance_stats = dict(issued=issued, received=incoming, receivable=receivable,
                         invoiced_paid=marked_paid, income=actual_income, expense=actual_expense)
    monthly = {}
    for movement in items:
        if movement.booked_on:
            key = movement.booked_on.strftime('%Y-%m')
            entry = monthly.setdefault(key, {'month':key, 'income':0.0, 'expense':0.0})
            if movement.kind in ('income', 'expense'):
                entry[movement.kind] += float(movement.amount or 0)
    finance_chart = [monthly[k] for k in sorted(monthly)][-12:]
    return render_template("finance.html", items=items, projects=projects, org=org,
                           today=date.today(), finance_stats=finance_stats, finance_chart=finance_chart)


@web_bp.route("/projects", methods=["GET", "POST"])
@login_required
def projects():
    org = current_organization()
    if request.method == "POST":
        if org and not _can_edit_org(org):
            abort(403)
        if not org:
            flash("Nový projekt můžeš vytvořit až v rámci firmy v Edovi.", "danger")
            return redirect(url_for("web.projects"))
        name = (request.form.get("name") or "").strip()
        if name:
            db.session.add(Project(organization_id=org.id, name=name, code=(request.form.get("code") or "").strip() or None,
                                   project_type=request.form.get("project_type") or "other"))
            db.session.commit(); flash("Projekt byl přidán.", "success")
        return redirect(url_for("web.projects"))

    email = str(current_user.email or "").strip().lower()
    # Fresh Key membership never grants Eda access. Local organization/project ACL remains authoritative.
    # A login with the invited e-mail activates pending local project access.
    pending_local = []
    shared_rows = []
    if _project_members_ready():
        try:
            pending_local = ProjectMember.query.filter(
                func.lower(ProjectMember.email) == email, ProjectMember.status == "pending"
            ).all()
            for row in pending_local:
                row.user_id = current_user.id; row.status = "active"
            if pending_local:
                db.session.commit()
            shared_rows = ProjectMember.query.filter(
                or_(ProjectMember.user_id == current_user.id, func.lower(ProjectMember.email) == email),
                ProjectMember.status == "active",
            ).all()
        except Exception as exc:
            _project_members_fail(exc)
            pending_local = []
            shared_rows = []

    org_membership = Membership.query.filter_by(user_id=current_user.id,organization_id=org.id).first() if org else None
    base_items = Project.query.filter_by(organization_id=org.id).all() if org and (current_user.is_admin or (org_membership and org_membership.role in {"owner", "admin"})) else []
    by_id = {p.id: p for p in base_items}
    for access in shared_rows:
        if access.project:
            by_id[access.project.id] = access.project
    items = sorted(by_id.values(), key=lambda x: (not x.active, (x.name or "").lower()))

    stats = {p.id: _project_financial_state(p.organization_id, p) for p in items}
    project_roles = {p.id: _project_access_role(p) for p in items}
    fresh_key_requests = []
    if fresh_key.configured():
        try:
            fresh_key.bootstrap_account(current_user.id, current_user.email, current_user.name or current_user.email)
            fresh_key_requests = fresh_key.pending_link_requests(current_user.id)
        except Exception:
            fresh_key_requests = []
    if not _project_members_ready() and _project_members_migration_error():
        flash("Sdílení projektů čeká na databázovou opravu; samotné projekty a finance zůstávají dostupné.", "warning")
    return render_template("projects.html", items=items, stats=stats, project_roles=project_roles, org=org, can_create=bool(org), fresh_key_requests=fresh_key_requests)


@web_bp.route("/projects/<int:project_id>")
@login_required
def project_detail(project_id):
    project = _project_for_current_user(project_id)
    org = project.organization
    transactions = Transaction.query.filter_by(organization_id=org.id, project_id=project.id).order_by(Transaction.booked_on.desc(), Transaction.id.desc()).limit(200).all()
    invoices = Invoice.query.filter_by(organization_id=org.id, project_id=project.id).order_by(Invoice.issued_on.desc(), Invoice.id.desc()).limit(200).all()
    documents = InboxDocument.query.filter_by(organization_id=org.id, project_id=project.id).order_by(InboxDocument.id.desc()).limit(100).all()
    shops = Shop.query.filter_by(organization_id=org.id, project_id=project.id).order_by(Shop.name.asc()).all()
    shop_ids = [x.id for x in shops]
    orders = Order.query.filter(Order.organization_id == org.id, Order.shop_id.in_(shop_ids)).order_by(Order.ordered_at.desc(), Order.id.desc()).all() if shop_ids else []
    financial = _project_financial_state(org.id, project, transactions=transactions, invoices=invoices, documents=documents, orders=orders)
    fresh_link = {"linked": False, "applications": [], "members": [], "can_manage": False}
    fresh_key_error = ""; fresh_key_requests = []
    if fresh_key.configured():
        try:
            fresh_key.bootstrap_account(current_user.id, current_user.email, current_user.name or current_user.email)
            website = next((x.site_url for x in shops if x.site_url), "")
            fresh_link = fresh_key.project_summary(current_user.id, project, website_url=website, role=_project_access_role(project) or "viewer")
            fresh_key_requests = [x for x in fresh_key.pending_link_requests(current_user.id) if str(x.get("target_external_project_id") or "") == str(project.id)]
        except Exception as exc:
            fresh_key_error = str(exc)
    else:
        fresh_key_error = "Fresh Key není nakonfigurován."
    return render_template("project_detail.html", org=org, project=project, transactions=transactions, invoices=invoices,
                           documents=documents, shops=shops, orders=orders, financial=financial, fresh_link=fresh_link, fresh_key_error=fresh_key_error, fresh_key_requests=fresh_key_requests,
                           project_role=_project_access_role(project), can_manage_project=_can_manage_project(project), project_people=_project_people_local(project),
                           can_access_org=bool(current_user.is_admin or Membership.query.filter_by(user_id=current_user.id, organization_id=org.id).first()),
                           income=financial["income"], expense=financial["expense"], profit=financial["accounting_profit"],
                           order_total=financial["order_total"], pending_doc_total=financial["pending_doc_total"])


@web_bp.get("/fresh-key/login")
@login_required
def fresh_key_login():
    project_id = int(request.args.get("project_id") or 0)
    state = secrets.token_urlsafe(32)
    session["fresh_key_login_state"] = state
    session["fresh_key_login_project"] = project_id
    return_url = (current_app.config.get("APP_URL") or request.url_root.rstrip("/")).rstrip("/") + url_for("web.fresh_key_callback")
    try:
        return redirect(fresh_key.authorize_url(state, return_url))
    except Exception as exc:
        flash(f"Fresh účet se nepodařilo otevřít: {exc}", "danger")
        return redirect(url_for("web.project_detail", project_id=project_id) if project_id else url_for("web.dashboard"))


@web_bp.get("/fresh-key/callback")
@login_required
def fresh_key_callback():
    state = str(request.args.get("state") or "")
    code = str(request.args.get("fresh_key_code") or "")
    expected = str(session.pop("fresh_key_login_state", "") or "")
    project_id = int(session.pop("fresh_key_login_project", 0) or 0)
    if not expected or not state or not hmac.compare_digest(expected, state) or not code:
        flash("Fresh účet se nepodařilo bezpečně ověřit.", "danger")
        return redirect(url_for("web.project_detail", project_id=project_id) if project_id else url_for("web.dashboard"))
    try:
        data = fresh_key.sso_exchange(code)
        ident = data.get("identity") or {}
        email = str(ident.get("email") or "").strip().lower()
        if email != str(current_user.email or "").strip().lower():
            raise RuntimeError("Fresh účet používá jiný e-mail než Eda.")
        token = str(data.get("binding_token") or "")
        external = str(data.get("external_user_id") or "")
        if token:
            fresh_key.sso_bind(token, current_user.id)
        elif external != str(current_user.id):
            raise RuntimeError("Fresh účet je v Edovi propojen s jiným uživatelem.")
        flash("Fresh účet je propojený.", "success")
    except Exception as exc:
        flash(f"Fresh účet se nepodařilo propojit: {exc}", "danger")
    return redirect(url_for("web.project_detail", project_id=project_id) if project_id else url_for("web.dashboard"))


@web_bp.post("/projects/<int:project_id>/linking/publish")
@login_required
def project_linking_publish(project_id):
    project = _project_for_current_user(project_id)
    if not _can_manage_project(project): abort(403)
    try:
        fresh_key.bootstrap_account(current_user.id, current_user.email, current_user.name or current_user.email)
        website = next((x.site_url for x in project.shops if x.site_url), "")
        link_id = fresh_key.publish_project(current_user.id, project, website_url=website, role=_project_access_role(project) or "viewer")
        for person in _project_people_local(project):
            if str(person.get("email") or "").lower() == str(current_user.email or "").lower(): continue
            try: fresh_key.add_member(current_user.id, link_id, str(person.get("email") or ""), str(person.get("role") or "viewer"), notify=False)
            except Exception: pass
        flash("Projekt byl odeslán do Fresh Key. V AI Enginu se zobrazí jako propojení s Edou.", "success")
    except Exception as exc:
        flash(f"Odeslání do Fresh Key se nepodařilo: {exc}", "danger")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web_bp.post("/projects/<int:project_id>/linking/request/accept")
@login_required
def project_linking_request_accept(project_id):
    project = _project_for_current_user(project_id)
    if not _can_manage_project(project): abort(403)
    try:
        website = next((x.site_url for x in project.shops if x.site_url), "")
        accepted = fresh_key.accept_link_request(current_user.id, str(request.form.get("request_id") or ""), project, website_url=website, role=_project_access_role(project) or "viewer")
        link_id = str(accepted.get("fresh_key_id") or "")
        if link_id:
            for person in _project_people_local(project):
                if str(person.get("email") or "").lower() == str(current_user.email or "").lower():
                    continue
                try: fresh_key.add_member(current_user.id, link_id, str(person.get("email") or ""), str(person.get("role") or "viewer"), notify=False)
                except Exception: pass
        flash("Propojení bylo potvrzeno.", "success")
    except Exception as exc:
        flash(f"Propojení se nepodařilo potvrdit: {exc}", "danger")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web_bp.post("/fresh-key/link-request/decline")
@login_required
def fresh_key_link_request_decline():
    request_id = str(request.form.get("request_id") or "")
    back = str(request.form.get("back") or "projects")
    try:
        fresh_key.decline_link_request(current_user.id, request_id)
        flash("Návrh propojení byl odmítnut.", "success")
    except Exception as exc:
        flash(f"Návrh se nepodařilo odmítnout: {exc}", "danger")
    if back.isdigit():
        return redirect(url_for("web.project_detail", project_id=int(back)))
    return redirect(url_for("web.projects"))


@web_bp.post("/fresh-key/link-request/<request_id>/create")
@login_required
def fresh_key_link_request_create(request_id):
    org = current_organization()
    if not org:
        flash("Nový projekt v Edovi lze vytvořit jen v rámci firmy, ke které máš přístup.", "danger")
        return redirect(url_for("web.projects"))
    try:
        pending = fresh_key.pending_link_requests(current_user.id)
        req = next((x for x in pending if str(x.get("id") or "") == request_id), None)
        if not req or req.get("target_external_project_id"):
            raise RuntimeError("Návrh na vytvoření projektu už není aktivní.")
        name = (request.form.get("name") or req.get("target_project_name") or req.get("link_name") or "Projekt").strip()
        project = Project(organization_id=org.id, name=name, code=(request.form.get("code") or "").strip() or None,
                          project_type=request.form.get("project_type") or "other")
        db.session.add(project); db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=current_user.id, email=str(current_user.email or "").lower(),
                                     role="manager", relationship="member", status="active", invited_by=current_user.id))
        db.session.commit()
        fresh_key.register_project(current_user.id, project, role="manager")
        fresh_key.accept_link_request(current_user.id, request_id, project, role="manager")
        flash("Nový projekt byl vytvořen v Edovi a propojen po tvém potvrzení.", "success")
        return redirect(url_for("web.project_detail", project_id=project.id))
    except Exception as exc:
        db.session.rollback()
        flash(f"Projekt se nepodařilo vytvořit: {exc}", "danger")
        return redirect(url_for("web.projects"))


@web_bp.post("/projects/<int:project_id>/linking/member")
@login_required
def project_linking_member_add(project_id):
    project = _project_for_current_user(project_id)
    if not _can_manage_project(project): abort(403)
    email = (request.form.get("email") or "").strip().lower()
    role = _normalize_access_role(request.form.get("role") or "editor")
    if role == "owner": role = "manager"
    relationship = _normalize_relationship(request.form.get("relationship") or "member")
    try:
        row = _upsert_project_member_local(project, email, role, relationship, current_user.id)
        notice = _notify_local_project_share(project, email, role)
        link = _fresh_link_for_project(project)
        if link:
            try: fresh_key.add_member(current_user.id, link["fresh_key_id"], email, role, notify=False)
            except Exception: pass
        if row and row.status == "active":
            flash("Uživatel byl přidán do projektu v Edovi a dostal potvrzení spolupráce.", "success")
        else:
            flash("Pozvánka k projektu byla uložena a odeslána e-mailem.", "success")
        if not notice.get("email_sent"):
            flash("Přístup/pozvánka je uložená, ale e-mail se nepodařilo odeslat. Zkontroluj SMTP v AI Enginu.", "warning")
    except Exception as exc:
        flash(f"Sdílení se nepodařilo: {exc}", "danger")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web_bp.post("/projects/<int:project_id>/linking/member/update")
@login_required
def project_linking_member_update(project_id):
    project = _project_for_current_user(project_id)
    if not _can_manage_project(project): abort(403)
    email = (request.form.get("email") or "").strip().lower()
    role = _normalize_access_role(request.form.get("role") or "editor")
    if role == "owner": role = "manager"
    relationship = _normalize_relationship(request.form.get("relationship") or "member")
    try:
        if not _project_members_ready():
            raise RuntimeError("Sdílení projektu čeká na databázovou opravu project_members.")
        row=ProjectMember.query.filter_by(project_id=project.id, email=email).first()
        if row:
            row.role=role; row.relationship=relationship; db.session.commit()
        link=_fresh_link_for_project(project)
        if link:
            target=next((m for m in link.get("members",[]) if str(m.get("email") or "").lower()==email),None)
            if target:
                try: fresh_key.update_member(current_user.id,link["fresh_key_id"],str(target.get("identity_id") or ""),role)
                except Exception: pass
        flash("Role uživatele byla upravena v Edovi.", "success")
    except Exception as exc:
        db.session.rollback(); flash(f"Úprava se nepodařila: {exc}", "danger")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web_bp.post("/projects/<int:project_id>/linking/member/remove")
@login_required
def project_linking_member_remove(project_id):
    project = _project_for_current_user(project_id)
    if not _can_manage_project(project): abort(403)
    email = (request.form.get("email") or "").strip().lower()
    try:
        if not _project_members_ready():
            raise RuntimeError("Sdílení projektu čeká na databázovou opravu project_members.")
        row=ProjectMember.query.filter_by(project_id=project.id, email=email).first()
        if row: db.session.delete(row); db.session.commit()
        link=_fresh_link_for_project(project)
        if link:
            target=next((m for m in link.get("members",[]) if str(m.get("email") or "").lower()==email),None)
            if target:
                try: fresh_key.remove_member(current_user.id,link["fresh_key_id"],str(target.get("identity_id") or ""))
                except Exception: pass
        flash("Uživatel nebo pozvánka byly z projektu odebrány.", "success")
    except Exception as exc:
        db.session.rollback(); flash(f"Odebrání se nepodařilo: {exc}", "danger")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web_bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def project_edit(project_id):
    project = _project_for_current_user(project_id)
    org = project.organization
    if not _can_manage_project(project): abort(403)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Název projektu je povinný.", "danger")
            return redirect(url_for("web.project_edit", project_id=project.id))
        project.name = name
        project.code = (request.form.get("code") or "").strip() or None
        project.project_type = request.form.get("project_type") or "other"
        project.active = bool(request.form.get("active"))
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="project", action="updated", entity_type="project", entity_id=str(project.id)))
        db.session.commit()
        flash("Projekt byl upraven.", "success")
        return redirect(url_for("web.project_detail", project_id=project.id))
    return render_template("project_edit.html", org=org, project=project)


@web_bp.post("/projects/<int:project_id>/delete")
@login_required
def project_delete(project_id):
    project = _project_for_current_user(project_id)
    org = project.organization
    if not _can_edit_org(org): abort(403)
    # Preserve accounting history: detach linked records instead of deleting them.
    Task.query.filter_by(organization_id=org.id, project_id=project.id).update({"project_id": None})
    Transaction.query.filter_by(organization_id=org.id, project_id=project.id).update({"project_id": None})
    Shop.query.filter_by(organization_id=org.id, project_id=project.id).update({"project_id": None})
    Invoice.query.filter_by(organization_id=org.id, project_id=project.id).update({"project_id": None})
    InboxDocument.query.filter_by(organization_id=org.id, project_id=project.id).update({"project_id": None})
    TaskSchedule.query.filter_by(organization_id=org.id, project_id=project.id).update({"project_id": None})
    OrderAccountingSnapshot.query.filter_by(organization_id=org.id, project_id=project.id).update({"project_id": None})
    db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="project", action="deleted", entity_type="project", entity_id=str(project.id), detail_json=json.dumps({"name": project.name}, ensure_ascii=False)))
    db.session.delete(project)
    db.session.commit()
    flash("Projekt byl smazán. Účetní historie zůstala zachována bez projektové vazby.", "success")
    return redirect(url_for("web.projects"))


@web_bp.route("/shops", methods=["GET", "POST"])
@login_required
def shops():
    org = _org_or_redirect()
    if request.method == "POST":
        url = (request.form.get("site_url") or "").strip().rstrip("/")
        existing = Shop.query.filter_by(organization_id=org.id, site_url=url).first() if url else None
        if existing:
            flash("E-shop s touto URL už existuje.", "warning")
            return redirect(url_for("web.shops"))
        shop = Shop(organization_id=org.id, name=(request.form.get("name") or "E-shop").strip(),
                    platform=request.form.get("platform") or "woocommerce", site_url=url or None,
                    project_id=request.form.get("project_id") or None)
        db.session.add(shop)
        db.session.flush()
        manifest_url = (request.form.get("manifest_url") or "").strip()
        if manifest_url:
            conn = _shop_connection(org.id, shop.id, create=True)
            conn.base_url = manifest_url
            conn.status = "not_tested"
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="shop", action="created", entity_type="shop", entity_id=str(shop.id)))
        db.session.commit()
        flash("E-shop byl přidán.", "success")
        return redirect(url_for("web.shops"))
    items = Shop.query.filter_by(organization_id=org.id).order_by(Shop.name).all()
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name).all()
    order_counts = dict(db.session.query(Order.shop_id, func.count(Order.id)).join(Shop).filter(Shop.organization_id == org.id).group_by(Order.shop_id).all())
    connections = {x.id: _shop_connection(org.id, x.id) for x in items}
    accounting_connections = {x.id: _shop_accounting_connection(org.id, x.id) for x in items}
    return render_template("shops.html", items=items, projects=projects, order_counts=order_counts, connections=connections, accounting_connections=accounting_connections, org=org)


@web_bp.route("/shops/<int:shop_id>")
@login_required
def shop_detail(shop_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    orders = Order.query.filter_by(organization_id=org.id, shop_id=shop.id).order_by(Order.ordered_at.desc(), Order.id.desc()).limit(300).all()
    total = sum((Decimal(o.total or 0) for o in orders), Decimal("0"))
    by_status = dict(db.session.query(Order.status, func.count(Order.id)).filter_by(organization_id=org.id, shop_id=shop.id).group_by(Order.status).all())
    connection = _shop_connection(org.id, shop.id)
    accounting = _shop_accounting_connection(org.id, shop.id)
    invoice_docs = EdaInvoiceDocument.query.filter_by(organization_id=org.id, shop_id=shop.id).all() if current_app.config.get('EDA_INVOICING_READY', False) else []
    invoice_by_order = {doc.order_id: doc for doc in invoice_docs}
    from .models import EdaAdvanceDocument
    advance_docs = EdaAdvanceDocument.query.filter_by(organization_id=org.id,shop_id=shop.id).all() if current_app.config.get('EDA_INVOICING_READY',False) else []
    advance_by_order = {doc.order_id:doc for doc in advance_docs}
    # Privacy: show customer names only when their encrypted accounting details were previously loaded.
    snapshots = OrderAccountingSnapshot.query.filter_by(organization_id=org.id, shop_id=shop.id).all()
    customer_by_order = {}
    for snapshot in snapshots:
        payload = _private_order_payload(snapshot)
        if payload:
            billing = payload.get('billing') or {}
            customer = (billing.get('company') or (' '.join(filter(None, [billing.get('first_name'), billing.get('last_name')])))).strip()
            if customer:
                customer_by_order[snapshot.order_id] = customer
    can_issue = _can_edit_org(org) and current_app.config.get('EDA_INVOICING_READY', False)
    return render_template('shop_detail.html', org=org, shop=shop, orders=orders, total=total,
       by_status=by_status, connection=connection, accounting=accounting,
       invoice_by_order=invoice_by_order, advance_by_order=advance_by_order, customer_by_order=customer_by_order, can_issue=can_issue)


@web_bp.route("/shops/<int:shop_id>/edit", methods=["GET", "POST"])
@login_required
def shop_edit(shop_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    connection = _shop_connection(org.id, shop.id, create=True)
    accounting = _shop_accounting_connection(org.id, shop.id, create=True)
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name).all()
    if request.method == "POST":
        shop.name = (request.form.get("name") or "E-shop").strip()
        shop.platform = request.form.get("platform") or "woocommerce"
        shop.site_url = (request.form.get("site_url") or "").strip().rstrip("/") or None
        shop.project_id = request.form.get("project_id") or None
        shop.active = bool(request.form.get("active"))
        connection.base_url = (request.form.get("manifest_url") or "").strip() or None
        if not connection.base_url:
            connection.status = "not_configured"
        accounting.orders_endpoint = (request.form.get("accounting_orders_endpoint") or "").strip() or accounting.orders_endpoint
        if accounting.orders_endpoint and not accounting.detail_endpoint:
            accounting.detail_endpoint = accounting.orders_endpoint.rstrip("/") + "/{id}"
        submitted_key = (request.form.get("accounting_key") or "").strip()
        if submitted_key:
            accounting.encrypted_key = encrypt_text(submitted_key)
            accounting.status = "not_tested"
            accounting.last_error = None
        if request.form.get("clear_accounting_key"):
            accounting.encrypted_key = None
            accounting.status = "not_configured"
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="shop", action="updated", entity_type="shop", entity_id=str(shop.id)))
        db.session.commit()
        flash("E-shop byl upraven.", "success")
        return redirect(url_for("web.shop_detail", shop_id=shop.id))
    accounting_key_mask = "uložen" if accounting.encrypted_key else ""
    return render_template("shop_edit.html", org=org, shop=shop, projects=projects, connection=connection, accounting=accounting, accounting_key_mask=accounting_key_mask)


@web_bp.post("/shops/<int:shop_id>/test")
@login_required
def shop_test(shop_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    connection = _shop_connection(org.id, shop.id, create=True)
    try:
        result = test_fresh_core_connection(org.id, shop, connection)
        if connection.base_url and result["status"] in {"connected", "sync_ready"}:
            try:
                discovered = discover_from_manifest(connection.base_url)
                accounting = _shop_accounting_connection(org.id, shop.id, create=True)
                accounting.orders_endpoint = discovered["orders_endpoint"]
                accounting.detail_endpoint = discovered["detail_endpoint"]
                accounting.auth_header = discovered["auth_header"]
                accounting.remote_enabled = discovered["remote_enabled"]
                accounting.plugin_version = discovered["plugin_version"]
                if not accounting.encrypted_key:
                    accounting.status = "key_missing" if discovered["remote_enabled"] else "remote_disabled"
                db.session.commit()
            except FreshCoreAccountingError:
                pass
        if result["status"] in {"connected", "sync_ready"}:
            flash(f"Fresh Core konektor odpovídá ({result['elapsed_ms']} ms).", "success")
        elif result["status"] == "web_reachable":
            flash("Web odpovídá, ale není nastaven Fresh Core manifest. Samotná URL webu objednávky nesynchronizuje.", "warning")
        else:
            flash(result.get("message") or "Konektivitu se nepodařilo ověřit.", "danger")
    except FreshCoreError as exc:
        flash(f"Fresh Core: {exc}", "danger")
    return redirect(request.referrer or url_for("web.shop_detail", shop_id=shop.id))


@web_bp.post("/shops/<int:shop_id>/sync")
@login_required
def shop_sync(shop_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    connection = _shop_connection(org.id, shop.id, create=True)
    accounting = _shop_accounting_connection(org.id, shop.id)
    try:
        result = sync_fresh_core_orders(org.id, shop, connection, accounting=accounting)
        count = int(result.get("items") or 0)
        source = result.get("source") or "Fresh Core"
        if accounting and accounting.encrypted_key and accounting.orders_endpoint:
            accounting.status = "connected"
            accounting.last_checked_at = datetime.utcnow()
            accounting.last_error = None
            db.session.commit()
        flash(f"Synchronizace dokončena: {count} objednávek ({source}).", "success")
    except FreshCoreError as exc:
        if accounting and accounting.encrypted_key and accounting.orders_endpoint:
            accounting.status = "error"
            accounting.last_checked_at = datetime.utcnow()
            accounting.last_error = str(exc)[:1000]
            db.session.add(SyncLog(
                organization_id=org.id, integration=f"shop_accounting:{shop.id}",
                status="error", items=0, message=str(exc)[:1000],
            ))
            db.session.commit()
        flash(str(exc), "warning")
    return redirect(request.referrer or url_for("web.shop_detail", shop_id=shop.id))


@web_bp.post("/shops/<int:shop_id>/accounting/discover")
@login_required
def shop_accounting_discover(shop_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    manifest = _shop_connection(org.id, shop.id)
    if not manifest or not manifest.base_url:
        flash("Nejdřív nastav Fresh Core manifest URL.", "warning")
        return redirect(url_for("web.shop_edit", shop_id=shop.id))
    accounting = _shop_accounting_connection(org.id, shop.id, create=True)
    try:
        discovered = discover_from_manifest(manifest.base_url)
        accounting.orders_endpoint = discovered["orders_endpoint"]
        accounting.detail_endpoint = discovered["detail_endpoint"]
        accounting.auth_header = discovered["auth_header"]
        accounting.remote_enabled = discovered["remote_enabled"]
        accounting.plugin_version = discovered["plugin_version"]
        accounting.status = "key_missing" if discovered["remote_enabled"] and not accounting.encrypted_key else ("remote_disabled" if not discovered["remote_enabled"] else "not_tested")
        accounting.last_error = None
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="shop_accounting", action="discovered", entity_type="shop", entity_id=str(shop.id), detail_json=json.dumps({"plugin_version": accounting.plugin_version, "remote_enabled": accounting.remote_enabled}, ensure_ascii=False)))
        db.session.commit()
        if discovered["remote_enabled"]:
            flash("Accounting API bylo načteno z Fresh Core manifestu. Teď vlož Accounting key a ověř spojení.", "success")
        else:
            flash("Fresh Core Accounting bridge je na e-shopu zatím vypnutý. Zapni ho ve WordPressu a obnov manifest.", "warning")
    except FreshCoreAccountingError as exc:
        accounting.status = "error"
        accounting.last_error = str(exc)[:1000]
        db.session.commit()
        flash(str(exc), "danger")
    return redirect(url_for("web.shop_edit", shop_id=shop.id))


@web_bp.post("/shops/<int:shop_id>/accounting/test")
@login_required
def shop_accounting_test(shop_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    accounting = _shop_accounting_connection(org.id, shop.id, create=True)
    try:
        result = FreshCoreAccountingClient(accounting).ping()
        accounting.status = "connected"
        accounting.last_checked_at = datetime.utcnow()
        accounting.last_error = None
        accounting.plugin_version = result.get("version") or accounting.plugin_version
        db.session.add(SyncLog(organization_id=org.id, integration=f"shop_accounting:{shop.id}", status="ok", items=0, message=f"Accounting API připojeno; Fresh Core {accounting.plugin_version or '—'}"))
        db.session.commit()
        flash("Fresh Core Accounting API je připojené a klíč je platný.", "success")
    except FreshCoreAccountingError as exc:
        accounting.status = "error"
        accounting.last_checked_at = datetime.utcnow()
        accounting.last_error = str(exc)[:1000]
        db.session.commit()
        flash(str(exc), "danger")
    return redirect(request.referrer or url_for("web.shop_edit", shop_id=shop.id))


@web_bp.get("/shops/<int:shop_id>/orders/<int:order_id>")
@login_required
def shop_order_detail(shop_id, order_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    order = Order.query.filter_by(id=order_id, organization_id=org.id, shop_id=shop.id).first_or_404()
    accounting = _shop_accounting_connection(org.id, shop.id)
    snapshot = _order_snapshot(org.id, shop.id, order.id)
    private_order = _private_order_payload(snapshot)
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name).all()
    proposal = _invoice_payload_from_order(shop, order, private_order) if private_order else None
    eda_invoice = EdaInvoiceDocument.query.filter_by(shop_id=shop.id, order_id=order.id).first() if current_app.config.get("EDA_INVOICING_READY", False) else None
    from .models import EdaAdvanceDocument, InvoiceIssuerSettings
    issuer_settings = InvoiceIssuerSettings.query.filter_by(organization_id=org.id).first() if current_app.config.get("EDA_INVOICING_READY", False) else None
    eda_advance = EdaAdvanceDocument.query.filter_by(shop_id=shop.id,order_id=order.id).first() if current_app.config.get('EDA_INVOICING_READY',False) else None
    eda_invoice_url = None
    if eda_invoice and not eda_invoice.revoked:
        try:
            from .invoicing import _public_doc_url
            eda_invoice_url = _public_doc_url(eda_invoice)
        except Exception:
            pass
    eda_advance_url = None
    if eda_advance and not eda_advance.revoked:
        try:
            from .invoicing import _public_doc_url
            eda_advance_url=_public_doc_url(eda_advance)
        except Exception: pass
    from .services.eda_invoice_pdf import INVOICE_LOGIC_VERSION
    return render_template("order_detail.html", org=org, shop=shop, order=order, accounting=accounting, snapshot=snapshot, private_order=private_order, projects=projects, proposal=proposal, flexi_configured=FlexiBeeClient(org.id).configured, eda_invoice=eda_invoice, eda_invoice_url=eda_invoice_url, eda_advance=eda_advance, eda_advance_url=eda_advance_url, issuer_vat_mode=(issuer_settings.vat_mode if issuer_settings else "unknown"), invoice_logic_version=INVOICE_LOGIC_VERSION)


@web_bp.post("/shops/<int:shop_id>/orders/<int:order_id>/accounting/load")
@login_required
def shop_order_accounting_load(shop_id, order_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    order = Order.query.filter_by(id=order_id, organization_id=org.id, shop_id=shop.id).first_or_404()
    accounting = _shop_accounting_connection(org.id, shop.id)
    if not accounting:
        flash("E-shop nemá nastavené Accounting API.", "warning")
        return redirect(url_for("web.shop_order_detail", shop_id=shop.id, order_id=order.id))
    snapshot = _order_snapshot(org.id, shop.id, order.id, create=True)
    try:
        payload = FreshCoreAccountingClient(accounting).order_detail(order.external_id)
        snapshot.encrypted_payload = encrypt_text(serialize_private_order(payload))
        snapshot.project_id = snapshot.project_id or shop.project_id
        snapshot.status = "loaded"
        snapshot.fetched_at = datetime.utcnow()
        snapshot.last_error = None
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="order_accounting", action="private_detail_loaded", entity_type="order", entity_id=str(order.id), detail_json=json.dumps({"shop_id": shop.id, "external_id": order.external_id}, ensure_ascii=False)))
        db.session.commit()
        flash("Fakturační údaje objednávky byly bezpečně načteny z Fresh Core Accounting API.", "success")
    except FreshCoreAccountingError as exc:
        snapshot.status = "error"
        snapshot.last_error = str(exc)[:1000]
        db.session.commit()
        flash(str(exc), "danger")
    return redirect(url_for("web.shop_order_detail", shop_id=shop.id, order_id=order.id))


@web_bp.post("/shops/<int:shop_id>/orders/<int:order_id>/invoice/prepare")
@login_required
def shop_order_invoice_prepare(shop_id, order_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    order = Order.query.filter_by(id=order_id, organization_id=org.id, shop_id=shop.id).first_or_404()
    snapshot = _order_snapshot(org.id, shop.id, order.id)
    private_order = _private_order_payload(snapshot)
    if not snapshot or not private_order:
        flash("Nejdřív načti účetní detail objednávky z Fresh Core.", "warning")
        return redirect(url_for("web.shop_order_detail", shop_id=shop.id, order_id=order.id))
    if not FlexiBeeClient(org.id).configured:
        flash("Pro tuto firmu ještě není připojená ABRA Flexi.", "warning")
        return redirect(url_for("web.shop_order_detail", shop_id=shop.id, order_id=order.id))
    project_id = request.form.get("project_id", type=int)
    if project_id:
        project = Project.query.filter_by(id=project_id, organization_id=org.id).first()
        snapshot.project_id = project.id if project else snapshot.project_id
    type_code = (request.form.get("type_code") or current_app.config.get("FLEXIBEE_INVOICE_TYPE_CODE", "FAKTURA")).strip()
    due_on = (request.form.get("due_on") or "").strip() or None
    payload = _invoice_payload_from_order(shop, order, private_order, type_code=type_code, due_on=due_on)
    payload["order_accounting_snapshot_id"] = snapshot.id
    payload["project_id"] = snapshot.project_id
    prompt = f"Připrav a po schválení vystav fakturu k objednávce #{private_order.get('number') or order.external_id} z e-shopu {shop.name}."
    job = create_job(org.id, current_user.id, prompt, job_type="create_invoice_request", extra=payload)
    snapshot.status = "invoice_prepared"
    snapshot.invoice_external_ref = payload["external_id"]
    db.session.commit()
    execute_job(job, allow_write=False)
    return redirect(url_for("web.worker_job", job_id=job.id))


@web_bp.post("/shops/<int:shop_id>/delete")
@login_required
def shop_delete(shop_id):
    org = _org_or_redirect()
    shop = Shop.query.filter_by(id=shop_id, organization_id=org.id).first_or_404()
    order_count = Order.query.filter_by(organization_id=org.id, shop_id=shop.id).count()
    if order_count:
        shop.active = False
        flash("E-shop obsahuje objednávky, proto byl archivován místo smazání. Historie objednávek zůstala zachována.", "warning")
    else:
        conn = _shop_connection(org.id, shop.id)
        if conn:
            db.session.delete(conn)
        db.session.delete(shop)
        flash("E-shop byl smazán.", "success")
    db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="shop", action="deleted_or_archived", entity_type="shop", entity_id=str(shop.id), detail_json=json.dumps({"orders": order_count}, ensure_ascii=False)))
    db.session.commit()
    return redirect(url_for("web.shops"))


@web_bp.route("/integrations")
@login_required
def integrations():
    org = _org_or_redirect()
    flexi_client = FlexiBeeClient(org.id)
    flexi = {
        "configured": flexi_client.configured,
        "base_url": flexi_client.base,
        "company": flexi_client.company,
    }
    core = {"configured": bool(current_app.config.get("FRESH_CORE_SHARED_SECRET"))}
    return render_template("integrations.html", org=org, flexi=flexi, core=core)


@web_bp.route("/companies", methods=["GET", "POST"])
@login_required
def companies():
    org = _org_or_redirect()
    if not org:
        return redirect(url_for("web.dashboard"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "create":
            ico = (request.form.get("ico") or "").strip()
            name = (request.form.get("name") or "").strip()
            company = None
            if ico:
                try:
                    company = ares_get_company(ico)
                except AresError as exc:
                    flash(f"ARES: {exc}", "danger")
                    return redirect(url_for("web.companies"))
                if not company:
                    flash("ARES subjekt s tímto IČO nenašel.", "danger")
                    return redirect(url_for("web.companies"))
                name = company.name or name
                ico = company.ico
            if not name:
                flash("Zadej název firmy nebo IČO pro načtení z ARES.", "danger")
                return redirect(url_for("web.companies"))
            new_org = Organization(name=name, ico=ico or None, currency=(request.form.get("currency") or "CZK").upper()[:8])
            db.session.add(new_org)
            db.session.flush()
            db.session.add(Membership(user_id=current_user.id, organization_id=new_org.id, role="owner"))
            if company:
                _apply_ares_company(new_org, company)
            else:
                _ensure_org_profile(new_org)
            db.session.add(AuditLog(organization_id=new_org.id, user_id=current_user.id, category="organization", action="created", entity_type="organization", entity_id=str(new_org.id), detail_json=json.dumps({"ico": new_org.ico, "ares": bool(company)}, ensure_ascii=False)))
            db.session.commit()
            session["organization_id"] = new_org.id
            flash(f"Firma {new_org.name} byla přidána k účtu.", "success")
            return redirect(url_for("web.companies"))

        if not _can_edit_org(org):
            flash("Tuto firmu nemůžeš upravovat.", "danger")
            return redirect(url_for("web.companies"))

        if action == "verify_ares":
            ico = (request.form.get("ico") or org.ico or "").strip()
            if not ico:
                flash("Nejdřív vyplň IČO.", "danger")
                return redirect(url_for("web.companies"))
            try:
                company = ares_get_company(ico)
            except AresError as exc:
                profile = _ensure_org_profile(org)
                profile.ares_status = "error"
                db.session.commit()
                flash(f"ARES: {exc}", "danger")
                return redirect(url_for("web.companies"))
            if not company:
                profile = _ensure_org_profile(org)
                profile.ares_status = "not_found"
                db.session.commit()
                flash("ARES subjekt s tímto IČO nenašel.", "danger")
                return redirect(url_for("web.companies"))
            _apply_ares_company(org, company)
            db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="organization", action="ares_verified", entity_type="organization", entity_id=str(org.id), detail_json=json.dumps({"ico": company.ico}, ensure_ascii=False)))
            db.session.commit()
            flash("Údaje firmy byly ověřeny a doplněny z ARES.", "success")
            return redirect(url_for("web.companies"))

        if action == "update":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Název firmy je povinný.", "danger")
                return redirect(url_for("web.companies"))
            profile = _ensure_org_profile(org)
            from .models import InvoiceIssuerSettings
            chosen_vat_mode = (request.form.get('vat_mode') or 'unknown').strip()
            if chosen_vat_mode not in {'unknown','nonvat','vat'}:
                flash('Neplatný režim DPH.', 'danger')
                return redirect(url_for('web.companies'))
            new_ico = (request.form.get("ico") or "").strip() or None
            new_dic = (request.form.get("dic") or "").strip() or None
            new_address = (request.form.get("address") or "").strip() or None
            identity_changed = (org.name != name or org.ico != new_ico or profile.dic != new_dic or profile.address != new_address)
            org.name = name
            org.ico = new_ico
            org.currency = (request.form.get("currency") or "CZK").upper()[:8]
            profile.dic = new_dic
            profile.address = new_address
            profile.legal_form = (request.form.get("legal_form") or "").strip() or None
            profile.nace = (request.form.get("nace") or "").strip() or None
            if identity_changed and profile.ares_status == "verified":
                profile.ares_status = "modified"
            founded = (request.form.get("founded_on") or "").strip()
            if founded:
                try:
                    profile.founded_on = date.fromisoformat(founded)
                except ValueError:
                    flash("Datum vzniku není platné.", "danger")
                    return redirect(url_for("web.companies"))
            else:
                profile.founded_on = None
            if current_app.config.get('EDA_INVOICING_READY'):
                issuer = InvoiceIssuerSettings.query.filter_by(organization_id=org.id).first()
                if not issuer:
                    issuer = InvoiceIssuerSettings(organization_id=org.id)
                    db.session.add(issuer)
                issuer.vat_mode = chosen_vat_mode
                # Company is the single editable legal identity; mirror it to
                # issuer settings so PDF generation cannot use old firm data.
                issuer.company_name = name
                issuer.ico = new_ico or ''
                issuer.address = new_address or ''
                issuer.dic = new_dic or ''
            db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="organization", action="updated", entity_type="organization", entity_id=str(org.id)))
            db.session.commit()
            flash("Údaje firmy byly uloženy.", "success")
            return redirect(url_for("web.companies"))

    organizations = [m.organization for m in Membership.query.filter_by(user_id=current_user.id).order_by(Membership.id.asc()).all()]
    profile = OrganizationProfile.query.filter_by(organization_id=org.id).first()
    company_shops = Shop.query.filter_by(organization_id=org.id, active=True).order_by(Shop.name).all()
    from .models import InvoiceIssuerSettings
    issuer_settings = InvoiceIssuerSettings.query.filter_by(organization_id=org.id).first() if current_app.config.get('EDA_INVOICING_READY') else None
    return render_template('companies.html', org=org, organizations=organizations,
                           profile=profile, can_edit=_can_edit_org(org), company_shops=company_shops, issuer_settings=issuer_settings)


@web_bp.get("/companies/ares/<ico>")
@login_required
def companies_ares(ico):
    try:
        company = ares_get_company(ico)
    except AresError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not company:
        return jsonify({"ok": False, "error": "Subjekt nebyl v ARES nalezen."}), 404
    return jsonify({"ok": True, "company": company.as_dict()})


@web_bp.route("/organization/switch/<int:organization_id>", methods=["POST"])
@login_required
def organization_switch(organization_id):
    membership = Membership.query.filter_by(user_id=current_user.id, organization_id=organization_id).first_or_404()
    session["organization_id"] = membership.organization_id
    flash(f"Aktivní firma: {membership.organization.name}", "success")
    return redirect(request.referrer or url_for("web.dashboard"))


CHAT_ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".txt", ".csv", ".json", ".md"}


def _chat_conversation(org):
    requested_id = request.args.get("conversation", type=int)
    if requested_id:
        conversation = ChatConversation.query.filter_by(
            id=requested_id,
            organization_id=org.id,
            user_id=current_user.id,
        ).first()
        if conversation:
            return conversation
    conversation = ChatConversation.query.filter_by(
        organization_id=org.id,
        user_id=current_user.id,
        status="active",
    ).order_by(ChatConversation.id.desc()).first()
    if not conversation:
        conversation = ChatConversation(
            organization_id=org.id,
            user_id=current_user.id,
            title="Nová konverzace",
            status="active",
        )
        db.session.add(conversation)
        db.session.commit()
    return conversation


def _save_chat_attachments(org, conversation, message):
    files = request.files.getlist("attachments")
    if not files:
        return []
    storage_root = Path(current_app.instance_path) / "chat"
    target_dir = storage_root / str(org.id) / str(conversation.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for file in files[:8]:
        if not file or not file.filename:
            continue
        original = secure_filename(file.filename) or "attachment"
        ext = Path(original).suffix.lower()
        if ext not in CHAT_ALLOWED_EXTENSIONS:
            raise ValueError(f"Nepodporovaný typ přílohy: {file.filename}")
        stored_leaf = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{original}"
        relative = Path(str(org.id)) / str(conversation.id) / stored_leaf
        path = storage_root / relative
        file.save(path)
        size = path.stat().st_size
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        read_result = read_document(path, ext)
        extracted_text = read_result.text
        attachment = ChatAttachment(
            message_id=message.id,
            organization_id=org.id,
            original_name=file.filename[:255],
            stored_name=str(relative).replace("\\", "/"),
            mime_type=file.mimetype or None,
            size_bytes=size,
            sha256=digest,
            status=read_result.status,
            extracted_text=extracted_text,
        )
        db.session.add(attachment)
        # Every accounting-looking attachment uploaded in Chat is also registered in the document inbox.
        if ext in {".pdf", ".jpg", ".jpeg", ".png", ".webp"}:
            upload_root = Path(current_app.instance_path) / "uploads" / str(org.id)
            upload_root.mkdir(parents=True, exist_ok=True)
            inbox_name = f"chat_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{original}"
            shutil.copy2(path, upload_root / inbox_name)
            inbox_doc = InboxDocument(
                organization_id=org.id,
                user_id=current_user.id,
                original_name=file.filename[:255],
                stored_name=inbox_name,
                mime_type=file.mimetype or None,
                size_bytes=size,
                status="awaiting_project",
                document_type=None,
                extracted_text=extracted_text,
                source="chat",
            )
            db.session.add(inbox_doc)
        saved.append(attachment)
    db.session.flush()
    return saved


@web_bp.route("/chat", methods=["GET", "POST"])
@login_required
def chat():
    org = _org_or_redirect()
    if not org:
        return redirect(url_for("web.dashboard"))
    conversation = _chat_conversation(org)

    if request.method == "POST":
        prompt = (request.form.get("prompt") or "").strip()
        has_files = any(f and f.filename for f in request.files.getlist("attachments"))
        if not prompt and not has_files:
            flash("Napiš Edovi zprávu nebo přilož soubor.", "danger")
            return redirect(url_for("web.chat", conversation=conversation.id))

        user_message = ChatMessage(
            conversation_id=conversation.id,
            organization_id=org.id,
            user_id=current_user.id,
            role="user",
            content=prompt,
            status="complete",
        )
        db.session.add(user_message)
        db.session.flush()
        try:
            attachments = _save_chat_attachments(org, conversation, user_message)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("web.chat", conversation=conversation.id))

        if conversation.title == "Nová konverzace":
            title_source = prompt or (attachments[0].original_name if attachments else "Nová konverzace")
            conversation.title = title_source[:90]

        conversation.updated_at = datetime.utcnow()
        db.session.add(AuditLog(
            organization_id=org.id,
            user_id=current_user.id,
            category="chat",
            action="message_sent",
            entity_type="chat_conversation",
            entity_id=str(conversation.id),
            detail_json=json.dumps({"message_id": user_message.id, "attachments": len(attachments)}, ensure_ascii=False),
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        ))
        db.session.commit()

        history = ChatMessage.query.filter_by(conversation_id=conversation.id).order_by(ChatMessage.id.asc()).all()
        assistant = ChatMessage(
            conversation_id=conversation.id,
            organization_id=org.id,
            role="assistant",
            content="",
            status="processing",
        )
        db.session.add(assistant)
        db.session.commit()
        try:
            guide_state = _organization_guide_state(org)
            result = run_chat(
                organization=org,
                user=current_user,
                conversation=conversation,
                user_message=user_message,
                history=history,
                attachments=attachments,
                storage_root=Path(current_app.instance_path) / "chat",
                organization_state=guide_state,
            )
            assistant.content = result.reply
            assistant.status = "complete"
            assistant.engine_run_id = result.run_id
            assistant.engine_provider = result.provider
            assistant.engine_model = result.model
            assistant.input_tokens = result.input_tokens
            assistant.output_tokens = result.output_tokens
            assistant.cost_usd = result.cost_usd
            assistant.latency_ms = result.latency_ms
            assistant.raw_json = json.dumps(result.raw or {}, ensure_ascii=False, default=str)
            included = set((result.raw or {}).get("_eda_attachment_ids_included", []))
            for attachment in attachments:
                attachment.engine_included = attachment.id in included
            db.session.add(AuditLog(
                organization_id=org.id,
                user_id=current_user.id,
                category="ai_engine",
                action="chat_completed",
                entity_type="chat_message",
                entity_id=str(assistant.id),
                detail_json=json.dumps({
                    "run_id": result.run_id,
                    "provider": result.provider,
                    "model": result.model,
                    "latency_ms": result.latency_ms,
                }, ensure_ascii=False),
            ))
        except AIEngineError as exc:
            assistant.status = "error"
            assistant.error = str(exc)
            assistant.content = "Nepodařilo se dokončit odpověď přes Fresh AI Engine. Zkontroluj prosím jeho připojení v administraci."
            db.session.add(DiagnosticEvent(
                level="error",
                source="ai_engine",
                message="Chat call failed",
                request_path="/chat",
                detail=str(exc)[:4000],
            ))
        db.session.commit()
        return redirect(url_for("web.chat", conversation=conversation.id))

    messages = ChatMessage.query.filter_by(conversation_id=conversation.id).order_by(ChatMessage.id.asc()).all()
    conversations = ChatConversation.query.filter_by(
        organization_id=org.id,
        user_id=current_user.id,
    ).order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc()).limit(20).all()
    guide_state = _organization_guide_state(org)
    return render_template("chat.html", org=org, conversation=conversation, messages=messages, conversations=conversations, guide_state=guide_state)


@web_bp.post("/chat/new")
@login_required
def chat_new():
    org = _org_or_redirect()
    ChatConversation.query.filter_by(organization_id=org.id, user_id=current_user.id, status="active").update({"status": "archived"})
    conversation = ChatConversation(organization_id=org.id, user_id=current_user.id, title="Nová konverzace", status="active")
    db.session.add(conversation)
    db.session.commit()
    return redirect(url_for("web.chat", conversation=conversation.id))


@web_bp.route("/chat/attachment/<int:attachment_id>")
@login_required
def chat_attachment(attachment_id):
    org = _org_or_redirect()
    attachment = ChatAttachment.query.filter_by(id=attachment_id, organization_id=org.id).first_or_404()
    storage_root = Path(current_app.instance_path) / "chat"
    return send_from_directory(storage_root, attachment.stored_name, as_attachment=False, download_name=attachment.original_name)


@web_bp.route("/eda", methods=["GET", "POST"], strict_slashes=False)
@login_required
def worker():
    org = _org_or_redirect()
    if request.method == "POST":
        prompt = (request.form.get("prompt") or "").strip()
        job_type = (request.form.get("job_type") or "").strip() or None
        if not prompt and job_type:
            preset = next((p for p in PRESETS if p["code"] == job_type), None)
            prompt = preset["name"] if preset else job_type
        if not prompt:
            flash("Napiš Edovi úkol.", "danger")
            return redirect(url_for("web.worker"))
        extra = {}
        if request.form.get("year"):
            extra["year"] = request.form.get("year")
        if request.form.get("month"):
            extra["month"] = request.form.get("month")
        job = create_job(org.id, current_user.id, prompt, job_type=job_type, extra=extra)
        # Read-only steps run immediately. Write steps stop before approval.
        execute_job(job, allow_write=False)
        return redirect(url_for("web.worker_job", job_id=job.id))
    jobs = AgentJob.query.filter_by(organization_id=org.id).order_by(AgentJob.id.desc()).limit(40).all()
    return render_template("worker.html", org=org, jobs=jobs, presets=PRESETS, today=date.today())


@web_bp.route("/eda/job/<int:job_id>")
@login_required
def worker_job(job_id):
    org = _org_or_redirect()
    job = AgentJob.query.filter_by(id=job_id, organization_id=org.id).first_or_404()
    return render_template("worker_job.html", org=org, job=job)


@web_bp.route("/eda/job/<int:job_id>/run", methods=["POST"])
@login_required
def worker_job_run(job_id):
    org = _org_or_redirect()
    job = AgentJob.query.filter_by(id=job_id, organization_id=org.id).first_or_404()
    execute_job(job, allow_write=False)
    flash("Eda úkol znovu zpracoval.", "success")
    return redirect(url_for("web.worker_job", job_id=job.id))


@web_bp.route("/eda/job/<int:job_id>/approve", methods=["POST"])
@login_required
def worker_job_approve(job_id):
    org = _org_or_redirect()
    job = AgentJob.query.filter_by(id=job_id, organization_id=org.id).first_or_404()
    job.approved_by_user_id = current_user.id
    for action in job.actions:
        if action.status == "waiting_approval":
            action.status = "pending"
    db.session.commit()
    execute_job(job, allow_write=True)
    flash("Schválené kroky byly provedeny.", "success" if job.status != "error" else "danger")
    return redirect(url_for("web.worker_job", job_id=job.id))



@web_bp.route("/eda/invoice-request", methods=["GET", "POST"])
@login_required
def invoice_request():
    org = _org_or_redirect()
    if request.method == "POST":
        required = {
            "partner_code": (request.form.get("partner_code") or "").strip(),
            "type_code": (request.form.get("type_code") or "").strip(),
            "amount": (request.form.get("amount") or "").strip().replace(" ", "").replace(",", "."),
            "description": (request.form.get("description") or "").strip(),
        }
        if not all(required.values()):
            flash("Vyplň partnera, typ dokladu, částku a popis.", "danger")
            return render_template("invoice_request.html", org=org, today=date.today())
        try:
            Decimal(required["amount"])
        except InvalidOperation:
            flash("Částka není platná.", "danger")
            return render_template("invoice_request.html", org=org, today=date.today())
        extra = {
            **required,
            "currency": (request.form.get("currency") or "CZK").strip().upper(),
            "variable_symbol": (request.form.get("variable_symbol") or "").strip(),
            "issued_on": (request.form.get("issued_on") or date.today().isoformat()),
            "due_on": (request.form.get("due_on") or "").strip(),
            "external_id": (request.form.get("external_id") or "").strip(),
        }
        prompt = f"Vystav fakturu pro {required['partner_code']} na {required['amount']} {extra['currency']}: {required['description']}"
        job = create_job(org.id, current_user.id, prompt, job_type="create_invoice_request", extra=extra)
        execute_job(job, allow_write=False)
        return redirect(url_for("web.worker_job", job_id=job.id))
    return render_template("invoice_request.html", org=org, today=date.today())


def _document_path(org_id, doc):
    return Path(current_app.instance_path) / "uploads" / str(org_id) / doc.stored_name


def _analyze_inbox_doc(org, doc):
    path = _document_path(org.id, doc)
    read_result = read_document(path, path.suffix.lower())
    doc.extracted_text = read_result.text
    doc.status = read_result.status if read_result.status not in {"text_extracted", "needs_vision"} else "analyzing"
    db.session.commit()
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name).all()
    proposal, result = analyze_inbox_document(organization=org, document=doc, file_path=path, projects=projects, user_id=current_user.id)
    if doc.project_id:
        proposal["project_id"] = doc.project_id
        proposal["project_reason"] = "Projekt vybral uživatel při nahrání dokladu."
    suggested = proposal.get("project_id")
    if not doc.project_id and suggested and any(p.id == int(suggested) for p in projects if str(suggested).isdigit()):
        doc.project_id = int(suggested)
    doc.document_type = proposal.get("document_type") or doc.document_type
    doc.proposed_json = json.dumps(proposal, ensure_ascii=False, default=str)
    doc.status = "analyzed" if doc.project_id else "awaiting_project"
    db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="document", action="analyzed", entity_type="inbox_document", entity_id=str(doc.id), detail_json=json.dumps({"run_id": result.run_id, "model": result.model}, ensure_ascii=False)))
    db.session.commit()
    return proposal


@web_bp.route("/documents", methods=["GET", "POST"])
@login_required
def documents():
    org = _org_or_redirect()
    upload_root = Path(current_app.instance_path) / "uploads" / str(org.id)
    upload_root.mkdir(parents=True, exist_ok=True)
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name).all()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Vyber soubor.", "danger")
            return redirect(url_for("web.documents"))
        original = secure_filename(file.filename) or "document"
        ext = Path(original).suffix.lower()
        if ext not in {".pdf", ".jpg", ".jpeg", ".png", ".webp"}:
            flash("Podporujeme PDF, JPG, PNG a WEBP.", "danger")
            return redirect(url_for("web.documents"))
        stored = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{original}"
        path = upload_root / stored
        file.save(path)
        size = path.stat().st_size
        doc = InboxDocument(
            organization_id=org.id,
            user_id=current_user.id,
            project_id=request.form.get("project_id") or None,
            original_name=file.filename[:255],
            stored_name=stored,
            mime_type=file.mimetype,
            size_bytes=size,
            status="awaiting_analysis",
        )
        db.session.add(doc)
        db.session.flush()
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="document", action="uploaded", entity_type="inbox_document", entity_id=str(doc.id), detail_json=json.dumps({"name": file.filename, "size": size}, ensure_ascii=False)))
        db.session.commit()
        try:
            _analyze_inbox_doc(org, doc)
            flash("Doklad byl nahrán a Eda připravil návrh zpracování.", "success")
        except Exception as exc:
            doc.status = "analysis_error"
            db.session.add(DiagnosticEvent(level="error", source="document_analysis", message="Document analysis failed", request_path="/documents", detail=str(exc)[:4000]))
            db.session.commit()
            flash(f"Doklad je uložený, ale automatická analýza se nepodařila: {exc}", "warning")
        return redirect(url_for("web.document_detail", document_id=doc.id))
    items = InboxDocument.query.filter_by(organization_id=org.id).order_by(InboxDocument.created_at.desc()).limit(150).all()
    invoices = Invoice.query.filter_by(organization_id=org.id).order_by(Invoice.issued_on.desc(), Invoice.id.desc()).all()
    invoice_docs = EdaInvoiceDocument.query.filter_by(organization_id=org.id).all() if current_app.config.get('EDA_INVOICING_READY', False) else []
    from .models import EdaAdvanceDocument
    advance_docs = EdaAdvanceDocument.query.filter_by(organization_id=org.id).all() if current_app.config.get('EDA_INVOICING_READY',False) else []
    pdf_by_invoice_id = {d.invoice_id: d for d in invoice_docs if not d.revoked}
    direction_filter = request.args.get('direction', '').strip()
    type_filter = request.args.get('type', '').strip()
    project_filter = request.args.get('project', type=int)
    combined = []
    for inv in invoices:
        if direction_filter and direction_filter != inv.direction: continue
        if type_filter and type_filter != 'invoice': continue
        if project_filter and inv.project_id != project_filter: continue
        doc = pdf_by_invoice_id.get(inv.id)
        url = None
        if doc:
            try:
                from .invoicing import _public_doc_url
                url = _public_doc_url(doc)
            except Exception:
                # Missing/decommissioned token must not break the entire document list.
                current_app.logger.warning('Document %s has an unreadable PDF link.', doc.id)
                url = None
        combined.append(dict(kind='invoice', date=inv.issued_on or inv.created_at.date(),
            title=inv.number or inv.external_id, direction=inv.direction, type='Faktura',
            amount=inv.total, paid=inv.paid, project=inv.project.name if inv.project else '—',
            source=inv.source, url=url, detail_url=None))
    # Proforma requests are displayed, but MUST NOT be included in revenue,
    # receivables, booked payments or the general ledger.
    for advance in advance_docs:
        if direction_filter and direction_filter != 'issued': continue
        if type_filter and type_filter != 'advance': continue
        if project_filter:
            linked_shop = Shop.query.filter_by(id=advance.shop_id,organization_id=org.id).first()
            if not linked_shop or not linked_shop.project_id or int(linked_shop.project_id) != project_filter:
                continue
        url = None
        if not advance.revoked:
            try:
                from .invoicing import _public_doc_url
                url = _public_doc_url(advance)
            except Exception:
                current_app.logger.warning('Advance document %s PDF link unavailable.',advance.id)
        combined.append(dict(kind='advance',date=advance.issued_on,title=advance.number,
            direction='issued',type='Zálohová výzva',amount=advance.amount,
            paid=None,project='Dle objednávky',source='eda (mimo účetní tržby)',
            url=url,detail_url=url_for('web.shop_order_detail',shop_id=advance.shop_id,order_id=advance.order_id)))
    for d in items:
        if direction_filter: continue  # Unclassified uploads have no reliable debit/credit direction.
        if type_filter and type_filter != 'upload': continue
        if project_filter and d.project_id != project_filter: continue
        combined.append(dict(kind='upload', date=d.created_at.date(), title=d.original_name,
            direction='unclassified', type=d.document_type or 'Nahraný doklad', amount=None,
            paid=None, project=d.project.name if d.project else '—', source=d.source,
            url=None, detail_url=url_for('web.document_detail', document_id=d.id)))
    combined.sort(key=lambda item: item['date'] or date.min, reverse=True)
    issued_total = sum((Decimal(i.total or 0) for i in invoices if i.direction == 'issued'), Decimal('0'))
    received_total = sum((Decimal(i.total or 0) for i in invoices if i.direction == 'received'), Decimal('0'))
    unpaid_total = sum((max(Decimal(i.remaining or 0), Decimal('0')) for i in invoices if i.direction == 'issued' and not i.paid), Decimal('0'))
    return render_template('documents.html', items=items, documents=combined[:300],
        projects=projects, org=org, direction_filter=direction_filter,
        type_filter=type_filter, project_filter=project_filter,
        document_stats=dict(issued=issued_total, received=received_total, outstanding=unpaid_total))


@web_bp.route("/documents/<int:document_id>", methods=["GET", "POST"])
@login_required
def document_detail(document_id):
    org = _org_or_redirect()
    doc = InboxDocument.query.filter_by(id=document_id, organization_id=org.id).first_or_404()
    projects = Project.query.filter_by(organization_id=org.id, active=True).order_by(Project.name).all()
    proposal = _json_load(doc.proposed_json, {})
    if request.method == "POST":
        doc.project_id = request.form.get("project_id") or None
        fields = ["document_type", "supplier_name", "supplier_ico", "document_number", "variable_symbol", "issued_on", "due_on", "currency", "total_without_vat", "vat", "total", "category", "accounting_note", "flexi_type_code", "flexi_partner_code"]
        for field in fields:
            value = (request.form.get(field) or "").strip()
            proposal[field] = value or None
        proposal["project_id"] = int(doc.project_id) if doc.project_id else None
        doc.document_type = proposal.get("document_type")
        doc.proposed_json = json.dumps(proposal, ensure_ascii=False, default=str)
        if doc.status not in {"ready_for_booking", "booked"}:
            doc.status = "analyzed" if doc.project_id else "awaiting_project"
        db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="document", action="proposal_updated", entity_type="inbox_document", entity_id=str(doc.id)))
        db.session.commit()
        flash("Návrh dokladu byl uložen.", "success")
        return redirect(url_for("web.document_detail", document_id=doc.id))
    return render_template("document_detail.html", org=org, doc=doc, proposal=proposal, projects=projects, can_delete_inbox=_can_edit_org(org), flexi_configured=FlexiBeeClient(org.id).configured)


@web_bp.post("/documents/<int:document_id>/delete-draft")
@login_required
def document_delete_draft(document_id):
    """Delete only unbooked incoming material, never an issued or booked invoice."""
    org = _org_or_redirect()
    if not _can_edit_org(org):
        abort(403)
    doc = InboxDocument.query.filter_by(id=document_id, organization_id=org.id).first_or_404()
    if doc.status == "booked":
        abort(409, "Zaúčtovaný doklad nelze smazat.")
    # Only an uploaded document can be deleted here. Issued invoice PDF uses
    # another model and another private storage area and cannot be affected.
    upload_root = (Path(current_app.instance_path) / "uploads" / str(org.id)).resolve()
    stored = Path(doc.stored_name)
    if stored.name != doc.stored_name or stored.is_absolute():
        abort(409, "Neplatná cesta uloženého dokladu.")
    path = (upload_root / stored).resolve()
    if path.parent != upload_root:
        abort(409, "Doklad je mimo povolené úložiště.")
    other_refs = InboxDocument.query.filter(
        InboxDocument.organization_id == org.id,
        InboxDocument.stored_name == doc.stored_name,
        InboxDocument.id != doc.id,
    ).count()
    db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id,
        category="document", action="unbooked_upload_deleted",
        entity_type="inbox_document", entity_id=str(doc.id),
        detail_json=json.dumps({"original_name":doc.original_name,"status":doc.status,
                                "project_id":doc.project_id}, ensure_ascii=False)))
    db.session.delete(doc)
    db.session.commit()
    # Leave an orphan (rather than rolling back a committed audit) if file cleanup fails.
    if not other_refs:
        try:
            if path.is_file(): path.unlink()
        except OSError:
            current_app.logger.warning("Unbooked document file cleanup failed: %s", doc.id)
    flash("Nezaúčtovaný podklad byl smazán. Historie operace zůstává zachována.", "success")
    return redirect(url_for("web.documents"))


@web_bp.post("/documents/<int:document_id>/analyze")
@login_required
def document_analyze(document_id):
    org = _org_or_redirect()
    doc = InboxDocument.query.filter_by(id=document_id, organization_id=org.id).first_or_404()
    try:
        _analyze_inbox_doc(org, doc)
        flash("Eda doklad znovu analyzoval.", "success")
    except Exception as exc:
        doc.status = "analysis_error"
        db.session.commit()
        flash(f"Analýza selhala: {exc}", "danger")
    return redirect(url_for("web.document_detail", document_id=doc.id))


@web_bp.post("/documents/<int:document_id>/prepare")
@login_required
def document_prepare(document_id):
    org = _org_or_redirect()
    doc = InboxDocument.query.filter_by(id=document_id, organization_id=org.id).first_or_404()
    proposal = _json_load(doc.proposed_json, {})
    if not doc.project_id:
        flash("Nejdřív přiřaď doklad k projektu.", "danger")
        return redirect(url_for("web.document_detail", document_id=doc.id))
    dtype = proposal.get("document_type") or doc.document_type or "other"
    proposal["booking"] = {
        "status": "ready",
        "target_evidence": "faktura-prijata" if dtype == "received_invoice" else ("pokladni-pohyb" if dtype == "receipt" else "manual_review"),
        "prepared_at": datetime.utcnow().isoformat(),
        "requires_approval": True,
    }
    doc.proposed_json = json.dumps(proposal, ensure_ascii=False, default=str)
    doc.status = "ready_for_booking"
    db.session.add(AuditLog(organization_id=org.id, user_id=current_user.id, category="document", action="prepared_for_booking", entity_type="inbox_document", entity_id=str(doc.id), detail_json=json.dumps(proposal.get("booking"), ensure_ascii=False)))
    db.session.commit()
    flash("Doklad je připravený k zaúčtování. Ostrý zápis zůstává pod schválením.", "success")
    return redirect(url_for("web.document_detail", document_id=doc.id))


@web_bp.post("/documents/<int:document_id>/flexi-dry-run")
@login_required
def document_flexi_dry_run(document_id):
    org = _org_or_redirect()
    doc = InboxDocument.query.filter_by(id=document_id, organization_id=org.id).first_or_404()
    proposal = _json_load(doc.proposed_json, {})
    if (proposal.get("document_type") or doc.document_type) != "received_invoice":
        flash("Dry-run do ABRA Flexi je v této verzi připraven pro přijaté faktury. Účtenky zůstávají jako připravený pokladní doklad.", "warning")
        return redirect(url_for("web.document_detail", document_id=doc.id))
    type_code = proposal.get("flexi_type_code")
    partner_code = proposal.get("flexi_partner_code")
    if not type_code or not partner_code:
        flash("Pro Flexi dry-run doplň typ přijaté faktury a kód dodavatele z adresáře ABRA Flexi.", "danger")
        return redirect(url_for("web.document_detail", document_id=doc.id))
    data = {
        "external_id": f"DOC-{doc.id}",
        "type_code": type_code,
        "partner_code": partner_code,
        "variable_symbol": proposal.get("variable_symbol"),
        "description": proposal.get("accounting_note") or proposal.get("supplier_name") or doc.original_name,
        "issued_on": proposal.get("issued_on"),
        "due_on": proposal.get("due_on"),
        "currency": proposal.get("currency") or "CZK",
        "amount": proposal.get("total") or proposal.get("total_without_vat"),
    }
    try:
        result = FlexiBeeClient(org.id).create_received_invoice(data, dry_run=True)
        proposal.setdefault("booking", {})["dry_run"] = {"ok": True, "checked_at": datetime.utcnow().isoformat(), "response": result}
        doc.proposed_json = json.dumps(proposal, ensure_ascii=False, default=str)
        doc.status = "validated_for_booking"
        db.session.commit()
        flash("ABRA Flexi dry-run prošel. Doklad nebyl uložen, pouze zvalidován.", "success")
    except Exception as exc:
        proposal.setdefault("booking", {})["dry_run"] = {"ok": False, "checked_at": datetime.utcnow().isoformat(), "error": str(exc)}
        doc.proposed_json = json.dumps(proposal, ensure_ascii=False, default=str)
        db.session.commit()
        flash(f"Flexi dry-run neprošel: {exc}", "danger")
    return redirect(url_for("web.document_detail", document_id=doc.id))


@web_bp.route("/documents/<int:document_id>/file")
@login_required
def document_file(document_id):
    org = _org_or_redirect()
    doc = InboxDocument.query.filter_by(id=document_id, organization_id=org.id).first_or_404()
    upload_root = Path(current_app.instance_path) / "uploads" / str(org.id)
    return send_from_directory(upload_root, doc.stored_name, as_attachment=False, download_name=doc.original_name)


@web_bp.route("/admin", methods=["GET", "POST"])
@login_required
def admin_console():
    if not current_user.is_admin:
        flash("Admin konzole je dostupná pouze administrátorovi.", "danger")
        return redirect(url_for("web.dashboard"))
    org = _org_or_redirect()
    if request.method == "POST" and request.form.get("action") == "create_organization":
        return redirect(url_for("web.companies"))
    orgs = [m.organization for m in Membership.query.filter_by(user_id=current_user.id).order_by(Membership.id).all()]
    users = User.query.order_by(User.id).all()
    sync_logs = SyncLog.query.order_by(SyncLog.id.desc()).limit(20).all()
    audit_logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(30).all()
    diag_events = DiagnosticEvent.query.order_by(DiagnosticEvent.id.desc()).limit(30).all()
    stderr_path = Path(current_app.root_path).parent / "stderr.log"
    stderr_size = stderr_path.stat().st_size if stderr_path.exists() else 0
    metrics = current_app.extensions.get("eda_metrics", {})
    return render_template("admin.html", org=org, organizations=orgs, users=users, sync_logs=sync_logs,
                           audit_logs=audit_logs, diag_events=diag_events, stderr_size=stderr_size,
                           metrics=metrics, version="0.5.1", standards_count=AccountingStandard.query.count())


@web_bp.route("/admin/standards", methods=["GET", "POST"])
@login_required
def admin_standards():
    if not current_user.is_admin:
        flash("Účetní standardy může spravovat pouze administrátor.", "danger")
        return redirect(url_for("web.dashboard"))
    org = _org_or_redirect()
    if request.method == "POST":
        action = request.form.get("action") or "create"
        if action in {"create", "update"}:
            standard = AccountingStandard() if action == "create" else db.session.get(AccountingStandard, request.form.get("standard_id", type=int))
            if not standard:
                flash("Standard nebyl nalezen.", "danger")
                return redirect(url_for("web.admin_standards"))
            title = (request.form.get("title") or "").strip()
            if not title:
                flash("Název standardu je povinný.", "danger")
                return redirect(url_for("web.admin_standards"))
            standard.code = (request.form.get("code") or "").strip() or None
            standard.title = title
            standard.category = (request.form.get("category") or "accounting").strip()[:80]
            standard.content = (request.form.get("content") or "").strip() or None
            standard.source_name = (request.form.get("source_name") or "").strip() or None
            standard.source_url = (request.form.get("source_url") or "").strip() or None
            standard.active = bool(request.form.get("active"))
            for key in ("valid_from", "valid_to", "next_review_on"):
                raw = (request.form.get(key) or "").strip()
                setattr(standard, key, date.fromisoformat(raw) if raw else None)
            db.session.add(standard)
            db.session.add(AuditLog(user_id=current_user.id, category="accounting_standard", action=action, entity_type="accounting_standard", entity_id=str(standard.id) if standard.id else None))
            db.session.commit()
            flash("Účetní standard byl uložen.", "success")
            return redirect(url_for("web.admin_standards", edit=standard.id))
        if action == "delete":
            standard = db.session.get(AccountingStandard, request.form.get("standard_id", type=int))
            if standard:
                db.session.delete(standard); db.session.commit()
                flash("Standard byl smazán.", "success")
            return redirect(url_for("web.admin_standards"))
    standards = AccountingStandard.query.order_by(AccountingStandard.active.desc(), AccountingStandard.category, AccountingStandard.code, AccountingStandard.title).all()
    edit_id = request.args.get("edit", type=int)
    edit_standard = db.session.get(AccountingStandard, edit_id) if edit_id else None
    return render_template("admin_standards.html", org=org, standards=standards, edit_standard=edit_standard)


@web_bp.post("/admin/standards/<int:standard_id>/review")
@login_required
def admin_standard_review(standard_id):
    if not current_user.is_admin:
        flash("AI revizi standardů může spustit pouze administrátor.", "danger")
        return redirect(url_for("web.dashboard"))
    standard = db.session.get(AccountingStandard, standard_id)
    if not standard:
        return redirect(url_for("web.admin_standards"))
    try:
        result, ai = review_standard(standard, current_user.id)
        standard.ai_review_json = json.dumps(result, ensure_ascii=False, default=str)
        standard.review_status = result.get("status") or "reviewed"
        standard.last_reviewed_at = datetime.utcnow()
        db.session.add(AuditLog(user_id=current_user.id, category="accounting_standard", action="ai_review", entity_type="accounting_standard", entity_id=str(standard.id), detail_json=json.dumps({"run_id": ai.run_id, "model": ai.model}, ensure_ascii=False)))
        db.session.commit()
        flash("AI revize standardu byla dokončena. Výsledek je návrh k lidské kontrole, ne automatická změna standardu.", "success")
    except Exception as exc:
        standard.review_status = "review_error"
        db.session.add(DiagnosticEvent(level="error", source="standards_review", message="Accounting standard review failed", request_path=f"/admin/standards/{standard.id}/review", detail=str(exc)[:4000]))
        db.session.commit()
        flash(f"AI revize se nepodařila: {exc}", "danger")
    return redirect(url_for("web.admin_standards", edit=standard.id))


@web_bp.get("/standards")
@login_required
def standards_library():
    org = _org_or_redirect()
    q = (request.args.get("q") or "").strip()
    query = AccountingStandard.query.filter_by(active=True)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(AccountingStandard.title.ilike(like), AccountingStandard.code.ilike(like), AccountingStandard.content.ilike(like)))
    items = query.order_by(AccountingStandard.category, AccountingStandard.code, AccountingStandard.title).all()
    return render_template("standards.html", org=org, items=items, q=q)
