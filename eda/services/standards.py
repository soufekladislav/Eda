import io
import json
import re
from html.parser import HTMLParser
from datetime import datetime
import requests
from pypdf import PdfReader
from .ai_engine import run_text_operation, AIEngineError


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        data = re.sub(r"\s+", " ", data or "").strip()
        if data:
            self.parts.append(data)


def fetch_source_text(url: str, max_chars: int = 80000):
    if not url:
        raise RuntimeError("Standard nemá nastavený zdrojový odkaz.")
    r = requests.get(url, timeout=30, headers={"User-Agent": "Eda/0.5.1 standards-review"})
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        reader = PdfReader(io.BytesIO(r.content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    else:
        parser = _TextExtractor()
        parser.feed(r.text)
        text = "\n".join(parser.parts)
    return text[:max_chars]


def review_standard(standard, admin_user_id: int):
    source_text = fetch_source_text(standard.source_url)
    prompt = (
        "Jsi Eda a provádíš revizi interní účetní znalostní báze. Porovnej interní standard se zdrojovým textem. "
        "Nejde o právní stanovisko. Vrať stručné JSON: {\"status\":\"ok|needs_review\",\"summary\":\"...\","
        "\"changes\":[\"...\"],\"recommended_update\":\"...\",\"source_limitations\":[\"...\"]}. "
        "Nevymýšlej informace, které ve zdroji nejsou.\n\n"
        f"INTERNÍ STANDARD:\n{standard.content or ''}\n\nZDROJ:\n{source_text}"
    )
    result = run_text_operation(
        prompt=prompt,
        operation="accounting_standard.review",
        input_data={
            "standard": {"id": standard.id, "code": standard.code, "title": standard.title, "content": standard.content},
            "source_url": standard.source_url,
            "source_text": source_text,
        },
        project_id=f"eda:standards:{standard.id}",
        user_id=str(admin_user_id),
    )
    raw = (result.reply or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    try:
        data = json.loads(raw)
    except Exception:
        data = {"status": "needs_review", "summary": result.reply, "changes": [], "recommended_update": None}
    return data, result
