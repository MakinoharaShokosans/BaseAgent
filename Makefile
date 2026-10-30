# BaseAgent 开发任务入口。包装 uv 与 pnpm 命令，细节见 docs/01_architecture/05_tech_stack.md §6。

.DEFAULT_GOAL := help
BACKEND := backend
FRONTEND := frontend

.PHONY: help install start lint fmt test test-backend test-frontend typecheck check clean

help: ## 列出可用任务
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

install: ## 安装后端与前端依赖（需要联网）
	cd $(BACKEND) && uv sync
	cd $(FRONTEND) && pnpm install

start: ## 启动沙箱 sidecar、后端与前端
	cd $(BACKEND) && uv run baseagent start

lint: ## 后端 ruff + pyright + import-linter，前端 eslint
	cd $(BACKEND) && uv run ruff check . && uv run pyright && uv run lint-imports
	cd $(FRONTEND) && pnpm lint

fmt: ## 格式化后端与前端代码
	cd $(BACKEND) && uv run ruff format . && uv run ruff check --fix .
	cd $(FRONTEND) && pnpm format

typecheck: ## 前端类型检查
	cd $(FRONTEND) && pnpm typecheck

test-backend: ## 后端测试（默认离线，不含 real_llm 与 slow）
	cd $(BACKEND) && uv run pytest

test-frontend: ## 前端单元与组件测试
	cd $(FRONTEND) && pnpm test

test: test-backend test-frontend ## 全部离线测试

check: lint typecheck test ## 提交前的完整检查

clean: ## 清理构建与缓存产物（不碰数据目录）
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache $(BACKEND)/.hypothesis $(BACKEND)/htmlcov
	rm -rf $(BACKEND)/.import_linter_cache .import_linter_cache
	find $(BACKEND)/src $(BACKEND)/tests -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(FRONTEND)/dist $(FRONTEND)/coverage
