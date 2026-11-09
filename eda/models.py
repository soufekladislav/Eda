from datetime import datetime, date
from decimal import Decimal
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from .extensions import db


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Organization(TimestampMixin, db.Model):
    __tablename__ = "organizations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    ico = db.Column(db.String(32), nullable=True)
    currency = db.Column(db.String(8), default="CZK", nullable=False)


class OrganizationProfile(TimestampMixin, db.Model):
    __tablename__ = "organization_profiles"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    dic = db.Column(db.String(32), nullable=True)
    address = db.Column(db.String(500), nullable=True)
    legal_form = db.Column(db.String(180), nullable=True)
    legal_form_code = db.Column(db.String(32), nullable=True)
    nace = db.Column(db.String(500), nullable=True)
    founded_on = db.Column(db.Date, nullable=True)
    ares_status = db.Column(db.String(32), nullable=False, default="not_verified", index=True)
    ares_verified_at = db.Column(db.DateTime, nullable=True)
    ares_raw_json = db.Column(db.Text, nullable=True)
    organization = db.relationship("Organization", backref=db.backref("profile", uselist=False, cascade="all, delete-orphan"))


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(120), nullable=False, default="Uživatel")
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active_user = db.Column(db.Boolean, default=True, nullable=False)

    @property
    def is_active(self):
        return self.is_active_user

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Membership(TimestampMixin, db.Model):
    __tablename__ = "memberships"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = db.Column(db.String(32), default="owner", nullable=False)
    user = db.relationship("User", backref="memberships")
    organization = db.relationship("Organization", backref="memberships")
    __table_args__ = (db.UniqueConstraint("user_id", "organization_id", name="uq_membership_user_org"),)


class Project(TimestampMixin, db.Model):
    __tablename__ = "projects"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(180), nullable=False)
    code = db.Column(db.String(80), nullable=True)
    project_type = db.Column(db.String(40), default="other", nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    organization = db.relationship("Organization", backref="projects")


class ProjectMember(TimestampMixin, db.Model):
    """Project-level access without granting access to the whole organization."""
    __tablename__ = "project_members"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    role = db.Column(db.String(32), default="editor", nullable=False)
    relationship = db.Column(db.String(24), default="member", nullable=False)
    status = db.Column(db.String(24), default="pending", nullable=False, index=True)
    invited_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    project = db.relationship("Project", backref=db.backref("project_members", cascade="all, delete-orphan"))
    user = db.relationship("User", foreign_keys=[user_id])
    inviter = db.relationship("User", foreign_keys=[invited_by])
    __table_args__ = (db.UniqueConstraint("project_id", "email", name="uq_project_member_project_email"),)


class Task(TimestampMixin, db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    task_type = db.Column(db.String(40), default="general", nullable=False)
    due_date = db.Column(db.Date, nullable=True, index=True)
    status = db.Column(db.String(24), default="open", nullable=False, index=True)
    priority = db.Column(db.String(16), default="normal", nullable=False)
    source = db.Column(db.String(32), default="manual", nullable=False)
    external_ref = db.Column(db.String(160), nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)
    project = db.relationship("Project", backref="tasks")


class Transaction(TimestampMixin, db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    kind = db.Column(db.String(16), nullable=False, index=True)  # income / expense
    amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    currency = db.Column(db.String(8), default="CZK", nullable=False)
    booked_on = db.Column(db.Date, default=date.today, nullable=False, index=True)
    category = db.Column(db.String(120), nullable=True)
    description = db.Column(db.String(255), nullable=True)
    source = db.Column(db.String(32), default="manual", nullable=False)
    external_ref = db.Column(db.String(160), nullable=True, index=True)
    project = db.relationship("Project", backref="transactions")


class Shop(TimestampMixin, db.Model):
    __tablename__ = "shops"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    name = db.Column(db.String(180), nullable=False)
    platform = db.Column(db.String(64), default="woocommerce", nullable=False)
    site_url = db.Column(db.String(500), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    last_sync_at = db.Column(db.DateTime, nullable=True)
    project = db.relationship("Project", backref="shops")
    __table_args__ = (db.UniqueConstraint("organization_id", "site_url", name="uq_shop_org_url"),)


class Order(TimestampMixin, db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id = db.Column(db.String(160), nullable=False)
    status = db.Column(db.String(80), nullable=True, index=True)
    total = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    currency = db.Column(db.String(8), default="CZK", nullable=False)
    ordered_at = db.Column(db.DateTime, nullable=True, index=True)
    customer_email = db.Column(db.String(255), nullable=True)
    raw_json = db.Column(db.Text, nullable=True)
    shop = db.relationship("Shop", backref="orders")
    __table_args__ = (db.UniqueConstraint("shop_id", "external_id", name="uq_order_shop_external"),)


class ShopAccountingConnection(TimestampMixin, db.Model):
    __tablename__ = "shop_accounting_connections"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    orders_endpoint = db.Column(db.String(500), nullable=True)
    detail_endpoint = db.Column(db.String(500), nullable=True)
    auth_header = db.Column(db.String(120), default="X-Fresh-Core-Accounting-Key", nullable=False)
    encrypted_key = db.Column(db.Text, nullable=True)
    remote_enabled = db.Column(db.Boolean, nullable=True)
    status = db.Column(db.String(32), default="not_configured", nullable=False, index=True)
    plugin_version = db.Column(db.String(40), nullable=True)
    last_checked_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    shop = db.relationship("Shop", backref=db.backref("accounting_connection", uselist=False, cascade="all, delete-orphan"))
    __table_args__ = (db.UniqueConstraint("organization_id", "shop_id", name="uq_shop_accounting_org_shop"),)


class OrderAccountingSnapshot(TimestampMixin, db.Model):
    __tablename__ = "order_accounting_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    status = db.Column(db.String(32), default="not_loaded", nullable=False, index=True)
    encrypted_payload = db.Column(db.Text, nullable=True)
    fetched_at = db.Column(db.DateTime, nullable=True)
    flexi_dry_run_at = db.Column(db.DateTime, nullable=True)
    flexi_written_at = db.Column(db.DateTime, nullable=True)
    invoice_external_ref = db.Column(db.String(180), nullable=True, index=True)
    validation_json = db.Column(db.Text, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    order = db.relationship("Order", backref=db.backref("accounting_snapshot", uselist=False, cascade="all, delete-orphan"))
    project = db.relationship("Project")
    shop = db.relationship("Shop")


class Invoice(TimestampMixin, db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    direction = db.Column(db.String(16), nullable=False)  # issued / received
    external_id = db.Column(db.String(160), nullable=False)
    number = db.Column(db.String(120), nullable=True)
    variable_symbol = db.Column(db.String(64), nullable=True)
    issued_on = db.Column(db.Date, nullable=True)
    due_on = db.Column(db.Date, nullable=True, index=True)
    total = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    remaining = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    currency = db.Column(db.String(8), default="CZK", nullable=False)
    paid = db.Column(db.Boolean, default=False, nullable=False, index=True)
    source = db.Column(db.String(32), default="flexibee", nullable=False)
    raw_json = db.Column(db.Text, nullable=True)
    project = db.relationship("Project", backref="invoices")
    __table_args__ = (db.UniqueConstraint("organization_id", "direction", "external_id", name="uq_invoice_org_direction_external"),)


class Integration(TimestampMixin, db.Model):
    __tablename__ = "integrations"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(180), nullable=False)
    base_url = db.Column(db.String(500), nullable=True)
    external_company = db.Column(db.String(160), nullable=True)
    status = db.Column(db.String(32), default="not_configured", nullable=False)
    last_sync_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    __table_args__ = (db.UniqueConstraint("organization_id", "kind", "name", name="uq_integration_org_kind_name"),)


class SyncLog(TimestampMixin, db.Model):
    __tablename__ = "sync_logs"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    integration = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False)
    message = db.Column(db.Text, nullable=True)
    items = db.Column(db.Integer, default=0, nullable=False)


class AgentJob(TimestampMixin, db.Model):
    __tablename__ = "agent_jobs"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    prompt = db.Column(db.Text, nullable=False)
    job_type = db.Column(db.String(64), default="custom", nullable=False, index=True)
    status = db.Column(db.String(32), default="planned", nullable=False, index=True)
    plan_json = db.Column(db.Text, nullable=True)
    result_json = db.Column(db.Text, nullable=True)
    summary = db.Column(db.Text, nullable=True)
    requires_approval = db.Column(db.Boolean, default=False, nullable=False)
    approved_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    organization = db.relationship("Organization", backref="agent_jobs")
    user = db.relationship("User", foreign_keys=[user_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_user_id])


class AgentAction(TimestampMixin, db.Model):
    __tablename__ = "agent_actions"
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("agent_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    position = db.Column(db.Integer, default=0, nullable=False)
    action_type = db.Column(db.String(80), nullable=False, index=True)
    label = db.Column(db.String(255), nullable=False)
    payload_json = db.Column(db.Text, nullable=True)
    risk_level = db.Column(db.String(16), default="read", nullable=False)  # read / write / external
    requires_approval = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(32), default="pending", nullable=False, index=True)
    result_json = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    job = db.relationship("AgentJob", backref=db.backref("actions", cascade="all, delete-orphan", order_by="AgentAction.position"))


class InboxDocument(TimestampMixin, db.Model):
    __tablename__ = "inbox_documents"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(120), nullable=True)
    size_bytes = db.Column(db.Integer, default=0, nullable=False)
    status = db.Column(db.String(32), default="uploaded", nullable=False, index=True)
    document_type = db.Column(db.String(48), nullable=True)
    extracted_text = db.Column(db.Text, nullable=True)
    proposed_json = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(32), default="upload", nullable=False)
    organization = db.relationship("Organization", backref="inbox_documents")
    project = db.relationship("Project", backref="inbox_documents")
    user = db.relationship("User")


class ChatConversation(TimestampMixin, db.Model):
    __tablename__ = "chat_conversations"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False, default="Nová konverzace")
    status = db.Column(db.String(24), nullable=False, default="active", index=True)
    organization = db.relationship("Organization", backref="chat_conversations")
    user = db.relationship("User", backref="chat_conversations")


class ChatMessage(TimestampMixin, db.Model):
    __tablename__ = "chat_messages"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    role = db.Column(db.String(16), nullable=False, index=True)  # user / assistant / system
    content = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(24), nullable=False, default="complete", index=True)
    engine_run_id = db.Column(db.String(160), nullable=True, index=True)
    engine_provider = db.Column(db.String(80), nullable=True)
    engine_model = db.Column(db.String(160), nullable=True)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    cost_usd = db.Column(db.Numeric(14, 6), nullable=True)
    latency_ms = db.Column(db.Integer, nullable=True)
    raw_json = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    conversation = db.relationship("ChatConversation", backref=db.backref("messages", cascade="all, delete-orphan", order_by="ChatMessage.id"))
    organization = db.relationship("Organization")
    user = db.relationship("User")


class ChatAttachment(TimestampMixin, db.Model):
    __tablename__ = "chat_attachments"
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(160), nullable=True)
    size_bytes = db.Column(db.Integer, default=0, nullable=False)
    sha256 = db.Column(db.String(64), nullable=True, index=True)
    status = db.Column(db.String(32), nullable=False, default="uploaded", index=True)
    engine_included = db.Column(db.Boolean, nullable=False, default=False)
    extracted_text = db.Column(db.Text, nullable=True)
    message = db.relationship("ChatMessage", backref=db.backref("attachments", cascade="all, delete-orphan"))
    organization = db.relationship("Organization")


class AuditLog(TimestampMixin, db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    category = db.Column(db.String(64), nullable=False, index=True)
    action = db.Column(db.String(120), nullable=False, index=True)
    entity_type = db.Column(db.String(64), nullable=True)
    entity_id = db.Column(db.String(120), nullable=True)
    detail_json = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    organization = db.relationship("Organization", backref="audit_logs")
    user = db.relationship("User")


class DiagnosticEvent(TimestampMixin, db.Model):
    __tablename__ = "diagnostic_events"
    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String(16), default="info", nullable=False, index=True)
    source = db.Column(db.String(64), nullable=False, index=True)
    message = db.Column(db.String(500), nullable=False)
    duration_ms = db.Column(db.Integer, nullable=True)
    request_path = db.Column(db.String(500), nullable=True)
    detail = db.Column(db.Text, nullable=True)


class TaskSchedule(TimestampMixin, db.Model):
    __tablename__ = "task_schedules"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    task_type = db.Column(db.String(40), default="tax", nullable=False)
    frequency = db.Column(db.String(24), default="monthly", nullable=False, index=True)  # monthly / annual / once
    day_of_month = db.Column(db.Integer, nullable=True)
    month_of_year = db.Column(db.Integer, nullable=True)
    one_time_due_on = db.Column(db.Date, nullable=True)
    reminder_days = db.Column(db.Integer, default=3, nullable=False)
    priority = db.Column(db.String(16), default="high", nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False, index=True)
    note = db.Column(db.Text, nullable=True)
    organization = db.relationship("Organization", backref="task_schedules")
    project = db.relationship("Project", backref="task_schedules")


class AccountingStandard(TimestampMixin, db.Model):
    __tablename__ = "accounting_standards"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(80), nullable=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(80), default="accounting", nullable=False, index=True)
    content = db.Column(db.Text, nullable=True)
    source_name = db.Column(db.String(180), nullable=True)
    source_url = db.Column(db.String(1000), nullable=True)
    valid_from = db.Column(db.Date, nullable=True)
    valid_to = db.Column(db.Date, nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    review_status = db.Column(db.String(32), default="not_reviewed", nullable=False, index=True)
    last_reviewed_at = db.Column(db.DateTime, nullable=True)
    next_review_on = db.Column(db.Date, nullable=True)
    ai_review_json = db.Column(db.Text, nullable=True)

# Eda-native invoice archive: one authoritative PDF per WooCommerce order.
class InvoiceIssuerSettings(TimestampMixin, db.Model):
    __tablename__ = "eda_invoice_issuer_settings"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True)
    company_name = db.Column(db.String(180), nullable=False, default="")
    ico = db.Column(db.String(32), nullable=False, default="")
    dic = db.Column(db.String(32), nullable=False, default="")
    address = db.Column(db.String(500), nullable=False, default="")
    bank_account = db.Column(db.String(120), nullable=False, default="")
    iban = db.Column(db.String(120), nullable=False, default="")
    swift = db.Column(db.String(32), nullable=False, default="")
    vat_mode = db.Column(db.String(20), nullable=False, default="unknown")
    series_prefix = db.Column(db.String(20), nullable=False, default="AR")
    due_days = db.Column(db.Integer, nullable=False, default=14)


class InvoiceSequence(db.Model):
    __tablename__ = "eda_invoice_sequences"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    next_number = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.UniqueConstraint("organization_id", "year", name="uq_eda_invoice_sequence_org_year"),)


class ShopInvoicePublisher(TimestampMixin, db.Model):
    __tablename__ = "eda_shop_invoice_publishers"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, unique=True)
    encrypted_write_key = db.Column(db.Text, nullable=True)


class EdaInvoiceDocument(TimestampMixin, db.Model):
    __tablename__ = "eda_invoice_documents"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, unique=True)
    pdf_file = db.Column(db.String(240), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    encrypted_token = db.Column(db.Text, nullable=False)
    revoked = db.Column(db.Boolean, nullable=False, default=False)
    publish_status = db.Column(db.String(24), nullable=False, default="not_sent")
    publish_error = db.Column(db.Text, nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)
    snapshot_encrypted = db.Column(db.Text, nullable=False)
    pdf_sha256 = db.Column(db.String(64), nullable=False)
    invoice = db.relationship("Invoice")
    __table_args__ = (db.UniqueConstraint("shop_id", "order_id", name="uq_eda_invoice_document_shop_order"),)


# Advance payment request (proforma) is NOT an issued accounting invoice.
class EdaAdvanceSequence(db.Model):
    __tablename__ = "eda_advance_sequences"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    next_number = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.UniqueConstraint('organization_id','year',name='uq_eda_advance_sequence_org_year'),)


class EdaAdvanceDocument(TimestampMixin, db.Model):
    __tablename__ = "eda_advance_documents"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False)
    shop_id = db.Column(db.Integer, db.ForeignKey('shops.id', ondelete='CASCADE'), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id', ondelete='CASCADE'), nullable=False)
    number = db.Column(db.String(80), nullable=False)
    amount = db.Column(db.Numeric(14,2), nullable=False)
    currency = db.Column(db.String(8), nullable=False, default='CZK')
    issued_on = db.Column(db.Date, nullable=False)
    pdf_file = db.Column(db.String(240), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    encrypted_token = db.Column(db.Text, nullable=False)
    snapshot_encrypted = db.Column(db.Text, nullable=False)
    publish_status = db.Column(db.String(24), nullable=False, default='not_sent')
    publish_error = db.Column(db.Text, nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)
    revoked = db.Column(db.Boolean, nullable=False, default=False)
    __table_args__ = (db.UniqueConstraint('shop_id','order_id',name='uq_eda_advance_shop_order'),)


class EdaShopEvent(TimestampMixin, db.Model):
    """Durable receipt for signed notifications; details always fetched from Fresh Core."""
    __tablename__ = 'eda_shop_events'
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shops.id', ondelete='CASCADE'), nullable=False, index=True)
    external_order_id = db.Column(db.String(160), nullable=False, index=True)
    status = db.Column(db.String(32), nullable=False, default='received')
    action = db.Column(db.String(32), nullable=True)
    error = db.Column(db.Text, nullable=True)

class EdaShopEventSettings(TimestampMixin, db.Model):
    __tablename__ = 'eda_shop_event_settings'
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shops.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True)
    automatic_enabled = db.Column(db.Boolean, nullable=False, default=False)
    auto_advance = db.Column(db.Boolean, nullable=False, default=False)
    auto_invoice = db.Column(db.Boolean, nullable=False, default=False)
    started_at = db.Column(db.DateTime, nullable=True)
