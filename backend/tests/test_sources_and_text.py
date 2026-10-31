from langchain_core.messages import AIMessage, HumanMessage

from app.agent.sources import as_shown, as_stored, select_cited, source_url
from app.agent.text import content_text, message_text

BASE = "https://support.example"
FILE = {"n": 1, "type": "file", "ref": "returns.md", "title": "Returns", "doc_id": 7, "pre": True}
PAGE = {"n": 2, "type": "url", "ref": "https://shop.example/help", "title": "Help"}
TICKET = {"n": 0, "type": "ticket", "ref": "FD-1", "reason": "money", "summary": "s"}


def test_a_web_page_links_to_itself_and_an_uploaded_file_to_the_public_download():
    assert source_url(PAGE, BASE) == "https://shop.example/help"
    assert source_url(FILE, BASE) == "https://support.example/api/kb/7/file"


def test_sources_without_a_place_to_open_have_no_url():
    assert source_url(TICKET, BASE) is None
    assert source_url({"n": 3, "type": "file", "ref": "x.md"}, BASE) is None  # a file whose document id is unknown


def test_the_stored_form_keeps_everything_and_the_shown_form_hides_the_bookkeeping():
    assert as_stored(FILE, BASE) == FILE | {"url": "https://support.example/api/kb/7/file"}
    assert as_shown(FILE, BASE) == {
        "n": 1, "type": "file", "ref": "returns.md", "title": "Returns", "url": "https://support.example/api/kb/7/file"}
    assert FILE == {"n": 1, "type": "file", "ref": "returns.md", "title": "Returns", "doc_id": 7, "pre": True}  # not mutated


def test_only_cited_sources_are_selected_and_tickets_never_are():
    assert select_cited("See [2].", [FILE, PAGE, TICKET]) == [PAGE]
    assert select_cited("No citation, but the tool found something.", [FILE, PAGE]) == [PAGE]  # prefetched ones are not a fallback
    assert select_cited("Nothing cited.", [FILE], fallback=True) == []


def test_text_of_a_string_and_of_content_blocks():
    assert content_text("plain") == "plain"
    assert content_text([{"type": "text", "text": "a"}, {"type": "tool_use", "id": "1"}, {"type": "text", "text": "b"}]) == "ab"
    assert content_text(["not a block", {"type": "text", "text": "c"}]) == "c"
    assert content_text([]) == ""


def test_message_text_reads_the_content_of_any_message():
    assert message_text(HumanMessage(content="hi")) == "hi"
    assert message_text(AIMessage(content=[{"type": "text", "text": "hello"}])) == "hello"
