from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, g, request
from datetime import datetime, timezone
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix
import time

load_dotenv()

from .config import Config
from .extensions import db, login_manager, csrf


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    if __import__("os").getenv("TRUST_PROXY", "0") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Additive Fresh Key access migration for existing production databases.
    # 0.6.4 only tried to ALTER project_members; when the table did not exist yet,
    # the exception was swallowed and every /projects request later crashed.
    # Create the one optional ACL table explicitly and then ensure its new column.
    app.config["FRESH_KEY_PROJECT_MEMBERS_READY"] = False
    app.config["FRESH_KEY_PROJECT_MEMBERS_ERROR"] = ""
    with app.app_context():
        try:
            from .models import ProjectMember
            ProjectMember.__table__.create(bind=db.engine, checkfirst=True)
            columns = {c["name"] for c in inspect(db.engine).get_columns("project_members")}
            if "relationship" not in columns:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE project_members ADD COLUMN relationship VARCHAR(24) NOT NULL DEFAULT 'member'"))
                columns = {c["name"] for c in inspect(db.engine).get_columns("project_members")}
            app.config["FRESH_KEY_PROJECT_MEMBERS_READY"] = "relationship" in columns
            if not app.config["FRESH_KEY_PROJECT_MEMBERS_READY"]:
                app.config["FRESH_KEY_PROJECT_MEMBERS_ERROR"] = "Tabulka project_members není připravená."
        except Exception as exc:
            # Core accounting pages must stay usable even if the DB account has no
            # CREATE/ALTER permission. Project sharing then degrades gracefully and
            # the manifest exposes the migration error for a one-time manual repair.
            app.config["FRESH_KEY_PROJECT_MEMBERS_READY"] = False
            app.config["FRESH_KEY_PROJECT_MEMBERS_ERROR"] = f"{type(exc).__name__}: {exc}"[:500]

    # Additive, isolated invoicing schema; never modify existing order/accounting tables.
    with app.app_context():
        try:
            from .models import InvoiceIssuerSettings, InvoiceSequence, ShopInvoicePublisher, EdaInvoiceDocument, EdaAdvanceSequence, EdaAdvanceDocument
            for model in (InvoiceIssuerSettings, InvoiceSequence, ShopInvoicePublisher, EdaInvoiceDocument, EdaAdvanceSequence, EdaAdvanceDocument):
                model.__table__.create(bind=db.engine, checkfirst=True)
            app.config['EDA_INVOICING_READY'] = True
        except Exception as exc:
            app.config['EDA_INVOICING_READY'] = False
            app.logger.error('Invoicing schema not installed; run SQL migration: %s', type(exc).__name__)

    # Isolated event tables; do not alter existing financial documents or orders.
    with app.app_context():
        try:
            from .models import EdaShopEvent, EdaShopEventSettings
            EdaShopEvent.__table__.create(bind=db.engine, checkfirst=True)
            EdaShopEventSettings.__table__.create(bind=db.engine, checkfirst=True)
            app.config['EDA_SHOP_EVENTS_READY'] = True
        except Exception as exc:
            app.config['EDA_SHOP_EVENTS_READY'] = False
            app.logger.error('Eda shop event tables unavailable: %s', type(exc).__name__)

    app.extensions["eda_metrics"] = {
        "requests": 0,
        "total_ms": 0.0,
        "avg_ms": 0.0,
        "last_ms": 0.0,
        "slowest_ms": 0.0,
        "last_path": None,
        "last_status": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    @app.before_request
    def _eda_request_start():
        g._eda_started = time.perf_counter()

    @app.after_request
    def _eda_request_metrics(response):
        started = getattr(g, "_eda_started", None)
        if started is not None:
            ms = (time.perf_counter() - started) * 1000
            metrics = app.extensions["eda_metrics"]
            metrics["requests"] += 1
            metrics["total_ms"] += ms
            metrics["avg_ms"] = metrics["total_ms"] / max(metrics["requests"], 1)
            metrics["last_ms"] = ms
            metrics["slowest_ms"] = max(metrics.get("slowest_ms", 0.0), ms)
            metrics["last_path"] = request.path
            metrics["last_status"] = response.status_code
            response.headers["X-Eda-Response-Time-Ms"] = f"{ms:.1f}"
        return response

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from .routes import web_bp
    from .api import api_bp
    app.register_blueprint(web_bp)
    from .admin_tools import admin_tools_bp
    app.register_blueprint(admin_tools_bp)
    from .invoicing import invoice_bp
    app.register_blueprint(invoice_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    from .shop_events import shop_events_bp
    app.register_blueprint(shop_events_bp, url_prefix='/api/v1')
    csrf.exempt(shop_events_bp)
    csrf.exempt(api_bp)

    @app.context_processor
    def _eda_global_context():
        from flask_login import current_user
        if not current_user.is_authenticated:
            return {"eda_notifications": [], "eda_link_notifications": [], "eda_shop_alerts": [], "eda_notification_count": 0}
        items = []
        links = []
        shop_alerts = []
        try:
            from .helpers import current_organization
            from .services.scheduler import reminder_items
            org = current_organization()
            items = reminder_items(org.id, limit=10) if org else []
        except Exception:
            items = []
        try:
            from .services import fresh_key
            if fresh_key.configured():
                links = fresh_key.pending_link_requests(current_user.id)[:10]
        except Exception:
            links = []
        try:
            from .models import EdaShopEvent
            org = current_organization()
            if org:
                from .models import Shop
                allowed = [x.id for x in Shop.query.filter_by(organization_id=org.id).all()]
                if allowed:
                    shop_alerts = (EdaShopEvent.query.filter(EdaShopEvent.shop_id.in_(allowed),
                         EdaShopEvent.status=='error').order_by(EdaShopEvent.id.desc()).limit(8).all())
        except Exception:
            shop_alerts=[]
        return {
            "eda_shop_alerts": shop_alerts,
            "eda_notifications": items,
            "eda_link_notifications": links,
            "eda_notification_count": len(items) + len(links) + len(shop_alerts),
        }

    return app
