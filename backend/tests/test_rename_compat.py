"""改名（Aegis → BaseAgent）后的兼容：旧环境变量、旧数据目录、旧命令名、旧技能目录都还能用。

原则：旧的永远还能用，新的永远优先，从不替用户搬数据。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from agent_runtime.api import provision_token
from agent_runtime.runtime.assembly import _default_global_skills_dir
from baseagent_config import ConfigError, env_value, load_settings, pick_home
from baseagent_config.envcompat import legacy_env_notes
from evaluation.agent_bench.cli import read_token

BACKEND = Path(__file__).resolve().parents[1]
MINIMAL = """
[models.reasoning]
[[models.reasoning.endpoints]]
name = "r"
base_url = "https://api.example.com"
model = "m-big"
api_key_env = "TEST_API_KEY"
timeout_sec = 60
context_window = 100_000
max_output_tokens = 2048

[models.fast]
[[models.fast.endpoints]]
name = "f"
base_url = "https://api.example.com"
model = "m-small"
api_key_env = "TEST_API_KEY"
timeout_sec = 60
context_window = 100_000
max_output_tokens = 2048
"""


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """干净的环境，HOME 指向临时目录（``~`` 展开因此不碰真实的用户目录）。"""
    for name in list(os.environ):
        if name.startswith(("BASEAGENT_", "AEGIS_")):
            monkeypatch.delenv(name)
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)
    return home


def load(tmp_path: Path, **kw: object):  # type: ignore[no-untyped-def]
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL, encoding="utf-8")
    return load_settings(config_path=path, **kw)  # type: ignore[arg-type]


# ==============================================================================
# 环境变量
# ==============================================================================


@pytest.mark.unit
def test_the_old_prefix_still_works_and_the_new_one_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BASEAGENT_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("AEGIS_SERVER__PORT", "2001")
    assert load(tmp_path).settings.server.port == 2001
    monkeypatch.setenv("BASEAGENT_SERVER__PORT", "2002")
    assert load(tmp_path).settings.server.port == 2002


@pytest.mark.unit
def test_old_override_names_are_validated_like_new_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BASEAGENT_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("AEGIS_SERVER__PROT", "9000")
    with pytest.raises(ConfigError, match="AEGIS_SERVER__PROT"):
        load(tmp_path)


@pytest.mark.unit
def test_using_old_names_produces_a_rename_hint_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BASEAGENT_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("AEGIS_API_TOKEN", "super-secret-token")
    monkeypatch.setenv("AEGIS_SERVER__PORT", "2001")
    monkeypatch.setenv("BASEAGENT_SERVER__PORT", "2002")  # 已经改过的不再提示
    text = "\n".join(load(tmp_path).warnings)
    assert "AEGIS_API_TOKEN 已改名为 BASEAGENT_API_TOKEN" in text
    assert "AEGIS_SERVER__PORT" not in text
    assert "super-secret-token" not in text
    assert legacy_env_notes({"BASEAGENT_X": "1"}) == []


@pytest.mark.unit
def test_env_value_prefers_the_new_name() -> None:
    assert env_value("HOME", {}) is None
    assert env_value("HOME", {"AEGIS_HOME": "old"}) == "old"
    assert env_value("HOME", {"AEGIS_HOME": "old", "BASEAGENT_HOME": "new"}) == "new"
    assert env_value("HOME", {"BASEAGENT_HOME": ""}) == ""  # 空字符串也算设置过


@pytest.mark.unit
def test_the_old_config_and_home_variables_are_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "elsewhere.toml"
    cfg.write_text(MINIMAL, encoding="utf-8")
    monkeypatch.setenv("AEGIS_CONFIG", str(cfg))
    monkeypatch.setenv("AEGIS_HOME", str(tmp_path / "old_home"))
    loaded = load_settings()
    assert loaded.config_path == cfg and loaded.home == tmp_path / "old_home"
    monkeypatch.setenv("BASEAGENT_HOME", str(tmp_path / "new_home"))
    assert load_settings().home == tmp_path / "new_home"


@pytest.mark.unit
@pytest.mark.security
def test_the_old_token_variable_still_provides_the_api_token(tmp_path: Path) -> None:
    env = {"AEGIS_API_TOKEN": " legacy "}
    assert provision_token(tmp_path / "t", env=env, env_name="BASEAGENT_API_TOKEN") == "legacy"
    both = {**env, "BASEAGENT_API_TOKEN": "fresh"}
    assert provision_token(tmp_path / "t2", env=both, env_name="BASEAGENT_API_TOKEN") == "fresh"
    assert not (tmp_path / "t").exists()  # 环境变量给了就不生成文件


@pytest.mark.unit
@pytest.mark.security
def test_bench_reads_the_old_token_variable_and_never_prints_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AEGIS_API_TOKEN", "legacy-token")
    assert read_token(None, None) == "legacy-token"
    monkeypatch.setenv("BASEAGENT_API_TOKEN", "new-token")
    assert read_token(None, None) == "new-token"
    assert capsys.readouterr().out == ""


# ==============================================================================
# 数据目录
# ==============================================================================


@pytest.mark.unit
def test_a_fresh_install_uses_the_new_data_directory(clean: Path, tmp_path: Path) -> None:
    loaded = load(tmp_path, create_home=False)
    assert loaded.home == clean / ".local/share/baseagent"
    assert not any("旧数据目录" in w for w in loaded.warnings)


@pytest.mark.unit
def test_the_old_data_directory_is_kept_when_the_new_one_does_not_exist(
    clean: Path, tmp_path: Path
) -> None:
    old = clean / ".local/share/aegis"
    old.mkdir(parents=True)
    (old / "aegis_meta.db").write_bytes(b"")  # 旧数据库
    loaded = load(tmp_path)
    assert loaded.home == old
    assert loaded.paths.metadata_db == old / "aegis_meta.db"  # 不另起新库、不丢数据
    assert any("沿用旧数据目录" in w and "mv" in w for w in loaded.warnings)
    assert not (clean / ".local/share/baseagent").exists()  # 从不替用户搬或建


@pytest.mark.unit
def test_once_the_new_directory_exists_it_wins(clean: Path, tmp_path: Path) -> None:
    (clean / ".local/share/aegis").mkdir(parents=True)
    (clean / ".local/share/baseagent").mkdir(parents=True)
    loaded = load(tmp_path)
    assert loaded.home == clean / ".local/share/baseagent"
    assert loaded.paths.metadata_db == loaded.home / "baseagent_meta.db"


@pytest.mark.unit
def test_pick_home_follows_the_same_rules(clean: Path) -> None:
    assert pick_home({}) == clean / ".local/share/baseagent"
    (clean / ".local/share/aegis").mkdir(parents=True)
    assert pick_home({}) == clean / ".local/share/aegis"
    assert pick_home({"AEGIS_HOME": "/x"}) == Path("/x")
    assert pick_home({"AEGIS_HOME": "/x", "BASEAGENT_HOME": "/y"}) == Path("/y")


@pytest.mark.unit
def test_an_old_config_file_that_names_the_old_directory_keeps_working(
    clean: Path, tmp_path: Path
) -> None:
    body = MINIMAL + '\n[app]\nhome = "~/.local/share/aegis"\n'
    path = tmp_path / "old.toml"
    path.write_text(body, encoding="utf-8")
    assert load_settings(config_path=path).home == clean / ".local/share/aegis"


# ==============================================================================
# 技能与规则目录、命令名
# ==============================================================================


@pytest.mark.unit
def test_the_global_skills_directory_falls_back_to_the_old_one(clean: Path) -> None:
    assert _default_global_skills_dir() == clean / ".baseagent/skills"
    (clean / ".aegis/skills").mkdir(parents=True)
    assert _default_global_skills_dir() == clean / ".aegis/skills"
    (clean / ".baseagent/skills").mkdir(parents=True)
    assert _default_global_skills_dir() == clean / ".baseagent/skills"


@pytest.mark.unit
def test_project_rules_accept_both_directory_names() -> None:
    from baseagent_config import Settings

    candidates = Settings.model_fields["context"].default.rules.candidates
    assert ".baseagent/rules.md" in candidates and ".aegis/rules.md" in candidates
    assert candidates.index(".baseagent/rules.md") < candidates.index(".aegis/rules.md")


@pytest.mark.unit
def test_old_command_names_remain_as_aliases_of_the_new_ones() -> None:
    scripts = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]["scripts"]
    for new, old in (
        ("baseagent", "aegis"),
        ("baseagent-server", "aegis-server"),
        ("baseagent-bench", "aegis-bench"),
    ):
        assert scripts[new] == scripts[old]
