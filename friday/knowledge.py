"""Local document knowledge base — P4 RAG.

Indexes your own files so FRIDAY can answer questions about them, using the same
bge-small-en embedding model already loaded for intent matching and memory
recall (see `friday.brain.matcher.embed`). No cloud call, no extra model.

Each document is split into overlapping chunks; each chunk gets its own
embedding, so retrieval finds the passage that actually answers the question
rather than the file that vaguely mentions it.

Supported formats: .txt, .md, .pdf, .docx
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from friday import store
from friday.config import CFG
from friday.log import get

log = get(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}

_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "venv", "AppData",
    "$RECYCLE.BIN", "System Volume Information", ".cache", "site-packages",
}


@dataclass(slots=True)
class Hit:
    doc_id: int
    path: str
    title: str
    seq: int
    text: str
    score: float


class UnsupportedFileType(ValueError):
    pass


# -- text extraction ----------------------------------------------------------


def _read_txt(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _read_pdf(p: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(p))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _read_docx(p: Path) -> str:
    import docx

    document = docx.Document(str(p))
    return "\n".join(para.text for para in document.paragraphs)


_READERS = {".txt": _read_txt, ".md": _read_txt, ".pdf": _read_pdf, ".docx": _read_docx}


def extract_text(path: Path) -> str:
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        raise UnsupportedFileType(f"unsupported file type: {path.suffix or path.name}")
    return reader(path)


# -- chunking -------------------------------------------------------------


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split into overlapping windows, preferring paragraph/sentence breaks."""
    size = size or CFG.knowledge.chunk_size
    overlap = overlap if overlap is not None else CFG.knowledge.chunk_overlap
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            boundary = text.rfind("\n\n", start, end)
            if boundary <= start:
                boundary = text.rfind(". ", start, end)
            if boundary > start:
                end = boundary + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


# -- embedding --------------------------------------------------------------


def _embed_many(texts: list[str]) -> np.ndarray:
    from friday.brain.matcher import embed

    return embed(texts).astype(np.float32)


# -- indexing -----------------------------------------------------------------


def index_file(path: str | Path) -> dict[str, Any]:
    """Index a single file: extract text, chunk, embed, replacing any prior copy."""
    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))
    if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileType(f"unsupported file type: {p.suffix or p.name}")

    text = extract_text(p)
    chunks = chunk_text(text)
    c = store.conn()

    existing = c.execute("SELECT id FROM kb_documents WHERE path = ?", (str(p),)).fetchone()
    if existing:
        c.execute("DELETE FROM kb_documents WHERE id = ?", (existing["id"],))

    if not chunks:
        c.commit()
        return {"path": str(p), "title": p.name, "chunks": 0}

    vectors = _embed_many(chunks)
    cur = c.execute(
        "INSERT INTO kb_documents (path, title, indexed_at, mtime, chunk_count) "
        "VALUES (?,?,?,?,?)",
        (str(p), p.name, store.now(), p.stat().st_mtime, len(chunks)),
    )
    doc_id = cur.lastrowid
    c.executemany(
        "INSERT INTO kb_chunks (doc_id, seq, text, embedding) VALUES (?,?,?,?)",
        [
            (doc_id, i, chunk, vector.tobytes())
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ],
    )
    c.commit()
    log.info("indexed %s: %d chunks", p.name, len(chunks))
    return {"path": str(p), "title": p.name, "chunks": len(chunks)}


def index_path(path: str | Path, recursive: bool = True) -> dict[str, Any]:
    """Index a file, or every supported file under a folder."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(str(p))

    if p.is_file():
        return index_file(p)

    indexed: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    scanned = 0
    max_files = CFG.knowledge.max_files

    walker = p.rglob("*") if recursive else p.glob("*")
    for entry in walker:
        if not entry.is_file() or entry.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        # Only skip noise directories *within* the requested folder — an
        # explicitly chosen root (which may itself sit under AppData, say)
        # must not be excluded by its own ancestry.
        if any(part in _SKIP_DIRS for part in entry.relative_to(p).parts[:-1]):
            continue
        scanned += 1
        if scanned > max_files:
            break
        try:
            result = index_file(entry)
            (indexed if result.get("chunks") else skipped).append(result["path"])
        except Exception as exc:
            log.warning("failed to index %s: %s", entry, exc)
            errors.append(str(entry))

    return {
        "path": str(p),
        "indexed": len(indexed),
        "skipped": len(skipped),
        "errors": errors,
    }


# -- retrieval ------------------------------------------------------------


def search(query: str, k: int = 5) -> list[Hit]:
    """Find the document chunks most similar in meaning to `query`."""
    from friday.brain.matcher import embed

    rows = store.conn().execute(
        "SELECT kb_chunks.doc_id, kb_chunks.seq, kb_chunks.text, kb_chunks.embedding, "
        "kb_documents.path AS doc_path, kb_documents.title AS doc_title "
        "FROM kb_chunks JOIN kb_documents ON kb_chunks.doc_id = kb_documents.id"
    ).fetchall()
    if not rows:
        return []

    vectors = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    scores = vectors @ embed([query])[0]

    ranked = sorted(zip(scores, rows), key=lambda pair: -pair[0])[:k]
    return [
        Hit(
            doc_id=row["doc_id"], path=row["doc_path"], title=row["doc_title"],
            seq=row["seq"], text=row["text"], score=float(score),
        )
        for score, row in ranked
    ]


def list_documents() -> list[dict[str, Any]]:
    return [
        dict(r) for r in store.conn().execute(
            "SELECT id, path, title, indexed_at, chunk_count "
            "FROM kb_documents ORDER BY indexed_at DESC"
        ).fetchall()
    ]


def forget_document(path_or_name: str) -> list[str]:
    """Delete indexed documents whose path contains `path_or_name`. Cascades to chunks."""
    like = f"%{path_or_name}%"
    c = store.conn()
    rows = c.execute("SELECT id, path FROM kb_documents WHERE path LIKE ?", (like,)).fetchall()
    removed = []
    for row in rows:
        c.execute("DELETE FROM kb_documents WHERE id = ?", (row["id"],))
        removed.append(row["path"])
    c.commit()
    return removed
