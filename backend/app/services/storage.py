"""上传文件的磁盘存储。

安全要点
--------
**用户提供的文件名永远不参与磁盘路径拼接。**
落盘时一律用服务端生成的 ``<uuid4><ext>``，原始文件名只存进数据库供展示。
这样路径穿越（``../../``）、空字节截断、Windows 保留设备名之类的
一整类问题从根上就不存在——不是靠过滤，而是靠根本不使用。

文件 IO 通过 ``anyio.to_thread`` 放到线程池执行，避免几十 MB 的读写
阻塞事件循环、拖慢同一进程内的其它请求。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import anyio

from app.core.config import settings
from app.core.errors import CorruptedFileError, ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


def compute_sha256(data: bytes) -> str:
    """内容指纹，用于去重。"""
    return hashlib.sha256(data).hexdigest()


class StorageService:
    """负责上传文件的落盘、读取与删除。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or settings.UPLOAD_DIR).resolve()

    # ---------------------------- 路径 ----------------------------
    def path_for(self, stored_name: str) -> Path:
        """由存储名得到绝对路径，并校验其确实落在上传目录内。

        ``stored_name`` 全部由本服务生成，理论上不会出问题；
        这里再校验一次是纵深防御——万一将来有人从数据库或用户输入
        把值传进来，这一层能兜住。
        """
        candidate = (self.root / stored_name).resolve()
        if not candidate.is_relative_to(self.root):
            raise CorruptedFileError("非法的文件路径", details={"stored_name": stored_name})
        return candidate

    async def ensure_root(self) -> None:
        """确保上传目录存在。容器启动时挂载的卷可能是空的。"""
        try:
            await anyio.to_thread.run_sync(lambda: self.root.mkdir(parents=True, exist_ok=True))
        except OSError as exc:
            raise ServiceUnavailableError(
                f"上传目录不可写：{self.root}",
                details={"reason": str(exc)},
            ) from exc

    # ---------------------------- 读写 ----------------------------
    async def save(self, data: bytes, extension: str) -> str:
        """写入文件，返回存储名（形如 ``3f2a....pdf``）。"""
        stored_name = f"{uuid4().hex}{extension}"
        target = self.path_for(stored_name)
        await self.ensure_root()

        def _write() -> None:
            # 先写临时文件再原子重命名：避免进程在写一半时挂掉，
            # 留下一个半截文件被后续请求当成完整文件读取
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(data)
            temporary.replace(target)

        try:
            await anyio.to_thread.run_sync(_write)
        except OSError as exc:
            raise ServiceUnavailableError(
                "文件写入失败，请检查磁盘空间与目录权限",
                details={"reason": str(exc)},
            ) from exc

        logger.debug("file_saved", stored_name=stored_name, size_bytes=len(data))
        return stored_name

    async def read(self, stored_name: str) -> bytes:
        target = self.path_for(stored_name)
        try:
            return await anyio.to_thread.run_sync(target.read_bytes)
        except FileNotFoundError as exc:
            raise CorruptedFileError(
                "文件已不存在，可能已被清理", details={"stored_name": stored_name}
            ) from exc

    async def delete(self, stored_name: str) -> None:
        """删除文件。文件不存在视为成功（幂等）。"""
        target = self.path_for(stored_name)
        try:
            await anyio.to_thread.run_sync(target.unlink)
            logger.debug("file_deleted", stored_name=stored_name)
        except FileNotFoundError:
            logger.warning("file_already_missing", stored_name=stored_name)
        except OSError as exc:
            # 删文件失败不应该让整个删除接口失败：数据库记录已删，
            # 残留的孤儿文件顶多占点磁盘，后续可以用清理任务处理
            logger.error(
                "file_delete_failed", stored_name=stored_name, error=str(exc), exc_info=True
            )


#: 进程内单例
storage = StorageService()
