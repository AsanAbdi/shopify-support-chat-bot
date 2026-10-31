import re


def select_cited(text: str, sources: list[dict], *, fallback: bool = True) -> list[dict]:
    """Sources the answer actually cites ([n]). Prefetched excerpts the model did not use are never shown.

    With `fallback`, an answer that used the knowledge-base *tool* but forgot the citation still gets the top
    source (better UX); the eval runs with fallback=False so a missing citation counts as a failure (F2).
    """
    sources = [s for s in sources if s.get("type") != "ticket"]
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    used = [s for s in sources if s["n"] in cited]
    if used or not fallback:
        return used
    return [s for s in sources if not s.get("pre")][:1]


INTERNAL_SOURCE_KEYS = {"doc_id", "pre"}  # bookkeeping of the agent: stored with the message, never sent to the widget


def source_url(source: dict, public_base_url: str) -> str | None:
    """Where the customer can open a source: a web page as it is, an uploaded file via the public download route."""
    if source.get("type") == "url":
        return source["ref"]
    if source.get("type") == "file" and source.get("doc_id"):
        return f"{public_base_url}/api/kb/{source['doc_id']}/file"
    return None


def as_stored(source: dict, public_base_url: str) -> dict:
    return source | {"url": source_url(source, public_base_url)}


def as_shown(source: dict, public_base_url: str) -> dict:
    visible = {k: v for k, v in source.items() if k not in INTERNAL_SOURCE_KEYS}
    return visible | {"url": source_url(source, public_base_url)}
