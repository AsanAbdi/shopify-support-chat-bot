import hashlib
from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.rag.loaders import LoadedDocument

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]


@dataclass
class ChunkDraft:
    content: str
    meta: dict
    content_hash: str


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_hash(doc: LoadedDocument) -> str:
    return sha256("\x00".join(s.text for s in doc.sections))


def chunk_document(doc: LoadedDocument, *, chunk_size: int, chunk_overlap: int) -> list[ChunkDraft]:
    """Header-aware splitting. Every chunk is prefixed with 'Title > Heading' so it is self-explanatory in retrieval."""
    md = MarkdownHeaderTextSplitter(_HEADERS, strip_headers=True)
    rc = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    drafts: list[ChunkDraft] = []
    seen: set[str] = set()
    for section in doc.sections:
        for piece in md.split_text(section.text):
            heading = " > ".join(
                piece.metadata[k] for _, k in _HEADERS if k in piece.metadata and piece.metadata[k] != doc.title
            )
            for part in rc.split_text(piece.page_content):
                part = part.strip()
                if len(part) < 20:
                    continue
                prefix = f"{doc.title} > {heading}" if heading else doc.title
                content = f"{prefix}\n{part}"
                h = sha256(content)
                if h in seen:
                    continue
                seen.add(h)
                drafts.append(ChunkDraft(content, {**section.meta, "heading": heading}, h))
    return drafts
