"""鉴权的集成测试。

鉴权是"要么全对要么全错"的功能：漏掉一个接口就等于没做。
因此这里既验证开关行为，也验证健康检查这类**必须**豁免的例外。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import settings

pytestmark = pytest.mark.integration

DOCUMENTS = "/api/v1/documents"
TASKS = "/api/v1/tasks"
SECRET = "test-secret-key-0123456789"


@pytest.fixture
def auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "API_KEYS", [SECRET])


@pytest.fixture
def auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "API_KEYS", [])


class TestAuthDisabled:
    async def test_business_endpoints_are_open(
        self, client: AsyncClient, auth_disabled: None
    ) -> None:
        """默认关闭，方便本地开发与公网作品集演示。"""
        assert (await client.get(DOCUMENTS)).status_code == 200
        assert (await client.get(TASKS)).status_code == 200
        assert (await client.get("/api/v1/templates")).status_code == 200

    async def test_providing_a_key_anyway_is_harmless(
        self, client: AsyncClient, auth_disabled: None
    ) -> None:
        response = await client.get(DOCUMENTS, headers={"X-API-Key": "whatever"})
        assert response.status_code == 200


class TestAuthEnabled:
    async def test_missing_key_returns_401(self, client: AsyncClient, auth_enabled: None) -> None:
        for path in (DOCUMENTS, TASKS, "/api/v1/templates", "/api/v1/stats"):
            response = await client.get(path)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_valid_key_grants_access(self, client: AsyncClient, auth_enabled: None) -> None:
        response = await client.get(DOCUMENTS, headers={"X-API-Key": SECRET})
        assert response.status_code == 200

    async def test_wrong_key_is_rejected(self, client: AsyncClient, auth_enabled: None) -> None:
        response = await client.get(DOCUMENTS, headers={"X-API-Key": "wrong-key"})
        assert response.status_code == 401

    async def test_bearer_token_also_works(self, client: AsyncClient, auth_enabled: None) -> None:
        """curl 与 Swagger 里用 Bearer 更顺手，两条路都通更省事。"""
        response = await client.get(DOCUMENTS, headers={"Authorization": f"Bearer {SECRET}"})
        assert response.status_code == 200

    async def test_wrong_bearer_token_is_rejected(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        response = await client.get(DOCUMENTS, headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    async def test_non_bearer_scheme_is_rejected(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        response = await client.get(DOCUMENTS, headers={"Authorization": f"Basic {SECRET}"})
        assert response.status_code == 401

    async def test_write_endpoints_are_protected_too(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        """漏掉写接口比漏掉读接口严重得多，必须逐个确认。"""
        assert (await client.post(DOCUMENTS, files=[])).status_code == 401
        assert (
            await client.post(TASKS, json={"document_ids": [], "template_key": "contract"})
        ).status_code == 401

    async def test_401_carries_www_authenticate_header(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        """RFC 9110 要求 401 必须带这个头，缺了它某些客户端不会补上凭证。"""
        response = await client.get(DOCUMENTS)
        assert "www-authenticate" in {key.lower() for key in response.headers}

    async def test_401_message_mentions_the_header_name(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        """报错要告诉调用方该带哪个头，而不是干巴巴一句"未授权"。"""
        response = await client.get(DOCUMENTS)
        assert "X-API-Key" in response.json()["error"]["message"]


class TestHealthAlwaysOpen:
    async def test_health_never_requires_a_key(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        """健康检查带鉴权会让容器编排永远探活失败，容器永远起不来。"""
        assert (await client.get("/api/v1/health/live")).status_code == 200
        assert (await client.get("/api/v1/health/ready")).status_code == 200

    async def test_openapi_schema_is_reachable(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        assert (await client.get("/openapi.json")).status_code == 200


class TestErrorEnvelope:
    async def test_error_response_has_uniform_shape(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        """前端只需要处理一种错误结构，避免每个接口写一套判断。"""
        body = (await client.get(DOCUMENTS)).json()

        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message", "details", "request_id"}

    async def test_request_id_is_present_for_support(
        self, client: AsyncClient, auth_enabled: None
    ) -> None:
        """用户报障时提供这个 ID，后端能直接定位到那次请求的日志。"""
        request_id = (await client.get(DOCUMENTS)).json()["error"]["request_id"]
        assert request_id

    async def test_request_id_is_echoed_in_response_header(
        self, client: AsyncClient, auth_disabled: None
    ) -> None:
        response = await client.get(DOCUMENTS)
        assert response.headers["x-request-id"]

    async def test_inbound_request_id_is_preserved(
        self, client: AsyncClient, auth_disabled: None
    ) -> None:
        """网关传下来的请求 ID 要沿用，否则链路追踪会断掉。"""
        response = await client.get(DOCUMENTS, headers={"X-Request-ID": "trace-from-gateway"})
        assert response.headers["x-request-id"] == "trace-from-gateway"

    async def test_sse_response_disables_proxy_buffering(
        self, client: AsyncClient, auth_disabled: None
    ) -> None:
        """与 nginx 的 proxy_buffering off 是双重保险，任一侧生效即可。"""
        response = await client.get(DOCUMENTS)
        assert response.headers["x-accel-buffering"] == "no"
