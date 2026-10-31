"""LangGraph in 50 lines: state, nodes, edges, a conditional edge, a loop, memory per thread, streaming. No model, no DB.

Run (Docker only, free):
    docker compose --profile test run --rm --no-deps tests python scripts/langgraph_basics.py

Map to the real agent (app/agent/graph.py):  clean = prepare, think = agent, lookup = tools, finalize = finalize.
"""

import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class State(TypedDict, total=False):
    text: str
    order_info: str
    answer: str
    steps: Annotated[list[str], operator.add]  # "append, don't overwrite" (our `messages` works the same way)
    history: Annotated[list[str], operator.add]


def clean(state):
    return {"text": state["text"].strip().lower(), "history": [state["text"]], "steps": ["clean"]}


def think(state):  # = our `agent`: decides what is needed
    if "order" in state["text"] and not state.get("order_info"):
        return {"steps": ["think: need data"]}
    info = state.get("order_info")
    return {"answer": f"Your order: {info}" if info else "Happy to help!", "steps": ["think: can answer"]}


def lookup(state):  # = our `tools`: fetches data
    return {"order_info": "shipped yesterday", "steps": ["lookup"]}


def finalize(state):
    return {"steps": ["finalize"]}


def route_after_think(state):  # = our `route_after_agent`: names the next node
    return "finalize" if state.get("answer") else "lookup"


g = StateGraph(State)
g.add_node("clean", clean)
g.add_node("think", think)
g.add_node("lookup", lookup)
g.add_node("finalize", finalize)
g.add_edge(START, "clean")
g.add_edge("clean", "think")
g.add_conditional_edges("think", route_after_think, {"lookup": "lookup", "finalize": "finalize"})
g.add_edge("lookup", "think")  # the loop: after the data arrived, think again
g.add_edge("finalize", END)
graph = g.compile(checkpointer=InMemorySaver())

for i, text in enumerate(["Where is my ORDER 1002?", "thanks!"]):
    result = graph.invoke({"text": text, "steps": []}, {"configurable": {"thread_id": f"user-{i}"}})
    print(f"{text!r}\n   path:   {result['steps']}\n   answer: {result['answer']}")

print("\nsame conversation, second message (thread_id remembers the first):")
same = {"configurable": {"thread_id": "user-0"}}
result = graph.invoke({"text": "and the refund?", "steps": []}, same)
print("   history:", result["history"])

print("\nwhat the graph emits while running (stream_mode='updates'):")
for part in graph.stream({"text": "order?", "steps": []}, {"configurable": {"thread_id": "demo"}}, stream_mode="updates"):
    print("  ", part)

print("\nthe diagram LangGraph draws itself (paste into mermaid.live):")
print(graph.get_graph().draw_mermaid())
