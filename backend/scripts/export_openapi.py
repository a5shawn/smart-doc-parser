"""导出 OpenAPI schema 到 ``docs/openapi.json``。

跑法：``make gen-api``，或 ``cd backend && uv run python -m scripts.export_openapi``

它有两个用途：

1. 前端用 ``openapi-typescript`` 从这份文件生成 TS 类型，
   **不需要后端在跑**——CI 与 Docker 构建因此都不依赖服务在线；
2. 作为接口契约的快照提交进版本库，CI 里用 ``git diff --exit-code`` 检查漂移。

**本脚本不连数据库**：``create_app()`` 只在 lifespan 里访问数据库，
而这里不会执行 lifespan。因此它可以在任意环境下运行。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import create_app

#: backend/scripts/export_openapi.py -> scripts -> backend -> <仓库根>
REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_PATH = REPO_ROOT / "docs" / "openapi.json"


def main() -> int:
    app = create_app()
    schema = app.openapi()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        # ensure_ascii=False 让中文描述保持可读——这份文件是要被人翻看的
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    operation_count = sum(len(methods) for methods in schema["paths"].values())
    print(f"已导出 {operation_count} 个接口到 {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    print(f"OpenAPI 版本: {schema['openapi']}｜接口文档标题: {schema['info']['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
