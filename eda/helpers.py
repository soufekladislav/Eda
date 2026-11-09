from flask import session
from flask_login import current_user
from .models import Membership, Organization


def current_organization():
    if not current_user.is_authenticated:
        return None
    org_id = session.get("organization_id")
    q = Membership.query.filter_by(user_id=current_user.id)
    membership = q.filter_by(organization_id=org_id).first() if org_id else q.first()
    if not membership:
        return None
    session["organization_id"] = membership.organization_id
    return membership.organization
