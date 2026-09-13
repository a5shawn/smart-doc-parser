"""抽取编排的单元测试。

全部使用 :class:`FakeLLMClient`，**不打真实 API**。
覆盖的是"模型不听话"的各种分支——这些才是真正会出问题的地方。
"""

from __future__ import annotations

import json

import pytest

from app.ai.base import StreamKind
from app.ai.extractor import Extractor
from app.ai.fake import FakeLLMClient, ScriptedResponse
from app.ai.templates import get_builtin_template
from app.core.errors import LLMInvalidJSONError, LLMTimeoutError

CONTRACT_DOCUMENT = """采购合同

甲方：北京星辰科技有限公司
乙方：上海云图信息技术有限公司
合同金额：人民币 1,280,000 元
签订日期：2026年3月15日
"""


@pytest.fixture
def contract_template():
    return get_builtin_template("contract")


def _payload(**overrides) -> str:
    base = {
        "contract_name": "采购合同",
        "party_a": "北京星辰科技有限公司",
        "party_b": "上海云图信息技术有限公司",
        "amount": 1280000,
        "currency": "CNY",
        "sign_date": "2026-03-15",
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class TestHappyPath:
    async def test_extracts_structured_data(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload())])
        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )

        assert outcome.data["party_a"] == "北京星辰科技有限公司"
        assert outcome.data["amount"] == 1280000
        assert outcome.warnings == []
        assert outcome.truncated is False
        assert outcome.attempts == 1

    async def test_preserves_raw_output_for_troubleshooting(self, contract_template) -> None:
        """结果可疑时必须能回溯是模型输出错了还是解析错了。"""
        raw = _payload()
        client = FakeLLMClient([ScriptedResponse(content=raw)])
        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        assert outcome.raw_output == raw

    async def test_propagates_usage_and_model(self, contract_template) -> None:
        client = FakeLLMClient(
            [ScriptedResponse(content=_payload(), model="test-model", latency_ms=999)]
        )
        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        assert outcome.model == "test-model"
        assert outcome.latency_ms == 999
        assert outcome.usage.prompt_tokens == 100

    async def test_prompt_contains_document_text(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload())])
        await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        assert "北京星辰科技有限公司" in client.last_prompt()

    async def test_prompt_contains_json_keyword(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload())])
        await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        assert client.last_system_prompt().count("JSON") > 0


class TestJsonRepair:
    async def test_handles_markdown_fenced_output(self, contract_template) -> None:
        """模型把 JSON 包在代码块里是最常见的输出噪声。"""
        client = FakeLLMClient([ScriptedResponse(content=f"```json\n{_payload()}\n```")])
        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        assert outcome.data["party_a"] == "北京星辰科技有限公司"
        assert outcome.repairs
        assert client.call_count == 1

    async def test_handles_preamble(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=f"好的，结果如下：\n{_payload()}")])
        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        assert outcome.data["party_a"] == "北京星辰科技有限公司"


class TestRetryOnInvalidJson:
    async def test_retries_once_with_repair_prompt(self, contract_template) -> None:
        """第一次输出坏了就带着纠正指令重试，而不是让用户重跑整个任务。"""
        client = FakeLLMClient(
            [
                ScriptedResponse(content="抱歉，我无法处理这份文档。"),
                ScriptedResponse(content=_payload()),
            ]
        )

        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )

        assert client.call_count == 2
        assert outcome.attempts == 2
        assert outcome.data["party_a"] == "北京星辰科技有限公司"
        # 第二次必须带上纠正文案，而不是原样重发
        assert "不是合法的 json" in client.last_prompt()

    async def test_fails_after_second_invalid_response(self, contract_template) -> None:
        client = FakeLLMClient(
            [
                ScriptedResponse(content="不是 JSON"),
                ScriptedResponse(content="仍然不是 JSON"),
            ]
        )

        with pytest.raises(LLMInvalidJSONError):
            await Extractor(client).extract(
                template=contract_template,
                document_text=CONTRACT_DOCUMENT,
                max_input_chars=10_000,
            )
        assert client.call_count == 2

    async def test_empty_content_hints_at_max_tokens(self, contract_template) -> None:
        """推理模型的 max_tokens 陷阱：调用成功但 content 为空。"""
        client = FakeLLMClient([ScriptedResponse(content=""), ScriptedResponse(content="")])

        with pytest.raises(LLMInvalidJSONError) as exc_info:
            await Extractor(client).extract(
                template=contract_template,
                document_text=CONTRACT_DOCUMENT,
                max_input_chars=10_000,
            )
        assert "max_tokens" in exc_info.value.message

    async def test_transport_errors_are_not_retried_as_json_problems(
        self, contract_template
    ) -> None:
        """超时是传输层问题，不该走 JSON 修复路径。"""
        client = FakeLLMClient([ScriptedResponse(error=LLMTimeoutError())])

        with pytest.raises(LLMTimeoutError):
            await Extractor(client).extract(
                template=contract_template,
                document_text=CONTRACT_DOCUMENT,
                max_input_chars=10_000,
            )
        assert client.call_count == 1

    async def test_top_level_array_is_rejected(self, contract_template) -> None:
        """顶层不是对象就没法当字段集合用，即使 JSON 本身合法。"""
        client = FakeLLMClient(
            [ScriptedResponse(content="[1, 2, 3]"), ScriptedResponse(content="[4, 5]")]
        )
        with pytest.raises(LLMInvalidJSONError, match="顶层不是对象"):
            await Extractor(client).extract(
                template=contract_template,
                document_text=CONTRACT_DOCUMENT,
                max_input_chars=10_000,
            )


class TestWarnings:
    async def test_missing_required_field_produces_warning(self, contract_template) -> None:
        """漏字段是常态，应该给 warning 而不是让任务失败。"""
        payload = json.dumps({"contract_name": "采购合同", "party_b": "乙公司"}, ensure_ascii=False)
        client = FakeLLMClient([ScriptedResponse(content=payload)])

        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )

        kinds = {warning["kind"] for warning in outcome.warnings}
        assert "missing_required" in kinds
        assert any(warning["path"] == "party_a" for warning in outcome.warnings)

    async def test_currency_decorated_amount_is_coerced_with_warning(
        self, contract_template
    ) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload(amount="¥1,280,000 元"))])

        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )

        assert outcome.data["amount"] == 1280000.0
        assert any(warning["kind"] == "type_coerced" for warning in outcome.warnings)

    async def test_nullish_string_is_normalized(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload(sign_date="null"))])

        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        assert outcome.data["sign_date"] is None

    async def test_invented_field_is_flagged(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload(notes="口头补充约定"))])

        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        assert any(warning["kind"] == "unexpected_field" for warning in outcome.warnings)

    async def test_warnings_never_duplicate_same_path_and_kind(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload(amount="不是数字"))])

        outcome = await Extractor(client).extract(
            template=contract_template, document_text=CONTRACT_DOCUMENT, max_input_chars=10_000
        )
        keys = [(warning["path"], warning["kind"]) for warning in outcome.warnings]
        assert len(keys) == len(set(keys))


class TestTruncation:
    async def test_long_document_is_truncated_with_warning(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload())])
        long_document = "甲方：北京星辰科技有限公司。" * 2000

        outcome = await Extractor(client).extract(
            template=contract_template, document_text=long_document, max_input_chars=1000
        )

        assert outcome.truncated is True
        assert outcome.warnings[0]["kind"] == "input_truncated"

    async def test_prompt_tells_model_about_truncation(self, contract_template) -> None:
        """不告知截断，模型会把"没找到"当成"不存在"，导致静默的低质量结果。"""
        client = FakeLLMClient([ScriptedResponse(content=_payload())])
        await Extractor(client).extract(
            template=contract_template,
            document_text="内容" * 5000,
            max_input_chars=1000,
        )
        assert "已被省略" in client.last_prompt()

    async def test_short_document_is_not_truncated(self, contract_template) -> None:
        client = FakeLLMClient([ScriptedResponse(content=_payload())])
        outcome = await Extractor(client).extract(
            template=contract_template, document_text="短文档", max_input_chars=10_000
        )
        assert outcome.truncated is False


class TestStreaming:
    async def test_chunks_are_forwarded_to_callback(self, contract_template) -> None:
        received: list[tuple[StreamKind, str]] = []

        async def collect(kind: StreamKind, delta: str) -> None:
            received.append((kind, delta))

        client = FakeLLMClient([ScriptedResponse(content=_payload(), reasoning="先分析文档结构")])
        await Extractor(client).extract(
            template=contract_template,
            document_text=CONTRACT_DOCUMENT,
            max_input_chars=10_000,
            on_chunk=collect,
        )

        kinds = {kind for kind, _ in received}
        assert kinds == {StreamKind.REASONING, StreamKind.CONTENT}
        # 分片拼接后应当还原出完整正文
        content = "".join(delta for kind, delta in received if kind is StreamKind.CONTENT)
        assert content == _payload()

    async def test_reasoning_comes_before_content(self, contract_template) -> None:
        """推理模型的分片顺序：先思考后正文。前端据此显示"思考中"。"""
        received: list[StreamKind] = []

        async def collect(kind: StreamKind, _delta: str) -> None:
            received.append(kind)

        client = FakeLLMClient([ScriptedResponse(content=_payload(), reasoning="思考内容")])
        await Extractor(client).extract(
            template=contract_template,
            document_text=CONTRACT_DOCUMENT,
            max_input_chars=10_000,
            on_chunk=collect,
        )

        assert received[0] is StreamKind.REASONING
        assert received[-1] is StreamKind.CONTENT

    async def test_failing_callback_does_not_break_extraction(self, contract_template) -> None:
        """推送进度失败是次要问题，不该让整个抽取任务失败。"""

        async def broken(_kind: StreamKind, _delta: str) -> None:
            raise RuntimeError("推送失败")

        # FakeLLMClient 直接调用回调，这里用真实客户端的保护逻辑来验证行为
        from app.ai.deepseek_client import DeepSeekClient

        await DeepSeekClient._safe_chunk(broken, StreamKind.CONTENT, "x")
