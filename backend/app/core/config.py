"""应用配置。

设计要点
--------
1. **单一事实来源**：仓库根目录的 ``.env`` 是唯一配置文件，本地裸机、本地 Docker、
   线上 Docker 全部读它。后端通过 pydantic-settings 读取，docker-compose 通过
   ``env_file`` 读取，前端通过 Vite 的 ``envDir`` 读取。
2. **环境变量优先于 .env**：pydantic-settings 的默认优先级就是如此，因此容器里由
   compose 注入的同名变量会覆盖 .env 中的值——这正是容器内需要用 ``db``
   替代 ``localhost`` 的实现方式。
3. **fail-fast**：配置错误在进程启动时就抛出，而不是等到第一个请求进来才炸。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote_plus

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/core/config.py -> core -> app -> backend -> <仓库根>
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_ROOT_DIR = _BACKEND_DIR.parent
_ENV_FILE = _ROOT_DIR / ".env"


class Settings(BaseSettings):
    """全部运行时配置。字段名（大写）即环境变量名。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        # 同一个 .env 里还有 VITE_* 等前端变量，后端不关心，忽略而不是报错
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------- 应用 ----------------------------
    APP_NAME: str = "smart-doc-parser"
    APP_ENV: Literal["development", "production", "test"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_JSON: bool = False
    # 监听全部网卡是容器部署的必需项：容器内绑定 127.0.0.1 的话，
    # 端口映射到宿主机后根本连不上。真正的访问控制由 compose 的端口映射
    # 和防火墙负责，而不是靠绑定地址。
    BACKEND_HOST: str = "0.0.0.0"  # noqa: S104
    BACKEND_PORT: int = Field(default=8000, ge=1, le=65535)
    BACKEND_WORKERS: int = Field(default=1, ge=1, le=32)
    FRONTEND_PORT: int = Field(default=80, ge=1, le=65535)
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["*"]

    # ---------------------------- 数据库 ----------------------------
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = Field(default=5432, ge=1, le=65535)
    POSTGRES_USER: str = "docparser"
    # 本地开发默认值，方便 clone 下来直接跑。真实部署必须在 .env 里改掉，
    # 且基础 compose 刻意不把 5432 暴露到宿主机（见 docker-compose.dev.yml 的说明）。
    POSTGRES_PASSWORD: str = "docparser_dev_pwd"  # noqa: S105
    POSTGRES_DB: str = "smart_doc_parser"
    POSTGRES_TEST_DB: str = "smart_doc_parser_test"
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=50)
    DB_MAX_OVERFLOW: int = Field(default=10, ge=0, le=100)
    DB_ECHO: bool = False

    # ---------------------------- 文件上传 ----------------------------
    UPLOAD_DIR: Path = Path("./data/uploads")
    MAX_UPLOAD_SIZE_MB: int = Field(default=20, ge=1, le=200)
    ALLOWED_EXTENSIONS: Annotated[list[str], NoDecode] = [".pdf", ".docx", ".txt", ".md"]

    # ---------------------------- 鉴权 ----------------------------
    AUTH_ENABLED: bool = False
    API_KEYS: Annotated[list[str], NoDecode] = []

    # ---------------------------- DeepSeek ----------------------------
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-flash"
    LLM_STREAMING: bool = True
    # deepseek-flash 是推理模型，思考 token 会占用该预算，给小了会因思考耗尽而返回空内容
    LLM_MAX_TOKENS: int = Field(default=8192, ge=256, le=65536)
    LLM_TEMPERATURE: float = Field(default=0.0, ge=0.0, le=2.0)
    LLM_TIMEOUT_SECONDS: int = Field(default=120, ge=5, le=600)
    LLM_MAX_RETRIES: int = Field(default=3, ge=0, le=10)
    MAX_INPUT_CHARS: int = Field(default=100_000, ge=1_000, le=2_000_000)

    # 单价（美元 / 百万 token），用于把用量换算成成本展示在界面上。
    # 官方价格会调整，集中放在这里便于不改代码就跟进。
    # 参考 https://api-docs.deepseek.com/zh-cn/quick_start/pricing
    LLM_PRICE_INPUT_PER_M_USD: float = Field(default=0.30, ge=0)
    LLM_PRICE_CACHED_INPUT_PER_M_USD: float = Field(default=0.006, ge=0)
    LLM_PRICE_OUTPUT_PER_M_USD: float = Field(default=1.20, ge=0)
    USD_TO_CNY: float = Field(default=7.1, gt=0)

    # ---------------------------- 任务调度 ----------------------------
    MAX_CONCURRENT_TASKS: int = Field(default=3, ge=1, le=50)
    TASK_TIMEOUT_SECONDS: int = Field(default=300, ge=10, le=3600)

    # ---------------------------- SSE ----------------------------
    SSE_POLL_INTERVAL_SECONDS: float = Field(default=1.0, ge=0.2, le=30)
    SSE_HEARTBEAT_SECONDS: float = Field(default=15.0, ge=1, le=120)

    # ---------------------------- 前端 ----------------------------
    VITE_API_BASE_URL: str = "/api/v1"

    # ======================== 校验器 ========================

    @field_validator("CORS_ORIGINS", "API_KEYS", "ALLOWED_EXTENSIONS", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """把 ``a,b,c`` 形式的字符串拆成列表。

        pydantic-settings 默认会把 list 字段当 JSON 解析（要求 ``["a","b"]``），
        而 .env 里写逗号分隔更自然，因此显式关掉 JSON 解码（``NoDecode``）后自己拆。
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("ALLOWED_EXTENSIONS", mode="after")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        """统一成小写且带前导点，避免 .PDF / pdf / .Pdf 三种写法导致校验漏判。"""
        normalized: list[str] = []
        for ext in value:
            ext = ext.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = f".{ext}"
            normalized.append(ext)
        return normalized

    @field_validator("UPLOAD_DIR", mode="after")
    @classmethod
    def _resolve_upload_dir(cls, value: Path) -> Path:
        """相对路径按仓库根目录解析，保证不论从哪个目录启动结果都一致。"""
        return value if value.is_absolute() else (_ROOT_DIR / value).resolve()

    @model_validator(mode="after")
    def _validate_combinations(self) -> Settings:
        if self.AUTH_ENABLED and not self.API_KEYS:
            raise ValueError(
                "AUTH_ENABLED=true 但 API_KEYS 为空。这样会导致所有请求都 401，"
                "通常是因为以为开了鉴权其实没配密钥。请设置 API_KEYS，"
                "或显式把 AUTH_ENABLED 改为 false。"
            )
        return self

    # ======================== 派生属性 ========================

    def _build_dsn(self, database: str) -> str:
        # 密码里可能含 @ / : 等字符，必须转义，否则拼出的 DSN 无法解析
        password = quote_plus(self.POSTGRES_PASSWORD)
        user = quote_plus(self.POSTGRES_USER)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{database}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """业务库连接串（异步驱动，供 SQLAlchemy 使用）。"""
        return self._build_dsn(self.POSTGRES_DB)

    @property
    def test_database_url(self) -> str:
        """测试库连接串，与业务库隔离，避免跑测试清空开发数据。"""
        return self._build_dsn(self.POSTGRES_TEST_DB)

    @property
    def sync_database_url(self) -> str:
        """同步驱动连接串。

        Alembic 的自动生成（``--autogenerate``）需要同步连接来反射数据库结构，
        因此保留一个 psycopg 风格的 DSN（仅在迁移工具内部使用）。
        """
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def startup_warnings(self) -> list[str]:
        """启动时应当打印出来的配置风险提示。

        单独抽出来是为了可测试：不阻止启动，但要让运维一眼看见。
        """
        warnings: list[str] = []
        if self.is_production and not self.AUTH_ENABLED:
            warnings.append(
                "APP_ENV=production 但 AUTH_ENABLED=false：接口对公网完全开放，"
                "任何人都能上传文件并消耗你的大模型额度。生产环境请开启鉴权。"
            )
        if self.is_production and "*" in self.CORS_ORIGINS:
            warnings.append("APP_ENV=production 且 CORS_ORIGINS 含 '*'：建议收敛为具体域名。")
        if self.BACKEND_WORKERS > 1:
            warnings.append(
                f"BACKEND_WORKERS={self.BACKEND_WORKERS} > 1：任务调度器与并发信号量是"
                "进程内的，多 worker 会让 MAX_CONCURRENT_TASKS 的实际上限翻倍。"
                "需要横向扩容时请先把任务执行拆到独立 worker（见 docs/architecture.md）。"
            )
        if not self.DEEPSEEK_API_KEY or self.DEEPSEEK_API_KEY.startswith("sk-xxx"):
            warnings.append(
                "DEEPSEEK_API_KEY 未配置或是占位值：文件上传与解析仍可用，"
                "但抽取任务会失败。请在 .env 中填入真实 key。"
            )
        if self.LLM_MAX_TOKENS < 2048:
            warnings.append(
                f"LLM_MAX_TOKENS={self.LLM_MAX_TOKENS} 偏小：deepseek-flash 的思考 token "
                "会占用该预算，过小会导致返回空内容。建议不低于 4096。"
            )
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。lru_cache 让配置只在首次访问时解析一次。"""
    return Settings()


settings = get_settings()
