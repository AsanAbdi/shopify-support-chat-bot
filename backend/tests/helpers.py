import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import GraphDeps, build_graph
from app.agent.llm import FakeChatModel
from app.agent.tools import ToolContext
from app.config import get_settings
from app.integrations.freshdesk import FreshdeskClient
from app.integrations.shopify import ShopifyClient
from app.rag.embeddings import get_embedder


@dataclass
class AgentHarness:
    graph: Any
    deps: GraphDeps
    thread: str = field(default_factory=lambda: str(uuid.uuid4()))

    async def say(self, text: str) -> tuple[str, dict]:
        cfg = {"configurable": {"thread_id": self.thread, "deps": self.deps}}
        out = await self.graph.ainvoke({"messages": [HumanMessage(content=text)]}, cfg)
        answer = next(m.content for m in reversed(out["messages"]) if isinstance(m, AIMessage))
        return answer, out

    async def state(self) -> dict:
        snap = await self.graph.aget_state({"configurable": {"thread_id": self.thread, "deps": self.deps}})
        return snap.values


def make_harness(sm, llm: FakeChatModel | None = None) -> AgentHarness:
    s = get_settings()
    llm = llm or FakeChatModel()
    deps = GraphDeps(s, llm, llm, ToolContext(s, sm, get_embedder(), ShopifyClient(s)), FreshdeskClient(s))
    return AgentHarness(build_graph(InMemorySaver()), deps)
