# ============================================================================
# smart-doc-parser 常用命令
# 所有命令都从仓库根目录执行。`make` 或 `make help` 查看全部目标。
# ============================================================================

.DEFAULT_GOAL := help
.PHONY: help up up-prod down restart build logs ps shell-backend shell-db \
        dev-db dev-backend dev-frontend install \
        lint fmt test test-unit test-integration check \
        lint-web test-web check-web check-api \
        migrate revision downgrade db-reset \
        gen-api smoke-llm clean

BACKEND := backend
FRONTEND := frontend

COMPOSE := docker compose
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

# ---------------------------------------------------------------------------
# 生产：基线 + 覆盖层，同名键以后者为准。这是所有 compose 命令的收口点。
#
# 为什么两个 --env-file 都要写：它以「替换」而非「追加」的方式覆盖默认的
# ./.env，只写 .env.production 的话基线就丢了，所有不在覆盖层里的键会退回
# 代码内置的默认值——包括数据库密码。
#
# 还要注意 --env-file 管的是 compose 文件里 ${...} 的插值（ports、build args、
# environment 块），而服务上的 env_file: 管的是注入容器的变量，是两套机制。
# 两套都要带上覆盖层，否则会出现"端口变了但容器里没变"这类错位。
# ---------------------------------------------------------------------------
COMPOSE_PROD := docker compose --env-file .env --env-file .env.production

## ------------------------------ 帮助 ------------------------------
help: ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

## --------------------------- Docker 部署 ---------------------------
up: ## 构建并启动全部服务（开发配置：基线 .env）
	$(COMPOSE) up -d --build

up-prod: ## 生产部署（基线 .env + 覆盖层 .env.production）
	@test -f .env || { \
		echo "缺少 .env。请先执行：cp .env.example .env"; exit 1; }
	@test -f .env.production || { \
		echo "缺少 .env.production。请先执行："; \
		echo "    cp .env.production.example .env.production"; \
		echo "然后填入真实的密钥与密码。"; exit 1; }
	$(COMPOSE_PROD) up -d --build

down: ## 停止并移除全部容器（保留数据卷）
	$(COMPOSE) down

restart: ## 重启 backend 容器（用于验证任务中断恢复）
	$(COMPOSE) restart backend

build: ## 只构建镜像，不启动
	$(COMPOSE) build

logs: ## 跟踪全部服务日志
	$(COMPOSE) logs -f --tail=100

ps: ## 查看服务状态
	$(COMPOSE) ps

shell-backend: ## 进入 backend 容器
	$(COMPOSE) exec backend sh

shell-db: ## 用 psql 连接数据库
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-docparser} -d $${POSTGRES_DB:-smart_doc_parser}

## --------------------------- 本地开发 ---------------------------
dev-db: ## 只启动 PostgreSQL（带 dev 覆盖，暴露 5432 到宿主机）
	$(COMPOSE_DEV) up -d db

dev-backend: ## 本地裸机启动后端（热重载，连 localhost 的数据库）
	cd $(BACKEND) && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend: ## 本地启动前端 dev server（Vite 代理 /api 到 localhost:8000）
	cd $(FRONTEND) && pnpm dev

install: ## 安装前后端依赖
	cd $(BACKEND) && uv sync
	cd $(FRONTEND) && pnpm install

## --------------------------- 质量检查 ---------------------------
lint: ## 后端：静态检查
	cd $(BACKEND) && uv run ruff check .

fmt: ## 后端：自动格式化 + 自动修复
	cd $(BACKEND) && uv run ruff format . && uv run ruff check --fix .

test: ## 后端：全部测试（需要数据库已启动，见 make dev-db）
	cd $(BACKEND) && uv run pytest

test-unit: ## 后端：只跑单元测试（不需要数据库）
	cd $(BACKEND) && uv run pytest tests/unit

test-integration: ## 后端：只跑集成测试（需要数据库）
	cd $(BACKEND) && uv run pytest tests/integration

check: ## 后端：lint + 格式校验 + 测试与覆盖率（CI 等价命令）
	cd $(BACKEND) && uv run ruff check . \
		&& uv run ruff format --check . \
		&& uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80

lint-web: ## 前端：eslint
	cd $(FRONTEND) && pnpm lint

test-web: ## 前端：vitest
	cd $(FRONTEND) && pnpm test:unit

check-web: ## 前端：lint + 类型检查 + 单测 + 构建（CI 等价命令）
	cd $(FRONTEND) && pnpm lint && pnpm type-check && pnpm test:unit && pnpm build

check-api: ## 检查接口契约是否漂移（等价于 CI 的 contract 任务，需已 git init）
	@$(MAKE) --no-print-directory gen-api
	@git diff --exit-code -- docs/openapi.json $(FRONTEND)/src/api/types.gen.ts \
		|| { echo "接口契约有漂移：请提交 make gen-api 的生成结果。"; exit 1; }
	@echo "契约一致，无漂移。"

## --------------------------- 数据库迁移 ---------------------------
migrate: ## 应用全部迁移到最新版本
	cd $(BACKEND) && uv run alembic upgrade head

revision: ## 根据模型变更自动生成迁移，用法：make revision m="add xxx table"
	cd $(BACKEND) && uv run alembic revision --autogenerate -m "$(m)"

downgrade: ## 回退一个迁移版本
	cd $(BACKEND) && uv run alembic downgrade -1

db-reset: ## 删除数据卷并重建数据库（会清空所有数据）
	$(COMPOSE) down -v
	$(COMPOSE_DEV) up -d db
	@sleep 3
	cd $(BACKEND) && uv run alembic upgrade head

## --------------------------- 代码生成 ---------------------------
gen-api: ## 由后端生成 OpenAPI schema，并刷新前端 TS 类型
	cd $(BACKEND) && uv run python -m scripts.export_openapi
	cd $(FRONTEND) && pnpm api:sync

smoke-llm: ## 用真实 DeepSeek key 打一次调用，验证连通性与计费字段（会消耗额度）
	cd $(BACKEND) && uv run python -m scripts.smoke_llm

## --------------------------- 清理 ---------------------------
clean: ## 清理构建产物与缓存（不动数据库与上传文件）
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache $(BACKEND)/.coverage
	find $(BACKEND) -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(FRONTEND)/dist $(FRONTEND)/.vite $(FRONTEND)/node_modules/.vite
