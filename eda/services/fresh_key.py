from __future__ import annotations

from urllib.parse import urlencode
import requests
from flask import current_app


def configured() -> bool:
    return bool((current_app.config.get("AI_ENGINE_URL") or "").strip() and (current_app.config.get("AI_ENGINE_API_KEY") or "").strip())


def _base() -> str:
    return (current_app.config.get("AI_ENGINE_URL") or "https://aiengine.freshcrackers.cz").rstrip("/")


def _headers(user_id=None) -> dict[str, str]:
    key = (current_app.config.get("AI_ENGINE_API_KEY") or "").strip()
    h = {"Authorization": f"Bearer {key}", "X-Fresh-App": "eda", "Accept": "application/json"}
    if user_id is not None:
        h["X-Fresh-User"] = str(user_id)
    return h


def _request(method: str, path: str, *, user_id=None, payload=None, params=None) -> dict:
    if not configured():
        raise RuntimeError("Fresh Key není nakonfigurován.")
    r = requests.request(method, _base() + path, headers=_headers(user_id), json=payload, params=params, timeout=25)
    try:
        data = r.json()
    except ValueError:
        data = {"ok": False, "error": r.text[:500]}
    if r.status_code >= 400 or not data.get("ok", False):
        raise RuntimeError(str(data.get("error") or f"Fresh Key HTTP {r.status_code}"))
    return data




def bootstrap_account(user_id: int, email: str, display_name: str = "") -> dict:
    """Create/sync the free Fresh Key identity and lightweight AI Engine account shell.

    Fresh Key itself is free and this endpoint never allocates or requires credits.
    """
    return _request("POST", "/v1/fresh-key/account/bootstrap", user_id=user_id, payload={
        "app_code": "eda",
        "external_user_id": str(user_id),
        "email": str(email or "").strip().lower(),
        "display_name": str(display_name or "").strip(),
        "email_verified": True,
    })


def manage_url(link_id: str = "") -> str:
    base = _base() + "/account/fresh-key"
    return base + ("?" + urlencode({"link": str(link_id)} ) if link_id else "")

def authorize_url(state: str, return_url: str) -> str:
    return _base() + "/fresh-key/sso/authorize?" + urlencode({"app_code": "eda", "state": state, "return_url": return_url})


def sso_exchange(code: str) -> dict:
    return _request("POST", "/v1/fresh-key/sso/exchange", payload={"app_code": "eda", "code": code})


def sso_bind(binding_token: str, user_id: int) -> str:
    data = _request("POST", "/v1/fresh-key/sso/bind", payload={"app_code": "eda", "binding_token": binding_token, "external_user_id": str(user_id)})
    return str(data.get("identity_id") or "")


def register_project(user_id: int, project, website_url: str = "", role: str = "member") -> dict:
    return _request("POST", "/v1/fresh-key/local-projects/sync", user_id=user_id, payload={
        "app_code": "eda",
        "external_user_id": str(user_id),
        "external_project_id": str(project.id),
        "name": str(project.name or ""),
        "website_url": str(website_url or ""),
        "organization_name": str(project.organization.name if project.organization else ""),
        "role": role,
    }).get("local_project") or {}


def publish_project(user_id: int, project, website_url: str = "", role: str = "member") -> str:
    """Explicitly publish an existing Eda project as a one-app Fresh Key link."""
    register_project(user_id, project, website_url=website_url, role=role)
    data = _request("POST", "/v1/fresh-key/links/sync", user_id=user_id, payload={
        "app_code": "eda",
        "external_user_id": str(user_id),
        "external_project_id": str(project.id),
        "name": str(project.name or ""),
        "website_url": str(website_url or ""),
        "organization_name": str(project.organization.name if project.organization else ""),
    })
    link = data.get("link") or data.get("project") or {}
    fresh_id = str(link.get("id") or "")
    if not fresh_id:
        raise RuntimeError("Fresh Key nevrátil ID propojení.")
    return fresh_id


def project_summary(user_id: int, project, website_url: str = "", role: str = "member") -> dict:
    locals_ = _request("GET", "/v1/fresh-key/local-projects", user_id=user_id).get("local_projects") or []
    synced = next((x for x in locals_ if str(x.get("app_code") or "") == "eda" and str(x.get("external_project_id") or "") == str(project.id)), None) or {}
    link_id = str(synced.get("fresh_key_id") or "")
    if not link_id:
        return {"linked": False, "applications": [], "members": [], "can_manage": False, "fresh_key_id": "", "manage_url": manage_url()}
    links = _request("GET", "/v1/fresh-key/links", user_id=user_id).get("links") or []
    link = next((x for x in links if str(x.get("id") or "") == link_id), None) or {"id": link_id, "bindings": []}
    members = _request("GET", f"/v1/fresh-key/links/{link_id}/members", user_id=user_id).get("members") or []
    return {
        "linked": True,
        "fresh_key_id": link_id,
        "name": link.get("name") or project.name,
        "applications": [x for x in (link.get("bindings") or []) if str(x.get("app_code") or "") != "ai-engine"],
        "members": members,
        "can_manage": bool(link.get("can_manage_connections")),
        "manage_url": manage_url(link_id),
    }



def notify_project_share(user_id: int, email: str, project_name: str, role: str, action_url: str = "") -> dict:
    return _request("POST", "/v1/fresh-key/project-share/notify", user_id=user_id, payload={
        "app_code": "eda", "external_user_id": str(user_id), "email": str(email or "").strip().lower(),
        "project_name": str(project_name or "Projekt"), "role": str(role or "member"), "action_url": str(action_url or ""),
    })


def available_links(user_id: int) -> list[dict]:
    return (_request("GET", "/v1/fresh-key/links", user_id=user_id).get("links") or [])

def pending_link_requests(user_id: int) -> list[dict]:
    return (_request("GET", "/v1/fresh-key/link-requests", user_id=user_id, params={"status": "pending"}).get("requests") or [])


def accept_link_request(user_id: int, request_id: str, project, website_url: str = "", role: str = "member") -> dict:
    data = _request("POST", f"/v1/fresh-key/link-requests/{request_id}/accept", user_id=user_id, payload={
        "app_code": "eda",
        "external_user_id": str(user_id),
        "external_project_id": str(project.id),
        "name": str(project.name or ""),
        "website_url": str(website_url or ""),
        "organization_name": str(project.organization.name if project.organization else ""),
        "role": role,
    })
    return data.get("request") or {}


def decline_link_request(user_id: int, request_id: str) -> dict:
    return (_request("POST", f"/v1/fresh-key/link-requests/{request_id}/decline", user_id=user_id, payload={
        "app_code": "eda", "external_user_id": str(user_id),
    }).get("request") or {})


def add_member(user_id: int, link_id: str, email: str, role: str, *, notify: bool = True) -> dict:
    return (_request("POST", f"/v1/fresh-key/links/{link_id}/members", user_id=user_id, payload={
        "app_code": "eda", "external_user_id": str(user_id), "email": email, "role": role,
        "can_manage_connections": role == "manager", "notify": bool(notify),
    }).get("member") or {})


def update_member(user_id: int, link_id: str, identity_id: str, role: str) -> dict:
    return (_request("POST", f"/v1/fresh-key/links/{link_id}/members", user_id=user_id, payload={
        "app_code": "eda", "external_user_id": str(user_id), "action": "update", "identity_id": identity_id,
        "role": role, "can_manage_connections": role == "manager",
    }).get("member") or {})


def remove_member(user_id: int, link_id: str, identity_id: str) -> None:
    _request("POST", f"/v1/fresh-key/links/{link_id}/members", user_id=user_id, payload={
        "app_code": "eda", "external_user_id": str(user_id), "action": "remove", "identity_id": identity_id,
    })
