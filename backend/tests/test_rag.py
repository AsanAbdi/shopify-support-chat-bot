import io
from pathlib import Path

from sqlalchemy import func, select

from app.config import get_settings
from app.models import Chunk, Document
from app.rag.chunking import chunk_document, document_hash
from app.rag.embeddings import get_embedder
from app.rag.indexer import index_document
from app.rag.loaders import load_csv, load_file, load_html, load_markdown, load_pdf
from app.rag.retriever import confidence, retrieve

ARTICLE = (
    "# Returns\n\n## Period\nYou can return items within 30 days of delivery. Items must be unworn and unwashed.\n\n"
    "## Refund\nThe refund arrives within 5-7 working days after we received the parcel.\n"
)


def test_chunks_carry_title_and_heading_prefix():
    doc = load_markdown(ARTICLE.encode(), "returns.md")
    chunks = chunk_document(doc, chunk_size=300, chunk_overlap=30)
    assert len(chunks) == 2
    assert chunks[0].content.startswith("Returns > Period\n")
    assert len({c.content_hash for c in chunks}) == 2


def test_document_hash_changes_with_content():
    a, b = load_markdown(ARTICLE.encode(), "a.md"), load_markdown(ARTICLE.replace("30", "14").encode(), "a.md")
    assert document_hash(a) != document_hash(b)


def test_html_loader_strips_chrome_and_keeps_structure():
    html = "<html><head><title>Shipping</title></head><body><nav>MENU</nav><article><h2>Costs</h2><p>EUR 4.90 to Germany.</p><ul><li>Free over 60</li></ul></article><footer>FOOT</footer></body></html>"
    doc = load_html(html)
    text = doc.sections[0].text
    assert doc.title == "Shipping" and "MENU" not in text and "FOOT" not in text
    assert "## Costs" in text and "- Free over 60" in text


def test_csv_rows_keep_their_headers():
    doc = load_csv(b"EU,UK,Foot length cm\n42,8,26.7\n43,9,27.3\n", "size_chart.csv")
    assert "EU=42; UK=8; Foot length cm=26.7" in doc.sections[0].text


def test_docx_loader():
    import docx

    d = docx.Document()
    d.add_heading("Warranty", level=1)
    d.add_paragraph("Two years of warranty on all products.")
    buf = io.BytesIO()
    d.save(buf)
    doc = load_file(buf.getvalue(), "warranty.docx")
    assert "Two years of warranty" in doc.sections[0].text


def test_pdf_loader():
    content = b"BT /F1 12 Tf 72 720 Td (Returns within 30 days) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    doc = load_pdf(bytes(out), "terms.pdf")
    assert "Returns within 30 days" in doc.sections[0].text and doc.sections[0].meta["page"] == 1


async def _make_doc(session, tmp_path: Path, name: str, body: str) -> Document:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    doc = Document(title=name, source_type="file", source_ref=name, stored_path=str(path))
    session.add(doc)
    await session.commit()
    return doc


async def test_reindex_skips_unchanged_and_embeds_only_changed_chunks(session, tmp_path):
    doc = await _make_doc(session, tmp_path, "returns.md", ARTICLE)
    first = await index_document(session, doc.id)
    assert first["status"] == "indexed" and first["new"] == 2

    again = await index_document(session, doc.id)
    assert again["status"] == "unchanged"

    ids_before = {c.content_hash: c.id for c in (await session.execute(select(Chunk))).scalars()}
    Path(doc.stored_path).write_text(ARTICLE.replace("5-7", "3-5"), encoding="utf-8")
    third = await index_document(session, doc.id)
    assert third["status"] == "indexed" and third["new"] == 1 and third["removed"] == 1

    session.expire_all()
    rows = (await session.execute(select(Chunk))).scalars().all()
    kept = [c for c in rows if c.content_hash in ids_before]
    assert len(rows) == 2 and len(kept) == 1 and kept[0].id == ids_before[kept[0].content_hash]  # untouched chunk keeps its row


async def test_failed_indexing_is_visible_in_status(session, tmp_path):
    doc = await _make_doc(session, tmp_path, "broken.pdf", "this is not a pdf")
    doc_id = doc.id
    res = await index_document(session, doc_id)
    assert res["status"] == "failed"
    session.expire_all()
    refreshed = await session.get(Document, doc_id)
    assert refreshed.status == "failed" and refreshed.error


async def test_deleting_a_document_removes_its_chunks(session, seeded_kb):
    doc = (await session.execute(select(Document).where(Document.source_ref == "returns.md"))).scalar_one()
    await session.delete(doc)
    await session.commit()
    assert (await session.execute(select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id))).scalar() == 0


async def test_hybrid_retrieval_finds_the_right_document(session, seeded_kb):
    hits = await retrieve(session, get_embedder(), "How many days do I have to return an item?")
    assert hits and hits[0].source == "returns.md"
    assert confidence(hits) >= get_settings().retrieval_min_similarity


async def test_exact_terms_are_found_by_full_text_side(session, seeded_kb):
    hits = await retrieve(session, get_embedder(), "iDEAL Klarna SEPA")
    assert hits[0].source == "payment-methods.md"


async def test_unrelated_question_has_low_confidence(session, seeded_kb):
    hits = await retrieve(session, get_embedder(), "What will the weather be like in Berlin tomorrow?")
    assert confidence(hits) < get_settings().retrieval_min_similarity


async def test_force_reindex_recomputes_even_unchanged_chunks(session, tmp_path, monkeypatch):
    """'Reindex all' after a model change must replace the vectors of unchanged chunks too, or they silently go stale."""
    doc = await _make_doc(session, tmp_path, "returns.md", ARTICLE)
    await index_document(session, doc.id)
    calls: list[int] = []

    class Marker:
        dim = get_settings().embeddings_dim

        async def embed_documents(self, texts):
            calls.append(len(texts))
            return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]

    monkeypatch.setattr("app.rag.indexer.get_embedder", lambda: Marker())
    assert (await index_document(session, doc.id))["status"] == "unchanged" and calls == []  # normal run: nothing to do
    res = await index_document(session, doc.id, force=True)
    assert res["status"] == "indexed" and res["new"] == 0 and res["embedded"] == 2 and calls == [2]
    session.expire_all()
    assert [float(c.embedding[0]) for c in (await session.execute(select(Chunk))).scalars()] == [1.0, 1.0]
