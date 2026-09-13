"""配置的单元测试。

配置错误的表现往往是"服务起来了但行为不对"，比启动失败更难查。
因此 fail-fast 的校验与启动告警都必须锁在测试里。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

# backend/tests/unit/test_config.py -> unit -> tests -> backend -> <仓库根>
REPO_ROOT = Path(__file__).resolve().parents[3]


def _settings(**overrides) -> Settings:
    """构造一个不读 .env 的配置对象。"""
    base = {
        "_env_file": None,
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PASSWORD": "pwd",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


#: 一份各项都合规的生产配置。生产模式有启动预检，凡是构造 APP_ENV=production
#: 的测试都得从这里出发，否则会因为不相干的检查而失败。
_PROD_SAFE = {
    "APP_ENV": "production",
    "POSTGRES_PASSWORD": "a-strong-unique-password",
    "AUTH_ENABLED": True,
    "API_KEYS": ["secret"],
    "CORS_ORIGINS": ["https://example.com"],
    "DEEPSEEK_API_KEY": "sk-realkey",
}


def _production(**overrides) -> Settings:
    """构造一份合规的生产配置；传 overrides 可逐项破坏以验证预检。"""
    return _settings(**{**_PROD_SAFE, **overrides})


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


class TestProductionPreflight:
    """生产环境的危险配置**拒绝启动**，而不是像开发环境那样只告警。

    为什么需要这层保护：分环境用的是「基线 + 覆盖层」结构，覆盖文件只写差异项。
    漏写任何一项都会**静默继承基线的开发值**——比如忘了覆盖 POSTGRES_PASSWORD，
    数据库就会用公开写在 .env.example 里的默认密码对外服务。
    这类错误不报错、不告警，只会安静地上线。

    下面是"每一项都是必改项"这句话的可执行版本。
    """

    def test_safe_production_config_starts(self) -> None:
        """预检不能误伤——配置都改到位时必须能正常启动。"""
        assert _production().is_production is True

    def test_rejects_dev_database_password(self) -> None:
        """基线里的密码是公开的，等于没有密码。"""
        with pytest.raises(ValidationError, match="POSTGRES_PASSWORD"):
            _production(POSTGRES_PASSWORD="docparser_dev_pwd")

    def test_rejects_auth_disabled(self) -> None:
        """接口对公网开放会被人白刷大模型额度。"""
        with pytest.raises(ValidationError, match="AUTH_ENABLED"):
            _production(AUTH_ENABLED=False)

    def test_rejects_wildcard_cors(self) -> None:
        with pytest.raises(ValidationError, match="CORS_ORIGINS"):
            _production(CORS_ORIGINS=["*"])

    def test_rejects_placeholder_api_key(self) -> None:
        with pytest.raises(ValidationError, match="DEEPSEEK_API_KEY"):
            _production(DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxx")

    def test_reports_every_problem_at_once(self) -> None:
        """一次列全比修一个跑一次好——部署时来回重启很浪费时间。"""
        with pytest.raises(ValidationError) as excinfo:
            _production(
                POSTGRES_PASSWORD="docparser_dev_pwd",
                AUTH_ENABLED=False,
                DEEPSEEK_API_KEY="",
            )
        message = str(excinfo.value)
        for field in ("POSTGRES_PASSWORD", "AUTH_ENABLED", "DEEPSEEK_API_KEY"):
            assert field in message

    def test_development_tolerates_all_of_them(self) -> None:
        """**本地开发体验必须完全不受影响**，这是加这层检查的前提。"""
        settings = _settings(
            APP_ENV="development",
            POSTGRES_PASSWORD="docparser_dev_pwd",
            AUTH_ENABLED=False,
            CORS_ORIGINS="*",
            DEEPSEEK_API_KEY="",
        )
        assert settings.is_production is False


class TestEnvOverlay:
    """分环境的加载机制：基线提供全部默认值，覆盖层只写差异项。

    整个分环境方案都建立在这条机制上，因此值得单独锁住——
    哪天 pydantic-settings 改了合并语义，这些测试会先炸，
    而不是等到线上某个键悄悄退回默认值。
    """

    def test_overlay_overrides_base(self, tmp_path: Path) -> None:
        base = tmp_path / ".env"
        overlay = tmp_path / ".env.production"
        base.write_text("APP_NAME=base-app\nMAX_CONCURRENT_TASKS=3\n", encoding="utf-8")
        overlay.write_text("MAX_CONCURRENT_TASKS=9\n", encoding="utf-8")

        settings = Settings(
            _env_file=(base, overlay),  # type: ignore[arg-type]
            POSTGRES_HOST="localhost",
            POSTGRES_PASSWORD="pwd",
        )
        # 只在基线里的键保留下来……
        assert settings.APP_NAME == "base-app"
        # ……两处都有的键以覆盖层为准
        assert settings.MAX_CONCURRENT_TASKS == 9

    def test_config_sources_never_returns_empty(self) -> None:
        """排查用的字段不能返回空值。

        容器里没有配置文件（配置全部由 compose 以环境变量注入），
        如果这时返回 []，看日志的人会以为配置压根没加载上——
        恰恰在最需要这个字段的时候它最没用。
        """
        assert _settings().config_sources, "config_sources 不应为空"

    def test_missing_overlay_is_tolerated(self, tmp_path: Path) -> None:
        """development 不该被迫建一个空的 .env.development。

        这条行为是 compose 那边用 `required: false` 的对应物，
        两边都依赖它才能在只有基线的情况下正常跑起来。
        """
        base = tmp_path / ".env"
        base.write_text("APP_NAME=base-app\n", encoding="utf-8")

        settings = Settings(
            _env_file=(base, tmp_path / ".env.does-not-exist"),  # type: ignore[arg-type]
            POSTGRES_HOST="localhost",
            POSTGRES_PASSWORD="pwd",
        )
        assert settings.APP_NAME == "base-app"


class TestEnvTemplateSync:
    """`.env.example` 必须与 Settings 声明的字段保持同步。

    模板漏了某个键，部署的人根本不知道它可配——只能去读源码。
    模板多了某个键更糟：写了但不生效，排查时会往完全错误的方向找。

    这类漂移只有到线上才会被发现，而一条测试就能永久防住。
    """

    @staticmethod
    def _template_names(filename: str) -> set[str]:
        path = REPO_ROOT / filename
        assert path.exists(), f"缺少配置文件模板：{path}"
        names: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            names.add(line.split("=", 1)[0].strip())
        return names

    def test_template_covers_every_setting(self) -> None:
        missing = sorted(set(Settings.model_fields) - self._template_names(".env.example"))
        assert not missing, (
            f"这些配置项在后端可配，但 .env.example 里没有，部署的人不会知道它们存在：{missing}"
        )

    def test_template_has_no_stale_keys(self) -> None:
        # VITE_* 是给前端用的，后端的 Settings 不声明它们，属于正常情况
        known = set(Settings.model_fields)
        stale = sorted(
            name
            for name in self._template_names(".env.example")
            if name not in known and not name.startswith("VITE_")
        )
        assert not stale, (
            f"这些键写在 .env.example 里，但后端 Settings 没有声明，写了也不生效：{stale}"
        )

    def test_production_overlay_only_contains_known_keys(self) -> None:
        """覆盖层模板里的键必须都是真实存在的，否则是拼错或已删除的项。"""
        known = set(Settings.model_fields)
        unknown = sorted(self._template_names(".env.production.example") - known)
        assert not unknown, f".env.production.example 里有 Settings 不认识的键：{unknown}"


class TestDerivedValues:
    def test_max_upload_size_bytes(self) -> None:
        assert _settings(MAX_UPLOAD_SIZE_MB=20).max_upload_size_bytes == 20 * 1024 * 1024

    def test_is_production(self) -> None:
        assert _production().is_production is True
        assert _settings(APP_ENV="development").is_production is False

    @pytest.mark.parametrize("env", ["development", "production", "test", "invalid"])
    def test_env_accepts_only_known_values(self, env: str) -> None:
        if env == "invalid":
            with pytest.raises(ValidationError):
                _settings(APP_ENV=env)
        elif env == "production":
            # production 有启动预检，必须用合规配置构造。
            # 先取出值再断言，避免被 ruff 的全大写常量启发式误判成 Yoda 条件。
            resolved = _production().APP_ENV
            assert resolved == env
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
