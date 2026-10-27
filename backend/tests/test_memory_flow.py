"""运行时侧的记忆流程：装入、收尾沉淀与压缩。出处：04_memory §6，02_window_and_compaction §1。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_runtime.core import AgentState, FailedAttempt, Message, ToolCall
from agent_runtime.memory import (
    Extraction,
    Fact,
    MemoryStore,
    SessionCompactor,
    WorkspaceMemory,
)
from agent_runtime.memory.compactor import CompactionConfig
from agent_runtime.memory.merge import MergeLimits
from agent_runtime.runtime.memory_flow import (
    MemoryFlow,
    outcome_of,
    task_was_tainted,
    touched_files,
)
from stage_support import MakeState, with_state

CFG = CompactionConfig(100, 0.8, 0.4, 2, (30,), MergeLimits(10, 5, 100, True))


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    (tmp_path / "ws").mkdir()
    s = MemoryStore(tmp_path / "m.db")
    await s.open()
    await s.create_workspace("w1", "p", str(tmp_path / "ws"))
    await s.create_session("s1", "w1")
    yield s
    await s.close()


class Ex:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def __call__(self, request: Any) -> Extraction | None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return Extraction("摘要", (), (), ())


def flow(store: MemoryStore, ex: Ex | None = None, **kw: Any) -> MemoryFlow:
    ex = ex or Ex()
    return MemoryFlow(
        store,
        SessionCompactor(store, ex, CFG, clock=lambda: 100.0),
        active_window_turns=kw.pop("active_window_turns", 2),
        max_pending=kw.pop("max_pending", 3),
        persist_task_facts=kw.pop("persist_task_facts", True),
        count_tokens=len,
        clock=lambda: 100.0,
        **kw,
    )


def finished(make_state: MakeState, **fields: Any) -> AgentState:
    return with_state(make_state(), **fields)


# ==============================================================================
# 开始：装入
# ==============================================================================


@pytest.mark.unit
async def test_a_new_session_starts_with_nothing(store: MemoryStore) -> None:
    ctx = await flow(store).prepare("s1")
    assert ctx.history == () and ctx.rolling_summary == "" and ctx.confirmed_facts == ()
    assert ctx.assets.workspace_memory == () and ctx.assets.session_summary is None


@pytest.mark.unit
async def test_history_is_the_turns_after_the_watermark_as_messages(store: MemoryStore) -> None:
    for i in range(3):
        await store.append_turn("s1", f"t{i}", "user", f"q{i}", 1)
        await store.append_turn("s1", f"t{i}", "assistant", f"a{i}", 1)
    ctx = await flow(store).prepare("s1")
    assert [(m.role, m.content) for m in ctx.history] == [
        (role, f"{mark}{i}") for i in range(3) for role, mark in (("user", "q"), ("assistant", "a"))
    ]


@pytest.mark.unit
async def test_when_few_turns_are_active_the_latest_ones_are_kept_anyway(
    store: MemoryStore,
) -> None:
    ids = []
    for i in range(3):
        ids.append(await store.append_turn("s1", f"t{i}", "user", f"q{i}", 1))
        ids.append(await store.append_turn("s1", f"t{i}", "assistant", f"a{i}", 1))
    import dataclasses

    await store.update_memory("s1", lambda m: dataclasses.replace(m, watermark=ids[-1]))
    ctx = await flow(store, active_window_turns=3).prepare("s1")
    assert [m.content for m in ctx.history] == [
        "a1",
        "q2",
        "a2",
    ]  # 保底：已压缩的原文仍保留最近 3 条


@pytest.mark.unit
async def test_memory_becomes_assets_with_the_right_trust_and_the_inherited_state(
    store: MemoryStore,
) -> None:
    import dataclasses

    fa = FailedAttempt(action="a", failure_reason="r", conclusion="c")
    from agent_runtime.memory import Failure

    await store.set_workspace_memory(
        "w1",
        WorkspaceMemory(profile=("称呼我小王",), guidelines=("先写测试",), pitfalls=("别用 X",)),
    )
    await store.update_memory(
        "s1",
        lambda m: dataclasses.replace(
            m,
            summary="摘要",
            facts=(Fact("F", "t0", "untrusted", "compressor", 1.0),),
            failures=(Failure(fa, "t0", 1.0),),
            pending_facts=(Fact("P", "t1", "trusted", "task", 2.0),),
        ),
    )
    ctx = await flow(store).prepare("s1")
    a = ctx.assets
    assert {(e.text, e.source, e.trust) for e in a.workspace_memory} == {
        ("称呼我小王", "user", "trusted"),
        ("先写测试", "user", "trusted"),
        ("别用 X", "user", "trusted"),
    }
    assert a.session_summary is not None and a.session_summary.trust == "untrusted"
    assert [(e.text, e.trust) for e in a.session_facts] == [("F", "untrusted")]
    assert a.session_failures == (fa,)
    assert [e.text for e in a.recent_facts] == ["P"]
    assert ctx.rolling_summary == "摘要" and ctx.confirmed_facts == ("F", "P")
    assert ctx.failed_attempts == (fa,)


@pytest.mark.unit
async def test_a_missing_session_is_an_error(store: MemoryStore) -> None:
    from agent_runtime.memory import SessionNotFound

    with pytest.raises(SessionNotFound):
        await flow(store).prepare("ghost")


# ==============================================================================
# 收尾
# ==============================================================================


@pytest.mark.unit
async def test_finishing_records_the_question_and_the_answer(
    store: MemoryStore, make_state: MakeState
) -> None:
    state = finished(make_state, facts=("入口 main.py",))
    report = await flow(store).finish("s1", state, delivery="做完了")
    turns = await store.turns_after("s1", 0)
    assert [(t.role, t.content, t.task_id) for t in turns] == [
        ("user", "fix the bug", "t1"),
        ("assistant", "做完了", "t1"),
    ]
    assert turns[0].token_count == len("fix the bug") and report.error is None


@pytest.mark.unit
async def test_the_canary_never_reaches_the_history(
    store: MemoryStore, make_state: MakeState
) -> None:
    state = finished(make_state)
    await flow(store).finish("s1", state, delivery="结论 canary-abc 结束")
    turns = await store.turns_after("s1", 0)
    assert "canary-abc" not in turns[1].content and "[SECURITY_REDACTED]" in turns[1].content


@pytest.mark.unit
async def test_facts_and_failures_go_to_pending_and_the_summary_stays_put(
    store: MemoryStore, make_state: MakeState
) -> None:
    fa = FailedAttempt(action="a", failure_reason="r", conclusion="c")
    state = finished(make_state, facts=("事实",), failed_attempts=(fa,))
    await flow(store).finish("s1", state, delivery="ok")
    m = await store.load_memory("s1")
    assert [f.text for f in m.pending_facts] == ["事实"] and m.pending_failures[0].attempt == fa
    assert m.facts == () and m.summary == ""


@pytest.mark.unit
async def test_facts_inherited_at_the_start_are_not_deposited_again(
    store: MemoryStore, make_state: MakeState
) -> None:
    import dataclasses

    await store.update_memory(
        "s1", lambda m: dataclasses.replace(m, facts=(Fact("旧", "t0", "trusted", "task", 1.0),))
    )
    state = finished(make_state, facts=("旧", "新"))
    await flow(store).finish("s1", state, delivery="ok")
    assert [f.text for f in (await store.load_memory("s1")).pending_facts] == ["新"]


@pytest.mark.unit
async def test_a_security_stop_deposits_no_facts(store: MemoryStore, make_state: MakeState) -> None:
    state = finished(make_state, facts=("可疑",))
    reason = "[SECURITY] Prompt leak detected via canary token"
    assert outcome_of("terminated", reason) == "failed"
    await flow(store).finish("s1", state, delivery="x", status="terminated", reason=reason)
    assert (await store.load_memory("s1")).pending_facts == ()


@pytest.mark.unit
def test_outcomes_follow_the_final_status() -> None:
    assert outcome_of("succeeded") == "success"
    assert outcome_of("terminated", "其他原因") == "terminated"
    assert outcome_of("cancelled", "用户取消") == "terminated"


@pytest.mark.unit
async def test_the_switch_can_turn_depositing_off(
    store: MemoryStore, make_state: MakeState
) -> None:
    state = finished(make_state, facts=("事实",))
    await flow(store, persist_task_facts=False).finish("s1", state, delivery="ok")
    assert (await store.load_memory("s1")).pending_facts == ()
    assert len(await store.turns_after("s1", 0)) == 2  # 流水照记


@pytest.mark.unit
async def test_a_task_that_read_untrusted_content_deposits_untrusted_facts(
    store: MemoryStore, make_state: MakeState
) -> None:
    tainted = Message(
        role="tool",
        content='<tool_observation tool="x" trust="untrusted">\n..\n</tool_observation>',
        tool_call_id="c",
        name="x",
    )
    call = Message(role="assistant", content="", tool_calls=(ToolCall(id="c", name="x", args={}),))
    state = finished(make_state, messages=(call, tainted), facts=("事实",))
    assert task_was_tainted(state.messages) is True
    await flow(store).finish("s1", state, delivery="ok")
    assert (await store.load_memory("s1")).pending_facts[0].trust == "untrusted"


@pytest.mark.unit
def test_clean_tasks_are_not_tainted() -> None:
    assert (
        task_was_tainted((Message(role="tool", content="plain", tool_call_id="c", name="bash"),))
        is False
    )
    assert task_was_tainted(()) is False


@pytest.mark.unit
def test_touched_files_come_from_write_calls_only() -> None:
    msgs = (
        Message(
            role="assistant",
            content="",
            tool_calls=(
                ToolCall(id="1", name="write_file", args={"path": "a.py", "content": "x"}),
                ToolCall(id="2", name="view_file", args={"path": "b.py"}),
                ToolCall(id="3", name="write_file", args={"path": "a.py", "content": "y"}),
                ToolCall(id="4", name="write_file", args={"path": 5}),
            ),
        ),
    )
    assert touched_files(msgs) == ("a.py",)


@pytest.mark.unit
async def test_the_last_action_target_is_recorded_when_files_were_written(
    store: MemoryStore, make_state: MakeState
) -> None:
    call = Message(
        role="assistant",
        content="",
        tool_calls=(ToolCall(id="1", name="write_file", args={"path": "a.py", "content": "x"}),),
    )
    await flow(store).finish("s1", finished(make_state, messages=(call,)), delivery="ok")
    assert (await store.load_memory("s1")).last_action_target == {"files": ("a.py",)}


@pytest.mark.unit
async def test_reaching_the_watermark_compacts_after_the_turns_are_written(
    store: MemoryStore, make_state: MakeState
) -> None:
    ex = Ex()
    f = flow(store, ex)
    for i in range(3):  # 每对 ≥ 30 个字符 -> 很快越过 80
        state = with_state(make_state(task_id=f"t{i}", task_goal="目" * 20), facts=())
        await f.finish("s1", state, delivery="答" * 20)
    assert ex.calls >= 1 and (await store.load_memory("s1")).watermark > 0


@pytest.mark.unit
async def test_overflowing_the_pending_cap_triggers_an_early_merge(
    store: MemoryStore, make_state: MakeState
) -> None:
    ex = Ex()
    state = finished(make_state, facts=("a", "b", "c", "d"))
    report = await flow(store, ex, max_pending=3).finish("s1", state, delivery="ok")
    assert ex.calls == 1 and report.compaction == "compacted"


@pytest.mark.unit
async def test_a_memory_failure_never_fails_the_task(
    store: MemoryStore, make_state: MakeState
) -> None:
    report = await flow(store).finish("ghost", finished(make_state), delivery="ok")
    assert report.error is not None  # 记下了，但没有抛


@pytest.mark.unit
async def test_a_failing_extractor_is_reported_and_leaves_the_turns_written(
    store: MemoryStore, make_state: MakeState
) -> None:
    f = flow(store, Ex(fail=True))
    state = finished(make_state, facts=("a", "b", "c", "d"))
    report = await f.finish("s1", state, delivery="ok")
    assert report.compaction == "failed" and report.error is None
    assert len(await store.turns_after("s1", 0)) == 2
