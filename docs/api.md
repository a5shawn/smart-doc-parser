# 接口文档

- **基地址**：`/api/v1`（相对路径。Docker 部署由 nginx 反代，本地开发由 Vite 代理）
- **交互式文档**：<http://localhost:8000/docs>（Swagger UI，可直接试调）
- **OpenAPI schema**：<http://localhost:8000/openapi.json>，仓库内快照见 [`openapi.json`](openapi.json)

---

## 鉴权

由环境变量 `AUTH_ENABLED` 控制，**默认关闭**（方便本地开发与演示）。

开启后，所有业务接口都要求请求头：

```
X-API-Key: <你的密钥>
```

也兼容 `Authorization: Bearer <密钥>`（curl 与 Swagger 里更顺手）。

**健康检查接口永不鉴权**——否则容器编排的健康检查会一直失败，容器永远起不来。

```bash
# 开启鉴权后
curl -H "X-API-Key: your-key" http://localhost/api/v1/documents
```

---

## 错误格式

所有非 2xx 响应都是**同一个信封结构**，前端只需要处理一种形状：

```json
{
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "文件大小 31.5 MB，超过 20 MB 上限",
    "details": { "size_bytes": 33030144, "limit_mb": 20 },
    "request_id": "0f3c8a1b..."
  }
}
```

- `code` 是给程序判断的**稳定标识**（前端据此决定展示方式，不受文案调整影响）
- `message` 是给人看的中文说明
- `request_id` 会同时出现在响应头 `X-Request-ID` 里，报障时提供它可直接定位日志

### 错误码表

| code | HTTP | 含义 | 用户能做什么 |
|---|---|---|---|
| `VALIDATION_ERROR` | 422 | 请求参数不合法 | 检查参数，`details.fields` 里有逐字段说明 |
| `UNAUTHORIZED` | 401 | 缺少或无效的 API Key | 在「设置」页配置密钥 |
| `FILE_TYPE_MISMATCH` | 400 | 扩展名不在白名单，或文件头与扩展名不符 | 确认文件本身没被改过后缀名 |
| `FILE_TOO_LARGE` | 413 | 超过大小上限 | 压缩或拆分文件 |
| `EMPTY_FILE` | 400 | 文件内容为空 | — |
| `CORRUPTED_FILE` | 422 | 文件损坏或格式不完整 | 重新导出该文件 |
| `PDF_NO_TEXT_LAYER` | 422 | 扫描件，没有可提取的文本 | **换一份电子版文档**（不是系统故障） |
| `DOCUMENT_NOT_FOUND` | 404 | 文档不存在 | 可能已被删除 |
| `TASK_NOT_FOUND` | 404 | 任务不存在 | — |
| `RESULT_NOT_FOUND` | 404 | 任务还没结果（或已失败） | 看任务状态与错误信息 |
| `TEMPLATE_NOT_FOUND` | 404 | 模板不存在 | 检查 `template_key` |
| `TEMPLATE_IN_USE` | 409 | 模板正被任务使用，无法删除 | 历史任务不受影响，只是不能删 |
| `INVALID_JSON_SCHEMA` | 400 | 自定义 schema 不合法 | 按 `details` 修正 schema |
| `CONFLICT` | 409 | 资源冲突（如模板重名） | 换个名字 |
| `LLM_TIMEOUT` | 504 | 模型调用超时 | 稍后重试；长文档可调大 `LLM_TIMEOUT_SECONDS` |
| `LLM_RATE_LIMITED` | 429 | 上游限流 | 稍后重试，或调小 `MAX_CONCURRENT_TASKS` |
| `LLM_INVALID_JSON` | 502 | 模型返回的内容两次都无法解析 | 重试；若持续出现请看任务详情里的原始输出 |
| `LLM_OUTPUT_TRUNCATED` | 502 | 输出被 `max_tokens` 截断 | 调大 `LLM_MAX_TOKENS`（推理模型的思考会占用这个预算） |
| `TASK_TIMEOUT` | 504 | 任务总耗时超时 | 调大 `TASK_TIMEOUT_SECONDS` |
| `SERVICE_RESTARTED` | 409 | 服务重启导致任务中断 | 点击「重新抽取」 |
| `INTERNAL_ERROR` | 500 | 服务内部错误 | 带上 `request_id` 报障 |

---

## 健康检查

### `GET /health/live` — 存活探针

只证明进程活着，**不检查任何外部依赖**。故意不碰数据库：
如果这里去连数据库，数据库抖动会导致编排系统不停重启一个本来健康的容器，
把一次小故障放大成雪崩。

```json
{ "status": "ok", "app": "smart-doc-parser", "version": "0.1.0", "environment": "development" }
```

### `GET /health/ready` — 就绪探针

会真的查一次数据库。连不上时返回 **503**，编排系统会把流量摘掉但**不会重启容器**。

```json
{ "status": "ok", "database": "ok" }
```

---

## 文档

### `POST /documents` — 上传文档

接收 **1~N 个文件**，逐个校验、解析并入库。

```
Content-Type: multipart/form-data
字段名：files（可重复）
支持：.pdf / .docx / .txt / .md，单文件 ≤ 20 MB
```

**部分成功语义**：单个文件失败不会让整批回滚。响应固定为 **200**——
请求本身处理成功了，每个文件的结果在响应体里分别给出。用单一状态码表达
「3 个成功 1 个失败」既做不到也不直观。

```bash
curl -X POST http://localhost/api/v1/documents \
  -F "files=@samples/contract.pdf" \
  -F "files=@samples/resume.docx"
```

```json
{
  "accepted": [
    {
      "id": "d1eccc18-b3a9-47c5-acbc-90261dd7eb74",
      "filename": "contract.pdf",
      "mime_type": "application/pdf",
      "size_bytes": 30611,
      "sha256": "883017c8...",
      "page_count": 1,
      "char_count": 305,
      "text_preview": "采购合同 甲方：北京星辰科技有限公司 ...",
      "text_status": "extracted",
      "text_error": null,
      "task_count": 0,
      "created_at": "2026-09-13T05:59:52.123456Z"
    }
  ],
  "duplicate": [],
  "rejected": [
    { "filename": "virus.exe", "code": "FILE_TYPE_MISMATCH", "message": "不支持的文件类型：.exe" }
  ]
}
```

**去重**：内容完全相同的文件（`sha256` 相同）不会重复存储，
会出现在 `duplicate` 里并复用已有记录。

**扫描件**：能正常入库，但 `text_status` 为 `no_text_layer`，
`text_error` 里写明原因。对这类文档创建抽取任务会明确失败并提示，
而不是静默返回空结果。

### `GET /documents` — 文档列表

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `page` | int | 1 | 页码 |
| `page_size` | int | 20 | 每页条数，最大 100 |

```bash
curl "http://localhost/api/v1/documents?page=1&page_size=20"
```

响应是统一的分页信封，**不含正文**（列表拉 20 份文档的全文纯属浪费带宽）：

```json
{ "items": [ ... ], "total": 42, "page": 1, "page_size": 20, "pages": 3 }
```

### `GET /documents/{document_id}` — 文档详情

比列表多一个 `text_content` 字段（提取出的完整纯文本）。
**排查抽取质量问题时先看它**——很多时候模型没抽到字段，是因为解析出来的文本里本来就没有。

### `DELETE /documents/{document_id}` — 删除文档

**不可恢复的硬删除**。外键是 `ON DELETE CASCADE`，
该文档下的所有抽取任务与结果会一并被数据库删除。

```json
{ "deleted": true, "cascaded_tasks": 3 }
```

`cascaded_tasks` 用于向用户回执影响范围——界面的确认弹窗应当明确写出
"将同时删除 N 个抽取任务及其结果"。

---

## 抽取任务

### `POST /tasks` — 创建抽取任务

**接口立刻返回，不等待模型。** 这是异步任务架构的意义所在。

```bash
curl -X POST http://localhost/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "document_ids": ["d1eccc18-b3a9-47c5-acbc-90261dd7eb74"],
    "template_key": "contract"
  }'
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `document_ids` | UUID[] | 要抽取的文档，1~50 个。**同一 ID 重复出现会自动去重** |
| `template_key` | string | 内置模板用 `contract` / `resume` / `invoice`；自定义模板用 `custom:<uuid>` |
| `batch_id` | UUID? | 不传则自动生成。同一批传入相同值便于聚合展示 |

```json
{
  "batch_id": "8d6e0420-9a59-40c2-bd23-da5412d56e61",
  "tasks": [
    {
      "id": "3ddd0e93-16a2-4804-90d8-edbdaaaf746f",
      "document_id": "d1eccc18-...",
      "document_filename": "contract.pdf",
      "template_key": "contract",
      "template_name": "合同",
      "batch_id": "8d6e0420-...",
      "status": "pending",
      "progress": 0,
      "stage_message": null,
      "error_code": null,
      "error_message": null,
      "model": null,
      "prompt_tokens": 0,
      "completion_tokens": 0,
      "reasoning_tokens": 0,
      "cached_tokens": 0,
      "cost_usd": 0.0,
      "latency_ms": null,
      "attempts": 0,
      "started_at": null,
      "finished_at": null,
      "created_at": "2026-09-13T05:59:53.000000Z"
    }
  ]
}
```

**同一份文档可以用不同模板反复抽取**，不需要重新上传文件。

### `GET /tasks` — 任务列表

| 参数 | 说明 |
|---|---|
| `status` | `pending` / `parsing` / `extracting` / `validating` / `completed` / `failed` |
| `template_key` | 按模板过滤 |
| `batch_id` | 按批次过滤 |
| `page` / `page_size` | 分页 |

### `GET /tasks/{task_id}` — 任务详情

包含完整的状态、进度、错误信息与用量统计。轮询它也能拿到进度，
但推荐用 SSE（见下）。

### `GET /tasks/{task_id}/result` — 抽取结果

```json
{
  "task_id": "3ddd0e93-...",
  "data": {
    "contract_name": "采购合同",
    "party_a": "北京星辰科技有限公司",
    "party_b": "上海云图信息技术有限公司",
    "amount": 1280000,
    "currency": "CNY",
    "sign_date": "2026-03-15",
    "delivery_date": "2026-06-30",
    "effective_date": null,
    "contract_subject": "乙方向甲方提供企业级数据中台建设服务，包括数据采集、清洗、建模与可视化",
    "payment_terms": "合同签订后5个工作日内，甲方支付合同总额的30%作为预付款",
    "breach_clause": null
  },
  "raw_output": "{\"contract_name\":\"采购合同\",...}",
  "warnings": [],
  "truncated": false,
  "created_at": "2026-09-13T06:00:02.000000Z"
}
```

**`warnings` 是这个接口最有价值的部分**。它列出所有非致命问题，
按 `kind` 分类：

| kind | 含义 |
|---|---|
| `missing_required` | 必填字段没抽到（值为 null 或键不存在） |
| `input_truncated` | 文档过长被截断，部分字段可能因此未抽到 |
| `type_coerced` | 类型被自动转换（例如 `"¥1,280,000 元"` → `1280000.0`） |
| `nullish_string` | 字符串 `"null"` / `"N/A"` 被归一化成真正的 `null` |
| `unexpected_field` | 模型返回了模板里没有的字段（可能是它自己编的） |
| `type_mismatch` | 类型不符 |
| `enum_mismatch` | 取值不在允许范围内 |
| `wrapped_scalar_as_array` | 数组字段只返回了单值，已包装成数组 |

**`raw_output` 保留模型原始返回**——结果可疑时，它能区分
"是模型输出错了"还是"是解析/校验错了"。

### `POST /tasks/{task_id}/retry` — 重新抽取

把任务重置为待执行并重新排队。**会删除上一次的结果**——
不删的话，重跑失败时界面上会出现"任务失败但结果还在"的矛盾状态。

进行中的任务调用此接口返回 409。

### `DELETE /tasks/{task_id}` — 删除任务

---

## 进度推送（SSE）

### `GET /tasks/stream`

服务端推送任务状态、进度与模型的流式输出。

| 参数 | 说明 |
|---|---|
| `batch_id` | 只看某个批次 |
| `task_ids` | 逗号分隔的任务 ID，只看这些任务。**全部结束后连接会自动关闭** |

不传参数时是**全局流**：跟踪所有进行中的任务 + 最近 5 分钟内完成的，
事件里带 `task_id` 由前端分发。

**为什么是全局单流而不是每个任务一条**：批量上传 20 份文档时，
如果每个任务开一条 SSE，会直接撞上浏览器「同源并发连接数约 6 个」的限制
（HTTP/1.1），整页连普通请求都发不出去。

```bash
curl -N "http://localhost/api/v1/tasks/stream?batch_id=8d6e0420-..."
```

### 事件类型

| event | 何时发送 | data |
|---|---|---|
| `snapshot` | 连接建立时**立即**发送 | `{"tasks": [TaskOut, ...]}` |
| `progress` | 任务状态或进度变化时 | `TaskOut` |
| `chunk` | 模型流式输出的分片 | `{"task_id", "kind", "text"}` |
| `done` | 任务进入终态 | `TaskOut` |

心跳是以冒号开头的注释行（`: ping`），客户端会忽略，
但足以让中间的代理认为连接活跃、不至于因空闲掐断。

### `chunk` 事件

```json
{ "task_id": "3ddd0e93-...", "kind": "content", "text": "北京星辰" }
```

`kind` 有三个取值：

| kind | 含义 |
|---|---|
| `reasoning` | 模型的思考过程。**是否出现不确定**（实测约 1/5 的调用会产生），前端宜默认折叠 |
| `content` | 最终答案的正文 |
| `reset` | **丢弃已累积的内容**。输出被 `max_tokens` 截断时会重跑，此时已推送的分片全部作废 |

> `reset` 事件必须处理。不处理的话，界面会把两次输出的文本拼在一起，
> 显示出一段看着合法、实则错乱的 JSON——用户会以为那是模型抽出来的真实结果，
> 比直接报错更危险。

### 实际输出示例

```
event: snapshot
data: {"tasks":[]}

event: progress
data: {"id":"3ddd0e93-...","status":"extracting","progress":30,"stage_message":"大模型抽取中",...}

event: chunk
data: {"task_id":"3ddd0e93-...","kind":"content","text":"{"}

event: chunk
data: {"task_id":"3ddd0e93-...","kind":"content","text":"\"contract"}

...

event: done
data: {"id":"3ddd0e93-...","status":"completed","progress":100,...}
```

### 前端如何消费

不要用 `EventSource`——**它无法设置请求头**，带不了 `X-API-Key`。
用 `fetch` + `ReadableStream` 手动解析（本项目前端就是这么做的，
见 `frontend/src/api/sse.ts`）。

**两个必须注意的实现细节**：

1. 用 `TextDecoder` 时**必须带 `{ stream: true }`**。中文是 UTF-8 三字节，
   一个字很可能被切在两个网络分片之间。没有这个选项会被解码成 `�`。
2. 解析器必须处理**跨分片的行边界**。一个分片可能把一行切成两半，
   甚至把 `\r\n` 切成 `\r` + `\n`。按"收到一个分片当一条完整消息"处理，
   会表现为"偶尔丢事件"这种极难复现的 bug。

---

## 模板

### `GET /templates` — 模板列表

返回全部可用模板：3 套内置模板在前，自定义模板在后。

```json
[
  {
    "key": "contract",
    "name": "合同",
    "description": "采购、服务、合作类合同的关键要素抽取：甲乙方、金额、日期与主要条款",
    "doc_types": ["采购合同", "服务合同", "合作协议", "框架协议"],
    "prompt_hint": "金额只填数字，不要带货币符号...",
    "builtin": true,
    "template_id": null,
    "created_at": null,
    "fields": [
      {
        "name": "party_a",
        "label": "甲方",
        "type": "string",
        "description": "甲方（通常是采购方/委托方）的完整公司名称，保留全称不要简称",
        "required": true,
        "enum": [],
        "item_type": null,
        "item_fields": []
      }
    ]
  }
]
```

**每个字段都带中文 `label`**，前端据此把 `party_a` 渲染成「甲方」。
数组字段的 `item_fields` 描述子字段结构，前端据此把嵌套数据渲染成表格。

### `POST /templates` — 新建自定义模板

```bash
curl -X POST http://localhost/api/v1/templates \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "采购单",
    "description": "采购单关键字段",
    "schema": {
      "type": "object",
      "properties": {
        "order_no": { "type": "string", "title": "订单号", "description": "采购单编号" },
        "total": { "type": "number", "title": "总金额" }
      },
      "required": ["order_no"]
    },
    "prompt_hint": "金额只填数字；日期统一为 YYYY-MM-DD"
  }'
```

字段名是 `schema`（不是 `json_schema`），值为标准 JSON Schema（Draft 2020-12），
**根节点的 `type` 必须是 `object`**。

**校验在入库前完成**：语法错误、根节点类型不对、Schema 本身不合法，
都会在创建时就被拒绝（`INVALID_JSON_SCHEMA`），
而不是等到建任务时才报错——那时的报错点离问题源头太远。

`prompt_hint` 会作为「业务规则」写进提示词，是提升抽取准确率最直接的手段。

### `DELETE /templates/{template_id}` — 删除自定义模板

内置模板不可删除。

**历史任务存的是模板快照（名称 + schema），因此删除模板不影响历史任务的展示与重跑。**
但有任务正在使用该模板时会拒绝删除（`TEMPLATE_IN_USE`）。

---

## 统计

### `GET /stats` — 用量与成本统计

```json
{
  "total_documents": 12,
  "total_tasks": 20,
  "completed_tasks": 18,
  "failed_tasks": 2,
  "running_tasks": 0,
  "total_prompt_tokens": 26740,
  "total_completion_tokens": 4860,
  "total_reasoning_tokens": 2100,
  "total_cached_tokens": 18400,
  "total_cost_usd": 0.0108,
  "avg_latency_ms": 1998.5,
  "total_cost_cny": 0.0767,
  "success_rate": 0.9,
  "avg_cost_per_task_usd": 0.00054
}
```

- `success_rate` 只统计**已结束**的任务——把还在排队的算进分母会让成功率虚低
- `reasoning_tokens` 已包含在 `completion_tokens` 内，不要重复相加
- `total_cached_tokens` 是命中前缀缓存的输入 token，可用来衡量 prompt 组织策略的效果
