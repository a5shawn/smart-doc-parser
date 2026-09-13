# 部署指南

从本地一键启动，到云服务器上线，以及上线之后要操心的事。

---

## 1. 本地部署（一键启动）

```bash
cp .env.example .env          # 填入 DEEPSEEK_API_KEY
docker compose up -d --build
docker compose ps             # 三个服务都 healthy 就绪
```

访问 <http://localhost>。

### 服务组成

| 服务 | 镜像 | 端口 | 说明 |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg16` | 不对外暴露 | 基础 compose 刻意不映射 5432 |
| `backend` | 本地构建 | `8000:8000` | 暴露是为了直接访问 Swagger 与调试 |
| `frontend` | 本地构建 | `80:80` | nginx 托管静态资源 + 反代 `/api` |

### 数据持久化

两个具名卷，`docker compose down` 不会删除它们：

| 卷 | 内容 |
|---|---|
| `smart-doc-parser-pgdata` | 数据库 |
| `smart-doc-parser-uploads` | 上传的原始文件 |

```bash
docker compose down          # 停止容器，保留数据
docker compose down -v       # 连同数据一起删除（不可恢复）
```

> **为什么上传目录用具名卷而不是 bind mount**：Linux 上 bind mount 不会继承镜像里
> 目录的属主，非 root 进程（uid 1001）会因为没权限而写不进去。
> macOS 的 Docker Desktop 会自动映射属主，本地看着一切正常，一上服务器就
> `Permission denied`——典型的"本地好好的"事故。

---

## 2. 云服务器上线

### 2.1 服务器准备

- **配置**：2 核 4 G 起步（阿里云/腾讯云新人价即可）。构建镜像时比较吃内存，
  1 核机器建议本地构建后推送镜像，或者加 swap
- **系统**：Ubuntu 22.04 / 24.04 或同级别发行版
- **安全组**：只放行 22（SSH）、80、443。**数据库端口不要对公网开放**

### 2.2 安装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER    # 重新登录后生效
docker compose version           # 确认 compose 可用
```

**国内服务器拉取镜像慢或失败时**，配置镜像加速器（`/etc/docker/daemon.json`）：

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com"
  ]
}
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart docker
```

> 注意：`docker buildx imagetools` 之类的命令**不走**加速器，
> 因此用它检查镜像是否存在可能得到误导性的失败结果。用 `docker pull` 验证。

### 2.3 部署

配置分两层：**基线** + **生产覆盖层**。服务器上两份都要有。

```bash
git clone <你的仓库地址> smart-doc-parser
cd smart-doc-parser

# 1. 基线：全部 41 个键，基本用默认值即可，不用改
cp .env.example .env

# 2. 生产覆盖层：只写需要改的 8 个键
cp .env.production.example .env.production
vim .env.production
```

`.env.production` 里逐项填真实值（每一项都是必改项）：

```bash
APP_ENV=production                   # 决定加载哪个覆盖层，必须在这一层设置
LOG_JSON=true                        # 结构化日志，便于采集
POSTGRES_PASSWORD=<openssl rand -base64 24>
AUTH_ENABLED=true                    # 公网部署必须开启
API_KEYS=<openssl rand -hex 24>
CORS_ORIGINS=https://你的域名         # 不要用 *
DEEPSEEK_API_KEY=sk-你的真实密钥
```

```bash
make up-prod                         # 基线 + 覆盖层
docker compose ps
```

> **别用 `docker compose up -d --build`**（不带 `--env-file`）。那样只会读基线，
> 数据库会用 `.env.example` 里公开的默认密码启动。后端的生产预检会拦住这种情况
> 并拒绝启动——但那是在容器起来之后，不如一开始就走对命令。
>
> `make up-prod` 展开后是：
> `docker compose --env-file .env --env-file .env.production up -d --build`
> 两个 `--env-file` 都要写：它是**替换**而非追加默认的 `./.env`，
> 只写覆盖层的话基线就丢了。

> **别忘了 `.env.production` 也在 `.gitignore` 里**。它含真实密钥，
> 不要提交。服务器上手工维护，并记着同步到你的密钥备份里。

### 2.4 配置 HTTPS

**推荐用云厂商的免费证书**（阿里云/腾讯云都提供），比 certbot 少一层续期维护。

1. 在云控制台申请免费 SSL 证书，绑定域名
2. 下载 nginx 格式的证书，上传到服务器 `/etc/nginx/ssl/`
3. 在前端容器挂载证书与 443 端口

`docker-compose.yml` 里给 `frontend` 服务加上：

```yaml
  frontend:
    ports:
      - "${FRONTEND_PORT:-80}:80"
      - "443:443"
    volumes:
      - /etc/nginx/ssl:/etc/nginx/ssl:ro
```

并在 `frontend/nginx/nginx.conf` 里加一个 HTTPS server 块：

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name 你的域名;

    ssl_certificate     /etc/nginx/ssl/你的证书.pem;
    ssl_certificate_key /etc/nginx/ssl/你的证书.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # 其余配置与 80 端口的 server 块保持一致
    # ...

    # SSE 同样需要关闭缓冲
    location /api/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}

# 80 端口全量跳转到 HTTPS
server {
    listen 80;
    server_name 你的域名;
    location /.well-known/acme-challenge/ { root /usr/share/nginx/html; }
    location / { return 301 https://$host$request_uri; }
}
```

### 2.5 上线检查清单

部署完逐条确认：

- [ ] 用的是 `make up-prod`（不是 `docker compose up`）
- [ ] `docker compose ps` 三个服务都是 `healthy`
- [ ] **启动日志里 `config_sources` 是 `[".env", ".env.production"]`** ——
      只看到 `.env` 说明覆盖层没加载上，此时跑的是开发配置
- [ ] `curl http://localhost/api/v1/health/ready` 返回 `{"status":"ok","database":"ok"}`
- [ ] `AUTH_ENABLED=true`，且不带密钥访问业务接口返回 401
- [ ] `CORS_ORIGINS` 已收敛为具体域名（不是 `*`）
- [ ] `POSTGRES_PASSWORD` 已改成强密码
- [ ] 数据库端口**没有**对公网暴露（`docker compose ps` 里 db 没有端口映射）
- [ ] 用 `curl -N http://你的域名/api/v1/tasks/stream` 确认事件**逐条**出现，
      而不是卡住后一次性刷出（后者说明 nginx 缓冲没关掉）
- [ ] 完整跑一次抽取，确认结果正确、成本正常
- [ ] 配置了数据库备份（见第 3 节）

---

## 3. 运维

### 备份

数据在两处：PostgreSQL 与上传文件卷。

```bash
#!/bin/bash
# backup.sh —— 建议加进 crontab，每天凌晨执行
set -euo pipefail

BACKUP_DIR=/data/backups
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

# 数据库
docker compose exec -T db pg_dump -U docparser smart_doc_parser \
  | gzip > "$BACKUP_DIR/db_$STAMP.sql.gz"

# 上传文件
docker run --rm \
  -v smart-doc-parser-uploads:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine tar czf "/backup/uploads_$STAMP.tar.gz" -C /data .

# 保留最近 30 天
find "$BACKUP_DIR" -name '*.gz' -mtime +30 -delete

echo "备份完成：$BACKUP_DIR/*_$STAMP.*"
```

**恢复**：

```bash
gunzip -c db_20260913_030000.sql.gz | docker compose exec -T db psql -U docparser -d smart_doc_parser
docker run --rm -v smart-doc-parser-uploads:/data -v /data/backups:/backup \
  alpine tar xzf /backup/uploads_20260913_030000.tar.gz -C /data
```

> 建议定期**实际演练一次恢复**。没验证过的备份等于没有备份。

### 升级

```bash
git pull
docker compose build
docker compose up -d
```

**数据库迁移由 backend 容器的入口脚本自动执行**，不需要手动跑。
迁移失败时容器会启动失败并保留旧版本运行——这是有意的设计，
避免"新代码配旧表结构"这种最难排查的状态。

发版前建议先手动备份一次：

```bash
docker compose exec -T db pg_dump -U docparser smart_doc_parser | gzip > pre_deploy.sql.gz
```

### 日志与监控

```bash
docker compose logs -f backend          # 跟踪
docker compose logs --tail=200 backend  # 最近 200 行
```

生产环境建议 `LOG_JSON=true`，日志是一行一个 JSON 对象，便于采集。
关键字段：`request_id`、`task_id`、`error_code`、`cost_usd`、`latency_ms`。

**值得设告警的指标**：

| 指标 | 阈值建议 |
|---|---|
| 任务失败率 | > 10% 持续 10 分钟 |
| `LLM_TIMEOUT` 出现频率 | 突然升高说明上游或网络有问题 |
| 单日累计成本 | 超过预算时告警，防止密钥泄漏被刷 |
| `/health/ready` 失败 | 立即告警 |
| 磁盘使用率 | > 80%，上传文件会持续增长 |

### 清理

上传文件只增不减。定期清理不再需要的文档：

```sql
-- 找出 90 天前、且没有被任何任务引用的文档
SELECT id, filename, size_bytes, created_at
FROM documents
WHERE created_at < now() - interval '90 days'
  AND NOT EXISTS (SELECT 1 FROM extraction_tasks t WHERE t.document_id = documents.id);
```

删除文档会连带删除任务与结果（`ON DELETE CASCADE`），
删除前请确认对应的文件也已清理。

---

## 4. 排错

### SSE 进度不推送 / 卡住后一次性刷出

**这是最常见的问题**，一定是代理缓冲。

```bash
# 诊断：事件应当逐条出现，带时间间隔
curl -N http://localhost/api/v1/tasks/stream
```

如果输出是"卡住几十秒，然后一次性全部出现"，说明缓冲没关掉。检查三处：

1. `frontend/nginx/nginx.conf` 里 `/api/` 段的
   `proxy_buffering off` + `proxy_cache off` + `X-Accel-Buffering no`
2. `proxy_read_timeout` 是否足够大（默认 60 秒会掐断，表现为"每 60 秒断一次"）
3. 如果前面还有一层云负载均衡（SLB/CLB），**它也可能缓冲**，
   需要在控制台开启"长连接"或"关闭响应缓冲"

### 容器起来了但抽取任务一直失败

```bash
docker compose logs backend | grep -i "task_failed" | tail -20
```

常见原因：

| 现象 | 原因 | 处理 |
|---|---|---|
| `LLM_INVALID_JSON` 持续出现 | 模型返回的内容无法解析 | 看任务详情的「原始输出」标签 |
| `LLM_OUTPUT_TRUNCATED` | `max_tokens` 太小 | 调大 `LLM_MAX_TOKENS`。**推理模型的思考 token 会占用这个预算** |
| `PDF_NO_TEXT_LAYER` | 上传的是扫描件 | 换电子版文档，这不是系统故障 |
| `LLM_TIMEOUT` | 上游慢或网络问题 | 调大 `LLM_TIMEOUT_SECONDS`，或减少 `MAX_CONCURRENT_TASKS` |

### 上传文件报 Permission denied

几乎一定是用了 bind mount 而不是具名卷。检查 `docker-compose.yml`：

```yaml
volumes:
  - uploads:/app/data/uploads      # 正确：具名卷
  # - ./data/uploads:/app/data/uploads   # 错误：Linux 上属主不对
```

修复：

```bash
docker compose exec backend id           # 确认是 uid=1001
docker compose exec backend ls -ld /app/data/uploads
```

### 镜像拉取失败

国内网络下 Docker Hub 常常不可达。确认加速器配置生效：

```bash
docker info --format '{{json .RegistryConfig.Mirrors}}'
```

为空的话按 2.2 节配置 `daemon.json`。

### 构建时内存不足（1 核 2G 机器）

前端构建（Vite 打包 3000+ 模块）比较吃内存。三个办法：

1. 加 swap：`sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`
2. 本地构建后推送镜像到镜像仓库，服务器只 `docker compose pull`
3. 把构建放到 CI 里做

### 前端页面白屏 / 刷新 404

- **白屏**：多半是 `index.html` 被缓存了。确认 nginx 配置里
  `location = /index.html` 的 `no-cache` 还在，然后强刷（Cmd/Ctrl + Shift + R）
- **刷新 404**：SPA fallback 丢了。确认 `location /` 段有
  `try_files $uri $uri/ /index.html;`

### 架构不匹配（Apple Silicon 上构建，部署到 x86 服务器）

本机是 arm64、服务器是 x86_64 时，构建出来的镜像跑不了。

```bash
# 明确指定目标平台构建
docker buildx build --platform linux/amd64 -t your-registry/smart-doc-parser-backend ./backend --push
```

或者在 CI 里构建（大多数 CI runner 本身就是 x86_64）。
**不要**把 `--platform` 写进 Dockerfile——那会让镜像在另一种架构上跑不起来。

---

## 5. 安全加固清单

上线前逐条核对：

- [x] `AUTH_ENABLED=true` 且 `API_KEYS` 是强随机值（`openssl rand -hex 24`）
- [x] `CORS_ORIGINS` 收敛为具体域名
- [x] `POSTGRES_PASSWORD` 改成强密码，且数据库端口不对公网暴露
- [x] `.env` 没有被提交进版本库（`.gitignore` 已覆盖，但值得再确认一次）
- [x] 启用 HTTPS，并考虑加 HSTS 头
- [x] 服务器防火墙只放行必要端口
- [x] 配置了自动备份并演练过恢复
- [x] `LOG_JSON=true`，日志被采集且保留足够时长
- [x] 为 DeepSeek 账户设置了消费限额（防止密钥泄漏后被刷爆）

### 关于密钥泄漏的应对

如果 `DEEPSEEK_API_KEY` 泄漏：

1. 立刻去 [platform.deepseek.com](https://platform.deepseek.com) 吊销该密钥
2. 生成新密钥，更新 `.env` 并 `docker compose up -d backend`
3. 检查用量账单，确认异常消费的规模

**预防**：`.env` 永远不进版本库；给 API Key 设置消费上限；
`.env.example` 里只放占位值。
