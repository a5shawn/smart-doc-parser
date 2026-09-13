"""Prompt 的公共部分与硬性约束。"""

from __future__ import annotations

#: DeepSeek 使用 ``response_format={"type": "json_object"}`` 时，
#: prompt 里**必须**出现 "json" 这个词，否则直接返回 400，
#: 错误信息为 ``Prompt must contain the word 'json' in some form``。
JSON_KEYWORD = "json"


def assert_json_keyword(*parts: str) -> None:
    """发请求前校验 prompt 里含 "json"。

    在本地就失败而不是等一次网络往返再拿到 400——既省时间也省 token。
    这个断言真正防的是**未来改 prompt 时不小心把关键词删掉**：
    那时线上会突然大面积 400，而错误信息指向的是请求参数而不是 prompt 文案。
    """
    combined = " ".join(parts).lower()
    if JSON_KEYWORD not in combined:
        lengths = " / ".join(str(len(part)) for part in parts)
        raise ValueError(
            f"prompt 中必须包含 {JSON_KEYWORD!r} 这个词，否则 JSON mode 会被拒绝。"
            f"当前各段长度分别为 {lengths}。"
        )


# 注意：这段文案里必须保留 "JSON" 字样（上面有断言保护），
# 它是 JSON mode 能否工作的前提，不是普通的措辞。
#
# 本文件在 pyproject.toml 里豁免了 E501：prompt 文案的长行由内容决定，
# 拆行只会让需要反复打磨的提示词更难维护。
BASE_SYSTEM_PROMPT = """你是企业级文档信息抽取引擎。你的唯一任务是从用户提供的文档文本中抽取指定字段，并输出一个合法的 JSON 对象。

严格遵守以下规则：

1. 只输出 JSON 对象本身。不要输出解释、前言、总结，也不要用 Markdown 代码块包裹。
2. 绝对不要编造。文档中没有提到的信息，对应字段一律填 null。宁可留空，也不要根据常识推测。
3. 字段名必须与给定的 JSON Schema 完全一致。不要新增字段，不要改名，不要输出 Schema 之外的任何键。
4. 字符串字段保留文档中的原始表述，不要改写、翻译、润色或补充。
5. 数值字段只输出数字本身，不要带单位、千分位分隔符或货币符号。
6. 如果文档内容与预期不符（例如要求抽取合同但文档是一份简历），所有字段填 null，不要强行匹配。"""


#: 文档文本的分隔标记。用醒目的定界符把「指令」和「待处理内容」隔开，
#: 是抵御提示词注入的基础手段——文档里若写着"忽略以上指令"，
#: 模型更容易识别出那是被处理的数据而不是给它下的命令。
DOCUMENT_OPEN = "<<<DOCUMENT"
DOCUMENT_CLOSE = "DOCUMENT>>>"
