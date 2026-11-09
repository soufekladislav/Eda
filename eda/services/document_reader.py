from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError


TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".md"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class DocumentReadResult:
    text: str | None
    status: str
    method: str
    page_count: int | None = None
    chars: int = 0
    error: str | None = None


def _clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_document(path: Path, extension: str, *, max_chars: int = 120_000, max_pages: int = 50) -> DocumentReadResult:
    """Best-effort local text extraction before a document reaches Fresh AI Engine.

    Text PDFs are extracted locally so a text-only Engine can still understand invoices.
    Image/scanned documents are marked as requiring vision and remain attached for an
    Engine version that supports multimodal input.
    """
    ext = (extension or path.suffix).lower()

    if ext in TEXT_EXTENSIONS:
        try:
            text = _clean_text(path.read_text(encoding="utf-8", errors="replace"))[:max_chars]
            return DocumentReadResult(
                text=text or None,
                status="text_extracted" if text else "empty",
                method="plain_text",
                chars=len(text),
            )
        except OSError as exc:
            return DocumentReadResult(None, "extract_error", "plain_text", error=str(exc)[:500])

    if ext == ".pdf":
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    return DocumentReadResult(None, "encrypted_pdf", "pypdf", page_count=len(reader.pages))

            pieces: list[str] = []
            page_count = len(reader.pages)
            for page in reader.pages[:max_pages]:
                try:
                    pieces.append(page.extract_text() or "")
                except Exception:
                    pieces.append("")
                if sum(len(p) for p in pieces) >= max_chars:
                    break
            text = _clean_text("\n\n".join(pieces))[:max_chars]
            if text:
                return DocumentReadResult(text, "text_extracted", "pypdf", page_count=page_count, chars=len(text))
            return DocumentReadResult(None, "needs_vision", "pypdf", page_count=page_count)
        except (PdfReadError, OSError, ValueError) as exc:
            return DocumentReadResult(None, "extract_error", "pypdf", error=str(exc)[:500])
        except Exception as exc:
            return DocumentReadResult(None, "extract_error", "pypdf", error=f"{type(exc).__name__}: {exc}"[:500])

    if ext in IMAGE_EXTENSIONS:
        return DocumentReadResult(None, "needs_vision", "engine_vision")

    return DocumentReadResult(None, "unsupported", "none")
