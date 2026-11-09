"""Superadmin control center / scoped company project permissions.
No schema migration; reuses the existing Membership and ProjectMember tables.
"""
import os
import json
from pathlib import Path
from datetime import datetime
from flask import Blueprint, current_app, abort, flash, redirect, render_template, request, url_for, jsonify, send_file
from flask_login import current_user, login_required
from sqlalchemy import func
from .extensions import db
from .models import (User, Organization, Membership, Project, ProjectMember,
                     AuditLog, AgentJob, AgentAction, InboxDocument, Invoice, EdaInvoiceDocument,
                     EdaShopEvent, TaskSchedule, SyncLog, DiagnosticEvent)
from .helpers import current_organization

admin_tools_bp = Blueprint('eda_admin_tools', __name__)
PROJECT_ROLES = ('manager', 'editor', 'viewer')


def _super():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


def _manager(org_id):
    if current_user.is_admin:
        return True
    membership = Membership.query.filter_by(user_id=current_user.id, organization_id=org_id).first()
    return bool(membership and membership.role in ('owner', 'admin'))


def _audit(action, entity_id='', org_id=None, details=None):
    db.session.add(AuditLog(organization_id=org_id, user_id=current_user.id,
        category='access_management', action=action, entity_type='user', entity_id=str(entity_id),
        detail_json=json.dumps(details or {}, ensure_ascii=False)))


def _server_usage():
    """Reports SHARED filesystem capacity; never misrepresents it as account quota."""
    result = {'path': current_app.instance_path, 'partition': None,
              'quota': None, 'load': None, 'note': 'Kapacita oddílu je sdílená; nejde o kvótu účtu.'}
    try:
        v = os.statvfs(current_app.instance_path)
        result['partition'] = dict(total=v.f_blocks*v.f_frsize,
                                   used=(v.f_blocks-v.f_bfree)*v.f_frsize,
                                   available=v.f_bavail*v.f_frsize)
    except OSError:
        pass
    try:
        result['load'] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    # Account quota is not reliably exposed by statvfs on shared hosting.
    return result


@admin_tools_bp.get('/admin/centrum')
@login_required
def center():
    _super()
    organizations = Organization.query.order_by(Organization.name).all()
    users = User.query.order_by(User.email).all()
    memberships = Membership.query.order_by(Membership.organization_id, Membership.user_id).all()
    projects = Project.query.order_by(Project.organization_id, Project.name).all()
    permissions = ProjectMember.query.order_by(ProjectMember.project_id, ProjectMember.email).all()
    jobs = AgentJob.query.order_by(AgentJob.id.desc()).limit(60).all()
    actions = AgentAction.query.order_by(AgentAction.id.desc()).limit(60).all()
    schedules = TaskSchedule.query.order_by(TaskSchedule.id.desc()).limit(50).all()
    syncs = SyncLog.query.order_by(SyncLog.id.desc()).limit(35).all()
    diagnostics = DiagnosticEvent.query.order_by(DiagnosticEvent.id.desc()).limit(35).all()
    stored = InboxDocument.query.order_by(InboxDocument.id.desc()).limit(80).all()
    invoices = Invoice.query.order_by(Invoice.id.desc()).limit(80).all()
    invoice_files = {f.invoice_id:f.id for f in EdaInvoiceDocument.query.filter(EdaInvoiceDocument.invoice_id.in_([i.id for i in invoices])).all()} if invoices else {}
    try:
        recent_events = EdaShopEvent.query.order_by(EdaShopEvent.id.desc()).limit(50).all()
    except Exception:
        db.session.rollback()
        recent_events = []
    stats = {'users':len(users), 'organizations':len(organizations),
             'projects':len(projects), 'jobs':len(jobs),
             'pending':sum(1 for j in jobs if j.status in ('planned','pending','waiting_approval'))}
    return render_template('admin_center.html', org=current_organization(),
        organizations=organizations, users=users, memberships=memberships,
        projects=projects, permissions=permissions, jobs=jobs, actions=actions,
        schedules=schedules, syncs=syncs, diagnostics=diagnostics,
        stored=stored, invoices=invoices, invoice_files=invoice_files, recent_events=recent_events, disk=_server_usage(), stats=stats)


@admin_tools_bp.post('/admin/uzivatele/<int:user_id>')
@login_required
def update_user(user_id):
    _super()
    user = User.query.get_or_404(user_id)
    action = request.form.get('action')
    if action == 'toggle_active':
        if user.id == current_user.id:
            abort(400, 'Nelze deaktivovat vlastní superadmin účet.')
        user.is_active_user = not user.is_active_user
        _audit('user_active_changed', user.id, details={'active':user.is_active_user})
    elif action == 'toggle_superadmin':
        if user.id == current_user.id:
            abort(400, 'Nelze změnit vlastní superadmin oprávnění.')
        user.is_admin = not user.is_admin
        _audit('superadmin_changed', user.id, details={'superadmin':user.is_admin})
    else:
        abort(400)
    db.session.commit()
    flash('Oprávnění účtu bylo aktualizováno.', 'success')
    return redirect(url_for('eda_admin_tools.center')+'#uzivatele')


@admin_tools_bp.post('/admin/firmy/<int:org_id>/pristup')
@login_required
def membership_update(org_id):
    _super()
    Organization.query.get_or_404(org_id)
    user_id = request.form.get('user_id', type=int)
    user = User.query.get_or_404(user_id)
    role = request.form.get('role')
    if role not in ('owner','admin','member','remove'):
        abort(400)
    membership = Membership.query.filter_by(organization_id=org_id, user_id=user.id).first()
    if role == 'remove':
        if membership and membership.role == 'owner' and Membership.query.filter_by(organization_id=org_id, role='owner').count() <= 1:
            abort(400, 'Nelze odebrat posledního vlastníka firmy.')
        if membership:
            db.session.delete(membership)
    else:
        if not membership:
            membership = Membership(user_id=user.id, organization_id=org_id)
            db.session.add(membership)
        membership.role = role
    _audit('membership_updated', user.id, org_id=org_id, details={'role':role})
    db.session.commit()
    flash('Přístup k firmě upraven.', 'success')
    return redirect(url_for('eda_admin_tools.center')+'#firmy')


def _set_project_member(project, email, role):
    email = email.strip().lower()
    if '@' not in email or len(email) > 255:
        abort(400, 'Neplatný e-mail.')
    user = User.query.filter(func.lower(User.email) == email).first()
    # Member membership only supplies company context; it never grants access to all projects.
    if user and role != 'remove' and not Membership.query.filter_by(user_id=user.id, organization_id=project.organization_id).first():
        db.session.add(Membership(user_id=user.id, organization_id=project.organization_id, role='member'))
    row = ProjectMember.query.filter_by(project_id=project.id, email=email).first()
    if role == 'remove':
        if row:
            db.session.delete(row)
    else:
        if role not in PROJECT_ROLES:
            abort(400)
        if not row:
            row = ProjectMember(project_id=project.id,email=email,invited_by=current_user.id)
            db.session.add(row)
        row.user_id = user.id if user else None
        row.status = 'active' if user else 'pending'
        row.role = role
        row.relationship = 'member'
    _audit('project_access_updated',user.id if user else email,org_id=project.organization_id,
           details={'project_id':project.id,'role':role})
    db.session.commit()


@admin_tools_bp.post('/admin/projekty/<int:project_id>/pristup')
@login_required
def project_access(project_id):
    _super()
    project = Project.query.get_or_404(project_id)
    _set_project_member(project, request.form.get('email',''), request.form.get('role','viewer'))
    flash('Projektové oprávnění uloženo.', 'success')
    return redirect(url_for('eda_admin_tools.center')+'#projekty')


@admin_tools_bp.route('/nastaveni/uzivatele',methods=['GET','POST'])
@login_required
def company_access():
    org = current_organization()
    if not org:
        abort(403)
    if not _manager(org.id):
        abort(403)
    projects = Project.query.filter_by(organization_id=org.id).order_by(Project.name).all()
    if request.method == 'POST':
        project_id = request.form.get('project_id', type=int)
        project = next((p for p in projects if p.id == project_id), None)
        if project is None:
            abort(404)
        _set_project_member(project, request.form.get('email',''),request.form.get('role','viewer'))
        flash('Projektový přístup nastaven.', 'success')
        return redirect(url_for('eda_admin_tools.company_access'))
    perms = ProjectMember.query.filter(ProjectMember.project_id.in_([p.id for p in projects])).order_by(ProjectMember.project_id).all() if projects else []
    return render_template('company_access.html', org=org, projects=projects, permissions=perms)


@admin_tools_bp.get('/admin/manifest')
@login_required
def secure_manifest():
    _super()
    from .api import manifest
    # Reuses the protected diagnostic implementation; this route requires superadmin
    # and works in the same authenticated application session, without a public token.
    return manifest()


@admin_tools_bp.get('/admin/uloziste/dokument/<int:document_id>')
@login_required
def document_download(document_id):
    _super()
    doc = InboxDocument.query.get_or_404(document_id)
    folder = Path(current_app.instance_path) / 'uploads' / str(doc.organization_id)
    # The archive lookup is by DB key, not user-controlled filesystem path.
    if Path(doc.stored_name).name != doc.stored_name:
        abort(404)
    path = folder / doc.stored_name
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=doc.original_name,
                     max_age=0)


@admin_tools_bp.get('/admin/uloziste/faktura/<int:document_id>')
@login_required
def invoice_download(document_id):
    _super()
    doc = EdaInvoiceDocument.query.get_or_404(document_id)
    if Path(doc.pdf_file).name != doc.pdf_file:
        abort(404)
    path = Path(current_app.instance_path) / 'invoices' / str(doc.organization_id) / doc.pdf_file
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name='Faktura-'+str(doc.invoice.number)+'.pdf', max_age=0)
