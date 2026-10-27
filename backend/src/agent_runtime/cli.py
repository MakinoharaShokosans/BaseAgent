"""统一入口 ``baseagent``：一条命令拉起沙箱 sidecar、后端和前端，并一起收掉。

``baseagent start`` 先加载配置（任何问题直接拒绝启动），按白名单给 sidecar 传环境（不继承密钥），
等各服务健康后打印看板。令牌不会打印，只显示文件路径。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from agent_runtime.api import provision_token
from baseagent_config import ConfigError, LoadedSettings, load_settings
from baseagent_config.envcompat import LEGACY_PREFIX, PREFIX

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class Service:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    health_url: str | None = None
    process: subprocess.Popen[bytes] | None = field(default=None, compare=False)


def load_dotenv(path: Path, environ: dict[str, str] | None = None) -> list[str]:
    """读取 ``KEY=VALUE`` 形式的 .env（不覆盖已有环境变量），返回新设置的变量名。不打印值。"""
    target = os.environ if environ is None else environ
    if not path.is_file():
        return []
    added: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in target:
            target[key] = value
            added.append(key)
    return added


def filtered_env(allow: Sequence[str], source: Mapping[str, str] | None = None) -> dict[str, str]:
    """只传白名单变量：sidecar 里的命令不应该读到 API 密钥。"""
    env_source = os.environ if source is None else source
    return {k: env_source[k] for k in allow if k in env_source}


def config_overrides(source: Mapping[str, str]) -> dict[str, str]:
    """``BASEAGENT_<节>__<键>`` 形式的配置覆盖。它们不是密钥，sidecar 读配置时需要看到同样的值，
    否则 .env 里的覆盖只对后端生效、sidecar 仍用文件里的值。
    ``BASEAGENT_API_TOKEN`` 等不带 ``__``，不会传。"""
    return {
        k: v
        for k, v in source.items()
        if k.startswith((PREFIX, LEGACY_PREFIX)) and "__" in k.split("_", 1)[1]
    }


def build_services(
    loaded: LoadedSettings,
    *,
    config: Path | None,
    with_frontend: bool,
    environ: Mapping[str, str] | None = None,
    python: str | None = None,
) -> list[Service]:
    source = os.environ if environ is None else environ
    settings = loaded.settings
    py = python or sys.executable
    config_args = ("--config", str(config)) if config else ()
    backend_dir = REPO_ROOT / "backend"
    sidecar_env = filtered_env(settings.services.sidecar_env_allowlist, source)
    sidecar_env.update(config_overrides(source))
    sidecar_env["PYTHONPATH"] = str(backend_dir / "src")
    sidecar_env["BASEAGENT_HOME"] = str(loaded.home)

    services: list[Service] = []
    if settings.services.auto_start_sidecars:
        services.append(
            Service(
                "bash_shell",
                (py, "-m", "services.bash_shell", *config_args),
                backend_dir,
                sidecar_env,
                f"http://{settings.bash_shell.host}:{settings.bash_shell.port}/health",
            )
        )
        services.append(
            Service(
                "web_search",
                (py, "-m", "services.web_search", *config_args),
                backend_dir,
                sidecar_env,
                f"http://{settings.web_search.host}:{settings.web_search.port}/health",
            )
        )
    backend_env = dict(source)
    backend_env["PYTHONPATH"] = str(backend_dir / "src")
    services.append(
        Service(
            "backend",
            (py, "-m", "agent_runtime.server", *config_args),
            backend_dir,
            backend_env,
            f"http://{settings.server.host}:{settings.server.port}/health",
        )
    )
    if with_frontend and shutil.which("pnpm"):
        frontend_env = filtered_env(["PATH", "HOME", "LANG", "TERM", "NODE_OPTIONS"], source)
        frontend_env["BASEAGENT_HOME"] = str(loaded.home)
        frontend_env["BASEAGENT_PROXY_TARGET"] = (
            f"http://{settings.server.host}:{settings.server.port}"
        )
        services.append(
            Service(
                "frontend",
                ("pnpm", "dev", "--strictPort"),
                REPO_ROOT / "frontend",
                frontend_env,
                "http://127.0.0.1:5173/",
            )
        )
    return services


def start_all(services: Sequence[Service]) -> list[Service]:
    started: list[Service] = []
    for service in services:
        process = subprocess.Popen(
            service.argv,
            cwd=service.cwd,
            env=dict(service.env),
            start_new_session=True,
        )
        started.append(dataclasses.replace(service, process=process))
    return started


async def wait_healthy(services: Sequence[Service], timeout_sec: float) -> dict[str, bool]:
    deadline = time.monotonic() + timeout_sec
    status = {s.name: False for s in services}
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline and not all(status.values()):
            for service in services:
                if status[service.name] or service.health_url is None:
                    status[service.name] = True
                    continue
                if service.process is not None and service.process.poll() is not None:
                    continue
                with contextlib.suppress(httpx.HTTPError):
                    status[service.name] = (
                        await client.get(service.health_url)
                    ).status_code == 200  # 别的程序占着端口时不能算正常
            await asyncio.sleep(0.25)
    return status


def stop_all(services: Sequence[Service], grace_sec: float) -> None:
    """先 SIGTERM 整个进程组，宽限期后仍未退出的 SIGKILL。"""
    live = [s for s in services if s.process is not None and s.process.poll() is None]
    for service in live:
        assert service.process is not None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(service.process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_sec
    for service in live:
        assert service.process is not None
        remaining = max(deadline - time.monotonic(), 0.0)
        try:
            service.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(service.process.pid, signal.SIGKILL)
            service.process.wait()


def dashboard(loaded: LoadedSettings, health: Mapping[str, bool], frontend: bool) -> str:
    s = loaded.settings
    lines = ["", "  BaseAgent 已启动"]
    if frontend:
        lines.append("  控制台    http://127.0.0.1:5173")
    lines += [
        f"  后端 API  http://{s.server.host}:{s.server.port}/api/v1",
        f"  数据目录  {loaded.home}",
        f"  访问令牌  {loaded.paths.api_token}（权限 0600，不在此显示）",
    ]
    for name, ok in health.items():
        lines.append(f"  {name:<12}{'正常' if ok else '未就绪'}")
    lines += [f"  警告      {w}" for w in loaded.warnings]
    lines.append("  按 Ctrl+C 停止全部服务")
    return "\n".join(lines)


def cmd_start(args: argparse.Namespace) -> int:
    try:
        loaded = load_settings(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    provision_token(
        loaded.paths.api_token, env=os.environ, env_name=loaded.settings.server.auth.token_env
    )
    services = build_services(loaded, config=args.config, with_frontend=not args.no_frontend)
    running = start_all(services)
    interrupted = False

    def on_signal(*_: object) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        health = asyncio.run(
            wait_healthy(running, float(loaded.settings.services.startup_timeout_sec) + 20)
        )
        print(dashboard(loaded, health, any(s.name == "frontend" for s in running)))
        while not interrupted:
            dead = [
                s.name for s in running if s.process is not None and s.process.poll() is not None
            ]
            if dead:
                print(f"服务已退出：{', '.join(dead)}，正在停止其余服务", file=sys.stderr)
                return 1
            time.sleep(0.5)
        return 0
    finally:
        stop_all(running, float(loaded.settings.services.stop_grace_sec))


def cmd_token(args: argparse.Namespace) -> int:
    loaded = load_settings(args.config, create_home=True)
    path = loaded.paths.api_token
    if args.rotate:
        path.unlink(missing_ok=True)
        provision_token(path)
        print(f"令牌已轮换：{path}（重启后端后生效）")
    else:
        print(path)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")  # 密钥放在仓库根的 .env（已被 .gitignore 忽略）
    parser = argparse.ArgumentParser(prog="baseagent")
    parser.add_argument("--config", type=Path, default=None, help="config.toml 的路径")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="启动沙箱 sidecar、后端与前端")
    start.add_argument("--no-frontend", action="store_true")
    start.set_defaults(run=cmd_start)
    token = sub.add_parser("token", help="显示令牌文件路径，或用 --rotate 轮换")
    token.add_argument("--rotate", action="store_true")
    token.set_defaults(run=cmd_token)
    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
