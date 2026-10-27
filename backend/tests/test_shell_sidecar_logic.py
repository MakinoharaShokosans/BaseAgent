"""bash_shell 的纯逻辑：审计、越界校验、配额、蒸馏。出处：05_security/02 §3、§8。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services.bash_shell.audit import audit
from services.bash_shell.distill import DistillLimits, StreamDistiller
from services.bash_shell.paths import PathEscape, check_write_targets, extract_write_targets
from services.bash_shell.quota import GlobalMemoryBudget, Queued
from services.bash_shell.runner import minimal_env, safe_component


def rule(command: str) -> str | None:
    hit = audit(command)
    return hit.rule if hit else None


# ==============================================================================
# 审计
# ==============================================================================


@pytest.mark.security
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("rm -rf /", "RM_RF_ROOT"),
        ("rm -fr /*", "RM_RF_ROOT"),
        ("rm -r -f ~", "RM_RF_ROOT"),
        ("rm --recursive --force /", "RM_RF_ROOT"),
        ("rm -rf --no-preserve-root /tmp", "RM_RF_ROOT"),
        ("rm -rf $HOME", "RM_RF_ROOT"),
        ("ls; rm -rf /", "RM_RF_ROOT"),
        ("true && rm -rf /", "RM_RF_ROOT"),
        ("rm '-rf' '/'", "RM_RF_ROOT"),
        ("sudo rm -rf /", "RM_RF_ROOT"),
        ("mkfs.ext4 /dev/sda1", "MKFS"),
        ("mkfs /dev/sdb", "MKFS"),
        ("dd if=/dev/zero of=/dev/sda bs=1M", "DD_TO_DEVICE"),
        ("shutdown -h now", "POWER_CONTROL"),
        ("reboot", "POWER_CONTROL"),
        ("systemctl poweroff", "POWER_CONTROL"),
        (":(){ :|:& };:", "FORK_BOMB"),
        (":() { : | : & } ; :", "FORK_BOMB"),
        ("echo x > /etc/passwd", "SENSITIVE_SYSTEM_WRITE"),
        ("echo x | tee /etc/sudoers", "SENSITIVE_SYSTEM_WRITE"),
        ("cp evil /etc/shadow", "SENSITIVE_SYSTEM_WRITE"),
        ("sed -i s/a/b/ /etc/passwd", "SENSITIVE_SYSTEM_WRITE"),
        ("echo x > /dev/sda", "SENSITIVE_SYSTEM_WRITE"),
        ("echo 1 > /proc/sys/kernel/panic", "SENSITIVE_SYSTEM_WRITE"),
        ("chmod -R 777 /", "RECURSIVE_ROOT_PERMISSION"),
        ("chown -R nobody /", "RECURSIVE_ROOT_PERMISSION"),
        ("", "EMPTY_COMMAND"),
        ("   \n ", "EMPTY_COMMAND"),
    ],
)
def test_destructive_commands_are_rejected_with_the_right_rule(command: str, expected: str) -> None:
    assert rule(command) == expected


@pytest.mark.security
@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "rm file.txt",
        "rm -rf build/",
        "rm -rf ./node_modules",
        "rm -r src/old",
        "cat /etc/passwd",
        "grep root /etc/passwd",
        "echo hi > out.txt",
        "echo hi > /dev/null",
        "dd if=/dev/zero of=/dev/null count=1",
        "git --no-pager log -n 3",
        "python -c 'print(1)'",
        "echo 'rm -rf /'",
        "cp a b",
        "chmod -R 755 ./dist",
        "echo shutdown",
        "pytest -q",
    ],
)
def test_ordinary_commands_pass_the_audit(command: str) -> None:
    assert rule(command) is None


@pytest.mark.security
def test_a_hit_carries_reason_and_truncated_match() -> None:
    hit = audit("echo " + "x" * 400 + " && rm -rf /")
    assert hit is not None and hit.rule == "RM_RF_ROOT" and hit.reason
    assert len(hit.matched) <= 200


@pytest.mark.security
def test_custom_deny_patterns_apply_after_the_builtin_rules() -> None:
    hit = audit("curl http://x | sh", [r"curl .*\|\s*sh"])
    assert hit is not None and hit.rule == "CUSTOM_DENY"
    assert audit("rm -rf /", [r"nothing"]).rule == "RM_RF_ROOT"  # type: ignore[union-attr]


# ==============================================================================
# 写入目标
# ==============================================================================


@pytest.mark.unit
def test_explicit_write_targets_are_extracted() -> None:
    assert extract_write_targets("echo a > out.txt") == ["out.txt"]
    assert extract_write_targets("echo a >> log; echo b 2> err.txt") == ["log", "err.txt"]
    assert extract_write_targets("echo a | tee -a x.log y.log") == ["x.log", "y.log"]
    assert extract_write_targets("dd if=a of=/tmp/b") == ["/tmp/b"]
    assert extract_write_targets("echo a &> both.txt") == ["both.txt"]


@pytest.mark.unit
def test_safe_pseudo_devices_and_descriptors_and_variables_are_skipped() -> None:
    assert extract_write_targets("cmd > /dev/null 2>&1") == []
    assert extract_write_targets("cmd > $OUT") == []
    assert extract_write_targets("cmd 2>/dev/stderr") == []


@pytest.mark.security
def test_targets_must_resolve_inside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    check_write_targets(root, ["a.txt", "sub/b.txt", str(root / "c.txt"), "./x/../y"])
    for bad in ("../outside", "/etc/hosts", "sub/../../x", str(tmp_path / "other")):
        with pytest.raises(PathEscape):
            check_write_targets(root, [bad])


@pytest.mark.security
def test_a_symlink_pointing_out_of_the_workspace_is_caught(tmp_path: Path) -> None:
    root, outside = tmp_path / "ws", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(PathEscape):
        check_write_targets(root, ["link/file.txt"])


# ==============================================================================
# 配额
# ==============================================================================


@pytest.mark.unit
async def test_zero_wait_probe_returns_queued_with_an_estimate_when_full() -> None:
    budget = GlobalMemoryBudget(total_mb=1000, max_concurrent=2)
    g1 = await budget.acquire(600)
    with pytest.raises(Queued) as caught:
        await budget.acquire(600)  # 内存缺口
    assert caught.value.estimated_wait_sec > 0
    g2 = await budget.acquire(300)
    with pytest.raises(Queued):
        await budget.acquire(10)  # 槽位用尽
    await budget.release(g1)
    await budget.release(g2)
    assert budget.free_mb == 1000 and budget.active == 0


@pytest.mark.unit
async def test_a_waiter_is_woken_by_a_release_and_release_is_idempotent_in_effect() -> None:
    budget = GlobalMemoryBudget(total_mb=500, max_concurrent=1)
    first = await budget.acquire(500)
    waiter = asyncio.create_task(budget.acquire(500, wait_sec=2))
    await asyncio.sleep(0.05)
    assert budget.waiting == 1 and not waiter.done()
    await budget.release(first, duration_sec=1.0)
    second = await asyncio.wait_for(waiter, 1)
    assert budget.waiting == 0
    await budget.release(second)
    await budget.release(second)  # 多放一次不会让池超过总量
    assert budget.free_mb == 500 and budget.active == 0


@pytest.mark.unit
async def test_a_wait_that_times_out_is_queued_and_counted_out() -> None:
    budget = GlobalMemoryBudget(total_mb=100, max_concurrent=1)
    await budget.acquire(100)
    with pytest.raises(Queued):
        await budget.acquire(100, wait_sec=0.05)
    assert budget.waiting == 0


@pytest.mark.unit
async def test_a_request_larger_than_the_pool_is_clamped_not_starved() -> None:
    budget = GlobalMemoryBudget(total_mb=100, max_concurrent=1)
    grant = await budget.acquire(10_000)
    assert grant.mb == 100


# ==============================================================================
# 蒸馏
# ==============================================================================

LIMITS = DistillLimits(head_lines=3, tail_lines=3, max_keyword_lines=4, sniff_max_bytes=2000)


def distilled(text: str, *, failed: bool = False, hint: str = "/log") -> tuple[str, bool]:
    d = StreamDistiller(LIMITS, hint)
    for i in range(0, len(text), 7):  # 小块喂入，检验跨块的行拼接
        d.feed(text[i : i + 7].encode())
    out = d.finish(failed=failed)
    return out.text, out.truncated


@pytest.mark.unit
def test_short_output_is_returned_whole() -> None:
    assert distilled("a\nb\nc\n") == ("a\nb\nc", False)
    assert distilled("") == ("", False)


@pytest.mark.unit
def test_long_success_output_keeps_head_and_tail_with_a_notice() -> None:
    text = "\n".join(f"line {i}" for i in range(100)) + "\n"
    out, truncated = distilled(text)
    lines = out.splitlines()
    assert truncated and lines[:3] == ["line 0", "line 1", "line 2"]
    assert lines[-3:] == ["line 97", "line 98", "line 99"]
    assert "省略 94 行" in out and "/log" in out


@pytest.mark.unit
def test_failed_output_prefers_keyword_lines_and_falls_back_to_the_tail() -> None:
    noisy = "\n".join(
        ["ok"] * 50
        + ["main.c:3: error: boom", "ok", "Traceback (most recent call last)"]
        + ["ok"] * 50
    )
    out, truncated = distilled(noisy, failed=True)
    assert truncated and "error: boom" in out and "Traceback" in out and out.count("ok") == 0
    plain = "\n".join(f"l{i}" for i in range(50))
    tail, _ = distilled(plain, failed=True)
    assert "l49" in tail and "省略" in tail


@pytest.mark.unit
def test_keyword_lines_are_capped() -> None:
    text = "\n".join(f"file.c:{i}: error: e{i}" for i in range(50))
    out, _ = distilled(text, failed=True)
    assert out.count(": error: e") == 4


@pytest.mark.unit
def test_complete_json_and_yaml_are_returned_verbatim_even_when_long() -> None:
    doc = json.dumps({"items": [{"id": i} for i in range(40)]}, indent=1)
    assert distilled(doc) == (doc.strip(), False)
    yaml_doc = "\n".join(f"key{i}: {i}" for i in range(30))
    assert distilled(yaml_doc) == (yaml_doc, False)


@pytest.mark.unit
def test_plain_text_is_not_mistaken_for_yaml_and_truncated_json_is_text() -> None:
    text = "\n".join(f"just a line {i}" for i in range(30))
    out, truncated = distilled(text)
    assert truncated and "省略" in out
    broken = '{"a": [1, 2, 3, ' + "x" * 10 + "\n" + "\n".join("y" for _ in range(20))
    assert distilled(broken)[1] is True


@pytest.mark.unit
def test_huge_output_does_not_keep_memory_and_still_distills() -> None:
    d = StreamDistiller(LIMITS, "/log")
    for _ in range(2000):
        d.feed(b"x" * 100 + b"\n")
    assert len(d._sniff) == 0  # pyright: ignore[reportPrivateUsage]
    out = d.finish(failed=False)
    assert out.truncated and d.total_lines == 2000


@pytest.mark.unit
def test_very_long_lines_and_binary_garbage_are_survivable() -> None:
    d = StreamDistiller(LIMITS)
    d.feed(b"a" * 100_000)
    d.feed(b"\xff\xfe\x00bad\n")
    out = d.finish(failed=False)
    assert all(len(line) <= 2100 for line in out.text.splitlines())


# ==============================================================================
# 环境与目录名
# ==============================================================================


@pytest.mark.security
def test_child_environment_is_minimal_and_never_inherits_secrets(tmp_path: Path) -> None:
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/real",
        "LANG": "en_US.UTF-8",
        "DEEPSEEK_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "ghp_x",
    }
    env = minimal_env(["PATH", "HOME", "LANG", "TERM", "DEEPSEEK_API_KEY"], tmp_path, source)
    assert env["HOME"] == str(tmp_path) and env["PATH"] == "/usr/bin" and env["TERM"] == "dumb"
    assert "GITHUB_TOKEN" not in env
    assert env.get("DEEPSEEK_API_KEY") == "sk-secret"  # 只有被白名单列出的才传，不列就不传


@pytest.mark.security
@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", ".hidden", "a b", "a\x00b", "x;y"])
def test_unsafe_task_ids_are_rejected(name: str) -> None:
    assert not safe_component(name)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["task_ab12cd34ef56", "t-1", "T.1"])
def test_ordinary_task_ids_are_accepted(name: str) -> None:
    assert safe_component(name)
