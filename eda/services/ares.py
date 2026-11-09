from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

ARES_BASE_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"


class AresError(RuntimeError):
    pass


def normalize_ico(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(8)


def valid_ico(ico: str) -> bool:
    ico = normalize_ico(ico)
    if len(ico) != 8 or not ico.isdigit():
        return False
    # Czech IČO checksum: weighted first 7 digits, control digit in 8th position.
    total = sum(int(ico[i]) * (8 - i) for i in range(7))
    remainder = total % 11
    control = (11 - remainder) % 10
    return control == int(ico[7])


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass
class AresCompany:
    ico: str
    name: str
    dic: str | None
    address: str | None
    legal_form: str | None
    legal_form_code: str | None
    nace: str | None
    founded_on: date | None
    raw: dict

    def as_dict(self) -> dict:
        return {
            "ico": self.ico,
            "name": self.name,
            "dic": self.dic,
            "address": self.address,
            "legal_form": self.legal_form,
            "legal_form_code": self.legal_form_code,
            "nace": self.nace,
            "founded_on": self.founded_on.isoformat() if self.founded_on else None,
        }


def get_company(ico: str, timeout: int = 12) -> AresCompany | None:
    normalized = normalize_ico(ico)
    if len(normalized) != 8 or not normalized.isdigit():
        raise AresError("IČO musí mít 8 číslic.")
    if not valid_ico(normalized):
        raise AresError("IČO nemá platný kontrolní součet.")

    try:
        response = requests.get(
            f"{ARES_BASE_URL}/ekonomicke-subjekty/{normalized}",
            headers={"Accept": "application/json", "User-Agent": "Eda/0.5.1"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise AresError(f"ARES není dostupný: {exc}") from exc

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:600]
        raise AresError(f"ARES vrátil HTTP {response.status_code}: {detail}")

    try:
        data = response.json()
    except ValueError as exc:
        raise AresError("ARES vrátil neplatnou JSON odpověď.") from exc

    sidlo = data.get("sidlo") or {}
    nace_values = data.get("czNace") or data.get("czNacePrevažujici") or []
    if isinstance(nace_values, list):
        nace = ", ".join(str(v) for v in nace_values[:8]) or None
    else:
        nace = str(nace_values) if nace_values else None

    legal_code = data.get("pravniForma") or data.get("pravniFormaRos")
    legal_name = data.get("pravniFormaNazev") or data.get("pravniFormaRosNazev")
    if not legal_name and legal_code:
        legal_name = str(legal_code)

    return AresCompany(
        ico=str(data.get("ico") or normalized),
        name=str(data.get("obchodniJmeno") or "").strip(),
        dic=(str(data.get("dic")).strip() if data.get("dic") else None),
        address=(str(sidlo.get("textovaAdresa")).strip() if sidlo.get("textovaAdresa") else None),
        legal_form=legal_name,
        legal_form_code=(str(legal_code) if legal_code else None),
        nace=nace,
        founded_on=_as_date(data.get("datumVzniku")),
        raw=data,
    )
