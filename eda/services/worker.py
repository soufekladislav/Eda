import json
from datetime import datetime, date
from decimal import Decimal
from flask import current_app
from ..extensions import db
from ..models import AgentJob, AgentAction, Order, Shop, Invoice, Project, AuditLog, OrderAccountingSnapshot
from .flexibee import FlexiBeeClient, sync_invoices


PRESETS = [
    {
        "code": "check_transfer_payments",
        "name": "Zkontrolovat platby e-shopu převodem",
        "description": "Porovná bankovní převody / objednávky z e-shopu s vydanými fakturami a stavem úhrady v ABRA Flexi.",
    },
    {
        "code": "check_posting",
        "name": "Zkontrolovat zaúčtované a nezaúčtované položky",
        "description": "Načte účetní deník z ABRA Flexi a rozdělí záznamy podle stavu zaúčtování.",
    },
    {
        "code": "vat_review",
        "name": "Připravit kontrolu DPH a kontrolního hlášení",
        "description": "Prověří podklady pro kontrolní hlášení a upozorní na chybějící řádky KH.",
    },
    {
        "code": "create_invoice_request",
        "name": "Připravit vydanou fakturu",
        "description": "Připraví návrh faktury, ověří jej přes ABRA Flexi dry-run a před ostrým zápisem vyžádá schválení.",
    },
]


def _j(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _audit(org_id, user_id, action, entity_type=None, entity_id=None, detail=None):
    db.session.add(AuditLog(
        organization_id=org_id,
        user_id=user_id,
        category="worker",
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail_json=_j(detail or {}),
    ))


def infer_job_type(prompt):
    p = (prompt or "").lower()
    if ("platb" in p or "převod" in p or "prevod" in p) and ("shop" in p or "objedn" in p):
        return "check_transfer_payments"
    if "zaúčt" in p or "zauct" in p:
        return "check_posting"
    if "kontrolní hláš" in p or "kontrolni hlas" in p or "dph" in p:
        return "vat_review"
    if "faktur" in p and ("vystav" in p or "vytvo" in p or "založ" in p or "zaloz" in p):
        return "create_invoice_request"
    return "custom"


def create_job(organization_id, user_id, prompt, job_type=None, extra=None):
    job_type = job_type or infer_job_type(prompt)
    title_map = {p["code"]: p["name"] for p in PRESETS}
    title = title_map.get(job_type) or ((prompt or "Nový úkol").strip()[:120])
    job = AgentJob(
        organization_id=organization_id,
        user_id=user_id,
        title=title,
        prompt=prompt.strip(),
        job_type=job_type,
        status="planned",
    )
    db.session.add(job)
    db.session.flush()

    actions = plan_actions(job_type, prompt, extra or {})
    for i, item in enumerate(actions, start=1):
        db.session.add(AgentAction(
            job_id=job.id,
            position=i,
            action_type=item["type"],
            label=item["label"],
            payload_json=_j(item.get("payload") or {}),
            risk_level=item.get("risk", "read"),
            requires_approval=bool(item.get("approval")),
            status="pending",
        ))
    job.requires_approval = any(bool(a.get("approval")) for a in actions)
    job.plan_json = _j(actions)
    _audit(organization_id, user_id, "job_created", "agent_job", job.id, {"type": job_type})
    db.session.commit()
    return job


def plan_actions(job_type, prompt, extra):
    if job_type == "check_transfer_payments":
        return [
            {"type": "sync_flexi_invoices", "label": "Načíst aktuální faktury a stav úhrad z ABRA Flexi", "risk": "read"},
            {"type": "compare_transfer_orders", "label": "Porovnat objednávky placené převodem s fakturami a úhradami", "risk": "read"},
            {"type": "summarize", "label": "Připravit přehled nesrovnalostí a doporučení", "risk": "read"},
        ]
    if job_type == "check_posting":
        return [
            {"type": "flexi_posting_check", "label": "Načíst zaúčtované a nezaúčtované záznamy z účetního deníku", "risk": "read"},
            {"type": "summarize", "label": "Připravit seznam položek vyžadujících kontrolu", "risk": "read"},
        ]
    if job_type == "vat_review":
        today = date.today()
        month = int(extra.get("month") or today.month)
        year = int(extra.get("year") or today.year)
        return [
            {"type": "flexi_vat_check", "label": "Prověřit data kontrolního hlášení a chybějící řádky KH", "risk": "read", "payload": {"year": year, "month": month}},
            {"type": "summarize", "label": "Připravit podklady k DPH a kontrolnímu hlášení", "risk": "read"},
        ]
    if job_type == "create_invoice_request":
        return [
            {"type": "prepare_invoice", "label": "Připravit návrh vydané faktury", "risk": "read", "payload": extra},
            {"type": "validate_invoice", "label": "Ověřit návrh přes ABRA Flexi dry-run", "risk": "write", "approval": False, "payload": extra},
            {"type": "create_invoice", "label": "Vytvořit vydanou fakturu v ABRA Flexi", "risk": "write", "approval": True, "payload": extra},
        ]
    return [
        {"type": "needs_ai_plan", "label": "Navrhnout řešení úkolu přes Fresh AI Engine", "risk": "read", "payload": {"prompt": prompt}},
    ]


def execute_job(job, allow_write=False):
    job.status = "running"
    job.started_at = job.started_at or datetime.utcnow()
    db.session.commit()

    context = {"results": []}
    failed = False
    waiting = False
    for action in job.actions:
        if action.status == "done":
            context["results"].append(_loads(action.result_json, {}))
            continue
        if action.requires_approval and not allow_write:
            action.status = "waiting_approval"
            waiting = True
            break
        try:
            action.status = "running"
            db.session.commit()
            result = execute_action(job, action, context, allow_write=allow_write)
            action.result_json = _j(result)
            action.status = "done"
            action.executed_at = datetime.utcnow()
            context["results"].append(result)
            db.session.commit()
        except Exception as exc:
            action.status = "error"
            action.error = str(exc)
            job.status = "error"
            job.summary = f"Úkol se zastavil na kroku „{action.label}“: {exc}"
            try:
                payload = _loads(action.payload_json, {})
                snapshot_id = payload.get("order_accounting_snapshot_id")
                if snapshot_id:
                    snapshot = db.session.get(OrderAccountingSnapshot, int(snapshot_id))
                    if snapshot and snapshot.organization_id == job.organization_id:
                        snapshot.status = "error"
                        snapshot.last_error = str(exc)[:2000]
            except Exception:
                pass
            failed = True
            db.session.commit()
            break

    if not failed:
        if waiting:
            job.status = "waiting_approval"
            job.summary = build_summary(job)
        else:
            job.status = "done"
            job.finished_at = datetime.utcnow()
            job.summary = build_summary(job)
        job.result_json = _j(context)
        _audit(job.organization_id, job.user_id, "job_executed", "agent_job", job.id, {"status": job.status})
        db.session.commit()
    return job


def execute_action(job, action, context, allow_write=False):
    payload = _loads(action.payload_json, {})
    if action.action_type == "sync_flexi_invoices":
        count = sync_invoices(job.organization_id)
        return {"type": "sync", "count": count, "message": f"Načteno {count} faktur z ABRA Flexi."}
    if action.action_type == "compare_transfer_orders":
        return compare_transfer_orders(job.organization_id)
    if action.action_type == "flexi_posting_check":
        client = FlexiBeeClient(job.organization_id)
        posted = client.get_evidence("ucetni-denik", params={"postingState": "posted"}, detail="custom:id,kod,datVyst,popis,zuctovano", limit=200)
        unposted = client.get_evidence("ucetni-denik", params={"postingState": "unposted"}, detail="custom:id,kod,datVyst,popis,zuctovano", limit=200)
        return {
            "type": "posting_check",
            "posted_count": len(posted),
            "unposted_count": len(unposted),
            "unposted": unposted[:50],
        }
    if action.action_type == "flexi_vat_check":
        client = FlexiBeeClient(job.organization_id)
        year = int(payload.get("year") or date.today().year)
        month = int(payload.get("month") or date.today().month)
        rows = client.get_control_statement_data(year=year, month=month)
        missing = client.get_control_statement_data(year=year, month=month, missing_line=True)
        return {"type": "vat_check", "year": year, "month": month, "items": len(rows), "missing_kh_line": len(missing), "missing_items": missing[:50]}
    if action.action_type == "prepare_invoice":
        return {"type": "invoice_proposal", "payload": payload, "message": "Návrh faktury je připraven k validaci."}
    if action.action_type == "validate_invoice":
        client = FlexiBeeClient(job.organization_id)
        result = client.create_issued_invoice(payload, dry_run=True)
        snapshot_id = payload.get("order_accounting_snapshot_id")
        if snapshot_id:
            snapshot = db.session.get(OrderAccountingSnapshot, int(snapshot_id))
            if snapshot and snapshot.organization_id == job.organization_id:
                snapshot.status = "dry_run_ok"
                snapshot.flexi_dry_run_at = datetime.utcnow()
                snapshot.validation_json = _j(result)
                snapshot.last_error = None
                db.session.commit()
        return {"type": "invoice_dry_run", "ok": True, "response": result}
    if action.action_type == "create_invoice":
        if not allow_write:
            raise RuntimeError("Zápis vyžaduje schválení uživatele.")
        client = FlexiBeeClient(job.organization_id)
        result = client.create_issued_invoice(payload, dry_run=False)
        snapshot_id = payload.get("order_accounting_snapshot_id")
        if snapshot_id:
            snapshot = db.session.get(OrderAccountingSnapshot, int(snapshot_id))
            if snapshot and snapshot.organization_id == job.organization_id:
                snapshot.status = "invoiced"
                snapshot.flexi_written_at = datetime.utcnow()
                snapshot.validation_json = _j(result)
                snapshot.last_error = None
                db.session.commit()
                try:
                    sync_invoices(job.organization_id)
                    variable_symbol = str(payload.get("variable_symbol") or "").strip()
                    if variable_symbol:
                        invoice = Invoice.query.filter_by(organization_id=job.organization_id, direction="issued", variable_symbol=variable_symbol).order_by(Invoice.id.desc()).first()
                        if invoice and snapshot.project_id:
                            invoice.project_id = snapshot.project_id
                            db.session.commit()
                except Exception:
                    # Faktura už byla zapsána; následná synchronizace nesmí změnit úspěšný výsledek zápisu.
                    pass
        return {"type": "invoice_created", "ok": True, "response": result}
    if action.action_type == "summarize":
        return {"type": "summary", "message": "Kontrola dokončena. Výsledky jsou uvedené v jednotlivých krocích."}
    if action.action_type == "needs_ai_plan":
        return {
            "type": "needs_ai_engine",
            "message": "Volný úkol je uložený. Pro plné plánování je potřeba připojit Fresh AI Engine k Edovi.",
            "prompt": payload.get("prompt") or job.prompt,
        }
    raise RuntimeError(f"Neznámá akce: {action.action_type}")


def compare_transfer_orders(organization_id):
    invoices = Invoice.query.filter_by(organization_id=organization_id, direction="issued").all()
    by_vs = {str(i.variable_symbol or "").strip(): i for i in invoices if i.variable_symbol}
    shops = Shop.query.filter_by(organization_id=organization_id, active=True).all()
    discrepancies = []
    checked = 0
    matched = 0
    for shop in shops:
        for order in Order.query.filter_by(organization_id=organization_id, shop_id=shop.id).all():
            raw = _loads(order.raw_json, {})
            method = " ".join([
                str(raw.get("payment_method") or ""),
                str(raw.get("payment_method_title") or ""),
                str(raw.get("payment") or ""),
            ]).lower()
            is_transfer = any(x in method for x in ("bacs", "bank", "převod", "prevod", "transfer"))
            if not is_transfer:
                continue
            checked += 1
            candidates = [str(order.external_id)]
            for key in ("number", "order_number", "variable_symbol", "var_sym"):
                if raw.get(key):
                    candidates.append(str(raw.get(key)))
            invoice = next((by_vs.get(c) for c in candidates if c in by_vs), None)
            if not invoice:
                discrepancies.append({"shop": shop.name, "order": order.external_id, "issue": "Nenašla se odpovídající faktura podle variabilního symbolu.", "total": str(order.total)})
                continue
            matched += 1
            if Decimal(invoice.total or 0) != Decimal(order.total or 0):
                discrepancies.append({"shop": shop.name, "order": order.external_id, "invoice": invoice.number, "issue": "Nesouhlasí částka objednávky a faktury.", "order_total": str(order.total), "invoice_total": str(invoice.total)})
            if not invoice.paid:
                discrepancies.append({"shop": shop.name, "order": order.external_id, "invoice": invoice.number, "issue": "Faktura není v ABRA Flexi označena jako uhrazená.", "remaining": str(invoice.remaining)})
    return {"type": "transfer_check", "checked": checked, "matched": matched, "discrepancies": discrepancies, "discrepancy_count": len(discrepancies)}


def build_summary(job):
    results = []
    for action in job.actions:
        data = _loads(action.result_json, {})
        if data.get("type") == "transfer_check":
            results.append(f"Zkontrolováno {data.get('checked', 0)} objednávek převodem; nalezeno {data.get('discrepancy_count', 0)} nesrovnalostí.")
        elif data.get("type") == "posting_check":
            results.append(f"Účetní deník: {data.get('posted_count', 0)} zaúčtovaných a {data.get('unposted_count', 0)} nezaúčtovaných položek v načteném rozsahu.")
        elif data.get("type") == "vat_check":
            results.append(f"Kontrola KH {data.get('month')}/{data.get('year')}: {data.get('missing_kh_line', 0)} položek bez řádku kontrolního hlášení.")
        elif data.get("type") == "invoice_created":
            results.append("Vydaná faktura byla vytvořena v ABRA Flexi.")
        elif data.get("type") == "needs_ai_engine":
            results.append(data.get("message"))
    return " ".join(results) if results else "Úkol byl zpracován."
