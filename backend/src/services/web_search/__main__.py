"""``python -m services.web_search``：按配置在回环地址上启动检索与抓取 sidecar。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from baseagent_config import ConfigError, load_settings
from services.web_search.app import WebServices, create_app
from services.web_search.netguard import SafeFetcher
from services.web_search.providers import DdgsProvider, DuckDuckGoProvider, SearchProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baseagent-web-search")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        loaded = load_settings(args.config, require_models=False)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    cfg = loaded.settings.web_search
    provider: SearchProvider
    if cfg.provider == "ddgs":
        provider = DdgsProvider(
            timeout_sec=float(cfg.request_timeout_sec), backends=cfg.ddgs_backends
        )
    elif cfg.provider == "ddg":
        provider = DuckDuckGoProvider(
            user_agent=cfg.user_agent, timeout_sec=float(cfg.request_timeout_sec)
        )
    else:
        print(f"未知的搜索提供者 {cfg.provider!r}（可选 ddgs、ddg）", file=sys.stderr)
        return 2
    services = WebServices(
        config=cfg,
        provider=provider,
        fetcher=SafeFetcher(
            allowed_ports=cfg.allowed_ports,
            max_redirects=cfg.max_redirects,
            max_bytes=cfg.max_download_bytes,
            allowed_types=cfg.allowed_content_types,
            user_agent=cfg.user_agent,
            timeout_sec=float(cfg.request_timeout_sec),
            block_private=cfg.block_private_addresses,
            allow_fake_ip=cfg.allow_fake_ip,
        ),
        artifacts_dir=loaded.paths.web_artifacts_dir,
    )
    uvicorn.run(create_app(services), host=cfg.host, port=cfg.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
