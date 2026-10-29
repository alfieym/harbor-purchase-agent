SHELL := /bin/sh

PY  := uv run --directory backend
WEB := npm --prefix frontend

# 直连 PyPI 不稳定时的默认镜像，可在命令行或环境变量中覆盖：
#   make setup UV_DEFAULT_INDEX=https://pypi.org/simple
export UV_DEFAULT_INDEX ?= https://mirrors.aliyun.com/pypi/simple

.DEFAULT_GOAL := help

.PHONY: help setup dev dev-api dev-web test lint format typecheck gen-api check clean distclean

help:
	@printf '%s\n' \
	  '用法: make <target>' \
	  '' \
	  '  setup      安装后端与前端依赖，并从 .env.example 生成本地 backend/.env' \
	  '  dev        并行启动后端 (8010) 与前端 (5173)' \
	  '  dev-api    仅启动后端（自动重载）' \
	  '  dev-web    仅启动前端' \
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
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache frontend/dist
	@echo "已清理缓存与构建产物。"

distclean: clean
	rm -rf backend/.venv frontend/node_modules
	@echo "已删除后端虚拟环境与前端依赖。"
