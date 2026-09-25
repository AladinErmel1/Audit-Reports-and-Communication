"""Turn an audit report PDF into clean, continuous text.

Audit reports repeat the same header/footer on every page (confidentiality notice,
office address, "Page 12", report number). Those lines break findings that span a
page boundary, so they are detected by frequency and removed before parsing.
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf

HEADER_LINES = 3
FOOTER_LINES = 6

_PAGE_NO = re.compile(r"^\s*P\s*a\s*g\s*e\s*[\s|I\\j\[1]*\d+\s*$", re.IGNORECASE)
_BULLET_ONLY = re.compile(r"^\s*[•●▪◦o\-–]\s*$")


def _norm(line: str) -> str:
    """Normalise a line for header/footer frequency counting."""
    return re.sub(r"[^a-z]", "", line.lower())


def extract_pages(pdf_path: str | Path) -> list[str]:
    with pymupdf.open(str(pdf_path)) as doc:
        return [page.get_text() for page in doc]


def clean_text(pages: list[str]) -> str:
    """Remove repeated page furniture and re-flow the text into paragraphs."""
    page_lines = [[ln.strip() for ln in p.splitlines() if ln.strip()] for p in pages]

    def edges(lines: list[str]) -> set[int]:
        """Indices of lines in the header/footer zone of a page."""
        n = len(lines)
        return set(range(min(HEADER_LINES, n))) | set(range(max(0, n - FOOTER_LINES), n))

    # A line that shows up in the header/footer zone of many pages is furniture.
    counts: Counter[str] = Counter()
    for lines in page_lines:
        counts.update({_norm(lines[i]) for i in edges(lines) if _norm(lines[i])})
    threshold = max(3, int(len(pages) * 0.4))
    furniture = [k for k, c in counts.items() if c >= threshold and len(k) > 3]

    def is_furniture(line: str) -> bool:
        n = _norm(line)
        if not n:
            return False
        # OCR'd reports spell the same footer slightly differently on each page.
        return any(n == f or (len(n) > 20 and SequenceMatcher(None, n, f).ratio() > 0.85)
                   for f in furniture)

    out: list[str] = []
    for lines in page_lines:
        zone = edges(lines)
        for i, ln in enumerate(lines):
            if i in zone and (is_furniture(ln) or _PAGE_NO.match(ln)):
                continue
            out.append(ln)

    # Re-join bullet markers that the PDF put on their own line.
    merged: list[str] = []
    pending_bullet = False
    for ln in out:
        if _BULLET_ONLY.match(ln):
            pending_bullet = True
            continue
        if pending_bullet:
            merged.append("• " + ln)
            pending_bullet = False
        else:
            merged.append(ln)
    return "\n".join(merged)


def extract_text(pdf_path: str | Path) -> str:
    return clean_text(extract_pages(pdf_path))
