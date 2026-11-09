import base64
import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests
from flask import current_app


class AIEngineError(RuntimeError):
    pass


@dataclass
class AIEngineResult:
    reply: str
    run_id: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    raw: dict | None = None


def _first(mapping: dict, *keys, default=None):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _as_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _text_from(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                text = _first(item, "text", "content", "message", "reply", "answer")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces).strip() or None
    if isinstance(value, dict):
        direct = _first(value, "reply", "answer", "text", "content", "message")
        if isinstance(direct, str):
            return direct.strip() or None
        for key in ("result", "output", "response", "data"):
            nested = value.get(key)
            text = _text_from(nested)
            if text:
                return text
    return None


def _extract_meta(data: dict, elapsed_ms: int) -> AIEngineResult:
    reply = None
    for key in ("result", "output", "response", "answer", "message", "content", "data"):
        reply = _text_from(data.get(key))
        if reply:
            break
    if not reply:
        reply = _text_from(data)
    if not reply:
        raise AIEngineError("Fresh AI Engine vrátil odpověď bez textového výsledku.")

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    routing = data.get("routing") if isinstance(data.get("routing"), dict) else {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    billing = data.get("billing") if isinstance(data.get("billing"), dict) else {}

    input_tokens = _as_int(_first(data, "input_tokens", "prompt_tokens"))
    if input_tokens is None:
        input_tokens = _as_int(_first(usage, "input_tokens", "prompt_tokens", "tokens_in"))
    output_tokens = _as_int(_first(data, "output_tokens", "completion_tokens"))
    if output_tokens is None:
        output_tokens = _as_int(_first(usage, "output_tokens", "completion_tokens", "tokens_out"))

    cost = _as_decimal(_first(data, "cost_usd", "provider_cost_usd", "cost"))
    if cost is None:
        cost = _as_decimal(_first(usage, "cost_usd", "cost"))
    if cost is None:
        cost = _as_decimal(_first(billing, "cost_usd", "provider_cost_usd", "cost"))

    latency = _as_int(_first(data, "latency_ms", "duration_ms"))
    if latency is None:
        latency = _as_int(_first(meta, "latency_ms", "duration_ms")) or elapsed_ms

    return AIEngineResult(
        reply=reply,
        run_id=str(_first(data, "run_id", "job_id", "id") or "") or None,
        provider=str(_first(data, "provider") or _first(routing, "provider") or _first(meta, "provider") or "") or None,
        model=str(_first(data, "model") or _first(routing, "model") or _first(meta, "model") or "") or None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        latency_ms=latency,
        raw=data,
    )


def _attachment_payload(attachment, storage_root: Path, remaining_bytes: int, text_limit: int):
    path = storage_root / attachment.stored_name
    item = {
        "id": attachment.id,
        "name": attachment.original_name,
        "mime_type": attachment.mime_type or "application/octet-stream",
        "size_bytes": attachment.size_bytes,
        "processing_status": attachment.status,
    }
    if attachment.extracted_text:
        item["extracted_text"] = attachment.extracted_text[:text_limit]
        item["text_chars"] = len(item["extracted_text"])
        # A normal PDF invoice is already usable by the text Engine once its text
        # has been extracted locally. Do not waste the request budget on duplicate
        # base64 binary data in that case.
        return item, 0, False

    if path.exists() and path.is_file() and remaining_bytes > 0 and attachment.size_bytes <= remaining_bytes:
        raw = path.read_bytes()
        item["content_base64"] = base64.b64encode(raw).decode("ascii")
        item["requires_vision"] = True
        return item, len(raw), True

    item["content_omitted"] = True
    if attachment.status == "needs_vision":
        item["requires_vision"] = True
    return item, 0, False


def _document_context(items: list[dict], max_chars: int) -> str:
    """Create a plain-text document block for text-only Engine adapters."""
    if not items:
        return ""
    parts: list[str] = []
    remaining = max_chars
    for item in items:
        if remaining <= 0:
            break
        name = item.get("name") or "příloha"
        extracted = (item.get("extracted_text") or "").strip()
        if extracted:
            chunk = extracted[:remaining]
            parts.append(f"--- PŘÍLOHA: {name} ---\n{chunk}")
            remaining -= len(chunk)
        elif item.get("requires_vision"):
            parts.append(
                f"--- PŘÍLOHA: {name} ---\n"
                "Text se lokálně nepodařilo vyčíst. Příloha vyžaduje vision/multimodální zpracování."
            )
    return "\n\n".join(parts)

def run_chat(*, organization, user, conversation, user_message, history, attachments, storage_root: Path, organization_state: dict | None = None) -> AIEngineResult:
    base_url = (current_app.config.get("AI_ENGINE_URL") or "").rstrip("/")
    api_key = current_app.config.get("AI_ENGINE_API_KEY") or ""
    if not base_url or not api_key:
        raise AIEngineError("Fresh AI Engine není nakonfigurován. Doplň AI_ENGINE_URL a AI_ENGINE_API_KEY do .env.")

    max_attachment_bytes = int(current_app.config.get("AI_ENGINE_ATTACHMENT_MAX_MB", 8)) * 1024 * 1024
    max_attachment_text = int(current_app.config.get("AI_ENGINE_ATTACHMENT_TEXT_MAX_CHARS", 60000))
    remaining = max_attachment_bytes
    attachment_items = []
    included_ids = []
    for attachment in attachments:
        item, used, included = _attachment_payload(attachment, storage_root, remaining, max_attachment_text)
        attachment_items.append(item)
        remaining -= used
        if included:
            included_ids.append(attachment.id)

    history_items = []
    for message in history[-20:]:
        if message.id == user_message.id:
            continue
        if message.role not in ("user", "assistant"):
            continue
        history_items.append({"role": message.role, "content": message.content})

    state = organization_state or {}
    system_prompt = (
        "Jsi Eda, finanční a provozní pracovník Fresh Crackers. Odpovídej česky, stručně a prakticky. "
        "Mluv jako profesionální, vstřícný a věcný kolega: bez zbytečných omluv, bez marketingových frází. "
        "Začni výsledkem nebo aktuálním stavem. Jasně odděluj ověřená data, odhady a doporučené akce. "
        "Když úloha běží nebo čeká na schválení, řekni to explicitně a netvrď, že už skončila. "
        "Pro srovnání můžeš vracet přehledné Markdown tabulky; v aktuálním chatu mohou být zobrazené jako prostý text. "
        "Odkazy na interní stránky uváděj jen tehdy, když jsou bezpečně známé z kontextu, a nikdy je nevymýšlej. "
        "Nejsi jen chat: aktivně provázíš uživatele nastavením firmy a upozorňuješ na chybějící předpoklady práce. "
        "Pracuješ vždy pouze s aktivní firmou uvedenou v kontextu. Nikdy netvrď, že jsi provedl zápis, "
        "odeslal doklad nebo změnil data, pokud ti aplikace neposkytla výsledek provedené akce. "
        "U účetních a finančních zápisů nejprve navrhni postup; zápis do ABRA Flexi musí projít schválením uživatele. "
        "Pokud jsou přiloženy účtenky, faktury nebo jiné dokumenty, vyčti z nich maximum relevantních údajů a navrhni "
        "zařazení k projektu, kategorii a další krok. Když něco chybí, polož jen nezbytnou doplňující otázku. "
        "Pokud firma nemá žádný projekt a uživatel chce zařazovat náklady, účtenky nebo faktury, výslovně doporuč nejdřív "
        "založit projekt a vysvětli jednou větou proč. Pokud údaje firmy nejsou ověřené v ARES, můžeš doporučit jejich ověření. "
        "Pokud organization_state obsahuje accounting_standards, používej je jako interní pracovní znalostní bázi Edy. Nevydávej interní standard za závazné právní stanovisko a při nejasnosti doporuč ověření oficiálního zdroje. "
        "Pokud organization_state obsahuje upcoming_reminders, upozorni přirozeně na nejbližší důležitý termín, zejména když je do 3 dnů nebo po splatnosti. "
        "Pokud jsou v organization_state unassigned_documents, můžeš připomenout, že čekají na přiřazení k projektu a přípravu k zaúčtování. "
        "Nevytvářej zbytečné překážky: pokud projekt pro daný úkol není nutný, pokračuj v práci."
    )

    base_prompt = user_message.content or "Zpracuj přiložené soubory."
    docs = _document_context(attachment_items, max_attachment_text)
    effective_prompt = base_prompt
    if docs:
        effective_prompt = (
            f"{base_prompt}\n\n"
            "Následuje obsah nebo stav příloh připravený aplikací Eda. "
            "Pracuj s ním jako s obsahem přiložených dokumentů.\n\n"
            f"{docs}"
        )

    input_data = {
        "system_prompt": system_prompt,
        "application": "Eda",
        "organization": {
            "id": organization.id,
            "name": organization.name,
            "ico": organization.ico,
            "currency": organization.currency,
        },
        "organization_state": state,
        "conversation_id": conversation.id,
        "messages": history_items + [{"role": "user", "content": effective_prompt}],
        "attachments": attachment_items,
        "document_processing": {
            "local_text_extraction": True,
            "vision_required": any(bool(item.get("requires_vision")) for item in attachment_items),
        },
        "capabilities": {
            "fresh_core": True,
            "flexibee": True,
            "worker_jobs": True,
            "write_actions_require_approval": True,
        },
    }

    payload = {
        "source": current_app.config.get("AI_ENGINE_APP_ID") or "eda",
        "project_id": f"eda:org:{organization.id}",
        "user_id": str(user.id),
        "task_type": "chat",
        "operation": "chat.conversation",
        "quality": current_app.config.get("AI_ENGINE_QUALITY") or "balanced",
        "prompt": effective_prompt,
        # Engine 0.5.x uses input_data; context is kept for backwards compatibility
        # but without duplicating base64 attachment bodies.
        "input_data": input_data,
        "context": {
            **{k: v for k, v in input_data.items() if k != "attachments"},
            "attachments": [
                {k: v for k, v in item.items() if k != "content_base64"}
                for item in attachment_items
            ],
        },
        "max_cost_usd": float(current_app.config.get("AI_ENGINE_MAX_COST_USD") or 0.20),
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Eda/0.5.1",
    }
    endpoint = f"{base_url}/v1/engine/run"
    timeout = int(current_app.config.get("AI_ENGINE_TIMEOUT_SECONDS") or 90)
    started = time.perf_counter()
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise AIEngineError(f"Fresh AI Engine není dostupný: {exc}") from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    try:
        data = response.json()
    except ValueError:
        data = {"raw_text": response.text[:4000]}

    if response.status_code >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = str(_first(data, "detail", "error", "message") or data)[:1500]
        else:
            detail = str(data)[:1500]
        raise AIEngineError(f"Fresh AI Engine vrátil HTTP {response.status_code}: {detail}")

    result = _extract_meta(data if isinstance(data, dict) else {"result": data}, elapsed_ms)
    result.raw = data if isinstance(data, dict) else {"result": data}
    result.raw["_eda_attachment_ids_included"] = included_ids
    return result


def engine_health():
    base_url = (current_app.config.get("AI_ENGINE_URL") or "").rstrip("/")
    api_key = current_app.config.get("AI_ENGINE_API_KEY") or ""
    if not base_url:
        return {"configured": False, "ok": False, "error": "AI_ENGINE_URL missing"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    started = time.perf_counter()
    try:
        response = requests.get(f"{base_url}/v1/health", headers=headers, timeout=10)
        elapsed = int((time.perf_counter() - started) * 1000)
        return {"configured": bool(api_key), "ok": response.status_code < 400, "http_status": response.status_code, "latency_ms": elapsed}
    except requests.RequestException as exc:
        return {"configured": bool(api_key), "ok": False, "error": str(exc)[:500]}


def run_text_operation(*, prompt: str, operation: str = "eda.analysis", input_data: dict | None = None, project_id: str = "eda:system", user_id: str = "system") -> AIEngineResult:
    """Run a compact non-chat task through Fresh AI Engine using the same stable endpoint."""
    base_url = (current_app.config.get("AI_ENGINE_URL") or "").rstrip("/")
    api_key = current_app.config.get("AI_ENGINE_API_KEY") or ""
    if not base_url or not api_key:
        raise AIEngineError("Fresh AI Engine není nakonfigurován.")
    prepared = input_data or {"prompt": prompt}
    context_data = dict(prepared)
    if isinstance(context_data.get("attachment"), dict):
        context_data["attachment"] = {k: v for k, v in context_data["attachment"].items() if k != "content_base64"}
    if isinstance(context_data.get("attachments"), list):
        context_data["attachments"] = [{k: v for k, v in item.items() if k != "content_base64"} if isinstance(item, dict) else item for item in context_data["attachments"]]
    payload = {
        "source": current_app.config.get("AI_ENGINE_APP_ID") or "eda",
        "project_id": project_id,
        "user_id": str(user_id),
        "task_type": "chat",
        "operation": operation,
        "quality": current_app.config.get("AI_ENGINE_QUALITY") or "balanced",
        "prompt": prompt,
        "input_data": prepared,
        "context": context_data,
        "max_cost_usd": float(current_app.config.get("AI_ENGINE_MAX_COST_USD") or 0.20),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Eda/0.5.1",
    }
    started = time.perf_counter()
    try:
        response = requests.post(
            f"{base_url}/v1/engine/run",
            headers=headers,
            json=payload,
            timeout=int(current_app.config.get("AI_ENGINE_TIMEOUT_SECONDS") or 90),
        )
    except requests.RequestException as exc:
        raise AIEngineError(f"Fresh AI Engine není dostupný: {exc}") from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    try:
        data = response.json()
    except ValueError:
        data = {"raw_text": response.text[:4000]}
    if response.status_code >= 400:
        detail = str(_first(data, "detail", "error", "message") or data)[:1500] if isinstance(data, dict) else str(data)[:1500]
        raise AIEngineError(f"Fresh AI Engine vrátil HTTP {response.status_code}: {detail}")
    return _extract_meta(data if isinstance(data, dict) else {"result": data}, elapsed_ms)


def _json_from_reply(reply: str):
    text = (reply or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
    return {"summary": reply, "parse_error": True}


def analyze_inbox_document(*, organization, document, file_path: Path, projects: list, user_id: int):
    """Extract/forward a document and ask Engine for a structured accounting proposal."""
    ext = file_path.suffix.lower()
    extracted = (document.extracted_text or "").strip()
    attachment = {
        "id": document.id,
        "name": document.original_name,
        "mime_type": document.mime_type or "application/octet-stream",
        "size_bytes": document.size_bytes,
    }
    if extracted:
        attachment["extracted_text"] = extracted[: int(current_app.config.get("AI_ENGINE_ATTACHMENT_TEXT_MAX_CHARS", 60000))]
    elif file_path.exists() and file_path.stat().st_size <= int(current_app.config.get("AI_ENGINE_ATTACHMENT_MAX_MB", 8)) * 1024 * 1024:
        attachment["content_base64"] = base64.b64encode(file_path.read_bytes()).decode("ascii")
        attachment["requires_vision"] = True

    project_list = [{"id": p.id, "name": p.name, "code": p.code, "type": p.project_type} for p in projects]
    prompt = (
        "Jsi Eda, finanční pracovník. Analyzuj přiložený účetní doklad a vrať POUZE validní JSON bez markdownu. "
        "Nevymýšlej chybějící údaje. Pro částky používej čísla, datum YYYY-MM-DD. "
        "Pokud lze doklad přiřadit k některému z projektů jen z kontextu, navrhni project_id, jinak null. "
        "Schéma: {\"document_type\":\"receipt|received_invoice|other\",\"supplier_name\":null,\"supplier_ico\":null,"
        "\"document_number\":null,\"variable_symbol\":null,\"issued_on\":null,\"due_on\":null,\"currency\":\"CZK\","
        "\"total_without_vat\":null,\"vat\":null,\"total\":null,\"category\":null,\"project_id\":null,"
        "\"project_reason\":null,\"accounting_note\":null,\"confidence\":0.0,\"warnings\":[]}."
    )
    input_data = {
        "system_prompt": prompt,
        "organization": {"id": organization.id, "name": organization.name, "ico": organization.ico},
        "projects": project_list,
        "attachment": attachment,
        "document_text": extracted[:60000] if extracted else None,
    }
    result = run_text_operation(
        prompt=prompt + "\n\nDoklad: " + (extracted[:60000] if extracted else "viz binární příloha"),
        operation="document.analyze",
        input_data=input_data,
        project_id=f"eda:org:{organization.id}:document:{document.id}",
        user_id=str(user_id),
    )
    return _json_from_reply(result.reply), result
