"""纯文本与 Markdown 提取。"""

from __future__ import annotations

from app.services.parsing.normalize import decode_bytes, normalize_text


def extract_plaintext(data: bytes) -> tuple[str, int | None, str]:
    """解码纯文本。

    :returns: ``(清洗后的文本, 页数, 解析器名)``，页数对纯文本无意义，返回 None
    """
    raw, encoding = decode_bytes(data)
    return normalize_text(raw), None, f"plaintext/{encoding}"
