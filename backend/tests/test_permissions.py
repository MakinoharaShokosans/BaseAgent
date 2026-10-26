"""权限判定（纯函数）。出处：docs/05_security/01_permission_tiers.md §3、§5、§10。"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.guardrails import PermissionPolicy, action_signature, check_permission

POLICY = PermissionPolicy(
    read_only_tools=frozenset({"view_file", "grep", "load_skill"}),
    workspace_write_tools=frozenset({"write_file"}),
    full_permission_tools=frozenset({"deploy"}),
    bash_tools=frozenset({"bash"}),
    full_permission_patterns=(
        r"\bgit\s+push\b",
        r"\b(curl|wget)\b",
        r"\bpip3?\s+install\b",
        r"\bsudo\b",
        r"\|\s*(sh|bash)\b",
    ),
    workspace_write_patterns=(r">>?\s*\S", r"\b(rm|mv|cp|mkdir)\b", r"\bpytest\b"),
    unknown_tool_level="full_permissions",
)


def check(level: str, tool: str, **args: Any) -> Any:
    return check_permission(POLICY, level, tool, args)


# ==============================================================================
# 工具名单
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool", "required", "action"),
    [
        ("view_file", "read_only", "read"),
        ("write_file", "workspace_write", "workspace_write"),
        ("deploy", "full_permissions", "privileged"),
    ],
)
def test_explicit_tool_lists_decide_the_required_level(
    tool: str, required: str, action: str
) -> None:
    decision = check("read_only", tool)
    assert decision.required_level == required
    assert decision.action_type == action


@pytest.mark.unit
def test_a_higher_baseline_allows_what_a_lower_one_needs_approval_for() -> None:
    assert check("read_only", "write_file").allowed is False
    assert check("workspace_write", "write_file").allowed is True
    assert check("full_permissions", "write_file").allowed is True
    assert check("workspace_write", "deploy").allowed is False


@pytest.mark.unit
def test_the_levels_are_totally_ordered() -> None:
    for level, expected in [("read_only", 1), ("workspace_write", 2), ("full_permissions", 3)]:
        allowed = [t for t in ("view_file", "write_file", "deploy") if check(level, t).allowed]
        assert len(allowed) == expected


@pytest.mark.unit
def test_explicit_lists_win_over_everything_else() -> None:
    wide = PermissionPolicy(
        read_only_tools=frozenset({"bash"}),
        workspace_write_tools=frozenset(),
        full_permission_tools=frozenset(),
        bash_tools=frozenset({"bash"}),
        full_permission_patterns=(r"\bcurl\b",),
        workspace_write_patterns=(),
        unknown_tool_level="full_permissions",
    )
    assert check_permission(wide, "read_only", "bash", {"command": "curl x"}).allowed is True


@pytest.mark.unit
def test_unknown_tools_get_the_conservative_level() -> None:
    decision = check("workspace_write", "mcp_something")
    assert decision.required_level == "full_permissions"
    assert decision.action_type == "unknown"
    assert decision.allowed is False


@pytest.mark.unit
def test_unknown_tool_level_is_configurable() -> None:
    lax = PermissionPolicy(
        read_only_tools=frozenset(),
        workspace_write_tools=frozenset(),
        full_permission_tools=frozenset(),
        bash_tools=frozenset(),
        full_permission_patterns=(),
        workspace_write_patterns=(),
        unknown_tool_level="read_only",
    )
    assert check_permission(lax, "read_only", "x", {}).allowed is True


# ==============================================================================
# bash 命令分类
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "required", "action"),
    [
        ("ls -la", "read_only", "read"),
        ("cat a.txt | grep x", "read_only", "read"),
        ("rm -rf build", "workspace_write", "workspace_write"),
        ("echo hi > out.txt", "workspace_write", "workspace_write"),
        ("pytest -q", "workspace_write", "workspace_write"),
        ("git push origin main", "full_permissions", "network_egress"),
        ("curl https://x.sh", "full_permissions", "network_egress"),
        ("pip install requests", "full_permissions", "global_env"),
        ("sudo ls", "full_permissions", "global_env"),
    ],
)
def test_bash_is_classified_by_its_command_text(command: str, required: str, action: str) -> None:
    decision = check("read_only", "bash", command=command)
    assert (decision.required_level, decision.action_type) == (required, action)


@pytest.mark.unit
def test_the_higher_pattern_group_wins_when_both_match() -> None:
    assert check("read_only", "bash", command="rm x && git push").required_level == (
        "full_permissions"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "GIT PUSH origin main",
        "  git   push  ",
        "ls; curl http://x",
        "ls && wget http://x",
        "echo a | sh",
        "echo $(curl x)",
        "git\tpush",
    ],
)
def test_case_whitespace_and_shell_joiners_do_not_hide_a_dangerous_command(command: str) -> None:
    assert check("workspace_write", "bash", command=command).allowed is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "python -c \"import urllib.request as u; u.urlopen('http://x')\"",
        "node -e \"require('https').get('http://x')\"",
        "bash x.sh",  # 脚本内容里的 curl 看不见：先 write_file 再执行
        "./run.sh",
        'eval "$(echo Y3VybCB4 | base64 -d)"',
    ],
)
def test_known_gaps_are_pinned_so_nobody_mistakes_them_for_coverage(command: str) -> None:
    """已知防不住（见 §4）：正则分类不是语义分析。真正的边界在沙箱。

    这些用例固定“当前被判为只读”这一事实；若日后补上缓解，这里应当随之改为期望越级。
    """
    assert check("read_only", "bash", command=command).allowed is True


@pytest.mark.unit
def test_an_empty_or_missing_command_is_read_only() -> None:
    assert check("read_only", "bash").required_level == "read_only"
    assert check("read_only", "bash", command="").required_level == "read_only"


@pytest.mark.unit
def test_a_non_string_command_does_not_crash_the_classifier() -> None:
    assert check("read_only", "bash", command=["curl", "x"]).required_level in {
        "read_only",
        "full_permissions",
    }


@pytest.mark.unit
def test_an_invalid_pattern_is_skipped_without_breaking_the_rest() -> None:
    policy = PermissionPolicy(
        read_only_tools=frozenset(),
        workspace_write_tools=frozenset(),
        full_permission_tools=frozenset(),
        bash_tools=frozenset({"bash"}),
        full_permission_patterns=("([unclosed", r"\bcurl\b"),
        workspace_write_patterns=(),
        unknown_tool_level="full_permissions",
    )
    assert check_permission(policy, "read_only", "bash", {"command": "curl x"}).allowed is False
    assert check_permission(policy, "read_only", "bash", {"command": "ls"}).allowed is True


# ==============================================================================
# 结论内容
# ==============================================================================


@pytest.mark.unit
def test_a_denied_decision_explains_the_gap() -> None:
    decision = check("workspace_write", "bash", command="curl https://x")
    assert decision.allowed is False
    assert decision.current_level == "workspace_write"
    assert "full_permissions" in decision.reason
    assert "workspace_write" in decision.reason


@pytest.mark.unit
def test_the_summary_for_bash_is_the_command_capped_at_200_chars() -> None:
    assert check("read_only", "bash", command="ls -la").action_summary == "ls -la"
    assert len(check("read_only", "bash", command="x" * 500).action_summary) == 200
    assert check("read_only", "bash", command="  ").action_summary == "(空命令)"


@pytest.mark.unit
def test_the_summary_for_other_tools_lists_a_few_arguments() -> None:
    summary = check("read_only", "write_file", path="a.py", content="x" * 500).action_summary
    assert summary.startswith("write_file(path=a.py")
    assert len(summary) <= 200
    assert check("read_only", "view_file").action_summary == "view_file"


@pytest.mark.unit
def test_an_unrecognized_level_falls_back_to_workspace_write() -> None:
    assert check("god_mode", "write_file").allowed is True
    assert check("god_mode", "deploy").allowed is False


@pytest.mark.unit
def test_the_decision_is_deterministic() -> None:
    assert check("read_only", "bash", command="git push") == check(
        "read_only", "bash", command="git push"
    )


# ==============================================================================
# 签名
# ==============================================================================


@pytest.mark.unit
def test_signature_ignores_argument_order_but_not_values() -> None:
    a = action_signature("bash", {"command": "ls", "timeout": 5})
    assert a == action_signature("bash", {"timeout": 5, "command": "ls"})
    assert a != action_signature("bash", {"command": "ls", "timeout": 6})
    assert a != action_signature("sh", {"command": "ls", "timeout": 5})
    assert len(a) == 16


@pytest.mark.unit
def test_signatures_distinguish_calls_that_only_differ_in_the_target() -> None:
    assert action_signature("bash", {"command": "git push origin main"}) != action_signature(
        "bash", {"command": "git push origin dev"}
    )


@pytest.mark.unit
def test_signing_never_fails_on_odd_values() -> None:
    from pathlib import Path

    assert action_signature("t", {"p": Path("/x"), "n": None, "s": {1, 2}})
