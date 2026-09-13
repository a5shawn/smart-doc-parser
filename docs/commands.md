# 命令速查

全部命令的清单，以及**每条 make 命令背后实际执行了什么**。

> `make help` 给出的是命令列表，这份文档多一层：把每个 target 展开成它真正跑的
> 那行命令。需要知道"它到底干了什么"或者想脱离 make 直接跑时看这里。
> 命令的准确定义以 `Makefile` 为准，本文档与它保持一致。

---

## 1. Make 命令

所有命令都从**仓库根目录**执行。

### 部署

| 命令 | 实际执行 |
|---|---|
| `make up` | `docker compose up -d --build` |
| `make up-prod` | `docker compose --env-file .env --env-file .env.production up -d --build` |
| `make build` | `docker compose build`（只构建，不启动） |
| `make down` | `docker compose down`（停止，**保留数据卷**） |
| `make restart` | `docker compose restart backend` |
| `make ps` | `docker compose ps` |
| `make logs` | `docker compose logs -f --tail=100` |
| `make shell-backend` | `docker compose exec backend sh` |
| `make shell-db` | `docker compose exec db psql -U docparser -d smart_doc_parser` |

`make up` 与 `make up-prod` 的区别只在读哪份配置，见
[`README.md` 的分环境一节](../README.md#分环境基线--覆盖层)。
`make up-prod` 会在 `.env.production` 缺失时给出提示而不是抛 compose 的原始报错。

### 本地开发

数据库跑在容器里，前后端跑在宿主机上（有热重载）。三个终端各跑一个：

| 命令 | 实际执行 |
|---|---|
| `make dev-db` | `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db` |
| `make dev-backend` | `uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` |
| `make dev-frontend` | `pnpm dev` |
| `make install` | `uv sync`（backend）+ `pnpm install`（frontend） |

`make dev-db` 叠加了 dev 覆盖文件，它只做一件事：把 `5432` 映射到 `127.0.0.1`，
让宿主机上的后端和测试能连上。不叠加的话数据库端口不对宿主机开放。

### 质量门禁

**与 CI 跑的是同一组命令**，本地过了 CI 就不会红。

| 命令 | 实际执行 |
|---|---|
| `make check` | `ruff check .` + `ruff format --check .` + `pytest --cov=app --cov-fail-under=80` |
| `make check-web` | `pnpm lint && pnpm type-check && pnpm test:unit && pnpm build` |
| `make check-api` | 重新生成 OpenAPI 与 TS 类型后 `git diff --exit-code`（需已 `git init`） |
| `make lint` | `ruff check .` |
| `make fmt` | `ruff format . && ruff check --fix .` |
| `make test` | `pytest`（**需要数据库**，先跑 `make dev-db`） |
| `make test-unit` | `pytest tests/unit`（**不需要**数据库） |
| `make test-integration` | `pytest tests/integration` |
| `make lint-web` | `pnpm lint` |
| `make test-web` | `pnpm test:unit` |

`make check` 里的覆盖率门禁是 80%。当前实际约 89%，留了余量。

`make check-api` 是**接口契约漂移检查**：改了后端字段却忘了重新生成前端类型时，
`tsc` 照样通过（前端用的是旧类型），一直到线上才发现读不到字段。
这条命令专门拦它。CI 里对应 `contract` 任务。

### 数据库迁移

| 命令 | 实际执行 |
|---|---|
| `make migrate` | `uv run alembic upgrade head` |
| `make revision m="add xxx"` | `uv run alembic revision --autogenerate -m "add xxx"` |
| `make downgrade` | `uv run alembic downgrade -1` |
| `make db-reset` | `docker compose down -v` → 重启 db → `alembic upgrade head` |

> `make db-reset` 会**删除数据卷**，所有数据不可恢复。只在本地开发用。

容器启动时会自动执行迁移（见 `backend/docker/entrypoint.sh`），
所以部署时不需要手动 `make migrate`。

### 代码生成与运维

| 命令 | 实际执行 |
|---|---|
| `make gen-api` | `uv run python -m scripts.export_openapi` → `pnpm api:sync` |
| `make smoke-llm` | `uv run python -m scripts.smoke_llm` |
| `make clean` | 清 `__pycache__` / `.pytest_cache` / `.ruff_cache` / `dist`（**不动数据库**） |

`make gen-api` 先从后端导出 OpenAPI schema，再用 openapi-typescript 生成前端类型。
**改了接口就要跑它并提交生成结果**，否则 `make check-api` 会失败。

`make smoke-llm` 用真实 API 跑一次完整抽取，打印 prompt 长度、分片到达节奏、
token 用量与成本。约消耗 ¥0.0014。调试抽取效果时比启动整个服务快得多。

---

## 2. 前端 pnpm 脚本

```bash
cd frontend
```

| 命令 | 说明 |
|---|---|
| `pnpm dev` | Vite dev server → <http://localhost:5173>，`/api` 代理到 8000 |
| `pnpm build` | 生产构建到 `dist/` |
| `pnpm preview` | 本地预览构建产物 |
| `pnpm type-check` | `vue-tsc --noEmit` |
| `pnpm lint` / `pnpm lint:fix` | `eslint .` / `eslint . --fix` |
| `pnpm format` | `prettier --write "src/**/*.{ts,vue,css}"` |
| `pnpm test:unit` | `vitest run`（49 项） |
| `pnpm test:watch` | `vitest` 监听模式 |
| `pnpm api:sync` | `openapi-typescript ../docs/openapi.json -o src/api/types.gen.ts` |

---

## 3. 后端脚本

```bash
cd backend
```

| 命令 | 说明 |
|---|---|
| `uv run python -m scripts.export_openapi` | 导出 `docs/openapi.json`。**不连数据库**，因此 CI 里也能跑 |
| `uv run python -m scripts.smoke_llm` | 真实 API 冒烟测试 |
| `uv run alembic upgrade head` | 迁移到最新 |
| `uv run alembic downgrade base` | 全部回退（CI 用它做往返检查） |
| `uv run pytest -q --collect-only` | 只看测试数量，不执行 |
| `uv run pytest tests/unit/test_config.py -v` | 跑单个文件 |

---

## 4. 常用 curl

完整的接口说明与错误码表见 [`api.md`](api.md)。

```bash
# ---- 健康检查 ----
curl http://localhost/api/v1/health/live     # 存活：不碰数据库
curl http://localhost/api/v1/health/ready    # 就绪：会真查一次数据库

# ---- 上传文档（-F 可重复，支持批量）----
curl -X POST http://localhost/api/v1/documents \
  -F "files=@samples/contract.pdf" \
  -F "files=@samples/resume.docx"

# ---- 创建抽取任务（立即返回，不等待模型）----
curl -X POST http://localhost/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"document_ids":["<文档ID>"],"template_key":"contract"}'

# ---- 查询 ----
curl http://localhost/api/v1/documents
curl "http://localhost/api/v1/tasks?status=completed&page=1&page_size=20"
curl http://localhost/api/v1/tasks/<任务ID>/result | python3 -m json.tool
curl http://localhost/api/v1/templates
curl http://localhost/api/v1/stats | python3 -m json.tool

# ---- SSE 进度流（全局单流）----
curl -N http://localhost/api/v1/tasks/stream

# ---- 重试 / 删除 ----
curl -X POST http://localhost/api/v1/tasks/<任务ID>/retry
curl -X DELETE http://localhost/api/v1/documents/<文档ID>

# ---- 开了鉴权之后（AUTH_ENABLED=true）----
curl -H "X-API-Key: your-key" http://localhost/api/v1/documents
curl -H "Authorization: Bearer your-key" http://localhost/api/v1/documents
```

**`curl -N` 是验证 SSE 是否被缓冲的标准手段**：事件应当**逐条带时间间隔**出现。
如果卡住几十秒然后一次性刷出，说明中间有代理在缓冲（见
[`deployment.md` 的排错一节](deployment.md#sse-进度不推送--卡住后一次性刷出)）。

---

## 5. Docker 排查

```bash
# 状态与配置
docker compose ps
docker compose config                       # 展开后的最终配置（能看到插值结果）
docker compose config -q                    # 只校验语法，不输出

# 回溯：这个容器是用哪些配置文件启动的
docker inspect sdp-backend --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'

# 日志
docker compose logs --tail=200 backend
docker compose logs -f backend
docker compose logs backend | grep task_failed

# 容器内部
docker compose exec backend sh
docker compose exec backend id              # 确认非 root（应为 uid=1001）
docker compose exec -T db pg_isready -U docparser -d smart_doc_parser

# 数据库
docker compose exec db psql -U docparser -d smart_doc_parser -c '\dt'

# 镜像
docker compose build --no-cache backend     # 不用缓存重建
```

---

## 6. 常见工作流

### 新环境从零跑起来

```bash
cp .env.example .env            # 填入 DEEPSEEK_API_KEY
make up                         # 构建并启动
make ps                         # 三个服务都 healthy 就绪
# → http://localhost
```

### 改完后端代码，提交前

```bash
make fmt                        # 自动格式化 + 修复
make check                      # ruff + 覆盖率门禁（需要 make dev-db 在跑）
make check-api                  # 如果改过接口，确认契约没漂移
git add -A && git commit
```

### 改了接口（请求/响应模型、路由、字段）

```bash
make gen-api                    # 重新生成 OpenAPI 与前端 TS 类型
make check-api                  # 确认生成物与代码同步
# 生成物要一起提交：docs/openapi.json 和 frontend/src/api/types.gen.ts
```

### 排查"抽取结果不对"

```bash
# 1. 解析出来的文本里到底有没有这个信息？
curl http://localhost/api/v1/documents/<文档ID> | python3 -m json.tool

# 2. 模型看到了什么、返回了什么？
curl http://localhost/api/v1/tasks/<任务ID>/result | python3 -m json.tool

# 3. 单独测一次模型，看 prompt 与分片节奏
make smoke-llm
```

顺序不能反：**很多时候模型没抽到字段，是因为解析出来的文本里本来就没有**
（表格丢字、扫描件、编码问题），这时改 prompt 是白费力气。

### 排查"配置改了不生效"

```bash
docker compose logs backend | grep application_starting
# 看 config_sources：实际加载了哪几个配置文件
# 只看到 .env 说明覆盖层没加载——多半是启动时忘了带 --env-file

docker compose config | grep -E "APP_ENV|POSTGRES_PASSWORD"
# 看插值后的最终值
```

### 排查"生产环境起不来"

生产模式有一道启动预检，危险的开发默认值会**拒绝启动并一次列全所有问题**：

```bash
make up-prod
docker compose logs backend | tail -30
```

看到 `拒绝启动` 就按列出的问题逐条改 `.env.production`。
完整清单见 [`.env.production.example`](../.env.production.example)。
