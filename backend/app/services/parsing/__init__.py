"""文档解析入口。

按扩展名分发到对应解析器，统一返回 :class:`ParsedDocument`。
新增格式只需在这里加一个分支——上层（storage / document_service / task_pipeline）无需改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import ParsingError
from app.services.parsing.docx import extract_docx
from app.services.parsing.normalize import (
    MIN_TEXT_CHARS,
    decode_bytes,
    has_text_layer,
    make_preview,
    normalize_text,
    truncate_for_llm,
)
from app.services.parsing.pdf import extract_pdf
from app.services.parsing.plaintext import extract_plaintext

__all__ = [
    "MIN_TEXT_CHARS",
    "ParsedDocument",
    "decode_bytes",
    "extract_text",
    "has_text_layer",
    "make_preview",
    "normalize_text",
    "truncate_for_llm",
]


@dataclass(slots=True, frozen=True)
class ParsedDocument:
    """解析结果。"""

    #: 清洗后的完整文本
    text: str
    #: 页数。.docx / .txt 无固定分页，为 None
    page_count: int | None
    #: 实际使用的解析器，便于排查「同一个文件换个环境结果不一样」
    parser: str
    #: 解析过程中的非致命提示
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def has_text_layer(self) -> bool:
        """是否有可用的文本层。

        False 通常意味着扫描版 PDF（只有图像，没有文字）。
        调用方应据此把文档标记为 ``no_text_layer``，而不是当成系统故障。
        """
        return has_text_layer(self.text)


#: 扩展名 → 解析器
_PARSERS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".txt": extract_plaintext,
    ".md": extract_plaintext,
}


def supported_extensions() -> frozenset[str]:
    return frozenset(_PARSERS)


def extract_text(filename: str, data: bytes) -> ParsedDocument:
    """从文件字节中提取纯文本。

    :param filename: 仅用于取扩展名。**不参与任何路径拼接。**
    :raises ParsingError: 扩展名不受支持
    """
    extension = Path(filename).suffix.lower()
    parser = _PARSERS.get(extension)
    if parser is None:
        raise ParsingError(
            f"暂不支持的文件类型：{extension or '（无扩展名）'}",
            details={"supported": sorted(_PARSERS)},
        )

    text, page_count, parser_name = parser(data)
    return ParsedDocument(text=text, page_count=page_count, parser=parser_name)
