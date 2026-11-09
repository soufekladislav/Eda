import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
import requests
from flask import current_app
from ..extensions import db
from ..models import Invoice, Integration, SyncLog


def _date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _decimal(value):
    try:
        return Decimal(str(value or 0))
    except InvalidOperation:
        return Decimal("0")


class FlexiBeeClient:
    def __init__(self, organization_id=None):
        c = current_app.config
        prefix = f"FLEXIBEE_ORG_{organization_id}_" if organization_id else ""
        import os
        def value(name, fallback_name):
            return os.getenv(prefix + name) if prefix and os.getenv(prefix + name) is not None else c.get(fallback_name)
        self.base = (value("BASE_URL", "FLEXIBEE_BASE_URL") or "").rstrip("/")
        self.company = value("COMPANY", "FLEXIBEE_COMPANY") or ""
        self.session = requests.Session()
        self.session.auth = (value("USERNAME", "FLEXIBEE_USERNAME") or "", value("PASSWORD", "FLEXIBEE_PASSWORD") or "")
        verify_value = value("VERIFY_SSL", "FLEXIBEE_VERIFY_SSL")
        self.verify = verify_value if isinstance(verify_value, bool) else str(verify_value or "1") == "1"
        self.organization_id = organization_id

    @property
    def configured(self):
        return bool(self.base and self.company and self.session.auth[0])

    def _check(self):
        if not self.configured:
            raise RuntimeError("ABRA Flexi není nakonfigurována v .env")

    def get_evidence(self, evidence, params=None, detail=None, limit=0):
        self._check()
        url = f"{self.base}/c/{self.company}/{evidence}.json"
        q = {"limit": limit}
        if detail:
            q["detail"] = detail
        q.update(params or {})
        r = self.session.get(url, params=q, timeout=35, verify=self.verify, allow_redirects=True)
        r.raise_for_status()
        payload = r.json()
        winstrom = payload.get("winstrom", payload) if isinstance(payload, dict) else {}
        rows = winstrom.get(evidence, []) if isinstance(winstrom, dict) else []
        if isinstance(rows, dict):
            rows = [rows]
        return rows or []

    def get(self, evidence, params=None):
        detail = "custom:id,kod,varSym,datVyst,datSplat,sumCelkem,zbyvaUhradit,mena,stavUhrK"
        return self.get_evidence(evidence, params=params, detail=detail, limit=0)

    def post_evidence(self, evidence, item, dry_run=True):
        self._check()
        url = f"{self.base}/c/{self.company}/{evidence}.json"
        params = {"dry-run": "true"} if dry_run else {}
        body = {"winstrom": {"@version": "1.0", evidence: [item]}}
        r = self.session.post(url, params=params, json=body, timeout=35, verify=self.verify, allow_redirects=True)
        if not r.ok:
            raise RuntimeError(f"ABRA Flexi HTTP {r.status_code}: {r.text[:1200]}")
        try:
            return r.json()
        except ValueError:
            return {"ok": True, "text": r.text[:3000]}

    def create_issued_invoice(self, data, dry_run=True):
        item = {}
        if data.get("external_id"):
            item["id"] = f"ext:EDA:{data['external_id']}"
            item["@update"] = "ignore"
        if data.get("type_code"):
            item["typDokl"] = f"code:{data['type_code']}"
        if data.get("partner_code"):
            item["firma"] = f"code:{data['partner_code']}"
        else:
            billing = data.get("billing") if isinstance(data.get("billing"), dict) else {}
            if billing:
                company = str(billing.get("company") or "").strip()
                first = str(billing.get("first_name") or "").strip()
                last = str(billing.get("last_name") or "").strip()
                contact_name = " ".join(x for x in (first, last) if x).strip()
                item["nazFirmy"] = company or contact_name or "Zákazník e-shopu"
                if contact_name:
                    item["kontaktJmeno"] = contact_name
                if billing.get("email"):
                    item["kontaktEmail"] = str(billing.get("email"))
                if billing.get("phone"):
                    item["kontaktTel"] = str(billing.get("phone"))
                street = " ".join(x for x in (str(billing.get("address_1") or "").strip(), str(billing.get("address_2") or "").strip()) if x).strip()
                if street:
                    item["ulice"] = street
                if billing.get("city"):
                    item["mesto"] = str(billing.get("city"))
                if billing.get("postcode"):
                    item["psc"] = str(billing.get("postcode"))
                country = str(billing.get("country") or "").strip().upper()
                if country:
                    item["stat"] = f"code:{country}"
                if billing.get("ico"):
                    item["ic"] = str(billing.get("ico"))
                if billing.get("dic"):
                    item["dic"] = str(billing.get("dic"))
        if data.get("variable_symbol"):
            item["varSym"] = str(data["variable_symbol"])
        if data.get("description"):
            item["popis"] = str(data["description"])
        if data.get("due_on"):
            item["datSplat"] = str(data["due_on"])
        if data.get("issued_on"):
            item["datVyst"] = str(data["issued_on"])
        if data.get("currency"):
            item["mena"] = f"code:{data['currency']}"
        if data.get("items"):
            item["polozkyDokladu"] = data["items"]
        elif data.get("amount") not in (None, ""):
            # Minimal one-line invoice. ABRA Flexi derives taxes/accounting rules from the document type;
            # dry-run is always executed before the real write.
            item["polozkyDokladu"] = [{
                "nazev": str(data.get("description") or "Položka"),
                "mnozMj": 1,
                "cenaMj": str(data["amount"]),
            }]
        if not item:
            raise RuntimeError("Návrh faktury neobsahuje žádná data pro ABRA Flexi.")
        return self.post_evidence("faktura-vydana", item, dry_run=dry_run)

    def health(self):
        self._check()
        url = f"{self.base}/c/{self.company}/evidence-list.json"
        r = self.session.get(url, timeout=15, verify=self.verify, allow_redirects=True)
        r.raise_for_status()
        return {"ok": True, "http_status": r.status_code, "company": self.company}

    def create_received_invoice(self, data, dry_run=True):
        item = {}
        if data.get("external_id"):
            item["id"] = f"ext:EDA:{data['external_id']}"
            item["@update"] = "ignore"
        if data.get("type_code"):
            item["typDokl"] = f"code:{data['type_code']}"
        if data.get("partner_code"):
            item["firma"] = f"code:{data['partner_code']}"
        if data.get("variable_symbol"):
            item["varSym"] = str(data["variable_symbol"])
        if data.get("description"):
            item["popis"] = str(data["description"])
        if data.get("due_on"):
            item["datSplat"] = str(data["due_on"])
        if data.get("issued_on"):
            item["datVyst"] = str(data["issued_on"])
        if data.get("currency"):
            item["mena"] = f"code:{data['currency']}"
        if data.get("items"):
            item["polozkyDokladu"] = data["items"]
        elif data.get("amount") not in (None, ""):
            item["polozkyDokladu"] = [{
                "nazev": str(data.get("description") or "Nákladový doklad"),
                "mnozMj": 1,
                "cenaMj": str(data["amount"]),
            }]
        if not item:
            raise RuntimeError("Návrh přijaté faktury neobsahuje data pro ABRA Flexi.")
        return self.post_evidence("faktura-prijata", item, dry_run=dry_run)

    def get_control_statement_data(self, year, month=None, quarter=None, missing_line=False):
        self._check()
        url = f"{self.base}/c/{self.company}/kontrolni-hlaseni-dph.json"
        params = {"rok": int(year), "limit": 0}
        if month:
            params["mesic"] = int(month)
        elif quarter:
            params["ctvrtleti"] = int(quarter)
        else:
            raise RuntimeError("Je potřeba zadat měsíc nebo čtvrtletí.")
        if missing_line:
            params["clenKonVykNull"] = "true"
        r = self.session.get(url, params=params, timeout=35, verify=self.verify)
        r.raise_for_status()
        payload = r.json()
        winstrom = payload.get("winstrom", payload) if isinstance(payload, dict) else {}
        rows = winstrom.get("kontrolni-hlaseni-dph", []) if isinstance(winstrom, dict) else []
        if isinstance(rows, dict):
            rows = [rows]
        return rows or []

    def get_control_statement(self, period, fmt="pdf", kind="B", save=False):
        self._check()
        if fmt not in ("pdf", "xml"):
            raise RuntimeError("Kontrolní hlášení podporuje pouze PDF nebo XML.")
        url = f"{self.base}/c/{self.company}/kontrolni-hlaseni.{fmt}"
        params = {"obdobi": period, "druh": kind}
        if save:
            params["ulozit"] = "true"
        r = self.session.get(url, params=params, timeout=60, verify=self.verify)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type")


def sync_invoices(organization_id):
    client = FlexiBeeClient(organization_id)
    if not client.configured:
        raise RuntimeError("ABRA Flexi není nakonfigurována v .env")
    total_items = 0
    for evidence, direction in (("faktura-vydana", "issued"), ("faktura-prijata", "received")):
        rows = client.get(evidence)
        for item in rows:
            external_id = str(item.get("id") or item.get("kod") or "").strip()
            if not external_id:
                continue
            inv = Invoice.query.filter_by(organization_id=organization_id, direction=direction, external_id=external_id).first()
            if not inv:
                inv = Invoice(organization_id=organization_id, direction=direction, external_id=external_id)
                db.session.add(inv)
            inv.number = str(item.get("kod") or "") or None
            inv.variable_symbol = str(item.get("varSym") or "") or None
            inv.issued_on = _date(item.get("datVyst"))
            inv.due_on = _date(item.get("datSplat"))
            inv.total = _decimal(item.get("sumCelkem"))
            inv.remaining = _decimal(item.get("zbyvaUhradit"))
            inv.paid = inv.remaining <= 0
            inv.currency = str(item.get("mena") or "CZK").replace("code:", "")[:8]
            inv.raw_json = json.dumps(item, ensure_ascii=False, default=str)
            total_items += 1
    db.session.add(SyncLog(organization_id=organization_id, integration="flexibee", status="ok", items=total_items, message="Synchronizace faktur"))
    db.session.commit()
    return total_items
