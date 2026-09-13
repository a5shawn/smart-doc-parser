"""纯文本清洗与截断。

解析器（pypdfium2 / python-docx / 纯文本）产出的原始文本往往带有
各种排版残留：Windows 换行、零宽字符、连续空行、行尾空格。
统一在这里处理，让下游的 prompt 构造与 token 估算面对干净的输入。
"""

from __future__ import annotations

import codecs
import re
import unicodedata

# 零宽字符与 BOM：肉眼不可见，但会白白消耗 token，也可能干扰模型对字段边界的判断
_INVISIBLE_CHARS = re.compile(r"[​‌‍⁠﻿]")
# 3 个及以上连续换行压成 2 个（保留段落感，去掉大段空白）
_EXCESS_NEWLINES = re.compile(r"\n{3,}")
# 行尾空白
_TRAILING_SPACES = re.compile(r"[ \t]+$", re.MULTILINE)
# 除换行和制表符外的控制字符
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: 判定「有文本层」的最少字符数。扫描版 PDF 逐页提取通常得到 0~几个字符。
MIN_TEXT_CHARS = 20

#: 需要做兼容性归一化的 Unicode 区段。
#:
#: 中文 PDF 用 CID 字体时，部分解析器（尤其是纯 Python 实现）会把字形映射到
#: 「康熙部首」等兼容区，产出**看起来一模一样但码位不同**的字符：
#: 例如「方」被提取成「⽅」(U+2F45)、「辰」被提取成「⾠」(U+2FA0)。
#: 后果是精确匹配全部失效——字段校验、全文检索、下游对比都会莫名其妙地失败，
#: 而肉眼看日志完全正常，极难排查。
#:
#: 这里只对这几个「兼容副本」区段做 NFKC，**不动全角标点**：
#: 整篇 NFKC 会把「：」变成「:」、「，」变成「,」，虽然模型看得懂，
#: 但会让界面展示的原文与上传的文档不一致。
_CJK_COMPAT_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),  # CJK 部首补充
    (0x2F00, 0x2FDF),  # 康熙部首
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
    (0xFE30, 0xFE4F),  # CJK 兼容形式
)


def _normalize_cjk_compat(text: str) -> str:
    """把兼容区汉字还原成标准码位。没有命中时原样返回，不做多余遍历。"""
    if not any(low <= ord(char) <= high for char in text for low, high in _CJK_COMPAT_RANGES):
        return text

    return "".join(
        unicodedata.normalize("NFKC", char)
        if any(low <= ord(char) <= high for low, high in _CJK_COMPAT_RANGES)
        else char
        for char in text
    )


def normalize_text(raw: str) -> str:
    """清洗原始文本。保守处理，不改变语义，只去除排版噪声。"""
    if not raw:
        return ""

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _normalize_cjk_compat(text)
    text = _INVISIBLE_CHARS.sub("", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _TRAILING_SPACES.sub("", text)
    text = _EXCESS_NEWLINES.sub("\n\n", text)
    return text.strip()


def decode_bytes(data: bytes) -> tuple[str, str]:
    """把字节流解码成文本，返回 ``(文本, 实际使用的编码)``。

    中文文档常见的编码依次尝试：
    utf-8-sig（带 BOM 的 UTF-8）→ utf-8 → gb18030（兼容 GBK/GB2312）→ big5。
    latin-1 作为最后兜底——它能把任意字节映射成字符而**不会抛异常**，
    保证解析流程不会因为一个编码怪异的文件就整个失败。

    先单独判 BOM 而不是直接把 ``utf-8-sig`` 放在第一个尝试：
    ``utf-8-sig`` 对**不带 BOM** 的普通 UTF-8 也能解码成功，
    那样所有文件都会被报成 ``utf-8-sig``，日志里反而看不出真实情况。
    """
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"

    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue

    return data.decode("latin-1", errors="replace"), "latin-1"


def has_text_layer(text: str) -> bool:
    """文本是否足够「有内容」。用于识别扫描版 PDF。"""
    return len(text.strip()) >= MIN_TEXT_CHARS


def make_preview(text: str, length: int = 500) -> str:
    """生成列表页用的短预览。"""
    cleaned = " ".join(text.split())
    if len(cleaned) <= length:
        return cleaned
    return cleaned[:length]


def truncate_for_llm(text: str, max_chars: int) -> tuple[str, bool]:
    """超长文本按「头 70% + 尾 30%」截断。

    为什么保留头尾而不是简单截断开头：合同的关键信息（甲乙方、金额、签署日期）
    既可能在开头，也可能在落款处；只留开头会丢掉落款。
    中间被省略的部分会以显式标记写入，让模型知道这里缺了内容，
    避免它把断裂的上下文当成完整信息来推断。
    """
    if len(text) <= max_chars:
        return text, False

    head_len = int(max_chars * 0.7)
    tail_len = max_chars - head_len
    omitted = len(text) - max_chars
    marker = f"\n\n[文档过长，此处省略约 {omitted} 字]\n\n"
    return text[:head_len] + marker + text[-tail_len:], True
