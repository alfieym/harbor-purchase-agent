SHELL := /bin/sh

PY  := uv run --directory backend
WEB := npm --prefix frontend

# 直连 PyPI 不稳定时的默认镜像，可在命令行或环境变量中覆盖：
#   make setup UV_DEFAULT_INDEX=https://pypi.org/simple
export UV_DEFAULT_INDEX ?= https://mirrors.aliyun.com/pypi/simple

.DEFAULT_GOAL := help

.PHONY: help setup dev dev-api dev-web worker cli test lint format typecheck gen-api check clean distclean docker-up docker-down docker-logs

help:
	@printf '%s\n' \
	  '用法: make <target>' \
	  '' \
	  '  setup      安装后端与前端依赖，并从 .env.example 生成本地 backend/.env' \
	  '  dev        并行启动后端 (8010) 与前端 (5173)' \
	  '  dev-api    仅启动后端（自动重载）' \
	  '  dev-web    仅启动前端' \
	  '  worker     S15：单独启动一个执行进程（需先 HARBOR_QUEUE_ENABLED=true 启动后端）' \
	  '  docker-up  构建并启动 redis / qdrant / api / worker（S15）' \
	  '  docker-logs 跟踪 api 与 worker 的日志' \
	  '  docker-down 停止整套服务' \
	  '  cli        启动采购助手交互式 REPL（S4）' \
	  '  test       运行后端测试' \
	  '  lint       检查 Python 代码风格与格式' \
	  '  format     自动修复 Python 风格并格式化' \
	  '  typecheck  运行 mypy 与 TypeScript 类型检查' \
	  '  gen-api    导出 OpenAPI 并生成前端 TypeScript 类型' \
	  '  check      lint + typecheck + test' \
	  '  clean      清理缓存与前端构建产物（保留依赖）' \
	  '  distclean  clean + 删除后端虚拟环境与前端 node_modules'

setup:
	uv sync --directory backend
	$(WEB) install
	test -f backend/.env || cp backend/.env.example backend/.env
	@echo "setup 完成。运行 'make dev' 启动前后端。"

dev:
	@./scripts/dev.sh

dev-api:
	$(PY) python -m harbor

dev-web:
	$(WEB) run dev

# S15：队列模式下的执行进程。与后端共用同一个 runs.sqlite3，靠 Redis 唤醒。
worker:
	$(PY) python -m harbor.worker

docker-up:
	docker compose up --build -d
	@echo "API: http://127.0.0.1:8010  （首次启动要建索引，健康检查通过前 worker 不会开始领取任务）"

docker-logs:
	docker compose logs -f api worker

docker-down:
	docker compose down

cli:
	$(PY) python -m harbor.agents

test:
	$(PY) pytest -q

lint:
	$(PY) ruff check .
	$(PY) ruff format --check .

format:
	$(PY) ruff check --fix .
	$(PY) ruff format .

typecheck:
	$(PY) mypy src
	$(WEB) run typecheck

gen-api:
	$(PY) python scripts/export_openapi.py
	$(WEB) run gen:api

check: lint typecheck test

clean:
	find backend -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache frontend/dist .cache
	@echo "已清理缓存与构建产物。"

distclean: clean
	rm -rf backend/.venv frontend/node_modules
	@echo "已删除后端虚拟环境与前端依赖。"
