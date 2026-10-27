"""后端进程入口：``python -m agent_runtime.server``。

加载配置（任何问题直接拒绝启动）、装配应用、用 uvicorn 在回环地址上提供 API。
令牌见 ``storage/api_token``（0600），不会打印到日志。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import uvicorn
from loguru import logger

from aegis_config import ConfigError, LoadedSettings, load_settings
from agent_runtime.api import create_app, provision_token
from agent_runtime.bootstrap import Application, configure_logging
from agent_runtime.cli import REPO_ROOT, load_dotenv


async def serve(loaded: LoadedSettings) -> None:
    server_cfg = loaded.settings.server
    token = (
        provision_token(loaded.paths.api_token, env=os.environ, env_name=server_cfg.auth.token_env)
        if server_cfg.auth.enabled
        else None
    )
    configure_logging(loaded)
    application = await Application.create(loaded)
    try:
        app = create_app(application.api_services(), token=token)
        config = uvicorn.Config(
            app, host=server_cfg.host, port=server_cfg.port, log_level="warning"
        )
        logger.info("Aegis 后端：http://{}:{}", server_cfg.host, server_cfg.port)
        await uvicorn.Server(config).serve()
    finally:
        await application.aclose()


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(prog="aegis-server")
    parser.add_argument("--config", type=Path, default=None, help="config.toml 的路径")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    overrides: dict[str, dict[str, object]] = {}
    if args.host or args.port:
        overrides["server"] = {k: v for k, v in (("host", args.host), ("port", args.port)) if v}
    try:
        loaded = load_settings(args.config, overrides)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    for warning in loaded.warnings:
        logger.warning(warning)
    asyncio.run(serve(loaded))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
