# 开发指南

本地怎么跑、怎么加功能、踩过的工具链坑。

---

## 1. 环境准备

| 工具 | 版本 | 说明 |
|---|---|---|
| Python | 3.12 | 本机没有的话 uv 会自动下载 |
| [uv](https://docs.astral.sh/uv/) | 0.12+ | 依赖与虚拟环境管理 |
| Node.js | 24 | |
| pnpm | 11 | `npm i -g pnpm@11` |
| Docker | 24+ | 本地开发只需要它跑数据库 |

```bash
# 安装依赖
make install          # 等价于 cd backend && uv sync + cd frontend && pnpm install
```

---

## 2. 本地开发流程

后端与前端跑在宿主机上（有热重载），只有数据库跑在容器里：

```bash
# 终端 1：数据库
make dev-db

# 终端 2：后端（热重载）
make dev-backend      # → http://localhost:8000/docs

# 终端 3：前端（热重载）
make dev-frontend     # → http://localhost:5173
```

前端通过 Vite 代理把 `/api` 转发到 `localhost:8000`，
因此**代码里用的仍是相对路径 `/api/v1`**，与 Docker 部署时完全一致。

### 配置从哪来

来源是**仓库根目录的两份文件叠加**：`.env`（基线）+ `.env.<环境名>`（覆盖层）。

| 谁 | 怎么读 |
|---|---|
| 后端 | `pydantic-settings`，`env_file=(.env, .env.<APP_ENV>)` |
| 前端 | `vite.config.ts` 的 `envDir: '..'`（只读 `VITE_*`） |
| docker-compose | 服务上的 `env_file:` + 命令行 `--env-file` |

本地开发只会用到基线，**不需要建 `.env.development`**——覆盖层文件不存在时会被
静默跳过。生产环境才需要 `.env.production`，见 [`deployment.md`](deployment.md)。

`frontend/.env` 这种文件**不应该存在**：前端的接口地址是相对路径，
开发和生产用的是同一个值，没有任何需要分环境的东西。

### 排查"改了配置怎么不生效"

先看启动日志里的 `config_sources`：

```json
{"event": "application_starting", "config_sources": [".env", ".env.production"], ...}
```

它列出**实际加载了哪几个文件**。只看到 `.env` 就说明覆盖层没被加载——
最常见的原因是启动时忘了带 `--env-file`，而不是配置写错了。

两个容易混的机制，改配置前要分清：

| 机制 | 管什么 | 谁来驱动 |
|---|---|---|
| compose 的 `--env-file` | compose 文件里 `${VAR}` 的插值（ports、build args、`environment:` 块） | 命令行 |
| 服务上的 `env_file:` | 注入到容器里的变量 | compose 文件 |

**两套都要带上覆盖层**，只带一个会出现"端口变了但容器里没变"这类错位。

---

## 3. 加一个新模板

**不需要改任何代码。** 有两种方式：

### 方式一：内置模板（推荐给通用场景）

在 `backend/app/ai/builtin/` 下新增一个 JSON 文件：

```json
{
  "key": "purchase_order",
  "name": "采购单",
  "description": "采购单关键字段抽取",
  "doc_types": ["采购单", "订货单"],
  "prompt_hint": "金额只填数字；日期统一为 YYYY-MM-DD",
  "fields": [
    {
      "name": "order_no",
      "label": "订单号",
      "type": "string",
      "description": "采购单编号，逐位照抄",
      "required": true
    },
    {
      "name": "items",
      "label": "明细",
      "type": "array",
      "item_type": "object",
      "description": "采购明细行",
      "item_fields": [
        { "name": "name", "label": "品名", "type": "string", "required": true },
        { "name": "qty", "label": "数量", "type": "number" }
      ]
    }
  ]
}
```

重启后端即可。文件名会成为模板的 `key`。

**支持的字段类型**：`string` / `number` / `integer` / `boolean` / `array`。
`array` 的 `item_type` 可以是 `string` 或 `object`（后者需要 `item_fields`）。

**字段定义一次，三处自动生效**：
1. 写进 prompt 的 JSON Schema
2. 服务端校验用的 JSON Schema
3. 前端渲染结果卡片用的中文标签

这就是"schema 驱动"的价值——不会出现"前端展示的字段和后端校验的字段对不上"。

### 方式二：自定义模板（给用户用）

在前端「抽取工作台 → 自定义模板」里粘贴 JSON Schema 即可，
存在数据库里，不需要重启服务。

---

## 4. 加一个新文档格式

以加 `.xlsx` 为例：

**第一步**：写解析器 `backend/app/services/parsing/xlsx.py`

```python
from app.core.errors import CorruptedFileError
from app.services.parsing.normalize import normalize_text


def extract_xlsx(data: bytes) -> tuple[str, int | None, str]:
    """返回 (清洗后的文本, 页数, 解析器名)。"""
    try:
        import openpyxl
        ...
    except Exception as exc:
        raise CorruptedFileError("Excel 解析失败") from exc

    return normalize_text(text), None, "openpyxl"
```

**第二步**：在 `backend/app/services/parsing/__init__.py` 的 `_PARSERS` 里注册：

```python
_PARSERS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".xlsx": extract_xlsx,        # 新增
    ".txt": extract_plaintext,
    ".md": extract_plaintext,
}
```

**第三步**：加进 `.env` 的 `ALLOWED_EXTENSIONS`。

**第四步**：如果这个格式有文件头魔数（Excel 也是 zip），
在 `backend/app/services/file_validation.py` 的 `MAGIC_NUMBERS` 里加上。

上层（存储、文档服务、任务流水线）不需要任何改动——它们只依赖
`extract_text()` 这个统一入口。

**别忘了加测试**：在 `backend/tests/fixtures/` 放一个真实的小文件，
在 `tests/unit/test_parsers.py` 里加用例。**用真实文件而不是构造的假数据**：
解析器的价值恰恰在于处理真实的排版噪声，用假数据测等于什么都没测。

---

## 5. 数据库迁移

```bash
# 1. 改 backend/app/db/models.py
# 2. 生成迁移
make revision m="add xxx field"
# 3. 检查生成的迁移文件（autogenerate 不是万能的）
# 4. 应用
make migrate
```

### 检查迁移时要注意

**autogenerate 处理不了 PostgreSQL 原生枚举的删除。**
`DROP TABLE` 不会连带删除类型定义，所以 `downgrade()` 里必须显式：

```python
def downgrade() -> None:
    op.drop_table("xxx")
    # DROP TABLE 不会删除原生枚举类型，必须显式清理，
    # 否则再次 upgrade 时 CREATE TYPE 会失败
    bind = op.get_bind()
    postgresql.ENUM(name="task_status").drop(bind, checkfirst=True)
```

**验证方式**：跑一次完整的往返。

```bash
uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head
```

往返能过，说明迁移是可逆的。

### 迁移的执行时机

**容器启动时自动执行**（见 `backend/docker/entrypoint.sh`）。
这样就不存在"忘了跑迁移"导致的服务异常——那类问题往往要到某个请求报错才被发现。

---

## 6. 代码规范

### 后端：ruff

```bash
make lint    # 检查
make fmt     # 自动格式化 + 自动修复
```

配置在 `backend/pyproject.toml`。启用的规则集：

| 前缀 | 作用 |
|---|---|
| `E` `W` | pycodestyle（基础风格） |
| `F` | pyflakes（未使用变量、未定义名称） |
| `I` | isort（import 排序） |
| `N` | pep8-naming |
| `UP` | pyupgrade（用现代语法） |
| `B` | flake8-bugbear（常见陷阱） |
| `C4` `SIM` `RET` `PTH` `TID` | 简化与现代化 |
| `ASYNC` | **异步代码陷阱**（对本项目尤其重要） |
| `S` | bandit（安全扫描） |
| `ARG` | 未使用参数 |
| `T20` | 禁止 `print` |

**遇到 lint 报错时的处理原则**：先判断这条规则是否真的不适用于当前场景。
确实不适用的，用 `# noqa: XXX` **并写明理由**，而不是直接加进 `ignore` 列表。
全局 ignore 会让同类问题在别处继续出现。

### 前端：ESLint + Prettier

```bash
cd frontend
pnpm lint          # 检查
pnpm lint:fix      # 自动修复
pnpm format        # Prettier 格式化
pnpm type-check    # vue-tsc 类型检查
```

---

## 7. 测试

```bash
make test              # 全部（需要数据库）
make test-unit         # 只跑单元测试（不需要数据库）
make test-integration  # 只跑集成测试
```

### 测试跑在真实的 PostgreSQL 上

不用 SQLite。本项目大量使用 JSONB、原生枚举、`gen_random_uuid()`
这些 SQLite 没有的特性，换库测试等于测了个不存在的系统。

集成测试执行前会先对测试库跑一遍 `alembic upgrade head`——
**这样迁移脚本本身也被纳入了验证**，而不只是模型定义。

### 模型调用全部打桩

`ai/base.py` 里的 `LLMClient` 是 `Protocol`，测试注入 `FakeLLMClient`。
**431 个测试一次真实 API 都不打**——快、免费、确定。

需要构造特定的模型行为时，覆盖 `llm_client` 夹具：

```python
@pytest.fixture
def llm_client() -> FakeLLMClient:
    """返回一个带代码块围栏的 JSON，验证修复逻辑。"""
    return FakeLLMClient([ScriptedResponse(content='```json\n{"a": 1}\n```')])
```

### 写测试时的几个原则

- **前端 SSE 解析器要覆盖跨分片边界**。这是最容易写错的地方，且出错后
  表现为"偶尔丢事件"，极难复现
- **安全相关的用例要写得具体**：不是"测试校验函数"，而是
  "把 exe 改名成 pdf 应该被拒绝"
- **回归测试要留注释说明它防的是什么**。例如那个康熙部首的用例，
  不写清楚的话，后来的人会以为它在测一个不存在的问题而删掉它

---

## 8. 调试技巧

### 看真实的 prompt

```python
from app.ai.prompts import build_extraction_prompt
from app.ai.templates import get_builtin_template

prompt = build_extraction_prompt(
    template=get_builtin_template("contract"),
    document_text="甲方：某某公司",
)
print(prompt.system)
print("---")
print(prompt.user)
```

抽取效果不好时，**第一件事是看 prompt 长什么样**。

### 看模型到底返回了什么

每个任务的结果里都存了 `raw_output`（模型原始返回）。
界面上在「原始输出」标签页，接口上是 `GET /tasks/{id}/result` 的 `raw_output`。

**它能区分"是模型输出错了"还是"是解析/校验错了"**——这两类问题的处理方式完全不同。

### 不用启动整个服务就能测模型

```bash
make smoke-llm
```

它会用真实 API 跑一次完整抽取，打印 prompt 长度、流式分片的到达节奏、
token 用量与成本。约消耗 ¥0.0014。

### 看 SSE 原始报文

```bash
curl -N http://localhost/api/v1/tasks/stream
```

事件应当**逐条出现**。如果卡住后一次性刷出，说明中间有代理在缓冲。

### 排查"结果不对"

按这个顺序查：

1. `GET /documents/{id}` 看 `text_content` —— **解析出来的文本里有没有这个信息？**
   很多时候模型没抽到，是因为文本里本来就没有（表格丢字、扫描件、编码问题）
2. `GET /tasks/{id}/result` 看 `raw_output` —— 模型看到正确的文本了吗？
3. 看 `warnings` —— 是必填缺失、类型转换，还是文档被截断？
4. 看 prompt —— `prompt_hint` 里的业务规则是否足够明确？

---

## 9. 工具链的坑（都踩过）

### TypeScript 7 与 vue-tsc 不兼容

**症状**：`pnpm type-check` 报 `ERR_PACKAGE_PATH_NOT_EXPORTED`，
指向 `vue-tsc/index.js` 的 `resolveTscPath`。

**原因**：TypeScript 7 是 Go 重写的原生版本，不再导出 `typescript/lib/tsc.js`，
而 vue-tsc 依赖这个路径。

**处理**：本项目把 TypeScript 钉在 `~5.9`。等 vue-tsc 支持 TS 7 后可以升级。

### pnpm 的 `allowBuilds` 配置位置

**症状**：`pnpm install` 以退出码 1 结束，报 `ERR_PNPM_IGNORED_BUILDS`，
提示跑 `pnpm approve-builds`——但那是个交互式命令，CI 和 Docker 里没法用。
更麻烦的是它会让 `pnpm run <任何脚本>` 一起失败。

**原因**：pnpm 10 起，这类设置从 `package.json` 的 `pnpm` 字段
**搬到了 `pnpm-workspace.yaml`**，写在 package.json 里会被静默忽略。
字段名也从 `onlyBuiltDependencies` 变成了 `allowBuilds`（映射形式）。

**处理**：见 `frontend/pnpm-workspace.yaml`。

```yaml
allowBuilds:
  core-js: true
```

### compose 文件在编辑器里报 `Unable to load schema`

**症状**：打开 `docker-compose.yml` 或 `docker-compose.dev.yml`，VS Code 报

```
Unable to load schema from
'https://raw.githubusercontent.com/compose-spec/compose-go/master/schema/compose-spec.json'
```

**原因**：`redhat.vscode-yaml` 扩展把 `docker-compose*.yml` **内置硬编码**映射到
上面这个 GitHub raw 地址（注意：SchemaStore 目录里其实**没有** compose 条目，
所以这不是能通过关掉 `yaml.schemaStore` 解决的问题）。而 `raw.githubusercontent.com`
在国内通常不可达，拉取就失败了。

**这只是编辑器侧的校验**，`docker compose up` / `build` / CI 完全不受影响。

**处理**：在两个 compose 文件的**首行**加 modeline 指令，指向 jsdelivr 镜像：

```yaml
# yaml-language-server: $schema=https://cdn.jsdelivr.net/gh/compose-spec/compose-go@master/schema/compose-spec.json
```

两个细节：

- **必须在第 1 行**。modeline 只认首行，写在注释块里面不生效
- 指令优先级**高于**扩展的内置映射，所以它能覆盖掉那个不可达的地址

顺带说明为什么 `ci.yml` 不报这个错：它的 schema 来自
`https://www.schemastore.org/github-workflow.json`，而 `schemastore.org` 是可达的。
只有 compose 这一个恰好被硬编码到了 GitHub raw。

**验证**：`Cmd+Shift+P` → `Developer: Reload Window`（编辑器侧的诊断状态要重载才刷新），
然后确认报错消失、字段补全恢复正常；再跑 `docker compose config -q` 确认解析没受影响。

> 换了网络环境后如果 jsdelivr 也不通，把地址换成
> `https://fastly.jsdelivr.net/...` 或 `https://gcore.jsdelivr.net/...` 即可。

### httpx 的 ASGITransport 会缓冲整个响应

**症状**：用 `AsyncClient(transport=ASGITransport(app))` 测 SSE 端点会直接挂死。

**原因**：`ASGIResponseStream` 的实现是 `yield b"".join(self._body)`——
它会把整个响应体收完才返回，而 SSE 是永不结束的流。

**处理**：SSE 的测试直接驱动异步生成器（见
`backend/tests/integration/test_sse.py` 顶部的说明）。
HTTP 层的属性（响应头、媒体类型）通过直接调用路由函数验证。

### Vite 的 `envDir` 指向仓库根目录

前端读的是仓库根目录的 `.env`，而不是 `frontend/.env`。
Docker 构建时 `frontend/` 是构建上下文，`../.env` 不存在——
Vite 会静默跳过缺失的 `.env` 文件，此时靠 Dockerfile 里的
`ENV VITE_API_BASE_URL` 兜底。

### 三处必须显式配置，否则 SSE 不工作

1. nginx：`proxy_buffering off` + `proxy_cache off` + `X-Accel-Buffering: no`
2. 应用响应头：`X-Accel-Buffering: no`（双重保险）
3. Vite dev proxy：`timeout: 0, proxyTimeout: 0`（否则长连接被 dev server 掐断）

---

## 10. 提交前检查

```bash
make check        # 后端：ruff check + format --check + pytest（含 80% 覆盖率门禁）
make check-web    # 前端：lint + type-check + test + build
make check-api    # 接口契约：重新生成 OpenAPI 与 TS 类型，比对有无漂移
```

三个都过再提交。**CI 跑的就是这三组命令**（外加镜像构建），
所以"本地过了 CI 就不会红"这句话是成立的。

### 改完后端接口时别忘了 `make gen-api`

改了请求/响应模型、加了字段、调了路由，都要重新生成一次前端类型并提交。

**忘了会怎样**：`tsc` 照样通过——因为前端用的是**旧类型**，
编译器认为一切正常。一直到线上发现某个字段读不到才暴露出来。

`make check-api` 就是专门拦这个的：它重新生成一遍再 `git diff`，
有差异就说明生成物和代码不同步。这条检查成本近乎为零，收益却很高。
CI 里是一个独立的 `contract` 任务。

### CI 里还有什么

`.github/workflows/ci.yml` 一共四个任务：

| 任务 | 内容 |
|---|---|
| `backend` | ruff + pytest（真实 PostgreSQL service）+ **迁移往返检查** |
| `frontend` | eslint + vue-tsc + vitest + 生产构建 |
| `contract` | 契约漂移检查（见上） |
| `docker` | 两个镜像的构建（只构建不推送） |

其中两个值得单独说：

- **迁移往返检查**跑 `upgrade → downgrade → upgrade`。
  autogenerate 处理不了 PostgreSQL 原生枚举的删除，
  `downgrade()` 里漏写 `DROP TYPE` 时，只有走一遍往返才能发现
- **镜像构建**在干净的 runner 上跑，能拦住"本地能构建、
  换台机器就失败"的情况——本地构建成功常常依赖残留的缓存层或未提交的文件
