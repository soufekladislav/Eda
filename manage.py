import argparse
from datetime import date, timedelta
from decimal import Decimal
from app import app
from eda.extensions import db
from eda.models import Organization, User, Membership, Project, Task, Transaction, Integration
from eda.services.flexibee import sync_invoices
from eda.services.scheduler import ensure_scheduled_tasks


def init_db():
    with app.app_context():
        db.create_all()
        print("Database schema ready.")


def seed_admin(email, password, name, organization):
    with app.app_context():
        db.create_all()
        org = Organization.query.first()
        if not org:
            org = Organization(name=organization); db.session.add(org); db.session.flush()
        user = User.query.filter_by(email=email.lower()).first()
        if not user:
            user = User(email=email.lower(), display_name=name, is_admin=True); user.set_password(password); db.session.add(user); db.session.flush()
        else:
            user.display_name = name; user.is_admin = True; user.set_password(password)
        if not Membership.query.filter_by(user_id=user.id, organization_id=org.id).first():
            db.session.add(Membership(user_id=user.id, organization_id=org.id, role="owner"))
        db.session.commit()
        print(f"Admin ready: {email} / organization #{org.id} {org.name}")


def seed_demo():
    with app.app_context():
        org = Organization.query.first()
        if not org: raise SystemExit("Run seed-admin first.")
        p1 = Project.query.filter_by(organization_id=org.id, name="Fresh Develop – zakázky").first() or Project(organization_id=org.id, name="Fresh Develop – zakázky", project_type="construction")
        p2 = Project.query.filter_by(organization_id=org.id, name="Fresh Crackers – e-shop").first() or Project(organization_id=org.id, name="Fresh Crackers – e-shop", project_type="eshop")
        db.session.add_all([p1,p2]); db.session.flush()
        if Transaction.query.filter_by(organization_id=org.id, source="demo").count() == 0:
            db.session.add_all([
                Transaction(organization_id=org.id, project_id=p1.id, kind="income", amount=Decimal("145000"), booked_on=date.today(), description="Zálohová platba", source="demo"),
                Transaction(organization_id=org.id, project_id=p1.id, kind="expense", amount=Decimal("62000"), booked_on=date.today(), description="Materiál a práce", source="demo"),
                Transaction(organization_id=org.id, project_id=p2.id, kind="income", amount=Decimal("18500"), booked_on=date.today(), description="E-shop", source="demo"),
                Transaction(organization_id=org.id, project_id=p2.id, kind="expense", amount=Decimal("6200"), booked_on=date.today(), description="Provoz e-shopu", source="demo"),
            ])
        if Task.query.filter_by(organization_id=org.id, source="demo").count() == 0:
            db.session.add_all([
                Task(organization_id=org.id, title="Kontrola podkladů k DPH", task_type="tax", due_date=date.today()+timedelta(days=3), priority="high", source="demo"),
                Task(organization_id=org.id, title="Zkontrolovat faktury po splatnosti", task_type="invoice", due_date=date.today(), source="demo"),
            ])
        db.session.commit(); print("Demo data ready.")


def sync_flexi():
    with app.app_context():
        org = Organization.query.first()
        if not org: raise SystemExit("No organization. Run seed-admin first.")
        try:
            count = sync_invoices(org.id)
            integration = Integration.query.filter_by(organization_id=org.id, kind="flexibee", name="ABRA Flexi").first()
            if not integration:
                integration = Integration(organization_id=org.id, kind="flexibee", name="ABRA Flexi")
                db.session.add(integration)
            integration.status = "connected"
            integration.last_error = None
            from datetime import datetime
            integration.last_sync_at = datetime.utcnow()
            db.session.commit(); print(f"FlexiBee synced: {count} invoices")
        except Exception as e:
            print(f"FlexiBee sync failed: {e}")
            raise


def scheduler_tick():
    with app.app_context():
        changed = 0
        for org in Organization.query.order_by(Organization.id).all():
            if ensure_scheduled_tasks(org.id):
                changed += 1
        print(f"Scheduler tick complete. Organizations with new tasks: {changed}")


def main():
    p=argparse.ArgumentParser(description="Eda management commands")
    sub=p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    a=sub.add_parser("seed-admin"); a.add_argument("--email", required=True); a.add_argument("--password", required=True); a.add_argument("--name", default="Administrátor"); a.add_argument("--organization", default="Fresh Crackers s.r.o.")
    sub.add_parser("seed-demo")
    sub.add_parser("sync-flexibee")
    sub.add_parser("scheduler-tick")
    args=p.parse_args()
    if args.cmd=="init-db": init_db()
    elif args.cmd=="seed-admin": seed_admin(args.email,args.password,args.name,args.organization)
    elif args.cmd=="seed-demo": seed_demo()
    elif args.cmd=="sync-flexibee": sync_flexi()
    elif args.cmd=="scheduler-tick": scheduler_tick()

if __name__ == "__main__": main()
