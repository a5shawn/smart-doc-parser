"""文档解析层单元测试。

夹具是真实文件而非 mock：解析器的价值恰恰在于处理真实的排版噪声，
用构造出来的假数据测等于什么都没测。
"""

from __future__ import annotations

import pytest

from app.core.errors import CorruptedFileError, ParsingError
from app.services.parsing import extract_text, supported_extensions
from app.services.parsing.docx import extract_docx
from app.services.parsing.pdf import extract_pdf


class TestDispatch:
    def test_supported_extensions(self) -> None:
        assert supported_extensions() == {".pdf", ".docx", ".txt", ".md"}

    @pytest.mark.parametrize("filename", ["a.exe", "a.doc", "a.png", "noextension"])
    def test_unsupported_extension_is_rejected(self, filename: str) -> None:
        with pytest.raises(ParsingError) as exc_info:
            extract_text(filename, b"whatever")
        assert "暂不支持" in exc_info.value.message

    def test_extension_matching_is_case_insensitive(self, contract_pdf_bytes: bytes) -> None:
        # .PDF / .Pdf 在真实上传里很常见（尤其从 Windows 传上来的文件）
        doc = extract_text("CONTRACT.PDF", contract_pdf_bytes)
        assert doc.has_text_layer is True


class TestPdf:
    def test_extracts_chinese_text(self, contract_pdf_bytes: bytes) -> None:
        doc = extract_text("contract.pdf", contract_pdf_bytes)

        assert doc.has_text_layer is True
        assert doc.page_count == 1
        # 关键字段必须真的被提取出来，而不只是"有输出"
        for keyword in ("甲方", "乙方", "北京星辰科技有限公司", "1,280,000", "2026年3月15日"):
            assert keyword in doc.text, f"未提取到 {keyword}"

    def test_reports_which_parser_was_used(self, contract_pdf_bytes: bytes) -> None:
        """记录实际解析器，便于排查"同一文件换个环境结果不同"。"""
        doc = extract_text("contract.pdf", contract_pdf_bytes)
        assert doc.parser in {"pypdfium2", "pypdf"}

    def test_scanned_pdf_has_no_text_layer(self, scanned_pdf_bytes: bytes) -> None:
        """扫描件不报错，而是明确标记"没有文本层"，由上层给出友好提示。"""
        doc = extract_text("scanned.pdf", scanned_pdf_bytes)

        assert doc.char_count == 0
        assert doc.has_text_layer is False
        assert doc.page_count == 1

    def test_fallback_parser_works(self, contract_pdf_bytes: bytes) -> None:
        """直接调用 pypdf 兜底路径，保证主解析器不可用时仍有可用输出。"""
        from app.services.parsing.normalize import normalize_text
        from app.services.parsing.pdf import _extract_with_pypdf

        text, page_count = _extract_with_pypdf(contract_pdf_bytes)
        assert page_count == 1
        assert normalize_text(text).startswith("采购合同")

    def test_fallback_parser_kangxi_radicals_are_normalized(
        self, contract_pdf_bytes: bytes
    ) -> None:
        """回归测试：pypdf 对 CID 字体的中文 PDF 会产出康熙部首区的字符。

        「方」被提取成「⽅」(U+2F45)——肉眼完全一样，但精确匹配会全部失败。
        如果没有 normalize_text 里的 NFKC，这个断言会挂，且线上表现为
        "字段抽取结果看着对、代码里比对就是不相等"这种无从下手的 bug。
        """
        from app.services.parsing.normalize import normalize_text
        from app.services.parsing.pdf import _extract_with_pypdf

        raw, _ = _extract_with_pypdf(contract_pdf_bytes)

        # 未归一化时确实存在兼容区字符，证明确实复现了这个场景
        assert "甲⽅" in raw
        assert "甲⽅" not in normalize_text(raw)

        normalized = normalize_text(raw)
        assert "北京星辰科技有限公司" in normalized
        assert "上海云图信息技术有限公司" in normalized

    @pytest.mark.parametrize("garbage", [b"not a pdf at all", b"%PDF-1.4\ntruncated", b""])
    def test_corrupt_pdf_raises_corrupted_file(self, garbage: bytes) -> None:
        with pytest.raises(CorruptedFileError) as exc_info:
            extract_pdf(garbage)
        assert exc_info.value.code == "CORRUPTED_FILE"


class TestDocx:
    def test_extracts_paragraphs(self, resume_docx_bytes: bytes) -> None:
        doc = extract_text("resume.docx", resume_docx_bytes)

        assert doc.has_text_layer is True
        assert "王小明" in doc.text
        assert "138-0000-0000" in doc.text

    def test_docx_has_no_page_count(self, resume_docx_bytes: bytes) -> None:
        """分页由渲染器决定，Word 文件本身没有固定页数。"""
        doc = extract_text("resume.docx", resume_docx_bytes)
        assert doc.page_count is None

    def test_table_is_rendered_as_markdown(self, resume_docx_bytes: bytes) -> None:
        """python-docx 的 paragraphs 会完全跳过表格；表格恰恰是简历/发票的价值所在。"""
        doc = extract_text("resume.docx", resume_docx_bytes)
        lines = doc.text.splitlines()

        header = next(line for line in lines if line.startswith("|"))
        assert header == "| 时间 | 公司 | 职位 | 主要工作 |"

        # 必须带 Markdown 分隔行，模型才能识别这是表格而非普通文本
        separator_index = lines.index(header) + 1
        assert lines[separator_index].count("---") == 4

    def test_table_content_is_preserved(self, resume_docx_bytes: bytes) -> None:
        doc = extract_text("resume.docx", resume_docx_bytes)
        assert "上海云图信息技术有限公司" in doc.text
        assert "前端工程师" in doc.text
        assert "打包耗时从 8 分钟降至 90 秒" in doc.text

    def test_legacy_doc_gets_clear_message(self) -> None:
        """旧版 .doc 是二进制格式，python-docx 读不了，报错要告诉用户怎么办。"""
        with pytest.raises(CorruptedFileError) as exc_info:
            extract_docx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1old binary doc")
        assert "另存为 .docx" in exc_info.value.message


class TestPlaintext:
    def test_utf8(self) -> None:
        doc = extract_text("notes.txt", "合同金额：100 万元".encode())
        assert doc.text == "合同金额：100 万元"
        assert doc.parser == "plaintext/utf-8"

    def test_gb18030(self) -> None:
        doc = extract_text("notes.txt", "合同金额：100 万元".encode("gb18030"))
        assert "合同金额" in doc.text
        assert doc.parser == "plaintext/gb18030"

    def test_markdown_is_treated_as_plaintext(self) -> None:
        doc = extract_text("readme.md", b"# Title\n\nbody text long enough")
        assert doc.text.startswith("# Title")
