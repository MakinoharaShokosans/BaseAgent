"""``python -m services.bash_shell``：按配置在回环地址上启动沙箱 sidecar。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from baseagent_config import ConfigError, load_settings
from services.bash_shell.app import ShellSettings, create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baseagent-bash-shell")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        loaded = load_settings(args.config, require_models=False)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    cfg = loaded.settings.bash_shell
    settings = ShellSettings(
        config=cfg,
        artifacts_dir=loaded.paths.bash_artifacts_dir,
        scratch_dir=loaded.home / cfg.child_home,
    )
    uvicorn.run(create_app(settings), host=cfg.host, port=cfg.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
