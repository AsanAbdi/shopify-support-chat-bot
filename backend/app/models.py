from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db import Base

EMBED_DIM = get_settings().embeddings_dim


def _now() -> datetime:
    return datetime.now().astimezone()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="agent")  # admin | agent
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(16))  # file | url
    source_ref: Mapped[str] = mapped_column(String(1000), unique=True)  # url or original filename
    stored_path: Mapped[str | None] = mapped_column(String(500))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|indexing|indexed|failed
    error: Mapped[str | None] = mapped_column(Text)
    chunks_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan", passive_deletes=True)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    tsv: Mapped[str] = mapped_column(TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True))

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)  # Shopify GID
    title: Mapped[str] = mapped_column(String(500))
    handle: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    product_type: Mapped[str] = mapped_column(String(200), default="")
    vendor: Mapped[str] = mapped_column(String(200), default="")
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    url: Mapped[str] = mapped_column(String(1000))
    variants: Mapped[list] = mapped_column(JSONB, default=list)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    embed_hash: Mapped[str] = mapped_column(String(32), default="")  # md5 of the embedded text: re-embed only when it changes
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', title || ' ' || coalesce(product_type,'') || ' ' || coalesce(description,''))", persisted=True)
    )
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_products_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_products_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # = session id = LangGraph thread_id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    lang: Mapped[str] = mapped_column(String(5), default="en")
    customer_email: Mapped[str | None] = mapped_column(String(320), index=True)
    order_number: Mapped[str | None] = mapped_column(String(40))
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    escalation_reason: Mapped[str | None] = mapped_column(String(40))
    ticket_id: Mapped[str | None] = mapped_column(String(40))
    ticket_summary: Mapped[str | None] = mapped_column(Text)
    had_no_answer: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    has_downvote: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    client_ip_hash: Mapped[str | None] = mapped_column(String(64))

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", passive_deletes=True, order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(12))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    no_answer: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback: Mapped[int | None] = mapped_column(SmallInteger)  # 1 | -1
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # no FK: usage statistics survive GDPR deletion of the conversation (they hold no personal data)
    conversation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_eur: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class UnansweredQuestion(Base):
    """Questions the bot could not answer from the knowledge base (admin: top unanswered)."""

    __tablename__ = "unanswered_questions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    question: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
