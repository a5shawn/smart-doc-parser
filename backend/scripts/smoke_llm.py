"""用真实 DeepSeek key 跑一次完整抽取，验证链路连通性。

跑法：``make smoke-llm``，或 ``cd backend && uv run python -m scripts.smoke_llm``

**这个脚本不进 CI**：它要花钱、依赖外网，而且上游抖动会造成随机失败。

它验证的是单元测试覆盖不到的部分：
- API key 是否有效、base_url 是否可达
- 流式分片是否真的分两阶段到达（思考 → 正文）
- usage 字段是否与代码里的解析逻辑对得上
- 成本换算出来的数字是否合理

会真实消耗额度，一次约 ¥0.01。

已知的输出噪声
--------------
脚本退出时可能打印一段 ``an error occurred during closing of asynchronous
generator ... generator didn't stop after athrow()`` 的堆栈。

这是上游 httpcore 的异步生成器清理时序问题：流式响应结束后的字节流生成器
留给垃圾回收去终结，而 ``asyncio.run()`` 已经先一步关闭了事件循环，
清理动作无处安放。**它不影响抽取结果，退出码仍是 0。**

试过 ``gc.collect()`` 以及 ``gc.collect() + await asyncio.sleep(0)`` 都无效，
因此没有保留这些无效的绕过代码。服务进程里事件循环长期存活，不存在这个问题
（每次请求后的垃圾回收都发生在循环运行期间）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from app.ai.deepseek_client import DeepSeekClient
from app.ai.extractor import Extractor
from app.ai.pricing import estimate_cost_usd, format_cost
from app.ai.prompts import build_extraction_prompt
from app.ai.templates import get_builtin_template
from app.core.config import settings
from app.services.parsing import extract_text

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"

DIVIDER = "─" * 78


def _load_document() -> str:
    """优先用真实的 PDF 夹具，没有就退回一段内置文本。"""
    pdf_path = FIXTURES_DIR / "contract.pdf"
    if pdf_path.exists():
        parsed = extract_text(pdf_path.name, pdf_path.read_bytes())
        return parsed.text

    return (
        "采购合同\n\n"
        "甲方：北京星辰科技有限公司\n"
        "乙方：上海云图信息技术有限公司\n"
        "合同金额：人民币 1,280,000 元\n"
        "签订日期：2026年3月15日\n"
    )


async def main() -> int:
    print(DIVIDER)
    print("DeepSeek 连通性与抽取链路冒烟测试")
    print(DIVIDER)
    print(f"模型      : {settings.DEEPSEEK_MODEL}")
    print(f"接口地址  : {settings.DEEPSEEK_BASE_URL}")
    print(f"密钥      : {settings.DEEPSEEK_API_KEY[:10]}...{settings.DEEPSEEK_API_KEY[-4:]}")
    print(f"流式输出  : {settings.LLM_STREAMING}")
    print(f"max_tokens: {settings.LLM_MAX_TOKENS}")
    print(DIVIDER)

    if not settings.DEEPSEEK_API_KEY or settings.DEEPSEEK_API_KEY.startswith("sk-xxx"):
        print("错误：DEEPSEEK_API_KEY 未配置。请在 .env 中填入真实密钥。", file=sys.stderr)
        return 1

    document = _load_document()
    template = get_builtin_template("contract")

    prompt = build_extraction_prompt(template=template, document_text=document)
    print(f"文档长度  : {len(document)} 字")
    print(f"prompt 长度: system {len(prompt.system)} / user {len(prompt.user)} 字")
    print(DIVIDER)

    # ---- 流式回调：观察分片的到达节奏 ----
    started = time.perf_counter()
    reasoning_chars = 0
    content_chars = 0
    first_content_at: float | None = None

    async def on_chunk(kind, delta: str) -> None:
        nonlocal reasoning_chars, content_chars, first_content_at
        if kind.value == "reasoning":
            reasoning_chars += len(delta)
        else:
            content_chars += len(delta)
            if first_content_at is None:
                first_content_at = time.perf_counter() - started

    async def on_restart() -> None:
        print("\n  [输出被截断，放大 max_tokens 后重跑]")

    client = DeepSeekClient()
    try:
        print("开始抽取（流式）...")
        outcome = await Extractor(client).extract(
            template=template,
            document_text=document,
            max_input_chars=settings.MAX_INPUT_CHARS,
            on_chunk=on_chunk,
            on_restart=on_restart,
        )
    except Exception as exc:
        print(f"\n失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()

    elapsed = time.perf_counter() - started
    cost = estimate_cost_usd(outcome.usage)

    print(f"\n{DIVIDER}")
    print("抽取结果")
    print(DIVIDER)
    for key, value in outcome.data.items():
        print(f"  {key:20} = {value!r}")

    if outcome.warnings:
        print(f"\n提示（{len(outcome.warnings)} 条）:")
        for warning in outcome.warnings:
            print(f"  [{warning['kind']}] {warning['path']}: {warning['message']}")

    print(f"\n{DIVIDER}")
    print("耗时与用量")
    print(DIVIDER)
    print(f"  总耗时           : {elapsed:.2f}s")
    print(f"  首个正文分片到达 : {first_content_at:.2f}s" if first_content_at else "  无正文分片")
    print(f"  思考阶段字符数   : {reasoning_chars}")
    print(f"  正文分片字符数   : {content_chars}")
    print(f"  模型耗时         : {outcome.latency_ms} ms")
    print(f"  模型             : {outcome.model}")
    print(f"  调用次数         : {outcome.attempts}")
    print(f"  prompt_tokens    : {outcome.usage.prompt_tokens}")
    print(f"  completion_tokens: {outcome.usage.completion_tokens}")
    print(f"    └ 其中思考     : {outcome.usage.reasoning_tokens}")
    print(f"  缓存命中 tokens  : {outcome.usage.cached_tokens}")
    print(f"\n  本次成本         : {format_cost(cost)}（${cost:.6f}）")

    print(f"\n{DIVIDER}")
    print("原始输出（前 400 字）")
    print(DIVIDER)
    print(outcome.raw_output[:400])

    print(f"\n{DIVIDER}")
    print("冒烟测试通过")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
