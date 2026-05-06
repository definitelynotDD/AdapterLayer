"""
server.py  —  FastAPI backend for API Explorer (Python-powered version)

Modes:
  1. Spec mode  — point at an OpenAPI 3.x / Swagger 2.x / GraphQL spec URL or file
  2. Direct mode — point at a plain data URL (e.g. data.gov.in resource endpoints)
  3. Bidassist mode — POST with JSON body to partner-api.bidassist.in
  4. PDF AI Terminal — chat with any PDF via Claude (requires Gemini_API_KEY in .env)

Start via:  python app.py
Direct:     uvicorn server:app --port 8000
"""
import asyncio
import base64
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ── Load .env ─────────────────────────────────────────────────────────────
_ENV_PATH = Path(__file__).parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())


# ── Request schemas ───────────────────────────────────────────────────────

class RunRequest(BaseModel):
    source: str
    auth_scheme: str = "none"
    token: str = ""
    header_name: str = "X-API-Key"
    query_param_name: str = "api-key"
    username: str = ""
    password: str = ""
    concurrency: int = 10


class DirectRequest(BaseModel):
    url: str
    auth_scheme: str = "none"
    token: str = ""
    header_name: str = "X-API-Key"
    query_param_name: str = "api-key"
    username: str = ""
    password: str = ""
    extra_params: dict = {}
    method: str = "GET"
    json_body: Optional[dict] = None


class ChatPDFRequest(BaseModel):
    """Request schema for the PDF AI Terminal."""
    pdf_source: str          # URL, file path, or base64-encoded PDF
    question: str            # User's question
    history: list = []       # Prior conversation turns [{role, content}]
    source_label: str = ""   # Human-readable label for the PDF
    model: str = "gemini-2.5-flash"  # Gemini model to use


# ── Data types ────────────────────────────────────────────────────────────

@dataclass
class Endpoint:
    path: str
    method: str = "GET"
    params: dict = field(default_factory=dict)
    description: str = ""


# ── Auth helpers ──────────────────────────────────────────────────────────

def _build_headers(auth_scheme, token, header_name, username, password):
    if auth_scheme == "bearer" and token:
        return {"Authorization": f"Bearer {token}"}
    if auth_scheme == "apikey-header" and token:
        name = header_name or "X-API-Key"
        return {name: token}
    if auth_scheme == "x-api-key" and token:
        return {"x-api-key": token}
    if auth_scheme == "basic" and username:
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}
    return {}


def _build_query_params(auth_scheme, token, query_param_name, extra={}):
    params = dict(extra)
    if auth_scheme == "query-param" and token:
        params[query_param_name or "api-key"] = token
    return params


# ── Rate limiter ──────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, rate: float = 5.0):
        self._rate = rate
        self._tokens = rate
        self._last = None
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if self._last is None:
                self._last = now
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


_rate_limiter = RateLimiter(rate=5.0)


# ── Spec detector ─────────────────────────────────────────────────────────

GRAPHQL_INTROSPECTION = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    types { name kind fields { name description } }
  }
}
"""


class SpecDetector:
    async def detect(self, source: str, headers: dict) -> tuple[str, dict]:
        is_url = urlparse(source).scheme in ("http", "https")
        if is_url:
            gql = await self._try_graphql(source, headers)
            if gql:
                return "graphql", gql
            async with httpx.AsyncClient(timeout=20, headers=headers,
                                         follow_redirects=True) as client:
                r = await client.get(source)
                r.raise_for_status()
                spec = self._parse(r.text)
        else:
            spec = self._parse(Path(source).read_text())
        return self._classify(spec), spec

    def _parse(self, text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return yaml.safe_load(text) or {}

    def _classify(self, spec):
        if "openapi" in spec:
            return "openapi3"
        if "swagger" in spec:
            return "swagger2"
        raise ValueError(
            "Not a valid OpenAPI/Swagger spec. Use 'Direct Fetch' tab for plain data URLs."
        )

    async def _try_graphql(self, url, headers):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url,
                    json={"query": GRAPHQL_INTROSPECTION},
                    headers={**headers, "Content-Type": "application/json"})
                data = r.json()
                if "data" in data and "__schema" in data["data"]:
                    return data["data"]
        except Exception:
            pass
        return None


# ── Enumerators ───────────────────────────────────────────────────────────

class OpenAPIEnumerator:
    def enumerate(self, spec, spec_type):
        if spec_type == "openapi3":
            servers = spec.get("servers", [])
            base_url = servers[0].get("url", "") if servers else ""
        else:
            host = spec.get("host", "")
            base_path = spec.get("basePath", "/")
            scheme = (spec.get("schemes") or ["https"])[0]
            base_url = f"{scheme}://{host}{base_path}"

        endpoints = []
        for path, methods in spec.get("paths", {}).items():
            if not isinstance(methods, dict):
                continue
            for method, details in methods.items():
                if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                    continue
                desc = ""
                if isinstance(details, dict):
                    desc = details.get("summary") or details.get("description") or ""
                endpoints.append(Endpoint(path=path, method=method.upper(), description=desc))
        return base_url, endpoints


class GraphQLEnumerator:
    def enumerate(self, schema, source_url):
        qt = schema.get("__schema", {}).get("queryType", {}).get("name", "Query")
        endpoints = []
        for t in schema.get("__schema", {}).get("types", []):
            if t.get("name") == qt and t.get("fields"):
                for f in t["fields"]:
                    endpoints.append(Endpoint(
                        path=f["name"], method="GRAPHQL",
                        description=f.get("description") or ""))
        return source_url, endpoints


# ── Parallel fetcher ──────────────────────────────────────────────────────

class DataFetcher:
    def __init__(self, auth_headers, auth_params, concurrency=10):
        self.auth_headers = auth_headers
        self.auth_params  = auth_params
        self.sem = asyncio.Semaphore(concurrency)

    async def fetch_all(self, base_url, endpoints, spec_type):
        async with httpx.AsyncClient(headers=self.auth_headers, timeout=20,
                                     follow_redirects=True) as client:
            results = await asyncio.gather(
                *[self._fetch_one(client, base_url, ep, spec_type) for ep in endpoints],
                return_exceptions=True)

        data, errors = {}, {}
        for ep, res in zip(endpoints, results):
            key = f"{ep.method} {ep.path}"
            if isinstance(res, Exception):
                errors[key] = str(res)
            else:
                data[key] = res
        return data, errors

    async def _fetch_one(self, client, base_url, ep, spec_type):
        async with self.sem:
            if spec_type == "graphql":
                r = await client.post(base_url,
                    json={"query": f"{{ {ep.path} }}"},
                    headers={"Content-Type": "application/json"})
                r.raise_for_status()
                return r.json().get("data", {}).get(ep.path)
            else:
                url = urljoin(base_url.rstrip("/") + "/", ep.path.lstrip("/"))
                merged_params = {**self.auth_params, **ep.params}
                r = await client.request(ep.method, url, params=merged_params)
                r.raise_for_status()
                try:
                    return r.json()
                except Exception:
                    return r.text


# ── PDF loader helper ─────────────────────────────────────────────────────

async def _load_pdf_bytes(source: str) -> bytes:
    """
    Load raw PDF bytes from a URL, local path, or base64 string.
    Returns raw bytes.
    """
    parsed = urlparse(source)

    # URL
    if parsed.scheme in ("http", "https"):
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(source)
            r.raise_for_status()
            return r.content

    # Local file path
    if parsed.scheme == "" or parsed.scheme == "file":
        p = Path(source.replace("file://", ""))
        if p.exists():
            return p.read_bytes()

    # Base64 fallback
    try:
        return base64.b64decode(source)
    except Exception:
        pass

    raise ValueError(f"Cannot load PDF from source: {source!r}")


# ── FastAPI app ───────────────────────────────────────────────────────────

app = FastAPI(title="API Explorer")

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_detector = SpecDetector()
_oa_enum  = OpenAPIEnumerator()
_gql_enum = GraphQLEnumerator()


@app.post("/run")
async def run(req: RunRequest):
    try:
        headers = _build_headers(req.auth_scheme, req.token, req.header_name,
                                 req.username, req.password)
        params  = _build_query_params(req.auth_scheme, req.token, req.query_param_name)
        spec_type, spec = await _detector.detect(req.source, headers)

        if spec_type == "graphql":
            base_url, endpoints = _gql_enum.enumerate(spec, req.source)
        else:
            base_url, endpoints = _oa_enum.enumerate(spec, spec_type)

        fetcher = DataFetcher(auth_headers=headers, auth_params=params,
                              concurrency=req.concurrency)
        data, errors = await fetcher.fetch_all(base_url, endpoints, spec_type)

        return {"spec_type": spec_type, "base_url": base_url,
                "endpoints": [asdict(ep) for ep in endpoints],
                "data": data, "errors": errors}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/fetch-direct")
async def fetch_direct(req: DirectRequest):
    try:
        await _rate_limiter.acquire()

        headers = _build_headers(req.auth_scheme, req.token, req.header_name,
                                 req.username, req.password)
        params  = _build_query_params(req.auth_scheme, req.token,
                                      req.query_param_name, req.extra_params)

        http_method = req.method.upper() if req.method else "GET"

        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers=headers) as client:
            if http_method == "POST" and req.json_body is not None:
                r = await client.post(
                    req.url,
                    json=req.json_body,
                    params=params,
                    headers={"Content-Type": "application/json"},
                )
            else:
                r = await client.request(http_method, req.url, params=params)

            r.raise_for_status()
            content_type = r.headers.get("content-type", "")
            if "json" in content_type:
                body = r.json()
                encoding = None
            elif "pdf" in content_type or req.url.lower().split("?")[0].endswith(".pdf"):
                body = base64.b64encode(r.content).decode("ascii")
                encoding = "base64"
            else:
                body = r.text
                encoding = None

        response = {"url": str(r.url), "status_code": r.status_code,
                    "content_type": content_type, "data": body}
        if encoding:
            response["encoding"] = encoding
        return response

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400,
            detail=f"HTTP {e.response.status_code}: {e.response.text[:500]}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── PDF extraction cache ──────────────────────────────────────────────────
# Keyed by pdf_source URL/path. Holds the extracted context string so we
# never re-extract the same PDF twice within a server session, and — more
# importantly — never re-send the full text on follow-up questions.
_pdf_context_cache: dict[str, str] = {}



@app.post("/chat-pdf")
async def chat_pdf(req: ChatPDFRequest):
    """
    PDF AI Terminal — strictly text-only, token-minimised.

    Strategy
    --------
    • Turn 0 (no history): extract PDF locally with pdfplumber/PyMuPDF,
      cache the result, prepend the FULL extracted context to the first
      user message.  The raw PDF bytes are NEVER sent to Gemini.
    • Turn 1+ (history present): the extracted context is already in the
      model's context window via the cached history the client sends back.
      We send ONLY the new question — zero extra context tokens.
    • Extraction cache: same URL/path → same extracted text within a
      server session (no repeated disk/network I/O either).
    """
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key or api_key == "your_google_api_key_here":
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_API_KEY is not configured. Please add it to your .env file."
        )

    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    is_first_turn = len(req.history) == 0

    # ── STEP 1: Extract PDF (first turn only, cached thereafter) ──────────
    # OCR on scanned PDFs can take 30-90 s, so we run it in a thread
    # executor to avoid blocking the asyncio event loop.
    if is_first_turn:
        if req.pdf_source not in _pdf_context_cache:
            try:
                from pdf_reader import TenderPDFReader
                import concurrent.futures

                def _extract(source: str) -> str:
                    reader = TenderPDFReader(source)
                    structured = reader.extract_structured()
                    return _build_pdf_context(structured)

                loop = asyncio.get_event_loop()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    ctx = await asyncio.wait_for(
                        loop.run_in_executor(pool, _extract, req.pdf_source),
                        timeout=180,   # 3 minutes max for OCR
                    )
                _pdf_context_cache[req.pdf_source] = ctx
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=504,
                    detail="PDF extraction timed out (>3 min). Try a shorter document."
                )
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not extract PDF (pdfplumber/PyMuPDF/OCR): {e}"
                )
        pdf_context = _pdf_context_cache[req.pdf_source]
    else:
        pdf_context = None   # not needed — already in history

    # ── STEP 2: Build Gemini contents array ───────────────────────────────
    system_prompt = (
        "You are a document analyst. You have been given the full text extracted "
        "from a PDF by Python libraries (pdfplumber / PyMuPDF). "
        "Answer questions using ONLY the provided document text. "
        "Be concise. Quote exact figures, dates, and reference numbers as they appear. "
        "If the information is not present in the document, say so explicitly."
    )

    contents: list[dict] = []

    # Replay prior turns (plain text — no PDF bytes ever)
    for turn in req.history:
        gemini_role = "model" if turn.get("role") == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": [{"text": turn.get("content", "")}]})

    # Current question
    if is_first_turn:
        label = req.source_label or req.pdf_source.split("/")[-1] or "document"
        user_text = (
            f"Document: {label}\n\n"
            f"[START OF PYTHON-EXTRACTED PDF TEXT]\n"
            f"{pdf_context}\n"
            f"[END OF EXTRACTED TEXT]\n\n"
            f"Question: {req.question}"
        )
    else:
        # Follow-up: question only — the context is already in the history
        user_text = req.question

    contents.append({"role": "user", "parts": [{"text": user_text}]})

    # ── STEP 3: Call Gemini ───────────────────────────────────────────────
    model = req.model if req.model in (
        "gemini-2.5-flash", "gemini-2.5-flash-lite-preview-06-17",
        "gemini-2.5-pro", "gemini-2.0-flash", "gemini-2.0-flash-lite",
    ) else "gemini-2.5-flash"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}",
                headers={"content-type": "application/json"},
                json={
                    "system_instruction": {"parts": {"text": system_prompt}},
                    "contents": contents,
                    "generationConfig": {"maxOutputTokens": 1024},
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.json().get("error", {}).get("message", e.response.text[:400])
        except Exception:
            body = e.response.text[:400]
        raise HTTPException(status_code=502, detail=f"Gemini API error: {body}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach Gemini: {e}")

    # ── STEP 4: Parse response ────────────────────────────────────────────
    answer = ""
    input_tokens = 0
    output_tokens = 0
    try:
        candidate = data.get("candidates", [{}])[0]
        for part in candidate.get("content", {}).get("parts", []):
            answer += part.get("text", "")
        usage = data.get("usageMetadata", {})
        input_tokens  = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
    except (KeyError, IndexError, TypeError):
        pass

    return {
        "answer":            answer,
        "input_tokens":      input_tokens,
        "output_tokens":     output_tokens,
        "model":             model,
        "extraction_method": "python-local (pdfplumber/PyMuPDF) — PDF bytes never sent to API",
        "context_sent":      is_first_turn,   # tells client whether full context was sent
    }


def _build_pdf_context(structured_data: dict) -> str:
    """
    Convert locally-extracted PDF data into a compact text block for Gemini.

    Rules to minimise tokens
    ─────────────────────────
    • Metadata  : only non-empty fields, one line each
    • TOC       : max 25 headings
    • Qual/Price: max 3 blocks × 400 chars each  (de-duplicated with full text)
    • Tables    : max 3 tables × 8 rows, cells truncated to 25 chars
    • Full text : up to 6 000 chars — enough for most tender NITs;
                  raises to 12 000 if no tables/qual/price were found
    No section is repeated — full text is the single source of truth.
    """
    parts: list[str] = []

    # ── Metadata ──────────────────────────────────────────────────────────
    meta = structured_data.get("metadata", {})
    meta_lines = []
    for key, label in (("title","Title"),("author","Author"),
                       ("pages","Pages"),("creation_date","Created")):
        val = str(meta.get(key, "")).strip()
        if val:
            meta_lines.append(f"{label}: {val}")
    if meta_lines:
        parts.append("=== METADATA ===")
        parts.extend(meta_lines)
        parts.append("")

    # ── TOC ───────────────────────────────────────────────────────────────
    toc = structured_data.get("toc", [])[:25]
    if toc:
        parts.append("=== DOCUMENT STRUCTURE ===")
        parts.extend(f"  {h}" for h in toc)
        parts.append("")

    # ── Tables (most information-dense, lowest char/token ratio) ──────────
    tables = structured_data.get("tables", [])
    if tables:
        parts.append("=== EXTRACTED TABLES ===")
        for i, tbl in enumerate(tables[:3], 1):
            parts.append(f"\nTable {i} (page {tbl.get('page','?')}):")
            for row in tbl.get("data", [])[:8]:
                parts.append("  " + " | ".join(str(c or "").strip()[:25] for c in row))
            extra = len(tbl.get("data", [])) - 8
            if extra > 0:
                parts.append(f"  … +{extra} more rows")
        parts.append("")

    # ── Full text (primary source — sent once, used for all answers) ───────
    full_text = structured_data.get("full_text", "").strip()
    # Expand budget if document has no structured sections
    char_budget = 12_000 if not tables and not toc else 6_000
    if full_text:
        parts.append("=== FULL EXTRACTED TEXT ===")
        if len(full_text) > char_budget:
            parts.append(full_text[:char_budget])
            parts.append(f"\n… [truncated — showed {char_budget} of {len(full_text)} chars]")
        else:
            parts.append(full_text)
        parts.append("")

    return "\n".join(parts)


# ── Serve UI ──────────────────────────────────────────────────────────────

_UI_PATH = Path(__file__).parent / "ui.html"

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    if _UI_PATH.exists():
        return HTMLResponse(_UI_PATH.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="ui.html not found next to server.py")