from dataclasses import dataclass, field
from decimal import Decimal

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from app.config import Settings


@dataclass
class UsageRecord:
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(eq=False)  # callback handlers must stay hashable (LangChain keeps them in sets)
class UsageCollector(AsyncCallbackHandler):
    """Collects token usage of every LLM call in one turn (agent, summaries, ticket summary)."""

    records: list[UsageRecord] = field(default_factory=list)

    async def on_llm_end(self, response: LLMResult, **kwargs) -> None:  # noqa: ARG002
        for gens in response.generations:
            for g in gens:
                msg = getattr(g, "message", None)
                usage = getattr(msg, "usage_metadata", None)
                if not usage:
                    continue
                meta = getattr(msg, "response_metadata", {}) or {}
                self.records.append(UsageRecord(
                    meta.get("model_name") or meta.get("model") or "unknown",
                    int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                ))


def cost_eur(rec: UsageRecord, settings: Settings) -> Decimal:
    if rec.model.startswith("fake"):  # the offline model is free: it must never eat the real monthly budget
        return Decimal(0)
    return (
        Decimal(rec.input_tokens) * Decimal(str(settings.llm_price_in_eur_per_mtok))
        + Decimal(rec.output_tokens) * Decimal(str(settings.llm_price_out_eur_per_mtok))
    ) / Decimal(1_000_000)
