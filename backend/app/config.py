from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (12-factor). See .env.example."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    # Demo deployments (for example on AWS) may keep the Shopify/Freshdesk mocks; otherwise prod refuses to start with them.
    allow_mock_integrations: bool = False
    public_base_url: str = "http://localhost:8000"
    # Origins of the shop(s) that embed the widget. "*" only for dev.
    cors_origins: list[str] = ["*"]

    database_url: str = "postgresql+psycopg://app:app@db:5432/app"
    redis_url: str = "redis://redis:6379/0"
    upload_dir: str = "/data/uploads"

    # --- LLM (provider/model are switched here, no code changes) ---
    llm_provider: Literal["anthropic", "openai", "bedrock", "fake"] = "fake"
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_summary_model: str | None = None  # cheaper model for summaries; defaults to llm_model
    # bedrock: Claude inside AWS, no API key (IAM role). llm_model is then a Bedrock model id or EU inference profile,
    # e.g. eu.anthropic.claude-haiku-4-5-20251001-v1:0
    aws_region: str = "eu-central-1"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 900
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    # Prices in EUR per 1M tokens (used for the cost dashboard and budget guard)
    llm_price_in_eur_per_mtok: float = 0.92
    llm_price_out_eur_per_mtok: float = 4.60
    llm_monthly_budget_eur: float = 100.0
    llm_budget_alert_ratio: float = 0.8

    # --- Embeddings / retrieval ---
    embeddings_provider: Literal["fastembed", "fake"] = "fastembed"
    embeddings_model: str = "intfloat/multilingual-e5-large"
    embeddings_dim: int = 1024  # must match the vector column; changing it needs a migration + reindex
    embeddings_threads: int = 1  # ONNX threads per call: 1 for chat latency under load, 0 = all cores (worker/bulk)
    embeddings_concurrency: int = 2  # parallel embedding calls in one process
    embeddings_query_prefix: str = "query: "
    embeddings_passage_prefix: str = "passage: "
    reranker: Literal["none", "fastembed"] = "none"
    reranker_model: str = "jinaai/jina-reranker-v2-base-multilingual"
    retrieval_top_k: int = 5
    retrieval_candidates: int = 20
    # Below this cosine similarity the best hit is treated as "nothing found"
    retrieval_min_similarity: float = 0.82  # calibrated on the demo KB for e5-large (sims are compressed: 0.75-0.9); `python -m scripts.calibrate`
    # Same gate when the cross-encoder reranker is enabled (score squashed to 0..1)
    retrieval_min_rerank: float = 0.15
    product_min_similarity: float = 0.76  # catalog queries are short and lenient; e5 scale
    url_fetch_allow_private: bool = False  # SSRF guard for admin-supplied URLs; dev/mock only
    chunk_size: int = 900
    chunk_overlap: int = 120

    # --- Agent ---
    summary_trigger_messages: int = 14  # compress history when it grows past this
    summary_keep_messages: int = 6
    agent_max_tool_rounds: int = 4
    escalation_sla_hours: int = 24
    shop_timezone: str = "Europe/Amsterdam"  # "today"/"tomorrow" for customers and the model's clock
    support_email: str = "support@demo-store.example.com"
    max_user_message_chars: int = 2000

    # --- Shopify / Freshdesk (point to mock-services until real credentials exist) ---
    shopify_base_url: str = "http://mock-services:9000"
    shopify_store_domain: str = "demo-store.myshopify.com"
    shopify_storefront_url: str = "https://demo-store.example.com"
    shopify_admin_token: SecretStr = SecretStr("mock-shopify-token")
    shopify_api_version: str = "2026-07"  # supported ~12 months; bump quarterly
    shopify_order_name_format: str = "#{number}"  # stores can customise order-name prefix/suffix (Settings > General)
    freshdesk_base_url: str = "http://mock-services:9000/freshdesk"
    freshdesk_api_key: SecretStr = SecretStr("mock-freshdesk-key")
    integration_timeout_s: float = 8.0

    # --- Security / GDPR ---
    jwt_secret: SecretStr = SecretStr("change-me-in-env")
    jwt_ttl_minutes: int = 60 * 8
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("admin12345")
    rate_limit_chat_per_min: int = 20
    rate_limit_http_per_min: int = 120
    rate_limit_login_per_min: int = 10
    session_ttl_hours: int = 24
    conversation_retention_days: int = 90

    # --- Alerts (all optional; Sentry also receives budget alerts as errors) ---
    alert_webhook_url: str | None = None  # Slack/Mattermost/Teams-compatible incoming webhook
    alert_telegram_bot_token: SecretStr | None = None
    alert_telegram_chat_id: str | None = None
    alert_telegram_api_url: str = "https://api.telegram.org"

    # --- Observability ---
    sentry_dsn: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def summary_model(self) -> str:
        return self.llm_summary_model or self.llm_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
