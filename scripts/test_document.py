#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eda.services.document_reader import read_document


def main():
    if len(sys.argv) != 2:
        print("Použití: python scripts/test_document.py /cesta/k/fakture.pdf")
        raise SystemExit(2)
    path = Path(sys.argv[1]).expanduser().resolve()
    if not path.exists():
        print(f"Soubor neexistuje: {path}")
        raise SystemExit(2)
    result = read_document(path, path.suffix.lower())
    print(f"status={result.status}")
    print(f"method={result.method}")
    print(f"pages={result.page_count}")
    print(f"chars={result.chars}")
    if result.error:
        print(f"error={result.error}")
    if result.text:
        print("--- prvních 3000 znaků ---")
        print(result.text[:3000])


if __name__ == "__main__":
    main()
