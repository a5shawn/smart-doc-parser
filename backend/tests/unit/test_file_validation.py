"""上传文件校验的单元测试。

这些断言对应的是真实攻击面：改扩展名绕过类型检查、用超长文件名撑爆存储、
用路径穿越写文件、用换行符注入响应头。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.errors import (
    EmptyFileError,
    FileTooLargeError,
    FileTypeMismatchError,
)
from app.services.file_validation import (
    canonical_mime_type,
    detect_extension,
    sanitize_filename,
    validate_upload,
)

PDF_HEADER = b"%PDF-1.7\n"


class TestSanitizeFilename:
    def test_keeps_normal_chinese_name(self) -> None:
        assert sanitize_filename("采购合同-2026.pdf") == "采购合同-2026.pdf"

    def test_strips_directory_components(self) -> None:
        """显示名里不该出现路径，否则会误导用户以为文件存在别处。"""
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("/var/log/app.log") == "app.log"

    def test_handles_windows_separators(self) -> None:
        assert sanitize_filename(r"C:\Users\me\合同.pdf") == "合同.pdf"

    def test_removes_control_characters(self) -> None:
        """换行符若进入响应头就会造成 HTTP 响应头注入。"""
        assert sanitize_filename("合同\r\nX-Evil: 1.pdf") == "合同X-Evil: 1.pdf"
        assert "\n" not in sanitize_filename("a\nb.pdf")

    def test_windows_reserved_device_names(self) -> None:
        """CON / NUL 这类名字在 Windows 上无法创建文件，加前缀规避。"""
        assert sanitize_filename("CON.pdf").startswith("_")
        assert sanitize_filename("nul.txt").startswith("_")

    def test_empty_or_dot_names_fall_back(self) -> None:
        assert sanitize_filename("") == "unnamed"
        assert sanitize_filename("..") == "unnamed"
        assert sanitize_filename("   ") == "unnamed"

    def test_truncates_very_long_names_but_keeps_extension(self) -> None:
        name = sanitize_filename("长" * 500 + ".pdf")
        assert len(name) <= 255
        assert name.endswith(".pdf")


class TestDetectExtension:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("a.PDF", ".pdf"),
            ("a.Pdf", ".pdf"),
            ("合同.docx", ".docx"),
            ("noext", ""),
        ],
    )
    def test_detect(self, filename: str, expected: str) -> None:
        assert detect_extension(filename) == expected


class TestValidateUpload:
    def test_accepts_pdf_with_correct_magic(self, contract_pdf_bytes: bytes) -> None:
        assert validate_upload("contract.pdf", contract_pdf_bytes) == ".pdf"

    def test_accepts_docx_zip_header(self, resume_docx_bytes: bytes) -> None:
        assert validate_upload("resume.docx", resume_docx_bytes) == ".docx"

    @pytest.mark.parametrize("filename", ["evil.exe", "script.sh", "photo.png", "noext"])
    def test_rejects_extension_outside_whitelist(self, filename: str) -> None:
        with pytest.raises(FileTypeMismatchError) as exc_info:
            validate_upload(filename, PDF_HEADER)
        assert exc_info.value.code == "FILE_TYPE_MISMATCH"
        assert ".pdf" in exc_info.value.details["allowed_extensions"]

    def test_rejects_renamed_executable(self) -> None:
        """核心用例：把 exe 改名成 pdf 是绕过扩展名检查的标准手法。"""
        fake = b"MZ\x90\x00\x03\x00\x00\x00executable content"
        with pytest.raises(FileTypeMismatchError) as exc_info:
            validate_upload("contract.pdf", fake)

        assert exc_info.value.code == "FILE_TYPE_MISMATCH"
        assert "不符" in exc_info.value.message
        # 报错里要给出实际文件头，方便排查"为什么这份 PDF 传不上来"
        assert exc_info.value.details["actual_prefix"].startswith("4d5a")

    def test_docx_extension_with_pdf_content_is_rejected(self, contract_pdf_bytes: bytes) -> None:
        with pytest.raises(FileTypeMismatchError):
            validate_upload("resume.docx", contract_pdf_bytes)

    def test_rejects_empty_file(self) -> None:
        with pytest.raises(EmptyFileError) as exc_info:
            validate_upload("contract.pdf", b"")
        assert exc_info.value.code == "EMPTY_FILE"

    def test_rejects_oversized_file(
        self, monkeypatch: pytest.MonkeyPatch, contract_pdf_bytes: bytes
    ) -> None:
        monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE_MB", 1)
        oversized = PDF_HEADER + b"x" * (1024 * 1024 + 1)

        with pytest.raises(FileTooLargeError) as exc_info:
            validate_upload("big.pdf", oversized)

        assert exc_info.value.code == "FILE_TOO_LARGE"
        assert exc_info.value.details["limit_mb"] == 1

    def test_size_limit_is_checked_before_magic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """一个 100 MB 的坏文件应报"太大"而不是"格式不对"——后者会让用户
        以为文件格式有问题，反复转换格式却始终失败。"""
        monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE_MB", 1)
        with pytest.raises(FileTooLargeError):
            validate_upload("big.pdf", b"x" * (2 * 1024 * 1024))

    def test_plaintext_needs_no_magic(self) -> None:
        """纯文本没有可靠的魔数，不做文件头校验。"""
        assert validate_upload("notes.txt", b"any bytes here") == ".txt"


class TestCanonicalMimeType:
    @pytest.mark.parametrize(
        ("extension", "expected"),
        [
            (".pdf", "application/pdf"),
            (".txt", "text/plain"),
            (
                ".docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (".weird", "application/octet-stream"),
        ],
    )
    def test_mapping(self, extension: str, expected: str) -> None:
        assert canonical_mime_type(extension) == expected

    def test_does_not_trust_client_supplied_content_type(self) -> None:
        """MIME 由扩展名推导，不用调用方自称的 Content-Type——那是可以随便伪造的。"""
        assert canonical_mime_type(".pdf") == "application/pdf"
