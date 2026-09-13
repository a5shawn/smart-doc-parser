"""上传文件校验。

三道关，缺一不可
----------------
1. **大小**：在读取阶段就设上限，避免一个超大文件把内存吃光
2. **扩展名白名单**：来自配置，不硬编码
3. **魔数（文件头）校验**：扩展名是用户随便改的，必须看真实字节

只做扩展名校验等于没做校验——把 ``evil.exe`` 改名成 ``contract.pdf``
就能通过。魔数才是文件类型的真实证据。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import settings
from app.core.errors import (
    EmptyFileError,
    FileTooLargeError,
    FileTypeMismatchError,
)
from app.services.parsing import supported_extensions

#: 扩展名 → 允许的文件头（任一匹配即可）
#:
#: 纯文本类没有可靠的魔数，因此不出现在这里——它们本来就没有结构可破坏，
#: 解析器也只是按文本解码，风险面完全不同。
MAGIC_NUMBERS: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    # .docx 本质是 zip 包（OOXML）。注意旧版 .doc 是 OLE 复合文档（\xd0\xcf\x11\xe0），
    # 不在此列，会在解析阶段被明确拒绝并提示用户另存为 .docx。
    ".docx": (b"PK\x03\x04",),
}

#: 需要校验魔数的扩展名
_MAGIC_CHECKED = frozenset(MAGIC_NUMBERS)

# 文件名里允许保留的字符：中英文、数字、常见符号。其余（尤其是路径分隔符与控制字符）替换掉。
_UNSAFE_FILENAME_CHARS = re.compile(r"[^\w一-鿿.\-()\[\] （）【】、,，.。]+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(filename: str, *, max_length: int = 255) -> str:
    """把用户提供的文件名清洗成可安全展示、可安全写进响应头的形式。

    即使本项目**从不**用原始文件名拼接磁盘路径，也要清洗，原因有二：

    1. 文件名会出现在界面和导出内容里，``../../etc/passwd`` 这种名字会误导用户；
    2. 文件名可能被放进 ``Content-Disposition`` 响应头，而其中若含 ``\\r\\n``
       就能注入额外的响应头（HTTP 响应头注入攻击）。
    """
    # 只取最后一段，丢掉任何目录成分。同时兼容 Windows 的反斜杠
    name = filename.replace("\\", "/").split("/")[-1]

    # 去掉控制字符（含 \r \n \t），这是防响应头注入的关键一步
    name = "".join(char for char in name if char.isprintable()).strip()

    if not name or name in {".", ".."}:
        return "unnamed"

    stem = Path(name).stem.upper()
    if stem in _WINDOWS_RESERVED:
        name = f"_{name}"

    if len(name) > max_length:
        suffix = Path(name).suffix
        name = name[: max_length - len(suffix)] + suffix

    return name


def detect_extension(filename: str) -> str:
    """取小写扩展名（含前导点）。"""
    return Path(filename).suffix.lower()


def validate_upload(filename: str, data: bytes) -> str:
    """校验上传文件，返回规范化后的扩展名。

    :raises EmptyFileError: 文件为空
    :raises FileTooLargeError: 超过 ``MAX_UPLOAD_SIZE_MB``
    :raises FileTypeMismatchError: 扩展名不在白名单，或文件头与扩展名不符
    """
    extension = detect_extension(filename)

    # ---- 第一关：扩展名白名单 ----
    allowed = set(settings.ALLOWED_EXTENSIONS) & supported_extensions()
    if extension not in allowed:
        raise FileTypeMismatchError(
            f"不支持的文件类型：{extension or '（无扩展名）'}",
            details={"allowed_extensions": sorted(allowed), "received": extension or None},
        )

    # ---- 第二关：大小 ----
    if len(data) == 0:
        raise EmptyFileError("上传的文件内容为空")

    if len(data) > settings.max_upload_size_bytes:
        raise FileTooLargeError(
            f"文件大小 {len(data) / 1024 / 1024:.1f} MB，"
            f"超过 {settings.MAX_UPLOAD_SIZE_MB} MB 上限",
            details={
                "size_bytes": len(data),
                "limit_bytes": settings.max_upload_size_bytes,
                "limit_mb": settings.MAX_UPLOAD_SIZE_MB,
            },
        )

    # ---- 第三关：魔数 ----
    if extension in _MAGIC_CHECKED:
        signatures = MAGIC_NUMBERS[extension]
        if not any(data.startswith(signature) for signature in signatures):
            # 提示用户"改扩展名没用"，比单纯说"格式错误"更有指导性
            raise FileTypeMismatchError(
                f"文件内容与扩展名 {extension} 不符。"
                "请确认文件本身没有被改过后缀名，或另存为正确的格式后重试。",
                details={
                    "extension": extension,
                    "expected_magic": [sig.hex() for sig in signatures],
                    "actual_prefix": data[:8].hex(),
                },
            )

    return extension


def canonical_mime_type(extension: str) -> str:
    """由扩展名推导 MIME 类型。

    **不使用客户端传来的 Content-Type**：那是调用方自称的，可以随意伪造，
    而我们已经有扩展名 + 魔数双重校验，推导出的类型比自称的可靠。
    """
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(extension, "application/octet-stream")
