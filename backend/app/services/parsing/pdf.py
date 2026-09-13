"""PDF 文本提取。

双解析器策略
------------
主用 **pypdfium2**：它是 Google PDFium 的绑定，文本提取质量（尤其是分栏、
表格排版）明显优于纯 Python 实现。
兜底用 **pypdf**：纯 Python、零系统依赖，保证在精简镜像里一定能跑。

pypdfium2 需要加载自带的 pdfium 动态库，在极端精简的容器里可能失败。
这里用「尝试导入 + 运行时降级」而不是直接依赖它，让服务在任何环境下都能启动，
最多是解析质量下降——这比整个服务起不来要好。
"""

from __future__ import annotations

import io

from app.core.errors import CorruptedFileError
from app.core.logging import get_logger
from app.services.parsing.normalize import has_text_layer, normalize_text

logger = get_logger(__name__)

#: 一次尝试导入；失败后不再重复 import（import 失败会走异常路径，代价较高）
_pdfium = None
_pdfium_import_error: str | None = None
try:  # pragma: no cover - 取决于运行环境
    import pypdfium2 as _pdfium_module

    _pdfium = _pdfium_module
except Exception as exc:
    _pdfium_import_error = f"{type(exc).__name__}: {exc}"


def _extract_with_pdfium(data: bytes) -> tuple[str, int]:
    """用 pypdfium2 提取。返回 (文本, 页数)。"""
    assert _pdfium is not None
    document = _pdfium.PdfDocument(data)
    try:
        page_count = len(document)
        chunks: list[str] = []
        for page in document:
            text_page = page.get_textpage()
            try:
                chunks.append(text_page.get_text_range())
            finally:
                text_page.close()
                page.close()
        return "\n\n".join(chunks), page_count
    finally:
        document.close()


def _extract_with_pypdf(data: bytes) -> tuple[str, int]:
    """用 pypdf 提取。返回 (文本, 页数)。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # 空密码能解开的加密 PDF 很常见（仅限制编辑），试一下
        try:
            reader.decrypt("")
        except Exception as exc:
            raise CorruptedFileError(
                "该 PDF 已加密，无法读取内容", details={"reason": str(exc)}
            ) from exc

    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(pages), len(reader.pages)


def extract_pdf(data: bytes) -> tuple[str, int, str]:
    """提取 PDF 文本。

    :returns: ``(清洗后的文本, 页数, 实际使用的解析器名)``
    :raises CorruptedFileError: 文件损坏或无法打开
    """
    errors: list[str] = []

    if _pdfium is not None:
        try:
            raw, page_count = _extract_with_pdfium(data)
            return normalize_text(raw), page_count, "pypdfium2"
        except Exception as exc:
            errors.append(f"pypdfium2: {type(exc).__name__}: {exc}")
            logger.warning("pdf_parser_failed", parser="pypdfium2", error=str(exc))
    else:
        errors.append(f"pypdfium2 不可用（{_pdfium_import_error}）")

    try:
        raw, page_count = _extract_with_pypdf(data)
        return normalize_text(raw), page_count, "pypdf"
    except CorruptedFileError:
        raise
    except Exception as exc:
        errors.append(f"pypdf: {type(exc).__name__}: {exc}")
        logger.warning("pdf_parser_failed", parser="pypdf", error=str(exc))

    raise CorruptedFileError(
        "PDF 解析失败，文件可能已损坏或格式不受支持",
        details={"attempts": errors},
    )


def pdf_has_text_layer(text: str) -> bool:
    """是否为「有文本层」的 PDF。

    返回 False 通常意味着这是扫描件——PDF 里只有图片，没有可提取的文字。
    此时不应报错，而应明确告诉用户原因（见 NoTextLayerError 的说明）。
    """
    return has_text_layer(text)
