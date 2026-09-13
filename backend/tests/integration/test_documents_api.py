"""文档接口集成测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.services import document_service
from app.services.storage import storage

pytestmark = pytest.mark.integration

ENDPOINT = "/api/v1/documents"


def _file(name: str, data: bytes, content_type: str = "application/octet-stream"):
    return ("files", (name, data, content_type))


def _stored_files(upload_dir) -> list:
    """上传目录里的文件。目录可能还没被创建（没有任何文件成功落盘时），此时视为空。"""
    return sorted(upload_dir.iterdir()) if upload_dir.exists() else []


class TestUpload:
    async def test_upload_single_pdf(self, client: AsyncClient, contract_pdf_bytes: bytes) -> None:
        response = await client.post(ENDPOINT, files=[_file("contract.pdf", contract_pdf_bytes)])

        assert response.status_code == 200
        body = response.json()
        assert len(body["accepted"]) == 1
        assert body["duplicate"] == []
        assert body["rejected"] == []

        document = body["accepted"][0]
        assert document["filename"] == "contract.pdf"
        assert document["text_status"] == "extracted"
        assert document["char_count"] > 0
        assert document["page_count"] == 1
        assert document["text_preview"]
        assert document["mime_type"] == "application/pdf"

    async def test_uploaded_file_is_written_to_disk(
        self, client: AsyncClient, contract_pdf_bytes: bytes, isolated_upload_dir
    ) -> None:
        await client.post(ENDPOINT, files=[_file("contract.pdf", contract_pdf_bytes)])

        written = _stored_files(isolated_upload_dir)
        assert len(written) == 1
        # 落盘用的是服务端生成的 uuid 名，不是用户的原始文件名
        assert written[0].suffix == ".pdf"
        assert written[0].stem != "contract"

    async def test_duplicate_upload_reuses_existing_record(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        first = await client.post(ENDPOINT, files=[_file("contract.pdf", contract_pdf_bytes)])
        second = await client.post(ENDPOINT, files=[_file("副本.pdf", contract_pdf_bytes)])

        assert len(first.json()["accepted"]) == 1
        # 内容相同就算重复，哪怕文件名不一样
        assert len(second.json()["duplicate"]) == 1
        assert second.json()["accepted"] == []
        assert second.json()["duplicate"][0]["id"] == first.json()["accepted"][0]["id"]

        listing = await client.get(ENDPOINT)
        assert listing.json()["total"] == 1

    async def test_scanned_pdf_is_accepted_with_warning_status(
        self, client: AsyncClient, scanned_pdf_bytes: bytes
    ) -> None:
        """扫描件不是错误：入库并标记状态，用户能在列表里看到它和原因。"""
        response = await client.post(ENDPOINT, files=[_file("scan.pdf", scanned_pdf_bytes)])

        document = response.json()["accepted"][0]
        assert document["text_status"] == "no_text_layer"
        assert document["char_count"] == 0
        assert "扫描件" in document["text_error"]

    async def test_batch_upload_partial_success(
        self,
        client: AsyncClient,
        contract_pdf_bytes: bytes,
        resume_docx_bytes: bytes,
        plain_text_bytes: bytes,
    ) -> None:
        """一个坏文件不能拖垮整批——这是拖 20 个文件时最反直觉的行为。"""
        response = await client.post(
            ENDPOINT,
            files=[
                _file("contract.pdf", contract_pdf_bytes),
                _file("resume.docx", resume_docx_bytes),
                _file("notes.txt", plain_text_bytes),
                _file("virus.exe", b"MZ\x90\x00 malware"),
                _file("broken.pdf", b"%PDF-1.7\ntruncated garbage"),
            ],
        )

        body = response.json()
        assert len(body["accepted"]) == 3
        assert len(body["rejected"]) == 2

        rejected = {item["filename"]: item["code"] for item in body["rejected"]}
        assert rejected["virus.exe"] == "FILE_TYPE_MISMATCH"
        assert rejected["broken.pdf"] == "CORRUPTED_FILE"

    async def test_rejected_file_does_not_leave_row_in_database(
        self, client: AsyncClient, isolated_upload_dir
    ) -> None:
        """校验失败的文件既不该入库，也不该在磁盘上留垃圾。"""
        response = await client.post(ENDPOINT, files=[_file("virus.exe", b"MZ\x90\x00 nope")])

        assert len(response.json()["rejected"]) == 1
        listing = await client.get(ENDPOINT)
        assert listing.json()["total"] == 0
        assert _stored_files(isolated_upload_dir) == []

    async def test_empty_file_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(ENDPOINT, files=[_file("empty.pdf", b"")])
        assert response.json()["rejected"][0]["code"] == "EMPTY_FILE"

    async def test_traversal_filename_is_sanitized(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        response = await client.post(
            ENDPOINT, files=[_file("../../../etc/passwd.pdf", contract_pdf_bytes)]
        )

        document = response.json()["accepted"][0]
        assert "/" not in document["filename"]
        assert document["filename"] == "passwd.pdf"

    async def test_rejection_message_is_actionable(self, client: AsyncClient) -> None:
        """报错要告诉用户"改后缀名没用"，而不是干巴巴一句"格式错误"。"""
        response = await client.post(ENDPOINT, files=[_file("fake.pdf", b"not a pdf at all")])
        message = response.json()["rejected"][0]["message"]
        assert "扩展名" in message


class TestList:
    async def test_pagination(
        self, client: AsyncClient, contract_pdf_bytes: bytes, resume_docx_bytes: bytes
    ) -> None:
        await client.post(ENDPOINT, files=[_file("a.pdf", contract_pdf_bytes)])
        await client.post(ENDPOINT, files=[_file("b.docx", resume_docx_bytes)])

        first_page = await client.get(ENDPOINT, params={"page": 1, "page_size": 1})
        body = first_page.json()

        assert body["total"] == 2
        assert body["pages"] == 2
        assert len(body["items"]) == 1

        second_page = await client.get(ENDPOINT, params={"page": 2, "page_size": 1})
        assert second_page.json()["items"][0]["id"] != body["items"][0]["id"]

    async def test_list_does_not_return_full_text(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """列表接口不返回正文：20 份文档的全文会造成几十 MB 的无效传输。"""
        await client.post(ENDPOINT, files=[_file("contract.pdf", contract_pdf_bytes)])
        item = (await client.get(ENDPOINT)).json()["items"][0]

        assert "text_content" not in item
        assert item["text_preview"]

    async def test_page_size_upper_bound_is_enforced(self, client: AsyncClient) -> None:
        response = await client.get(ENDPOINT, params={"page_size": 10_000})
        assert response.status_code == 422

    async def test_empty_list(self, client: AsyncClient) -> None:
        body = (await client.get(ENDPOINT)).json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["pages"] == 0


class TestDetail:
    async def test_detail_includes_full_text(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        created = await client.post(ENDPOINT, files=[_file("contract.pdf", contract_pdf_bytes)])
        document_id = created.json()["accepted"][0]["id"]

        detail = await client.get(f"{ENDPOINT}/{document_id}")

        assert detail.status_code == 200
        body = detail.json()
        assert "北京星辰科技有限公司" in body["text_content"]
        assert body["task_count"] == 0

    async def test_missing_document_returns_404(self, client: AsyncClient) -> None:
        response = await client.get(f"{ENDPOINT}/{uuid4()}")

        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "DOCUMENT_NOT_FOUND"
        # 每个错误都带 request_id，用户报障时可以直接提供给后端定位日志
        assert error["request_id"]

    async def test_invalid_uuid_returns_422(self, client: AsyncClient) -> None:
        assert (await client.get(f"{ENDPOINT}/not-a-uuid")).status_code == 422


class TestDelete:
    async def test_delete_removes_row_and_file(
        self, client: AsyncClient, contract_pdf_bytes: bytes, isolated_upload_dir
    ) -> None:
        created = await client.post(ENDPOINT, files=[_file("contract.pdf", contract_pdf_bytes)])
        document_id = created.json()["accepted"][0]["id"]
        assert len(_stored_files(isolated_upload_dir)) == 1

        response = await client.delete(f"{ENDPOINT}/{document_id}")

        assert response.status_code == 200
        assert response.json() == {"deleted": True, "cascaded_tasks": 0}
        assert (await client.get(ENDPOINT)).json()["total"] == 0
        assert _stored_files(isolated_upload_dir) == []

    async def test_delete_reports_cascaded_task_count(
        self, client: AsyncClient, db_session, contract_pdf_bytes: bytes
    ) -> None:
        """删除会连带删掉任务与结果，返回数量是为了让前端能明确提示影响范围。"""
        created = await client.post(ENDPOINT, files=[_file("contract.pdf", contract_pdf_bytes)])
        document_id = created.json()["accepted"][0]["id"]

        from app.db.enums import TaskStatus
        from app.db.models import ExtractionTask

        for _ in range(2):
            db_session.add(
                ExtractionTask(
                    document_id=document_id,
                    template_key="contract",
                    template_name="合同",
                    status=TaskStatus.PENDING,
                )
            )
        await db_session.commit()

        response = await client.delete(f"{ENDPOINT}/{document_id}")
        assert response.json()["cascaded_tasks"] == 2

    async def test_delete_missing_document_returns_404(self, client: AsyncClient) -> None:
        assert (await client.delete(f"{ENDPOINT}/{uuid4()}")).status_code == 404

    async def test_delete_is_idempotent_for_file_already_gone(
        self, client: AsyncClient, contract_pdf_bytes: bytes, isolated_upload_dir
    ) -> None:
        """文件被外部清理掉时，删记录不应该失败——数据库才是事实来源。"""
        created = await client.post(ENDPOINT, files=[_file("contract.pdf", contract_pdf_bytes)])
        document_id = created.json()["accepted"][0]["id"]

        for path in isolated_upload_dir.iterdir():
            path.unlink()

        assert (await client.delete(f"{ENDPOINT}/{document_id}")).status_code == 200


class TestStorageSafety:
    async def test_path_traversal_is_blocked(self) -> None:
        """即使有人把非法存储名传进来，也会在路径解析这一层被拦住。"""
        from app.core.errors import CorruptedFileError

        with pytest.raises(CorruptedFileError):
            storage.path_for("../../../etc/passwd")

    async def test_path_for_accepts_generated_name(self) -> None:
        assert storage.path_for("abc123.pdf").name == "abc123.pdf"

    async def test_service_rejects_traversal_via_public_api(self, db_session) -> None:
        from app.core.errors import FileTypeMismatchError

        with pytest.raises(FileTypeMismatchError):
            await document_service.create_document(
                db_session, filename="../../etc/passwd.exe", data=b"MZ\x90\x00"
            )
