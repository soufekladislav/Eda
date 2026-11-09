import json
from datetime import datetime
from urllib.parse import quote, urlencode
import requests
from .secrets import decrypt_text


class FreshCoreAccountingError(RuntimeError):
    pass


def _get_json(url, headers=None, timeout=20):
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise FreshCoreAccountingError(f"Fresh Core Accounting API není dostupné: {exc}") from exc
    if 300 <= r.status_code < 400:
        raise FreshCoreAccountingError('Accounting API přesměrovává REST požadavek (HTTP %s). Nastav přímo cílovou HTTPS URL ve Fresh Core; kvůli ochraně účetního klíče přesměrování nesledujeme.' % r.status_code)
    if not r.ok:
        detail = ""
        try:
            payload = r.json()
            detail = payload.get("message") or payload.get("error") or ""
        except Exception:
            detail = r.text[:500]
        suffix = f": {detail}" if detail else ""
        raise FreshCoreAccountingError(f"Fresh Core Accounting API vrátilo HTTP {r.status_code}{suffix}")
    try:
        return r.json(), r
    except ValueError as exc:
        raise FreshCoreAccountingError("Fresh Core Accounting API nevrátilo JSON.") from exc


def discover_from_manifest(manifest_url):
    payload, _ = _get_json(manifest_url, headers={"User-Agent": "Eda/0.6.6 accounting-discovery"}, timeout=20)
    if not isinstance(payload, dict):
        raise FreshCoreAccountingError("Fresh Core manifest nemá očekávaný formát.")
    commerce = payload.get("commerce") if isinstance(payload.get("commerce"), dict) else {}
    capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    orders_endpoint = commerce.get("accounting_orders_endpoint")
    detail_endpoint = commerce.get("accounting_order_detail_endpoint")
    auth_header = commerce.get("accounting_auth_header") or "X-Fresh-Core-Accounting-Key"
    enabled = commerce.get("accounting_enabled")
    if enabled is None:
        enabled = capabilities.get("accounting_enabled")
    version = payload.get("fresh_core_version") or payload.get("version") or payload.get("plugin_version")
    if not orders_endpoint:
        raise FreshCoreAccountingError("Manifest neobsahuje Fresh Core Accounting endpoint. Je na webu Fresh Core 0.6.0+?")
    if not detail_endpoint:
        detail_endpoint = str(orders_endpoint).rstrip("/") + "/{id}"
    return {
        "orders_endpoint": str(orders_endpoint),
        "detail_endpoint": str(detail_endpoint),
        "auth_header": str(auth_header),
        "remote_enabled": bool(enabled),
        "plugin_version": str(version) if version else None,
    }


class FreshCoreAccountingClient:
    def __init__(self, connection):
        self.connection = connection
        self.orders_endpoint = (connection.orders_endpoint or "").strip()
        self.detail_endpoint = (connection.detail_endpoint or "").strip()
        self.auth_header = (connection.auth_header or "X-Fresh-Core-Accounting-Key").strip()

    @property
    def configured(self):
        return bool(self.orders_endpoint and self.connection.encrypted_key)

    def _headers(self):
        if not self.configured:
            raise FreshCoreAccountingError("Accounting API není nakonfigurované. Doplň endpoint a Accounting key.")
        return {
            self.auth_header: decrypt_text(self.connection.encrypted_key),
            "User-Agent": "Eda/0.6.6 FreshCoreAccounting",
            "Accept": "application/json",
        }

    def ping_url(self):
        base = self.orders_endpoint.rstrip("/")
        if base.endswith("/orders"):
            return base[:-len("/orders")] + "/ping"
        return base.rsplit("/", 1)[0] + "/ping"

    def ping(self):
        payload, response = _get_json(self.ping_url(), headers=self._headers(), timeout=15)
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise FreshCoreAccountingError("Accounting ping nevrátil potvrzení ok=true.")
        return {
            "ok": True,
            "http_status": response.status_code,
            "version": payload.get("version"),
            "woocommerce": payload.get("woocommerce"),
            "time_gmt": payload.get("time_gmt"),
        }

    def orders_page(self, page=1, per_page=200):
        """Read a single authenticated order page without customer billing PII.

        The detailed billing data is deliberately fetched by `order_detail` only
        when a staff member opens a specific order in Eda.
        """
        if page < 1 or not 1 <= per_page <= 250:
            raise FreshCoreAccountingError("Neplatné stránkování objednávek.")
        query = urlencode({"page": page, "per_page": per_page, "include_billing": "false"})
        payload, _ = _get_json(
            self.orders_endpoint + ("&" if "?" in self.orders_endpoint else "?") + query,
            headers=self._headers(), timeout=35,
        )
        if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("orders"), list):
            raise FreshCoreAccountingError("Accounting API nevrátilo platný seznam objednávek.")
        if payload.get("available") is False:
            raise FreshCoreAccountingError("WooCommerce není v Accounting API dostupné.")
        return payload

    def order_detail(self, external_id):
        """Fetch actual order details. The canonical URL is derived from the working
        orders endpoint; old stored detail templates may be malformed/stale.
        Never send a credential to an off-site host when retrying.
        """
        from urllib.parse import urlsplit, urlunsplit
        ext = str(external_id or "").strip()
        if not ext.isdecimal():
            raise FreshCoreAccountingError("Neplatné ID objednávky pro účetní API.")
        if not self.orders_endpoint:
            raise FreshCoreAccountingError("Chybí URL seznamu objednávek v Accounting API.")
        base = urlsplit(self.orders_endpoint)
        if base.scheme != "https" or not base.hostname or not base.path.endswith('/accounting/orders'):
            raise FreshCoreAccountingError("URL účetního API neodpovídá očekávané bezpečné cestě /accounting/orders.")
        canonical = urlunsplit((base.scheme,base.netloc,base.path.rstrip('/')+'/'+quote(ext,safe=''),'',''))
        # Legacy template only if it resolves to the same expected route and host.
        candidates = [canonical]
        template = self.detail_endpoint
        if template and '{id}' in template:
            original = template.replace('{id}',quote(ext,safe=''))
            parsed = urlsplit(original)
            if parsed.scheme == 'https' and parsed.hostname == base.hostname and parsed.path.endswith('/accounting/orders/'+ext) and original != canonical:
                candidates.insert(0,original)
        last_error = None
        for url in candidates:
            try:
                payload, _ = _get_json(url, headers=self._headers(), timeout=25)
                if not isinstance(payload, dict) or not isinstance(payload.get('order'),dict):
                    raise FreshCoreAccountingError("Accounting API nevrátilo detail objednávky v očekávaném formátu.")
                return payload['order']
            except FreshCoreAccountingError as exc:
                last_error = exc
                if 'HTTP 404' not in str(exc) or url == candidates[-1]:
                    break
        # Include only a known API route, never its credentials.
        raise FreshCoreAccountingError(f"Detail objednávky #{ext} není dostupný na účetním endpointu ({base.path}/ID): {last_error}. Ověř Fresh Core a URL REST API.")


def serialize_private_order(order_data):
    return json.dumps(order_data, ensure_ascii=False, default=str)
