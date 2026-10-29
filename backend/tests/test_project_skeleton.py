"""骨架级冒烟测试：包可导入、参考配置自洽、密钥不入库。

这些测试只依赖标准库，因此在依赖尚未安装时也能运行。
"""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


# ==============================================================================
# 包结构
# ==============================================================================


def _package_names() -> list[str]:
    names = []
    for init in sorted(SRC.rglob("__init__.py")):
        parts = init.parent.relative_to(SRC).parts
        names.append(".".join(parts))
    return names


@pytest.mark.unit
@pytest.mark.parametrize("name", _package_names())
def test_every_package_is_importable(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__, f"{name} 缺少模块文档字符串，说明它的职责"


@pytest.mark.unit
def test_expected_top_level_packages_exist() -> None:
    top = {n.split(".")[0] for n in _package_names()}
    assert top == {
        "baseagent_config",
        "agent_runtime",
        "tools",
        "mcps",
        "services",
        "skills",
        "evaluation",
    }


@pytest.mark.unit
def test_no_removed_dependencies_are_imported() -> None:
    """LangGraph、LangChain、RAG 相关的包不应再出现在源码里。"""
    banned = re.compile(
        r"^\s*(?:from|import)\s+(langgraph|langchain|langchain_core|qdrant_client|fastembed)\b",
        re.M,
    )
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if banned.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, offenders


# ==============================================================================
# 参考配置
# ==============================================================================


@pytest.fixture(scope="module")
def cfg(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


@pytest.mark.unit
def test_services_listen_on_loopback_only(cfg: dict[str, Any]) -> None:
    for section in ("server", "bash_shell", "web_search"):
        assert cfg[section]["host"] in LOOPBACK, section


@pytest.mark.unit
@pytest.mark.parametrize("tier", ["reasoning", "fast"])
def test_each_model_tier_has_complete_endpoints(cfg: dict[str, Any], tier: str) -> None:
    endpoints = cfg["models"][tier]["endpoints"]
    assert endpoints, tier
    required = {
        "name",
        "base_url",
        "model",
        "api_key_env",
        "timeout_sec",
        "context_window",
        "max_output_tokens",
    }
    for endpoint in endpoints:
        assert required <= endpoint.keys(), (tier, required - endpoint.keys())
        assert endpoint["context_window"] > endpoint["max_output_tokens"] > 0
        assert "capabilities" in endpoint


@pytest.mark.unit
def test_threshold_relationships(cfg: dict[str, Any]) -> None:
    session = cfg["context"]["session"]
    assert 0 < session["compaction_ratio"] < session["high_watermark"] < 1
    task = cfg["context"]["task"]
    assert 0 < task["soft_ratio"] < task["hard_ratio"] < 1
    assert task["soft_tokens"] < task["hard_tokens"]
    assert cfg["bash_shell"]["timeout_sec"] <= cfg["bash_shell"]["max_timeout_sec"]


@pytest.mark.unit
def test_mcp_call_timeout_is_below_tool_dispatch_timeout(cfg: dict[str, Any]) -> None:
    """让 MCP 管理器的超时先咬合，而不是被派发器取消。"""
    assert cfg["mcp"]["call_timeout_sec"] < cfg["tools"]["dispatch"]["default_timeout_sec"]


@pytest.mark.unit
def test_subagent_cannot_delegate_recursively(cfg: dict[str, Any]) -> None:
    assert "spawn_subagent" in cfg["subagent"]["denied_tools"]
    assert cfg["subagent"]["max_depth"] >= 0


@pytest.mark.unit
def test_removed_features_are_not_configured(cfg: dict[str, Any]) -> None:
    assert "rag_url" not in cfg["services"]
    assert "code_search" not in cfg
    flat = repr(cfg)
    assert "rag_search" not in flat
    assert "delegate_code_search" not in flat


@pytest.mark.unit
def test_dangerous_defaults_are_off(cfg: dict[str, Any]) -> None:
    assert cfg["skills"]["allow_workspace"] is False
    assert cfg["context"]["rules"]["allow_workspace"] is False
    assert cfg["tracing"]["export_content"] is False
    assert cfg["tracing"]["otlp"]["enabled"] is False
    assert cfg["tracing"]["langfuse"]["enabled"] is False
    assert cfg["server"]["auth"]["enabled"] is True
    assert cfg["web_search"]["block_private_addresses"] is True
    assert all(not s["enabled"] for s in cfg["mcp"]["servers"].values())


@pytest.mark.unit
def test_permission_patterns_compile(cfg: dict[str, Any]) -> None:
    for key in ("full_permission_patterns", "workspace_write_patterns"):
        for pattern in cfg["permissions"][key]:
            re.compile(pattern, re.IGNORECASE)


# ==============================================================================
# 密钥与环境变量
# ==============================================================================


@pytest.mark.unit
def test_no_secret_like_values_in_config(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8")
    # 脱敏规则里的正则本身会含有这些前缀，先剔除
    scrubbed = re.sub(r"value_patterns\s*=\s*\[.*?\]", "", text, flags=re.S)
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", scrubbed)
    assert not re.search(r"ghp_[A-Za-z0-9]{20,}", scrubbed)
    assert not re.search(r"AKIA[0-9A-Z]{16}", scrubbed)


@pytest.mark.unit
def test_every_env_reference_is_documented_in_env_example(
    repo_root: Path, config_path: Path
) -> None:
    text = config_path.read_text(encoding="utf-8")
    ref_keys = "api_key_env|token_env|headers_env|public_key_env|secret_key_env"
    refs = set(re.findall(rf'(?:{ref_keys})\s*=\s*"([A-Z][A-Z0-9_]*)"', text))
    refs |= set(re.findall(r'"env:([A-Z][A-Z0-9_]*)"', text))
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    missing = sorted(name for name in refs if name not in example)
    assert not missing, missing


@pytest.mark.unit
def test_gitignore_protects_secrets(repo_root: Path) -> None:
    content = (repo_root / ".gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in content.splitlines()}
    assert ".env" in lines
    assert "!.env.example" in lines
    assert "storage/" in lines
