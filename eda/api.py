import json
import hmac
import os
import platform
import sys
from datetime import datetime, date, timezone
from importlib.metadata import version as package_version, PackageNotFoundError
from pathlib import Path
from decimal import Decimal, InvalidOperation
from functools import wraps
from flask import Blueprint, request, jsonify, current_app
from flask_login import current_user
from sqlalchemy import inspect as sa_inspect, func
from .extensions import db
from .models import (Organization, OrganizationProfile, User, Membership, Project, Task, TaskSchedule, Shop, Order, Transaction, Invoice, Integration, SyncLog, AgentJob, AgentAction, InboxDocument, ChatConversation, ChatMessage, ChatAttachment, AuditLog, DiagnosticEvent, AccountingStandard, ShopAccountingConnection, OrderAccountingSnapshot)
from .services.ai_engine import engine_health
from .services.flexibee import FlexiBeeClient

api_bp = Blueprint("api", __name__)


def require_core_secret(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        expected = current_app.config.get("FRESH_CORE_SHARED_SECRET") or ""
        supplied = request.headers.get("X-Fresh-Core-Secret", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped




def require_diagnostic_access(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if current_user.is_authenticated and getattr(current_user, "is_admin", False):
            return fn(*args, **kwargs)
        expected = current_app.config.get("DIAGNOSTIC_TOKEN") or ""
        supplied = request.headers.get("X-Eda-Diagnostic-Token", "")
        if expected and hmac.compare_digest(supplied, expected):
            return fn(*args, **kwargs)
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return wrapped

def org_from_request():
    org_id = request.headers.get("X-Eda-Organization")
    if org_id and str(org_id).isdigit():
        org = db.session.get(Organization, int(org_id))
        if org:
            return org

    # In a multi-company account never silently route incoming accounting/shop data
    # to the first company. If Fresh Core sends site_url, resolve the company from
    # the configured shop. Otherwise the organization header is required.
    payload = request.get_json(silent=True) or {}
    site_url = (payload.get("site_url") or "").strip().rstrip("/") if isinstance(payload, dict) else ""
    if site_url:
        matches = Shop.query.filter_by(site_url=site_url).all()
        org_ids = sorted({x.organization_id for x in matches})
        if len(org_ids) == 1:
            return db.session.get(Organization, org_ids[0])

    orgs = Organization.query.order_by(Organization.id.asc()).limit(2).all()
    if len(orgs) == 1:
        return orgs[0]
    return None


def parse_dt(value):
    if not value: return None
    try: return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError: return None


def _pkg(name):
    try:
        return package_version(name)
    except PackageNotFoundError:
        return None


def _model_count(model):
    try:
        return model.query.count()
    except Exception:
        return None


def _iso(value):
    return value.isoformat() if value else None


@api_bp.get("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        database = True
    except Exception:
        database = False
    return jsonify({"app": "Eda", "version": "0.6.6", "ok": database, "database": database})




@api_bp.get("/manifest")
@require_diagnostic_access
def manifest():
    db_error = None
    try:
        db.session.execute(db.text("SELECT 1"))
        database_ok = True
    except Exception as exc:
        database_ok = False
        db_error = f"{type(exc).__name__}: {exc}"[:1000]

    metrics = dict(current_app.extensions.get("eda_metrics", {}))
    instance = Path(current_app.instance_path)
    chat_dir = instance / "chat"
    try:
        chat_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        inspector = sa_inspect(db.engine)
        present_tables = sorted(inspector.get_table_names())
        expected_tables = sorted(db.metadata.tables.keys())
    except Exception:
        present_tables = []
        expected_tables = sorted(db.metadata.tables.keys())

    recent_events = []
    try:
        for event in DiagnosticEvent.query.order_by(DiagnosticEvent.id.desc()).limit(10).all():
            recent_events.append({
                "id": event.id,
                "created_at": _iso(event.created_at),
                "level": event.level,
                "source": event.source,
                "message": event.message,
                "duration_ms": event.duration_ms,
                "request_path": event.request_path,
                "detail": (event.detail or "")[:500] or None,
            })
    except Exception:
        recent_events = []

    recent_syncs = []
    try:
        for sync in SyncLog.query.order_by(SyncLog.id.desc()).limit(10).all():
            recent_syncs.append({
                "id": sync.id,
                "created_at": _iso(sync.created_at),
                "organization_id": sync.organization_id,
                "integration": sync.integration,
                "status": sync.status,
                "items": sync.items,
                "message": sync.message,
            })
    except Exception:
        recent_syncs = []

    organizations = []
    try:
        for org in Organization.query.order_by(Organization.id.asc()).all():
            organizations.append({
                "id": org.id,
                "name": org.name,
                "ico": org.ico,
                "currency": org.currency,
                "ares_verified": bool(org.profile and org.profile.ares_status == "verified"),
                "ares_verified_at": _iso(org.profile.ares_verified_at) if org.profile else None,
                "profile_complete": bool(org.ico and org.profile and org.profile.address),
                "flexibee_configured": FlexiBeeClient(org.id).configured,
                "shops": Shop.query.filter_by(organization_id=org.id).count(),
                "projects": Project.query.filter_by(organization_id=org.id).count(),
                "documents": InboxDocument.query.filter_by(organization_id=org.id).count(),
                "worker_jobs": AgentJob.query.filter_by(organization_id=org.id).count(),
            })
    except Exception:
        organizations = []

    attachment_processing = {"by_status": {}, "engine_binary_included": 0, "recent": []}
    try:
        status_rows = db.session.query(ChatAttachment.status, func.count(ChatAttachment.id)).group_by(ChatAttachment.status).all()
        attachment_processing["by_status"] = {str(status or "unknown"): int(count) for status, count in status_rows}
        attachment_processing["engine_binary_included"] = ChatAttachment.query.filter_by(engine_included=True).count()
        for attachment in ChatAttachment.query.order_by(ChatAttachment.id.desc()).limit(10).all():
            attachment_processing["recent"].append({
                "id": attachment.id,
                "created_at": _iso(attachment.created_at),
                "organization_id": attachment.organization_id,
                "name": attachment.original_name,
                "mime_type": attachment.mime_type,
                "size_bytes": attachment.size_bytes,
                "status": attachment.status,
                "text_chars": len(attachment.extracted_text or ""),
                "engine_binary_included": bool(attachment.engine_included),
            })
    except Exception:
        pass

    last_ai_message = None
    try:
        msg = ChatMessage.query.filter(
            ChatMessage.role == "assistant",
            ChatMessage.engine_run_id.isnot(None),
        ).order_by(ChatMessage.id.desc()).first()
        if msg:
            last_ai_message = {
                "message_id": msg.id,
                "created_at": _iso(msg.created_at),
                "status": msg.status,
                "run_id": msg.engine_run_id,
                "provider": msg.engine_provider,
                "model": msg.engine_model,
                "input_tokens": msg.input_tokens,
                "output_tokens": msg.output_tokens,
                "cost_usd": str(msg.cost_usd) if msg.cost_usd is not None else None,
                "latency_ms": msg.latency_ms,
            }
    except Exception:
        pass

    counts = {
        "users": _model_count(User),
        "memberships": _model_count(Membership),
        "organizations": _model_count(Organization),
        "organization_profiles": _model_count(OrganizationProfile),
        "projects": _model_count(Project),
        "project_members": _model_count(ProjectMember) if current_app.config.get("FRESH_KEY_PROJECT_MEMBERS_READY") else 0,
        "tasks": _model_count(Task),
        "task_schedules": _model_count(TaskSchedule),
        "shops": _model_count(Shop),
        "orders": _model_count(Order),
        "transactions": _model_count(Transaction),
        "invoices": _model_count(Invoice),
        "integrations": _model_count(Integration),
        "sync_logs": _model_count(SyncLog),
        "worker_jobs": _model_count(AgentJob),
        "worker_actions": _model_count(AgentAction),
        "documents": _model_count(InboxDocument),
        "chat_conversations": _model_count(ChatConversation),
        "chat_messages": _model_count(ChatMessage),
        "chat_attachments": _model_count(ChatAttachment),
        "audit_logs": _model_count(AuditLog),
        "diagnostic_events": _model_count(DiagnosticEvent),
        "accounting_standards": _model_count(AccountingStandard),
        "shop_accounting_connections": _model_count(ShopAccountingConnection),
        "order_accounting_snapshots": _model_count(OrderAccountingSnapshot),
    }

    # Operational summaries for UI/debugging.
    shop_connectivity = {"by_status": {}, "items": []}
    try:
        rows = Integration.query.filter_by(kind="fresh_core_shop").order_by(Integration.id.desc()).all()
        for row in rows:
            shop_connectivity["by_status"][row.status] = shop_connectivity["by_status"].get(row.status, 0) + 1
            shop_id = None
            if row.name.startswith("shop:"):
                try: shop_id = int(row.name.split(":", 1)[1])
                except ValueError: pass
            shop_connectivity["items"].append({"shop_id": shop_id, "status": row.status, "manifest_url_configured": bool(row.base_url), "last_checked_at": _iso(row.last_sync_at), "last_error": row.last_error})
    except Exception:
        pass

    accounting_connectivity = {"by_status": {}, "items": []}
    try:
        for row in ShopAccountingConnection.query.order_by(ShopAccountingConnection.id.desc()).all():
            accounting_connectivity["by_status"][row.status] = accounting_connectivity["by_status"].get(row.status, 0) + 1
            accounting_connectivity["items"].append({
                "shop_id": row.shop_id,
                "organization_id": row.organization_id,
                "status": row.status,
                "orders_endpoint_configured": bool(row.orders_endpoint),
                "key_configured": bool(row.encrypted_key),
                "remote_enabled": row.remote_enabled,
                "plugin_version": row.plugin_version,
                "last_checked_at": _iso(row.last_checked_at),
                "last_error": row.last_error,
            })
    except Exception:
        pass

    order_accounting = {"by_status": {}}
    try:
        for status, count in db.session.query(OrderAccountingSnapshot.status, func.count(OrderAccountingSnapshot.id)).group_by(OrderAccountingSnapshot.status).all():
            order_accounting["by_status"][str(status or "unknown")] = int(count)
    except Exception:
        pass

    document_workflow = {"by_status": {}}
    try:
        for status, count in db.session.query(InboxDocument.status, func.count(InboxDocument.id)).group_by(InboxDocument.status).all():
            document_workflow["by_status"][str(status or "unknown")] = int(count)
    except Exception:
        pass

    schedules_info = {"enabled": 0, "total": 0}
    try:
        schedules_info["total"] = TaskSchedule.query.count()
        schedules_info["enabled"] = TaskSchedule.query.filter_by(enabled=True).count()
    except Exception:
        pass

    standards_info = {"total": 0, "active": 0, "by_review_status": {}}
    try:
        standards_info["total"] = AccountingStandard.query.count()
        standards_info["active"] = AccountingStandard.query.filter_by(active=True).count()
        for status, count in db.session.query(AccountingStandard.review_status, func.count(AccountingStandard.id)).group_by(AccountingStandard.review_status).all():
            standards_info["by_review_status"][str(status or "unknown")] = int(count)
    except Exception:
        pass

    ai_configured = bool(current_app.config.get("AI_ENGINE_API_KEY"))
    fresh_core_configured = bool(current_app.config.get("FRESH_CORE_SHARED_SECRET"))

    return jsonify({
        "app": "Eda",
        "version": "0.6.6",
        "ok": database_ok,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": {
            "multi_company": True,
            "company_editing": True,
            "ares_lookup": True,
            "ares_company_verification": True,
            "proactive_chat_guidance": True,
            "eda_worker_branding": True,
            "worker_jobs": True,
            "approval_workflow": True,
            "audit_log": True,
            "document_inbox": True,
            "document_text_extraction": True,
            "pdf_text_extraction": True,
            "image_and_scanned_pdf_vision_forwarding": True,
            "fresh_core": True,
            "flexibee_read": True,
            "flexibee_write_dry_run": True,
            "flexibee_invoice_create": True,
            "vat_control_statement_check": True,
            "ai_engine": True,
            "chat": True,
            "chat_attachments": True,
            "responsive_mobile_menu": True,
            "project_edit_delete": True,
            "project_detail_orders_invoices": True,
            "project_commercial_value": True,
            "project_invoice_status_tracking": True,
            "project_expected_profit": True,
            "shop_edit_delete": True,
            "shop_connectivity_test": True,
            "shop_order_detail": True,
            "shop_order_manual_sync": True,
            "dashboard_tasks_jobs": True,
            "simplified_navigation": True,
            "fresh_core_multi_company_routing": True,
            "document_project_assignment": True,
            "document_ai_analysis": True,
            "document_booking_preparation": True,
            "flexibee_received_invoice_dry_run": True,
            "task_scheduler": True,
            "proactive_notifications": True,
            "accounting_standards_library": True,
            "accounting_standards_admin": True,
            "accounting_standards_ai_review": True,
            "chat_accounting_standards_context": True,
            "fresh_core_accounting_api": True,
            "fresh_core_accounting_key_per_shop": True,
            "fresh_core_accounting_encrypted_key_storage": True,
            "order_private_billing_snapshot": True,
            "order_to_flexibee_invoice_workflow": True,
            "order_invoice_dry_run_before_approval": True,
        },
        "runtime": {
            **metrics,
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.system(),
            "pid": os.getpid(),
            "flask": _pkg("Flask"),
            "sqlalchemy": _pkg("SQLAlchemy"),
            "pymysql": _pkg("PyMySQL"),
            "pypdf": _pkg("pypdf"),
        },
        "database": {
            "ok": database_ok,
            "dialect": db.engine.url.get_backend_name() if database_ok else None,
            "driver": db.engine.url.get_driver_name() if database_ok else None,
            "expected_tables": expected_tables,
            "present_tables": present_tables,
            "missing_tables": sorted(set(expected_tables) - set(present_tables)),
            "error": db_error,
            "fresh_key_project_access": {
                "ready": bool(current_app.config.get("FRESH_KEY_PROJECT_MEMBERS_READY")),
                "error": current_app.config.get("FRESH_KEY_PROJECT_MEMBERS_ERROR") or None,
            },
        },
        "counts": counts,
        "organizations": organizations,
        "chat": {
            "attachment_processing": attachment_processing,
            "conversation_history_limit": 20,
            "max_files_per_message": 8,
        },
        "shop_connectivity": shop_connectivity,
        "shop_accounting_connectivity": accounting_connectivity,
        "order_accounting_workflow": order_accounting,
        "document_workflow": document_workflow,
        "scheduler": schedules_info,
        "accounting_standards": standards_info,
        "storage": {
            "instance_writable": os.access(instance, os.W_OK),
            "chat_storage_writable": chat_dir.exists() and os.access(chat_dir, os.W_OK),
            "max_request_mb": int(current_app.config.get("MAX_CONTENT_LENGTH", 0) / 1024 / 1024),
            "engine_binary_attachment_budget_mb": current_app.config.get("AI_ENGINE_ATTACHMENT_MAX_MB"),
            "engine_attachment_text_max_chars": current_app.config.get("AI_ENGINE_ATTACHMENT_TEXT_MAX_CHARS"),
            "allowed_chat_extensions": ["pdf", "jpg", "jpeg", "png", "webp", "txt", "csv", "json", "md"],
        },
        "document_processing": {
            "pdf": {
                "method": "pypdf text extraction",
                "works_for": "PDF with embedded/selectable text",
                "scanned_pdf_without_text": "forwarded as binary attachment and marked requires_vision",
            },
            "images": {
                "method": "forwarded to Fresh AI Engine as binary attachment",
                "requires_engine_vision": True,
            },
            "text_files": {
                "method": "local UTF-8 extraction",
            },
        },
        "ares": {
            "enabled": True,
            "base_url": "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest",
            "lookup": "/companies/ares/<ico>",
            "company_profiles_table": "organization_profiles",
        },
        "fresh_core": {
            "configured": fresh_core_configured,
            "accepts": ["orders", "order-status", "transactions"],
            "auth_header": "X-Fresh-Core-Secret",
            "organization_header": "X-Eda-Organization",
            "sync_modes": ["push_to_eda", "pull_from_manifest_if_orders_feed_declared"],
            "accounting_bridge": {
                "minimum_fresh_core_version": "0.6.0",
                "auth_header": "X-Fresh-Core-Accounting-Key",
                "key_scope": "per_shop",
                "private_billing_data_stored_encrypted": True,
            },
            "endpoints": {
                "manifest": "/api/v1/fresh-core/manifest",
                "orders": "/api/v1/fresh-core/orders",
                "order_status": "/api/v1/fresh-core/order-status",
                "transactions": "/api/v1/fresh-core/transactions",
            },
        },
        "flexibee": {
            "global_fallback_configured": FlexiBeeClient().configured,
            "per_organization_supported": True,
            "reads": ["faktura-vydana", "faktura-prijata", "kontrolni-hlaseni-dph"],
            "writes": ["faktura-vydana"],
            "write_mode": "dry-run before approval and final write",
        },
        "ai_engine": {
            "configured": ai_configured,
            "base_url": current_app.config.get("AI_ENGINE_URL"),
            "health_endpoint": "/api/v1/ai-engine/health",
            "run_endpoint": "/v1/engine/run",
            "operation": "chat.conversation",
            "task_type": "chat",
            "source": current_app.config.get("AI_ENGINE_APP_ID") or "eda",
            "quality": current_app.config.get("AI_ENGINE_QUALITY") or "balanced",
            "timeout_seconds": current_app.config.get("AI_ENGINE_TIMEOUT_SECONDS"),
            "attachments_max_mb": current_app.config.get("AI_ENGINE_ATTACHMENT_MAX_MB"),
            "attachment_text_max_chars": current_app.config.get("AI_ENGINE_ATTACHMENT_TEXT_MAX_CHARS"),
            "last_run": last_ai_message,
        },
        "security": {
            "manifest_access": ["admin_session", "X-Eda-Diagnostic-Token"],
            "csrf": True,
            "write_actions_require_approval": True,
            "organization_scoped_chat": True,
        },
        "routes": {
            "web": ["/dashboard", "/chat", "/eda", "/companies", "/tasks", "/planner", "/finance", "/projects", "/shops", "/documents", "/standards", "/integrations", "/admin", "/admin/standards"],
            "diagnostics": ["/api/v1/health", "/api/v1/manifest", "/api/v1/ai-engine/health"],
        },
        "recent_diagnostics": recent_events,
        "recent_syncs": recent_syncs,
    })

@api_bp.get("/ai-engine/health")
@require_diagnostic_access
def ai_engine_health():
    return jsonify(engine_health())


@api_bp.get("/fresh-core/manifest")
@require_core_secret
def core_manifest():
    return jsonify({
        "app": "Eda", "version": "0.6.6",
        "accepts": ["orders", "order-status", "transactions"],
        "auth": {"header": "X-Fresh-Core-Secret"},
        "organization_header": "X-Eda-Organization"
    })


@api_bp.post("/fresh-core/orders")
@require_core_secret
def core_orders():
    org = org_from_request()
    if not org: return jsonify({"ok": False, "error": "organization_not_found"}), 400
    payload = request.get_json(silent=True) or {}
    site_url = (payload.get("site_url") or "").strip().rstrip("/")
    shop_name = payload.get("shop_name") or site_url or "Fresh Core e-shop"
    shop = Shop.query.filter_by(organization_id=org.id, site_url=site_url).first() if site_url else None
    if not shop:
        shop = Shop(organization_id=org.id, name=shop_name, platform=payload.get("platform") or "woocommerce", site_url=site_url or None)
        db.session.add(shop); db.session.flush()
    count = 0
    for item in payload.get("orders", []):
        ext = str(item.get("id") or item.get("external_id") or "").strip()
        if not ext: continue
        order = Order.query.filter_by(shop_id=shop.id, external_id=ext).first()
        if not order:
            order = Order(organization_id=org.id, shop_id=shop.id, external_id=ext); db.session.add(order)
        try: order.total = Decimal(str(item.get("total") or 0))
        except InvalidOperation: order.total = Decimal("0")
        order.currency = str(item.get("currency") or "CZK")[:8]
        order.status = str(item.get("status") or "")[:80] or None
        order.ordered_at = parse_dt(item.get("ordered_at") or item.get("date_created"))
        order.customer_email = (item.get("customer_email") or "")[:255] or None
        order.raw_json = json.dumps(item, ensure_ascii=False, default=str)
        count += 1
    shop.last_sync_at = datetime.utcnow()
    conn = Integration.query.filter_by(organization_id=org.id, kind="fresh_core_shop", name=f"shop:{shop.id}").first()
    if conn:
        conn.status = "sync_ready"
        conn.last_sync_at = datetime.utcnow()
        conn.last_error = None
    db.session.add(SyncLog(organization_id=org.id, integration="fresh_core", status="ok", items=count, message=f"Objednávky: {shop.name}"))
    db.session.commit()
    return jsonify({"ok": True, "shop_id": shop.id, "items": count})


@api_bp.post("/fresh-core/order-status")
@require_core_secret
def core_order_status():
    org = org_from_request(); payload = request.get_json(silent=True) or {}
    if not org: return jsonify({"ok": False, "error": "organization_required"}), 400
    site_url = (payload.get("site_url") or "").strip().rstrip("/")
    shop = Shop.query.filter_by(organization_id=org.id, site_url=site_url).first()
    if not shop: return jsonify({"ok": False, "error": "shop_not_found"}), 404
    ext = str(payload.get("id") or payload.get("external_id") or "")
    order = Order.query.filter_by(shop_id=shop.id, external_id=ext).first()
    if not order: return jsonify({"ok": False, "error": "order_not_found"}), 404
    order.status = str(payload.get("status") or "")[:80]
    db.session.commit()
    return jsonify({"ok": True})


@api_bp.post("/fresh-core/transactions")
@require_core_secret
def core_transactions():
    org = org_from_request(); payload = request.get_json(silent=True) or {}
    if not org: return jsonify({"ok": False, "error": "organization_required"}), 400
    count = 0
    for item in payload.get("transactions", []):
        ref = str(item.get("external_ref") or item.get("id") or "").strip()
        if ref:
            existing = Transaction.query.filter_by(organization_id=org.id, source="fresh_core", external_ref=ref).first()
            if existing: continue
        try: amount = Decimal(str(item.get("amount") or 0))
        except InvalidOperation: continue
        booked_on = date.today()
        if item.get("booked_on"):
            try: booked_on = datetime.fromisoformat(str(item["booked_on"])[:10]).date()
            except ValueError: pass
        db.session.add(Transaction(organization_id=org.id, kind=item.get("kind") if item.get("kind") in ("income", "expense") else "income",
                                   amount=amount, currency=str(item.get("currency") or "CZK")[:8], booked_on=booked_on,
                                   category=(item.get("category") or "")[:120] or None, description=(item.get("description") or "")[:255] or None,
                                   source="fresh_core", external_ref=ref or None))
        count += 1
    db.session.commit()
    return jsonify({"ok": True, "items": count})
