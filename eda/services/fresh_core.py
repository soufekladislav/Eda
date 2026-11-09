import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse

import requests

from ..extensions import db
from ..models import Order, SyncLog
from .fresh_core_accounting import FreshCoreAccountingClient, FreshCoreAccountingError


class FreshCoreError(RuntimeError):
    pass


def _request_json(url, timeout=20):
    if not url:
        raise FreshCoreError("Chybí URL Fresh Core manifestu.")
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "Eda/0.6.6 FreshCore"})
    except requests.RequestException as exc:
        raise FreshCoreError(f"Konektor není dostupný: {exc}") from exc
    if response.status_code >= 400:
        raise FreshCoreError(f"Konektor vrátil HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise FreshCoreError("Zadaná URL nevrací JSON manifest Fresh Core.") from exc
    if not isinstance(payload, (dict, list)):
        raise FreshCoreError("Fresh Core manifest nemá očekávaný JSON formát.")
    return payload, response


def _walk(obj, path=()):
    if isinstance(obj, dict):
        for key, value in obj.items():
            p = path + (str(key),)
            yield p, value
            yield from _walk(value, p)
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            p = path + (str(idx),)
            yield p, value
            yield from _walk(value, p)


def _looks_like_order_list(value):
    if not isinstance(value, list) or not value:
        return False
    sample = value[0]
    if not isinstance(sample, dict):
        return False
    keys = {str(k).lower() for k in sample.keys()}
    return bool(keys & {"id", "external_id", "number"}) and bool(keys & {"status", "total", "date_created", "ordered_at"})


def _extract_embedded_orders(manifest):
    if isinstance(manifest, list) and _looks_like_order_list(manifest):
        return manifest
    for path, value in _walk(manifest):
        leaf = path[-1].lower() if path else ""
        if leaf in {"orders", "shop_orders", "woocommerce_orders", "objednavky"} and _looks_like_order_list(value):
            return value
    return None


def _candidate_endpoint(manifest, manifest_url, site_url=None):
    candidate_keys = {
        "orders_url", "orders_endpoint", "orders_api", "orders_feed", "orders_feed_url",
        "woocommerce_orders_url", "order_feed", "objednavky_url",
    }
    for path, value in _walk(manifest):
        leaf = path[-1].lower() if path else ""
        if leaf in candidate_keys and isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.startswith("http://") or raw.startswith("https://"):
                return raw
            return urljoin((site_url or manifest_url).rstrip("/") + "/", raw.lstrip("/"))
        if leaf == "orders" and isinstance(value, dict):
            for k in ("url", "endpoint", "href"):
                raw = value.get(k)
                if isinstance(raw, str) and raw.strip():
                    raw = raw.strip()
                    if raw.startswith("http://") or raw.startswith("https://"):
                        return raw
                    return urljoin((site_url or manifest_url).rstrip("/") + "/", raw.lstrip("/"))
    return None


def _normalize_order(item):
    billing = item.get("billing") if isinstance(item.get("billing"), dict) else {}
    return {
        "external_id": str(item.get("id") or item.get("external_id") or item.get("number") or "").strip(),
        "total": item.get("total") or item.get("total_price") or item.get("amount") or 0,
        "currency": item.get("currency") or item.get("currency_code") or "CZK",
        "status": item.get("status") or item.get("state") or "",
        "ordered_at": item.get("ordered_at") or item.get("date_created") or item.get("created_at") or item.get("date"),
        "customer_email": item.get("customer_email") or item.get("email") or billing.get("email"),
        "raw": item,
    }


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def import_orders(organization_id, shop, items, source="fresh_core_pull"):
    count = 0
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = _normalize_order(raw)
        ext = item["external_id"]
        if not ext:
            continue
        order = Order.query.filter_by(shop_id=shop.id, external_id=ext).first()
        if not order:
            order = Order(organization_id=organization_id, shop_id=shop.id, external_id=ext)
            db.session.add(order)
        try:
            order.total = Decimal(str(item["total"] or 0))
        except (InvalidOperation, ValueError, TypeError):
            order.total = Decimal("0")
        order.currency = str(item["currency"] or "CZK")[:8]
        order.status = str(item["status"] or "")[:80] or None
        order.ordered_at = _parse_dt(item["ordered_at"])
        order.customer_email = str(item["customer_email"] or "")[:255] or None
        order.raw_json = json.dumps(item["raw"], ensure_ascii=False, default=str)
        count += 1
    shop.last_sync_at = datetime.utcnow()
    db.session.add(SyncLog(
        organization_id=organization_id,
        integration=f"shop:{shop.id}",
        status="ok",
        items=count,
        message=f"Objednávky načteny ({source})",
    ))
    db.session.commit()
    return count


def test_connection(organization_id, shop, connection):
    target = (connection.base_url or shop.site_url or "").strip()
    if not target:
        raise FreshCoreError("Nejdřív nastav URL e-shopu nebo Fresh Core manifest.")

    started = datetime.utcnow()
    if not connection.base_url:
        try:
            response = requests.get(target, timeout=15, headers={"User-Agent": "Eda/0.6.6 connectivity-check"})
        except requests.RequestException as exc:
            connection.status = "error"
            connection.last_sync_at = datetime.utcnow()
            connection.last_error = str(exc)[:1000]
            db.session.commit()
            raise FreshCoreError(str(exc)) from exc
        elapsed = int((datetime.utcnow() - started).total_seconds() * 1000)
        if response.status_code >= 400:
            connection.status = "error"
            connection.last_error = f"HTTP {response.status_code}"
            message = f"Web vrátil HTTP {response.status_code}."
        else:
            connection.status = "web_reachable"
            connection.last_error = None
            message = "Web odpovídá, ale není nastaven Fresh Core manifest."
        connection.last_sync_at = datetime.utcnow()
        db.session.add(SyncLog(organization_id=organization_id, integration=f"shop:{shop.id}", status=connection.status, items=0, message=message))
        db.session.commit()
        return {"status": connection.status, "elapsed_ms": elapsed, "message": message}

    manifest, response = _request_json(connection.base_url, timeout=15)
    elapsed = int((datetime.utcnow() - started).total_seconds() * 1000)
    connection.last_sync_at = datetime.utcnow()
    connection.last_error = None
    embedded = _extract_embedded_orders(manifest)
    endpoint = _candidate_endpoint(manifest, connection.base_url, shop.site_url)
    capability = "embedded_orders" if embedded is not None else ("orders_endpoint" if endpoint else "push_only")
    connection.status = "sync_ready" if capability != "push_only" else "connected"
    message = f"Fresh Core manifest OK; režim objednávek: {capability}"
    db.session.add(SyncLog(organization_id=organization_id, integration=f"shop:{shop.id}", status=connection.status, items=0, message=message))
    db.session.commit()
    return {"status": connection.status, "elapsed_ms": elapsed, "message": message, "orders_mode": capability}


# The accounting API may return fields that should not be persisted in the
# general orders table (for example payment references or free-text notes).
# Billing and delivery PII belong in encrypted OrderAccountingSnapshot records.
_SYNC_ALLOWED_FIELDS = frozenset({
    "id", "external_id", "number", "status", "date_created", "date_modified",
    "date_paid", "currency", "total", "total_tax", "shipping_total",
    "discount_total", "payment_method", "payment_method_title", "is_paid",
    "needs_payment", "created_via", "items",
})
_SYNC_ALLOWED_ITEM_FIELDS = frozenset({
    "id", "product_id", "variation_id", "sku", "name", "quantity",
    "total", "total_tax", "subtotal", "subtotal_tax",
})


def _safe_accounting_order(item):
    row = {k: v for k, v in item.items() if k in _SYNC_ALLOWED_FIELDS}
    row["items"] = [
        {k: v for k, v in line.items() if k in _SYNC_ALLOWED_ITEM_FIELDS}
        for line in (item.get("items") or []) if isinstance(line, dict)
    ]
    return row


def sync_accounting_orders(organization_id, shop, accounting, per_page=200, max_pages=100):
    """Pull all pages before importing; an API error never commits a half batch."""
    client = FreshCoreAccountingClient(accounting)
    if not client.configured:
        raise FreshCoreError("Accounting API nemá uložený endpoint a klíč.")
    all_rows = []
    try:
        for page in range(1, max_pages + 1):
            payload = client.orders_page(page=page, per_page=per_page)
            all_rows.extend(_safe_accounting_order(r) for r in payload["orders"] if isinstance(r, dict))
            if not payload.get("has_more"):
                break
        else:
            raise FreshCoreError(
                "Překročen limit 20 000 objednávek v jedné synchronizaci. "
                "Je potřeba přejít na inkrementální dávky. Žádné údaje nebyly změněny."
            )
    except FreshCoreAccountingError as exc:
        raise FreshCoreError(f"Autorizované načtení objednávek selhalo: {exc}") from exc
    try:
        count = import_orders(organization_id, shop, all_rows, source="accounting_api")
    except Exception:
        db.session.rollback()
        raise
    return {"items": count, "source": "zabezpečené Accounting API", "pages": page}


def sync_orders(organization_id, shop, connection, accounting=None):
    # Do not fall back silently to an incomplete static manifest after an
    # accounting authentication failure; tell the user the real error.
    if accounting and accounting.encrypted_key and accounting.orders_endpoint:
        return sync_accounting_orders(organization_id, shop, accounting)
    if not connection or not connection.base_url:
        raise FreshCoreError(
            "Fresh Core manifest není nastaven. Samotná URL webu nestačí k načtení objednávek. "
            "Doplň manifest URL v úpravě e-shopu."
        )

    manifest, _ = _request_json(connection.base_url, timeout=20)
    connection.status = "connected"
    connection.last_sync_at = datetime.utcnow()
    connection.last_error = None

    embedded = _extract_embedded_orders(manifest)
    if embedded is not None:
        count = import_orders(organization_id, shop, embedded, source="manifest")
        connection.status = "sync_ready"
        connection.last_sync_at = datetime.utcnow()
        db.session.commit()
        return {"items": count, "source": "manifest"}

    endpoint = _candidate_endpoint(manifest, connection.base_url, shop.site_url)
    if endpoint:
        payload, _ = _request_json(endpoint, timeout=30)
        if isinstance(payload, dict):
            items = payload.get("orders") or payload.get("data") or payload.get("items") or []
        else:
            items = payload
        if not isinstance(items, list):
            raise FreshCoreError("Endpoint objednávek odpověděl, ale nevrátil seznam objednávek.")
        count = import_orders(organization_id, shop, items, source="manifest_endpoint")
        connection.status = "sync_ready"
        connection.last_sync_at = datetime.utcnow()
        db.session.commit()
        return {"items": count, "source": endpoint}

    db.session.add(SyncLog(
        organization_id=organization_id,
        integration=f"shop:{shop.id}",
        status="waiting_for_orders_feed",
        items=0,
        message="Manifest je dostupný, ale neobsahuje objednávky ani orders endpoint. Očekává se push do /api/v1/fresh-core/orders.",
    ))
    connection.status = "connected"
    connection.last_sync_at = datetime.utcnow()
    db.session.commit()
    raise FreshCoreError(
        "Fresh Core je připojený, ale jeho manifest neposkytuje zdroj objednávek. "
        "Současná Eda umí objednávky přijmout na /api/v1/fresh-core/orders; Fresh Core na webu musí data aktivně poslat, "
        "nebo v manifestu zveřejnit orders endpoint. Konektivita tedy funguje, synchronizace objednávek zatím ne."
    )
