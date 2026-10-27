"""统一入口 aegis：服务装配、环境白名单、进程收尾。出处：docs/01_architecture/05_tech_stack.md。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from aegis_config import load_settings
from agent_runtime.cli import Service, build_services, dashboard, filtered_env, main, stop_all
from api_support import REPO_ROOT


@pytest.fixture
def loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real-0000000000")
    return load_settings(
        REPO_ROOT / "config" / "config.toml", {"app": {"home": str(tmp_path / "h")}}
    )


@pytest.mark.security
def test_sidecars_never_inherit_secrets(loaded) -> None:  # type: ignore[no-untyped-def]
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "DEEPSEEK_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "ghp_x",
        "LANG": "C.UTF-8",
    }
    services = {
        s.name: s for s in build_services(loaded, config=None, with_frontend=False, environ=env)
    }
    shell = services["bash_shell"].env
    assert "DEEPSEEK_API_KEY" not in shell and "GITHUB_TOKEN" not in shell
    assert shell["PATH"] == "/usr/bin" and shell["AEGIS_HOME"] == str(loaded.home)
    assert services["backend"].env["DEEPSEEK_API_KEY"] == "sk-secret"  # 后端要调用模型


@pytest.mark.unit
def test_services_are_the_sidecar_then_backend_with_loopback_health_urls(loaded) -> None:  # type: ignore[no-untyped-def]
    services = build_services(
        loaded, config=Path("/x/config.toml"), with_frontend=False, environ={}
    )
    assert [s.name for s in services] == ["bash_shell", "web_search", "backend"]
    assert services[0].argv[1:4] == ("-m", "services.bash_shell", "--config")
    assert services[1].argv[1:3] == ("-m", "services.web_search")
    assert services[2].argv[1:3] == ("-m", "agent_runtime.server")
    assert all(s.health_url and "127.0.0.1" in s.health_url for s in services)


@pytest.mark.unit
def test_filtered_env_keeps_only_the_allowlist() -> None:
    assert filtered_env(["A", "B"], {"A": "1", "C": "3"}) == {"A": "1"}


@pytest.mark.security
def test_the_dashboard_never_prints_the_token(loaded) -> None:  # type: ignore[no-untyped-def]
    loaded.paths.api_token.parent.mkdir(parents=True, exist_ok=True)
    loaded.paths.api_token.write_text("super-secret-token-value")
    text = dashboard(loaded, {"backend": True, "bash_shell": False}, frontend=True)
    assert "super-secret-token-value" not in text
    assert str(loaded.paths.api_token) in text and "未就绪" in text and "5173" in text


@pytest.mark.integration
def test_stop_all_terminates_process_groups_including_children_and_stragglers(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "child.pid"
    polite = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,time;"
            "p=subprocess.Popen(['sleep','60']);"
            f"open(r'{child_pid}','w').write(str(p.pid));"
            "time.sleep(60)",
        ],
        start_new_session=True,
    )
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ],
        start_new_session=True,
    )
    time.sleep(0.6)
    services = [
        Service("a", (), tmp_path, {}, process=polite),
        Service("b", (), tmp_path, {}, process=stubborn),
    ]
    started = time.monotonic()
    stop_all(services, grace_sec=1.0)
    assert polite.poll() is not None and stubborn.poll() is not None
    assert time.monotonic() - started < 5


@pytest.mark.unit
def test_start_refuses_to_run_with_a_bad_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("[server]\nhost = '0.0.0.0'\n")
    assert main(["--config", str(bad), "start", "--no-frontend"]) == 2
    assert "配置不合法" in capsys.readouterr().err


@pytest.mark.unit
def test_token_command_prints_the_path_and_rotates(
    loaded, tmp_path: Path, capsys, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    config = REPO_ROOT / "config" / "config.toml"
    monkeypatch.setenv("AEGIS_APP__HOME", str(tmp_path / "h2"))
    assert main(["--config", str(config), "token"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.endswith("api_token")
    path = Path(printed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old")
    assert main(["--config", str(config), "token", "--rotate"]) == 0
    assert path.read_text().strip() != "old" and path.stat().st_mode & 0o777 == 0o600


@pytest.mark.unit
def test_dotenv_sets_missing_variables_only_and_ignores_comments(tmp_path: Path) -> None:
    from agent_runtime.cli import load_dotenv

    f = tmp_path / ".env"
    f.write_text("# c\nDEEPSEEK_API_KEY='sk-abc'\nexport X=1\nEMPTY=\nKEEP=new\nbad line\n")
    env = {"KEEP": "old"}
    assert sorted(load_dotenv(f, env)) == ["DEEPSEEK_API_KEY", "X"]
    assert env == {"KEEP": "old", "DEEPSEEK_API_KEY": "sk-abc", "X": "1"}
    assert load_dotenv(tmp_path / "missing", env) == []
