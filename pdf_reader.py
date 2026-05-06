"""
pdf_reader.py  —  PDF extraction layer for Gemini / RAG integration
====================================================================
Provides three ways to feed a tender PDF to an LLM:

  1. extract_text(source)          → clean full text (best for RAG)
  2. extract_structured(source)    → dict with metadata, sections,
                                     tables, qualification criteria,
                                     and pricing blocks pre-identified
  3. pages_as_images(source, ...)  → list of base64 PNG strings so you
                                     can send pages directly to a vision
                                     model (Gemini 1.5 / 2.0 Pro)

`source` can be:
  - a local file path  ("/path/to/tender.pdf")
  - a raw bytes object (b"…")
  - a base64-encoded string (the payload your server.py already returns)
  - an http/https URL   ("http://localhost:3000/api/tenders/1/pdf")

Usage
-----
  from pdf_reader import TenderPDFReader

  reader = TenderPDFReader("http://localhost:3000/api/tenders/1/pdf")

  # ── Option A: plain text for RAG chunking ──
  text = reader.extract_text()

  # ── Option B: structured dict for targeted extraction ──
  data = reader.extract_structured()
  print(data["qualification_criteria"])
  print(data["pricing_blocks"])

  # ── Option C: page images for vision model ──
  images = reader.pages_as_images(dpi=150)   # list of base64 PNG strings
  # send images[0] to Gemini as an inline_data part
"""

from __future__ import annotations

import base64
import io
import re
import tempfile
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ── Third-party ──────────────────────────────────────────────────────────────
try:
    import httpx                        # async-friendly HTTP (already in your project)
except ImportError:
    import urllib.request as _urllib
    httpx = None                        # fallback to stdlib

import pdfplumber                       # layout-aware text + table extraction
import fitz                             # PyMuPDF — fast rendering + metadata
from pypdf import PdfReader as _PypdfReader  # form fields + metadata fallback

# ── Optional OCR (for scanned / image-only PDFs) ─────────────────────────
try:
    import pytesseract                  # pip install pytesseract
    from PIL import Image               # pip install pillow
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False

# Pages with fewer than this many chars are treated as image-only/scanned
_MIN_CHARS_FOR_TEXT = 50


# ─────────────────────────────────────────────────────────────────────────────
# Keyword lists used for section detection
# Feel free to extend these for your specific tender corpus.
# ─────────────────────────────────────────────────────────────────────────────

_QUALIFICATION_KEYWORDS = [
    "eligibility", "qualification", "criteria", "turnover", "experience",
    "net worth", "registration", "certificate", "license", "empanelment",
    "technical bid", "financial bid", "prequalification", "pre-qualification",
    "minimum requirement", "essential requirement", "class of contractor",
]

_PRICING_KEYWORDS = [
    "price", "rate", "cost", "amount", "bid amount", "quoted amount",
    "schedule of rates", "sor", "boe", "bill of quantities", "boq",
    "estimated cost", "tender value", "financial", "fee", "charges",
    "per unit", "lump sum", "earnest money", "emd", "security deposit",
    "performance security", "performance guarantee",
]


# ─────────────────────────────────────────────────────────────────────────────
# TenderPDFReader
# ─────────────────────────────────────────────────────────────────────────────

class TenderPDFReader:
    """
    Unified PDF reader for tender documents.

    Parameters
    ----------
    source : str | bytes
        File path, URL, raw bytes, or base64-encoded PDF string.
    """

    def __init__(self, source: str | bytes):
        self._source = source
        self._bytes: bytes | None = None          # lazily loaded raw PDF bytes
        self._tmp_path: str | None = None         # temp file path for pdfplumber

    # ── Public API ────────────────────────────────────────────────────────────

    def extract_text(self, page_separator: str = "\n\n--- PAGE {n} ---\n\n") -> str:
        """
        Extract all text from the PDF as a single string.

        Each page is preceded by a page-separator so you can chunk later.
        Returns empty string if the PDF is scanned / image-only.
        """
        pages = self._extract_pages_text()
        parts = []
        for i, txt in enumerate(pages, start=1):
            if page_separator:
                parts.append(page_separator.format(n=i))
            parts.append(txt)
        return "".join(parts)

    def extract_structured(self) -> dict[str, Any]:
        """
        Return a structured dict ready to pass to an LLM prompt.

        Keys
        ----
        metadata          : dict  — title, author, pages, creation_date, etc.
        full_text         : str   — complete extracted text
        pages             : list  — per-page text list
        tables            : list  — list of {page, data} dicts (pdfplumber)
        qualification_criteria : list — paragraphs/lines that mention eligibility
        pricing_blocks    : list — paragraphs/lines that mention pricing/rates
        toc               : list — headings detected heuristically
        """
        pages_text = self._extract_pages_text()
        full_text  = "\n\n".join(pages_text)
        tables     = self._extract_tables()
        metadata   = self._extract_metadata()

        return {
            "metadata":               metadata,
            "full_text":              full_text,
            "pages":                  pages_text,
            "tables":                 tables,
            "qualification_criteria": _find_relevant_blocks(full_text, _QUALIFICATION_KEYWORDS),
            "pricing_blocks":         _find_relevant_blocks(full_text, _PRICING_KEYWORDS),
            "toc":                    _detect_headings(full_text),
        }

    def pages_as_images(
        self,
        dpi: int = 150,
        page_numbers: list[int] | None = None,
        fmt: str = "png",
    ) -> list[dict[str, str]]:
        """
        Rasterise pages and return them as base64-encoded image strings.

        Parameters
        ----------
        dpi          : resolution — 150 is good for LLM vision, 72 for thumbnails
        page_numbers : 1-based list of pages to render; None = all pages
        fmt          : "png" or "jpeg"

        Returns
        -------
        List of dicts:  {"page": N, "mime_type": "image/png", "data": "<base64>"}
        These are ready to use as Gemini `inline_data` parts.
        """
        pdf_bytes = self._load_bytes()
        doc       = fitz.open(stream=pdf_bytes, filetype="pdf")
        results   = []

        pages_to_render = (
            [p - 1 for p in page_numbers]      # convert 1-based → 0-based
            if page_numbers
            else range(len(doc))
        )

        mat = fitz.Matrix(dpi / 72, dpi / 72)  # scale factor from 72-DPI base
        img_fmt = "png" if fmt.lower() != "jpeg" else "jpeg"

        for idx in pages_to_render:
            if idx < 0 or idx >= len(doc):
                continue
            page = doc[idx]
            pix  = page.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes(img_fmt)
            results.append({
                "page":      idx + 1,
                "mime_type": f"image/{img_fmt}",
                "data":      base64.b64encode(img_bytes).decode("ascii"),
            })

        doc.close()
        return results

    def page_count(self) -> int:
        """Return total number of pages."""
        pdf_bytes = self._load_bytes()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        n   = len(doc)
        doc.close()
        return n

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load_bytes(self) -> bytes:
        """Load raw PDF bytes from whatever source was provided."""
        if self._bytes is not None:
            return self._bytes

        src = self._source

        # Already bytes
        if isinstance(src, (bytes, bytearray)):
            self._bytes = bytes(src)
            return self._bytes

        # base64 string (what server.py returns)
        if isinstance(src, str) and not src.startswith(("http://", "https://", "/", ".")):
            try:
                self._bytes = base64.b64decode(src)
                return self._bytes
            except Exception:
                pass  # not base64 — fall through

        # URL
        if isinstance(src, str) and urlparse(src).scheme in ("http", "https"):
            self._bytes = _fetch_url(src)
            return self._bytes

        # Local file path
        if isinstance(src, str):
            self._bytes = Path(src).read_bytes()
            return self._bytes

        raise ValueError(f"Cannot load PDF from source of type {type(src)}")

    def _tmp_file(self) -> str:
        """Write bytes to a temp file and return path (pdfplumber needs a path)."""
        if self._tmp_path and os.path.exists(self._tmp_path):
            return self._tmp_path
        pdf_bytes = self._load_bytes()
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(pdf_bytes)
        tmp.flush()
        tmp.close()
        self._tmp_path = tmp.name
        return self._tmp_path

    def _extract_pages_text(self) -> list[str]:
        """
        Extract per-page text using pdfplumber.

        If a page yields fewer than _MIN_CHARS_FOR_TEXT characters (scanned /
        image-only page), it is automatically re-processed with Tesseract OCR
        so that scanned tender PDFs are handled transparently.
        """
        path = self._tmp_file()
        pages_text = []
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    txt = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                    pages_text.append(txt.strip())
        except Exception:
            pages_text = self._extract_pages_text_fitz()

        # ── OCR fallback for scanned pages ────────────────────────────────
        # Check whether the majority of pages are text-empty (scanned PDF)
        empty_count = sum(1 for t in pages_text if len(t) < _MIN_CHARS_FOR_TEXT)
        if empty_count > len(pages_text) // 2:
            # More than half the pages have no text — run full OCR
            pages_text = self._extract_pages_text_ocr()
        elif empty_count > 0:
            # Mixed PDF: OCR only the empty pages, keep text for the rest
            ocr_pages = self._extract_pages_text_ocr()
            pages_text = [
                ocr_pages[i] if len(t) < _MIN_CHARS_FOR_TEXT else t
                for i, t in enumerate(pages_text)
            ]

        return pages_text

    def _extract_pages_text_fitz(self) -> list[str]:
        """Fallback text extraction via PyMuPDF."""
        pdf_bytes = self._load_bytes()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = [page.get_text("text").strip() for page in doc]
        doc.close()
        return pages

    def _extract_pages_text_ocr(self, dpi: int = 150) -> list[str]:
        """
        OCR all pages using a single Tesseract subprocess call on a
        multi-page TIFF.

        Calling Tesseract once per page is slow because the language model
        reloads each time (~15-20 s/page).  Instead we:
          1. Render every page to grayscale via PyMuPDF at `dpi`.
          2. Save them all into one multi-page LZW-TIFF.
          3. Run  tesseract <tiff> <out> txt  once.
          4. Split the output on the form-feed \\f Tesseract inserts between pages.

        Returns one text string per page (empty string on failure).
        """
        import subprocess
        import tempfile

        if not _OCR_AVAILABLE:
            return [""] * self.page_count()

        pdf_bytes = self._load_bytes()
        doc       = fitz.open(stream=pdf_bytes, filetype="pdf")
        mat       = fitz.Matrix(dpi / 72, dpi / 72)
        n_pages   = len(doc)

        imgs: list = []
        for page in doc:
            pix = page.get_pixmap(matrix=mat, alpha=False)
            imgs.append(
                Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                     .convert("L")          # grayscale — smaller & faster
            )
        doc.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            tiff_path = os.path.join(tmpdir, "pages.tiff")
            out_base  = os.path.join(tmpdir, "ocr_out")

            imgs[0].save(
                tiff_path, save_all=True,
                append_images=imgs[1:], compression="tiff_lzw",
            )

            proc = subprocess.run(
                ["tesseract", tiff_path, out_base, "-l", "eng", "txt"],
                capture_output=True, text=True, timeout=300,
            )
            out_txt = out_base + ".txt"
            if proc.returncode != 0 or not os.path.exists(out_txt):
                return [""] * n_pages

            raw = Path(out_txt).read_text(encoding="utf-8", errors="replace")

        # Tesseract separates pages with a form-feed character
        page_texts = [p.strip() for p in raw.split("\f")]
        while len(page_texts) < n_pages:
            page_texts.append("")
        return page_texts[:n_pages]

    def _extract_tables(self) -> list[dict[str, Any]]:
        """Extract all tables from the PDF using pdfplumber."""
        path   = self._tmp_file()
        tables = []
        try:
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages, start=1):
                    for tbl in page.extract_tables():
                        if tbl:
                            tables.append({"page": i, "data": tbl})
        except Exception:
            pass
        return tables

    def _extract_metadata(self) -> dict[str, Any]:
        """Extract PDF metadata (title, author, dates, page count)."""
        pdf_bytes = self._load_bytes()
        meta = {"pages": 0}

        # PyMuPDF metadata
        try:
            doc  = fitz.open(stream=pdf_bytes, filetype="pdf")
            info = doc.metadata or {}
            meta.update({
                "pages":         len(doc),
                "title":         info.get("title", ""),
                "author":        info.get("author", ""),
                "subject":       info.get("subject", ""),
                "creator":       info.get("creator", ""),
                "creation_date": info.get("creationDate", ""),
                "mod_date":      info.get("modDate", ""),
            })
            doc.close()
        except Exception:
            pass

        return meta

    def __del__(self):
        """Clean up temp file on garbage collection."""
        if self._tmp_path and os.path.exists(self._tmp_path):
            try:
                os.unlink(self._tmp_path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Text analysis helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_relevant_blocks(text: str, keywords: list[str]) -> list[str]:
    """
    Return paragraphs / line-groups that contain any of the given keywords.
    A "block" is a run of non-empty lines separated by blank lines,
    or any single line that directly matches.
    """
    blocks   = re.split(r"\n{2,}", text)
    pattern  = re.compile(
        "|".join(re.escape(k) for k in keywords),
        re.IGNORECASE,
    )
    matched  = []
    for block in blocks:
        if pattern.search(block):
            clean = block.strip()
            if clean and clean not in matched:
                matched.append(clean)
    return matched


def _detect_headings(text: str) -> list[str]:
    """
    Heuristic heading detection:
    - Short lines (≤ 80 chars) that are ALL CAPS or Title Case
    - Lines that start with a numbering pattern  (1. / 1.1 / CLAUSE 3 etc.)
    """
    heading_re = re.compile(
        r"^(?:"
        r"\d+(\.\d+)*[\s\.\)]+\s*.+"        # 1. / 1.1 / 2)
        r"|CLAUSE\s+\d+"                    # CLAUSE 3
        r"|SECTION\s+\d+"                   # SECTION IV
        r"|PART\s+[A-Z\d]+"                 # PART B
        r"|ANNEXURE\s+[A-Z\d]+"             # ANNEXURE I
        r")",
        re.IGNORECASE,
    )
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 100:
            continue
        is_allcaps    = stripped.isupper() and len(stripped) > 3
        is_titlecase  = stripped.istitle() and len(stripped.split()) >= 2
        is_numbered   = bool(heading_re.match(stripped))
        if is_allcaps or is_titlecase or is_numbered:
            if stripped not in headings:
                headings.append(stripped)
    return headings


# ─────────────────────────────────────────────────────────────────────────────
# URL fetcher
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_url(url: str) -> bytes:
    """Fetch raw bytes from a URL (sync). Uses httpx if available."""
    if httpx is not None:
        response = httpx.get(url, follow_redirects=True, timeout=30)
        response.raise_for_status()
        return response.content
    else:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()


# ─────────────────────────────────────────────────────────────────────────────
# Convenience top-level functions (optional — mirrors the class API)
# ─────────────────────────────────────────────────────────────────────────────

def read_pdf_text(source: str | bytes) -> str:
    """Shortcut: TenderPDFReader(source).extract_text()"""
    return TenderPDFReader(source).extract_text()


def read_pdf_structured(source: str | bytes) -> dict[str, Any]:
    """Shortcut: TenderPDFReader(source).extract_structured()"""
    return TenderPDFReader(source).extract_structured()


def pdf_pages_as_images(
    source: str | bytes,
    dpi: int = 150,
    page_numbers: list[int] | None = None,
) -> list[dict[str, str]]:
    """Shortcut: TenderPDFReader(source).pages_as_images(...)"""
    return TenderPDFReader(source).pages_as_images(dpi=dpi, page_numbers=page_numbers)


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test  (python pdf_reader.py <path-or-url>)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python pdf_reader.py <path-or-url>")
        print("  e.g. python pdf_reader.py /path/to/tender.pdf")
        print("  e.g. python pdf_reader.py http://localhost:3000/api/tenders/1/pdf")
        sys.exit(0)

    src = sys.argv[1]
    print(f"\nReading PDF from: {src}\n{'='*60}")

    reader = TenderPDFReader(src)

    print(f"Pages: {reader.page_count()}")

    structured = reader.extract_structured()

    print(f"\n── Metadata ──")
    print(json.dumps(structured["metadata"], indent=2, default=str))

    print(f"\n── Table of Contents (detected headings) ──")
    for h in structured["toc"][:20]:
        print(f"  {h}")

    print(f"\n── Qualification Criteria blocks ({len(structured['qualification_criteria'])} found) ──")
    for b in structured["qualification_criteria"][:3]:
        print(f"\n  {b[:300]}{'…' if len(b)>300 else ''}")

    print(f"\n── Pricing Blocks ({len(structured['pricing_blocks'])} found) ──")
    for b in structured["pricing_blocks"][:3]:
        print(f"\n  {b[:300]}{'…' if len(b)>300 else ''}")

    print(f"\n── Tables ({len(structured['tables'])} found) ──")
    for t in structured["tables"][:2]:
        print(f"  Page {t['page']}: {len(t['data'])} rows × {len(t['data'][0]) if t['data'] else 0} cols")

    print(f"\n── Full text preview (first 500 chars) ──")
    print(structured["full_text"][:500])