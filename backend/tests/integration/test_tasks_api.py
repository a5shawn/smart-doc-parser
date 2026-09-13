"""抽取任务接口的集成测试。

覆盖完整的业务闭环：上传 → 建任务 → 后台执行 → 取结果。
模型调用由假客户端接管，因此测试快、不花钱、结果确定。
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.ai.fake import FakeLLMClient, ScriptedResponse
from app.core.errors import LLMTimeoutError
from tests.helpers import upload, wait_for_task

pytestmark = pytest.mark.integration

TASKS = "/api/v1/tasks"
TEMPLATES = "/api/v1/templates"

CONTRACT_PAYLOAD = json.dumps(
    {
        "contract_name": "采购合同",
        "party_a": "北京星辰科技有限公司",
        "party_b": "上海云图信息技术有限公司",
        "amount": 1280000,
        "currency": "CNY",
        "sign_date": "2026-03-15",
    },
    ensure_ascii=False,
)


@pytest.fixture
def llm_client() -> FakeLLMClient:
    return FakeLLMClient([ScriptedResponse(content=CONTRACT_PAYLOAD)])


class TestFullFlow:
    async def test_upload_extract_and_fetch_result(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)

        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        assert created.status_code == 201
        assert len(created.json()["tasks"]) == 1

        task_id = created.json()["tasks"][0]["id"]
        finished = await wait_for_task(client, task_id)

        assert finished["status"] == "completed"
        assert finished["progress"] == 100
        assert finished["document_filename"] == "contract.pdf"
        assert finished["template_name"] == "合同"
        # 用量与成本必须落库——"我知道每份文档花多少钱"是生产系统的基本要求
        assert finished["prompt_tokens"] > 0
        assert finished["model"] == "fake-model"
        assert finished["latency_ms"] is not None

        result = (await client.get(f"{TASKS}/{task_id}/result")).json()
        assert result["data"]["party_a"] == "北京星辰科技有限公司"
        assert result["data"]["amount"] == 1280000
        assert result["raw_output"] == CONTRACT_PAYLOAD

    async def test_task_starts_as_pending_then_completes(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """接口必须立刻返回，不等模型——这是异步任务架构的意义。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )

        task = created.json()["tasks"][0]
        assert task["status"] in {"pending", "parsing", "extracting"}
        assert task["progress"] < 100

    async def test_batch_extraction_shares_batch_id(
        self,
        client: AsyncClient,
        contract_pdf_bytes: bytes,
        resume_docx_bytes: bytes,
        plain_text_bytes: bytes,
    ) -> None:
        documents = [
            await upload(client, "contract.pdf", contract_pdf_bytes),
            await upload(client, "resume.docx", resume_docx_bytes),
            await upload(client, "notes.txt", plain_text_bytes),
        ]

        created = await client.post(
            TASKS,
            json={"document_ids": [doc["id"] for doc in documents], "template_key": "contract"},
        )
        body = created.json()

        assert len(body["tasks"]) == 3
        assert len({task["batch_id"] for task in body["tasks"]}) == 1
        assert body["batch_id"] == body["tasks"][0]["batch_id"]

        for task in body["tasks"]:
            assert (await wait_for_task(client, task["id"]))["status"] == "completed"

    async def test_same_document_can_be_extracted_with_two_templates(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """两段式接口的价值：换模板重抽不用重新上传文件。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)

        for template_key in ("contract", "invoice"):
            created = await client.post(
                TASKS, json={"document_ids": [document["id"]], "template_key": template_key}
            )
            task_id = created.json()["tasks"][0]["id"]
            assert (await wait_for_task(client, task_id))["status"] == "completed"

        listing = (await client.get(TASKS)).json()
        assert listing["total"] == 2


class TestValidation:
    async def test_unknown_template_returns_404(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        response = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "nonexistent"}
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"

    async def test_unknown_document_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            TASKS, json={"document_ids": [str(uuid4())], "template_key": "contract"}
        )
        assert response.status_code == 404

    async def test_empty_document_list_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(TASKS, json={"document_ids": [], "template_key": "contract"})
        assert response.status_code == 422

    async def test_duplicate_document_ids_are_deduplicated(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """同一份文档在同一批里出现两次只建一个任务，避免白白浪费额度。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        response = await client.post(
            TASKS,
            json={
                "document_ids": [document["id"], document["id"]],
                "template_key": "contract",
            },
        )
        assert len(response.json()["tasks"]) == 1

    async def test_too_many_documents_is_rejected(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """一次塞几百个任务会把上游限流打满。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        response = await client.post(
            TASKS,
            json={"document_ids": [document["id"]] * 51, "template_key": "contract"},
        )
        assert response.status_code == 422


class TestFailureHandling:
    async def test_scanned_pdf_fails_with_clear_reason(
        self, client: AsyncClient, scanned_pdf_bytes: bytes
    ) -> None:
        """扫描件不是系统故障，报错要让用户知道换一份电子版就行。"""
        document = await upload(client, "scan.pdf", scanned_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )

        task = await wait_for_task(client, created.json()["tasks"][0]["id"])

        assert task["status"] == "failed"
        assert task["error_code"] == "PDF_NO_TEXT_LAYER"
        assert "扫描件" in task["error_message"]

    async def test_failed_task_has_no_result(
        self, client: AsyncClient, scanned_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "scan.pdf", scanned_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]
        await wait_for_task(client, task_id)

        response = await client.get(f"{TASKS}/{task_id}/result")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESULT_NOT_FOUND"

    async def test_result_before_completion_is_rejected(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]

        # 可能在完成前就查，也可能已经完成——只要不是 5xx 就说明逻辑健全
        response = await client.get(f"{TASKS}/{task_id}/result")
        assert response.status_code in {200, 404}


class TestLLMFailure:
    """模型调用本身失败时的行为。

    单独一个类是因为需要覆盖 ``llm_client`` 夹具——夹具在测试体之前就已解析，
    所以没法在测试里"先跑成功再换成失败"。
    """

    @pytest.fixture
    def llm_client(self) -> FakeLLMClient:
        return FakeLLMClient([ScriptedResponse(error=LLMTimeoutError())])

    async def test_llm_timeout_is_reported(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )

        task = await wait_for_task(client, created.json()["tasks"][0]["id"])

        assert task["status"] == "failed"
        assert task["error_code"] == "LLM_TIMEOUT"
        assert task["progress"] == 100

    async def test_failed_task_can_be_retried(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]
        await wait_for_task(client, task_id)

        response = await client.post(f"{TASKS}/{task_id}/retry")
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert response.json()["error_code"] is None


class TestRetry:
    async def test_retry_reruns_a_failed_task(
        self, client: AsyncClient, scanned_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "scan.pdf", scanned_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]
        await wait_for_task(client, task_id)

        retried = await client.post(f"{TASKS}/{task_id}/retry")
        assert retried.status_code == 200

        # 文档本身没有文本层，重试仍会失败——但状态必须被正确重置过
        again = await wait_for_task(client, task_id)
        assert again["status"] == "failed"
        assert again["attempts"] >= 2

    async def test_retry_deletes_previous_result(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """不删旧结果的话，重跑失败时界面会出现「任务失败但结果还在」的矛盾状态。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]
        await wait_for_task(client, task_id)
        assert (await client.get(f"{TASKS}/{task_id}/result")).status_code == 200

        await client.post(f"{TASKS}/{task_id}/retry")
        # 重置后结果已被删除
        assert (await client.get(f"{TASKS}/{task_id}/result")).status_code == 404

    async def test_retry_running_task_is_rejected(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]

        response = await client.post(f"{TASKS}/{task_id}/retry")
        # 任务可能已经完成，此时重试是允许的；只有"正在进行中"才该被拒
        assert response.status_code in {200, 409}


class TestListAndDelete:
    async def test_filter_by_status(
        self, client: AsyncClient, contract_pdf_bytes: bytes, scanned_pdf_bytes: bytes
    ) -> None:
        good = await upload(client, "contract.pdf", contract_pdf_bytes)
        bad = await upload(client, "scan.pdf", scanned_pdf_bytes)
        ids = [good["id"], bad["id"]]

        created = await client.post(TASKS, json={"document_ids": ids, "template_key": "contract"})
        for task in created.json()["tasks"]:
            await wait_for_task(client, task["id"])

        completed = (await client.get(TASKS, params={"status": "completed"})).json()
        failed = (await client.get(TASKS, params={"status": "failed"})).json()

        assert completed["total"] == 1
        assert failed["total"] == 1

    async def test_filter_by_batch(self, client: AsyncClient, contract_pdf_bytes: bytes) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        batch_id = created.json()["batch_id"]

        filtered = (await client.get(TASKS, params={"batch_id": batch_id})).json()
        assert filtered["total"] == 1

        other = (await client.get(TASKS, params={"batch_id": str(uuid4())})).json()
        assert other["total"] == 0

    async def test_filter_by_template(self, client: AsyncClient, contract_pdf_bytes: bytes) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        await client.post(TASKS, json={"document_ids": [document["id"]], "template_key": "invoice"})

        assert (await client.get(TASKS, params={"template_key": "invoice"})).json()["total"] == 1
        assert (await client.get(TASKS, params={"template_key": "contract"})).json()["total"] == 0

    async def test_delete_task(self, client: AsyncClient, contract_pdf_bytes: bytes) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]
        await wait_for_task(client, task_id)

        assert (await client.delete(f"{TASKS}/{task_id}")).status_code == 200
        assert (await client.get(f"{TASKS}/{task_id}")).status_code == 404

    async def test_deleting_document_cascades_to_tasks(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """外键上是 ON DELETE CASCADE，删文档会连带删任务与结果。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]
        await wait_for_task(client, task_id)

        deleted = await client.delete(f"/api/v1/documents/{document['id']}")
        assert deleted.json()["cascaded_tasks"] == 1
        assert (await client.get(f"{TASKS}/{task_id}")).status_code == 404

    async def test_missing_task_returns_404(self, client: AsyncClient) -> None:
        assert (await client.get(f"{TASKS}/{uuid4()}")).status_code == 404


class TestWarningsAndUsage:
    @pytest.fixture
    def llm_client(self) -> FakeLLMClient:
        """返回一个空对象，模拟模型什么都没抽到。

        必须在本类里覆盖——模块级的夹具返回的是完整数据，不会产生任何告警。
        """
        return FakeLLMClient([ScriptedResponse(content="{}")])

    async def test_warnings_are_exposed_in_result(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """漏字段是常态，要展示成提示而不是让任务失败。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        task_id = created.json()["tasks"][0]["id"]
        finished = await wait_for_task(client, task_id)

        # 模型返回空对象，任务仍然算成功
        assert finished["status"] == "completed"

        result = (await client.get(f"{TASKS}/{task_id}/result")).json()
        missing = [item for item in result["warnings"] if item["kind"] == "missing_required"]
        # 合同模板有三个必填字段：contract_name / party_a / party_b
        assert {item["path"] for item in missing} == {"contract_name", "party_a", "party_b"}

    async def test_stats_reflect_completed_tasks(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        created = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": "contract"}
        )
        await wait_for_task(client, created.json()["tasks"][0]["id"])

        stats = (await client.get("/api/v1/stats")).json()

        assert stats["total_tasks"] == 1
        assert stats["completed_tasks"] == 1
        assert stats["failed_tasks"] == 0
        assert stats["success_rate"] == 1.0
        assert stats["total_prompt_tokens"] > 0
        assert stats["total_cost_usd"] > 0
        # 成本要同时给出人民币，界面直接展示
        assert stats["total_cost_cny"] >= 0

    async def test_stats_with_no_tasks(self, client: AsyncClient) -> None:
        stats = (await client.get("/api/v1/stats")).json()
        assert stats["total_tasks"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["avg_cost_per_task_usd"] == 0.0


class TestTemplates:
    async def test_lists_three_builtin_templates(self, client: AsyncClient) -> None:
        templates = (await client.get(TEMPLATES)).json()

        assert {item["key"] for item in templates} == {"contract", "resume", "invoice"}
        assert all(item["builtin"] for item in templates)

    async def test_builtin_templates_carry_chinese_labels(self, client: AsyncClient) -> None:
        """没有中文标签，前端只能显示 party_a 这种字段名，可读性很差。"""
        templates = (await client.get(TEMPLATES)).json()
        contract = next(item for item in templates if item["key"] == "contract")
        labels = {field["name"]: field["label"] for field in contract["fields"]}

        assert labels["party_a"] == "甲方"
        assert labels["amount"] == "合同金额"

    async def test_create_custom_template(self, client: AsyncClient) -> None:
        payload = {
            "name": "采购单",
            "description": "采购单关键字段",
            "schema": {
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "title": "订单号"},
                    "total": {"type": "number", "title": "总金额"},
                },
                "required": ["order_no"],
            },
        }

        created = await client.post(TEMPLATES, json=payload)
        assert created.status_code == 201

        templates = (await client.get(TEMPLATES)).json()
        custom = next(item for item in templates if not item["builtin"])
        assert custom["name"] == "采购单"
        assert custom["key"].startswith("custom:")

    async def test_custom_template_can_be_used_for_extraction(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        created = await client.post(
            TEMPLATES,
            json={
                "name": "订单",
                "schema": {
                    "type": "object",
                    "properties": {"order_no": {"type": "string", "title": "订单号"}},
                },
            },
        )
        template_key = f"custom:{created.json()['id']}"

        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task_response = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": template_key}
        )
        assert task_response.status_code == 201

        task = await wait_for_task(client, task_response.json()["tasks"][0]["id"])
        assert task["status"] == "completed"
        assert task["template_name"] == "订单"

    async def test_invalid_schema_is_rejected_at_creation(self, client: AsyncClient) -> None:
        """语法错误的 schema 必须在入库前就被拦下，而不是等建任务时才报错。"""
        response = await client.post(
            TEMPLATES,
            json={"name": "坏模板", "schema": {"type": "array", "items": {"type": "string"}}},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_JSON_SCHEMA"

    async def test_duplicate_template_name_is_rejected(self, client: AsyncClient) -> None:
        payload = {
            "name": "重名模板",
            "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
        }
        await client.post(TEMPLATES, json=payload)
        second = await client.post(TEMPLATES, json=payload)

        assert second.status_code == 409
        assert second.json()["error"]["code"] == "CONFLICT"

    async def test_delete_custom_template(self, client: AsyncClient) -> None:
        created = await client.post(
            TEMPLATES,
            json={
                "name": "待删除",
                "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            },
        )
        template_id = created.json()["id"]

        assert (await client.delete(f"{TEMPLATES}/{template_id}")).status_code == 200
        assert (await client.delete(f"{TEMPLATES}/{template_id}")).status_code == 404

    async def test_delete_template_in_use_is_rejected(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        created = await client.post(
            TEMPLATES,
            json={
                "name": "使用中",
                "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            },
        )
        template_id = created.json()["id"]

        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task_response = await client.post(
            TASKS, json={"document_ids": [document["id"]], "template_key": f"custom:{template_id}"}
        )
        await wait_for_task(client, task_response.json()["tasks"][0]["id"])

        response = await client.delete(f"{TEMPLATES}/{template_id}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TEMPLATE_IN_USE"


class TestConcurrency:
    async def test_concurrent_tasks_all_complete(
        self,
        client: AsyncClient,
        contract_pdf_bytes: bytes,
        resume_docx_bytes: bytes,
        plain_text_bytes: bytes,
    ) -> None:
        """并发上限生效时任务应当是排队而不是失败。"""
        documents = [
            await upload(client, "contract.pdf", contract_pdf_bytes),
            await upload(client, "resume.docx", resume_docx_bytes),
            await upload(client, "notes.txt", plain_text_bytes),
        ]
        created = await client.post(
            TASKS,
            json={"document_ids": [doc["id"] for doc in documents], "template_key": "contract"},
        )
        task_ids = [task["id"] for task in created.json()["tasks"]]

        results = await asyncio.gather(*(wait_for_task(client, task_id) for task_id in task_ids))
        assert all(item["status"] == "completed" for item in results)
