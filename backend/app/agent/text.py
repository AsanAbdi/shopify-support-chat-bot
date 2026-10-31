"""Plain text of LangChain message content: a string, or a list of provider content blocks."""

from langchain_core.messages import BaseMessage


def content_text(content: str | list) -> str:
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def message_text(message: BaseMessage) -> str:
    return content_text(message.content)
