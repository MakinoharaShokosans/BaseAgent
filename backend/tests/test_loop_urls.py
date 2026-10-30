"""websearch 的网址来源校验。出处：docs/04_tools/07_websearch_tool.md。"""

from __future__ import annotations

import pytest

from agent_runtime.core import Message, ToolCall, new_state
from agent_runtime.loop.urls import canonical, check_urls, known_urls, urls_in


def state_with(*messages: Message):  # type: ignore[no-untyped-def]
    return new_state(
        workspace_id="w",
        workspace_path="/w",
        session_id="s",
        task_id="t",
        task_goal="g",
        permission_level="read_only",
        canary_token="c",
        messages=list(messages),
    )


def call(urls: object, name: str = "websearch") -> ToolCall:
    return ToolCall(id="c1", name=name, args={"urls": urls})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("HTTPS://Docs.Example/a/", "https://docs.example/a"),
        ("https://docs.example:443/a#frag", "https://docs.example/a"),
        ("http://docs.example:80/", "http://docs.example"),
    ],
)
def test_the_same_page_written_differently_is_the_same_url(a: str, b: str) -> None:
    assert canonical(a) == canonical(b)


@pytest.mark.unit
def test_the_query_string_is_part_of_the_identity() -> None:
    assert canonical("https://a.example/p?x=1") != canonical("https://a.example/p?x=2")
    assert canonical("https://a.example/p?x=1") != canonical("https://a.example/p")


@pytest.mark.unit
def test_urls_are_found_in_prose_without_trailing_punctuation() -> None:
    text = "看 https://a.example/x，还有（https://b.example/y?q=1）以及 https://c.example/z。"
    assert urls_in(text) == {
        "https://a.example/x",
        "https://b.example/y?q=1",
        "https://c.example/z",
    }


@pytest.mark.security
def test_user_and_tool_outputs_are_valid_sources_while_assistant_is_excluded() -> None:
    state = state_with(
        Message(role="user", content="请读 https://user.example/doc"),
        Message(role="assistant", content="我去读 https://model.example/mine"),
        Message(
            role="tool", name="websearch", content="来源：https://found.example/p", tool_call_id="1"
        ),
        Message(
            role="tool",
            name="bash",
            content="echo https://laundered.example/?k=secret",
            tool_call_id="2",
        ),
        Message(
            role="tool",
            name="view_file",
            content="see https://file.example/planted",
            tool_call_id="3",
        ),
    )
    assert known_urls(state) == {
        "https://user.example/doc",
        "https://found.example/p",
        "https://laundered.example/?k=secret",
        "https://file.example/planted",
    }


@pytest.mark.security
def test_a_url_with_extra_data_appended_is_not_the_url_the_user_gave() -> None:
    state = state_with(Message(role="user", content="读 https://docs.example/a"))
    assert check_urls(state, call(["https://docs.example/a"])) is None
    leaked = check_urls(state, call(["https://docs.example/a?k=sk-secret"]))
    assert leaked is not None and "不能读取" in leaked
    other_host = check_urls(state, call(["https://docs.example.evil.com/a"]))
    assert other_host is not None


@pytest.mark.unit
def test_other_tools_and_calls_without_urls_are_not_checked() -> None:
    state = state_with()
    assert check_urls(state, call(["https://x.example"], name="bash")) is None
    assert check_urls(state, ToolCall(id="c", name="websearch", args={"query": "x"})) is None
    assert check_urls(state, call([])) is None
    assert check_urls(state, call("not a list")) is None  # 类型错误交给工具报
    assert check_urls(state, call([1])) is None


@pytest.mark.unit
def test_every_url_in_a_call_must_have_a_source() -> None:
    state = state_with(Message(role="user", content="https://a.example/1"))
    problem = check_urls(state, call(["https://a.example/1", "https://b.example/2"]))
    assert problem is not None and "b.example" in problem
