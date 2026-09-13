"""配置的单元测试。

配置错误的表现往往是"服务起来了但行为不对"，比启动失败更难查。
因此 fail-fast 的校验与启动告警都必须锁在测试里。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    """构造一个不读 .env 的配置对象。"""
    base = {
        "_env_file": None,
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PASSWORD": "pwd",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestFailFast:
    def test_auth_enabled_without_keys_is_rejected(self) -> None:
        """这是最容易犯也最难发现的配置错误：以为开了鉴权，其实谁也不放行。"""
        with pytest.raises(ValidationError, match="API_KEYS"):
            _settings(AUTH_ENABLED=True, API_KEYS=[])

    def test_auth_enabled_with_keys_is_accepted(self) -> None:
        settings = _settings(AUTH_ENABLED=True, API_KEYS=["secret"])
        assert settings.AUTH_ENABLED is True

    def test_auth_disabled_without_keys_is_fine(self) -> None:
        assert _settings(AUTH_ENABLED=False, API_KEYS=[]).AUTH_ENABLED is False


class TestCommaSeparatedParsing:
    def test_api_keys_splits_on_comma(self) -> None:
        assert _settings(API_KEYS="key1,key2,key3").API_KEYS == ["key1", "key2", "key3"]

    def test_trims_whitespace(self) -> None:
        assert _settings(API_KEYS=" key1 , key2 ").API_KEYS == ["key1", "key2"]

    def test_empty_string_yields_empty_list(self) -> None:
        assert _settings(API_KEYS="").API_KEYS == []

    def test_cors_origins_splits_on_comma(self) -> None:
        settings = _settings(CORS_ORIGINS="https://a.com,https://b.com")
        assert settings.CORS_ORIGINS == ["https://a.com", "https://b.com"]

    def test_dangling_commas_are_ignored(self) -> None:
        assert _settings(API_KEYS="key1,,key2,").API_KEYS == ["key1", "key2"]


class TestAllowedExtensions:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("pdf,docx", [".pdf", ".docx"]),
            (".PDF,.Docx", [".pdf", ".docx"]),
            (" .pdf , .docx ", [".pdf", ".docx"]),
            ("", []),
        ],
    )
    def test_normalization(self, raw: str, expected: list[str]) -> None:
        """扩展名的写法有 .PDF / pdf / .Pdf 三种，不统一会导致上传校验漏判。"""
        # 先取出值再断言：直接写 `....ALLOWED_EXTENSIONS == expected` 会被 ruff
        # 的全大写常量启发式误判成 Yoda 条件
        normalized = _settings(ALLOWED_EXTENSIONS=raw).ALLOWED_EXTENSIONS
        assert normalized == expected


class TestDatabaseUrl:
    def test_builds_asyncpg_dsn(self) -> None:
        settings = _settings(
            POSTGRES_HOST="db",
            POSTGRES_PORT=5432,
            POSTGRES_USER="user",
            POSTGRES_PASSWORD="pwd",
            POSTGRES_DB="mydb",
        )
        assert settings.database_url == "postgresql+asyncpg://user:pwd@db:5432/mydb"

    def test_escapes_special_characters_in_password(self) -> None:
        """密码里有 @ 或 : 时不转义会拼出无法解析的 DSN。"""
        settings = _settings(POSTGRES_PASSWORD="p@ss:w/rd")
        assert "p%40ss%3Aw%2Frd" in settings.database_url

    def test_sync_url_for_alembic(self) -> None:
        settings = _settings()
        assert settings.sync_database_url.startswith("postgresql://")
        assert "+asyncpg" not in settings.sync_database_url

    def test_test_database_url_uses_separate_database(self) -> None:
        """测试库必须与业务库分离，否则跑测试会清空开发数据。"""
        settings = _settings(POSTGRES_DB="app", POSTGRES_TEST_DB="app_test")
        assert settings.database_url.endswith("/app")
        assert settings.test_database_url.endswith("/app_test")


class TestUploadDir:
    def test_relative_path_resolves_against_repo_root(self) -> None:
        """不解析成绝对路径的话，从不同目录启动会写到不同地方。"""
        settings = _settings(UPLOAD_DIR="./data/uploads")
        assert settings.UPLOAD_DIR.is_absolute()
        assert settings.UPLOAD_DIR.name == "uploads"

    def test_absolute_path_is_kept(self) -> None:
        settings = _settings(UPLOAD_DIR="/app/data/uploads")
        assert str(settings.UPLOAD_DIR) == "/app/data/uploads"


class TestStartupWarnings:
    def test_production_without_auth_warns(self) -> None:
        """接口对公网开放会被人白刷大模型额度，必须让运维一眼看见。"""
        warnings = _settings(APP_ENV="production", AUTH_ENABLED=False).startup_warnings
        assert any("AUTH_ENABLED" in item for item in warnings)

    def test_production_with_wildcard_cors_warns(self) -> None:
        warnings = _settings(APP_ENV="production", CORS_ORIGINS="*").startup_warnings
        assert any("CORS" in item for item in warnings)

    def test_multiple_workers_warns_about_concurrency(self) -> None:
        """调度器是进程内的，多 worker 会让并发上限翻倍——这是知道边界在哪。"""
        warnings = _settings(BACKEND_WORKERS=4).startup_warnings
        assert any("BACKEND_WORKERS" in item for item in warnings)

    def test_missing_api_key_warns(self) -> None:
        warnings = _settings(DEEPSEEK_API_KEY="").startup_warnings
        assert any("DEEPSEEK_API_KEY" in item for item in warnings)

    def test_placeholder_api_key_warns(self) -> None:
        warnings = _settings(DEEPSEEK_API_KEY="sk-xxxxxxxxxxxx").startup_warnings
        assert any("DEEPSEEK_API_KEY" in item for item in warnings)

    def test_small_max_tokens_warns(self) -> None:
        """推理模型的思考 token 与正文共享预算，给小了会返回空内容。"""
        warnings = _settings(LLM_MAX_TOKENS=512).startup_warnings
        assert any("LLM_MAX_TOKENS" in item for item in warnings)

    def test_healthy_development_config_has_no_warnings(self) -> None:
        settings = _settings(
            APP_ENV="development",
            AUTH_ENABLED=False,
            DEEPSEEK_API_KEY="sk-realkey",
            BACKEND_WORKERS=1,
            LLM_MAX_TOKENS=8192,
        )
        assert settings.startup_warnings == []

    def test_production_with_auth_has_no_auth_warning(self) -> None:
        settings = _settings(
            APP_ENV="production",
            AUTH_ENABLED=True,
            API_KEYS=["secret"],
            CORS_ORIGINS="https://example.com",
            DEEPSEEK_API_KEY="sk-realkey",
        )
        assert not any("AUTH_ENABLED" in item for item in settings.startup_warnings)


class TestDerivedValues:
    def test_max_upload_size_bytes(self) -> None:
        assert _settings(MAX_UPLOAD_SIZE_MB=20).max_upload_size_bytes == 20 * 1024 * 1024

    def test_is_production(self) -> None:
        assert _settings(APP_ENV="production").is_production is True
        assert _settings(APP_ENV="development").is_production is False

    @pytest.mark.parametrize("env", ["development", "production", "test", "invalid"])
    def test_env_accepts_only_known_values(self, env: str) -> None:
        if env == "invalid":
            with pytest.raises(ValidationError):
                _settings(APP_ENV=env)
        else:
            resolved = _settings(APP_ENV=env).APP_ENV
            assert resolved == env


class TestBounds:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("MAX_UPLOAD_SIZE_MB", 0),
            ("LLM_MAX_TOKENS", 100),
            ("MAX_CONCURRENT_TASKS", 0),
            ("POSTGRES_PORT", 70000),
            ("LLM_TEMPERATURE", 5.0),
        ],
    )
    def test_out_of_range_values_are_rejected(self, field: str, value: float) -> None:
        """非法取值要在启动时就拒绝，而不是等某个请求触发了怪行为。"""
        with pytest.raises(ValidationError):
            _settings(**{field: value})
