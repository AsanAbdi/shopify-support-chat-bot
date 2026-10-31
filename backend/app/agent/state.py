from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    summary: str  # compressed older history
    lang: str
    customer_email: str | None
    order_number: str | None
    order_failures: int  # failed lookups in this conversation (enumeration defence)
    low_conf_streak: int  # consecutive KB misses
    pending_escalation: str | None  # reason code awaiting execution/email
    email_asked: bool
    ticket_id: str | None
    # per-turn scratch, reset in `prepare`
    turn_sources: list[dict]
    turn_no_answer: bool
    kb_context: str  # excerpts retrieved before the model runs (fast path: one LLM call for FAQ questions)
    tool_rounds: int
    injection: bool
