"""embedding dimension 384 -> 1024 (multilingual-e5-large); products.tags / embed_hash

Embeddings are derived data: rows are dropped and re-created by the indexer. After upgrading run
`POST /api/admin/reindex-all` (or restart the stack: the `seed` service reindexes the demo knowledge base).

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

NEW_DIM = 1024
OLD_DIM = 384


def _retype(dim: int) -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS ix_products_embedding")
    op.execute("DELETE FROM chunks")
    op.execute("DELETE FROM products")
    op.execute("UPDATE documents SET content_hash = NULL, chunks_count = 0, status = 'pending'")
    op.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({dim})")
    op.execute(f"ALTER TABLE products ALTER COLUMN embedding TYPE vector({dim})")
    op.execute("CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)")
    op.execute("CREATE INDEX ix_products_embedding ON products USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)")


def upgrade() -> None:
    op.add_column("products", sa.Column("tags", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column("products", sa.Column("embed_hash", sa.String(32), server_default="", nullable=False))
    _retype(NEW_DIM)


def downgrade() -> None:
    _retype(OLD_DIM)
    op.drop_column("products", "embed_hash")
    op.drop_column("products", "tags")
