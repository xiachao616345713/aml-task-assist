"""资料整理的原文依据、输入格式和 HTTP 来源鉴权。"""

import hashlib
import io
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from task_assist.aml.auth import Actor
from task_assist.api.deps import get_service
from task_assist.api.routers import materials
from task_assist.core.errors import ResourceNotFound
from task_assist.features.materials.extraction import (
    Evidence, ExtractedDraft, extract_text, make_prefill,
)
from task_assist.infrastructure.telemetry import Trace

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class Model:
    def __init__(self, result):
        self.result = result

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        return self.result


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.source = {"id": "source-1", "filename": "说明.txt", "text_content": "资料显示：已核对登记信息，股权比例存在差异。",
                       "sha256": "digest", "backend_path": "proof/a.txt", "size_bytes": 50}
        self.workflow = {"id": "wf", "process_type": "risk", "task_id": "task"}

    def test_nonstandard_text_and_word_tables(self):
        self.assertEqual(extract_text("核查情况：存在差异".encode("gb18030"), "text/plain"), "核查情况：存在差异")
        raw = b'{\n "reason": "verified"\n}'
        self.assertEqual(extract_text(raw, "application/json"), raw.decode())
        with self.assertRaises(ValueError):
            extract_text(b'{broken}', "application/json")
        with self.assertRaises(ValueError):
            extract_text(b'\0\0binary', "text/plain")
        file = io.BytesIO()
        with zipfile.ZipFile(file, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>说明</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>股权比例</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
        self.assertEqual(extract_text(file.getvalue(), DOCX), "说明\n股权比例")
        with self.assertRaises(ValueError):
            extract_text(b'bad zip', DOCX)

    def test_grounded_summary_maps_only_known_business_fields(self):
        draft = ExtractedDraft(summary="登记信息已核对，股权比例存在差异，需人工核实。", evidence=[
            Evidence(source_id="source-1", quote="已核对登记信息，股权比例存在差异")])
        factory = lambda: Model(draft)
        result = make_prefill(self.workflow, [self.source], factory)
        self.assertEqual(result["fields"]["reason"], draft.summary)
        citation = result["provenance"][0]
        self.assertEqual(self.source["text_content"][citation["start"]:citation["end"]], citation["quote"])
        self.assertEqual(result["fields"]["fileList"][0]["filePath"], "proof/a.txt")
        self.assertTrue(result["review_required"])
        self.workflow["process_type"] = "bnfValidate"
        bnf = make_prefill(self.workflow, [self.source], factory)
        self.assertEqual(set(bnf["fields"]), {"fileList"})
        self.assertEqual(bnf["summary"], draft.summary)

    def test_unknown_source_invented_quote_and_missing_evidence_fail(self):
        for evidence in ([Evidence(source_id="other-user-source", quote="差异")],
                         [Evidence(source_id="source-1", quote="材料齐全，同意通过")], []):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                make_prefill(self.workflow, [self.source], lambda: Model(ExtractedDraft(summary="总结", evidence=evidence)))

    def test_unreadable_files_are_reported_without_model_call(self):
        self.source["text_content"] = None
        result = make_prefill(self.workflow, [self.source], lambda: self.fail("不应调用模型"))
        self.assertEqual(result["unreadable_sources"], ["source-1"])
        self.assertEqual(result["fields"]["reason"], "")


class SourceStore:
    def __init__(self):
        self.data = b"%PDF-1.4 original proof"
        self.digest = hashlib.sha256(self.data).hexdigest()

    def source(self, source_id, owner):
        if owner != "alice" or source_id not in {"proof", "legacy"}:
            raise ResourceNotFound("资源不存在或无权访问")
        return {"id": source_id, "filename": "证明.pdf", "content_type": "application/pdf",
                "content": self.data if source_id == "proof" else None,
                "text_content": "", "sha256": self.digest, "size_bytes": len(self.data), "material_task_id": "mat"}

    def material_task(self, task_id, owner):
        return {"workflow_id": "workflow"}

    def workflow(self, workflow_id, owner):
        return {"process_type": "risk"}

    def audit(self, *args):
        pass


class SourceApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(materials.router)

        @app.middleware("http")
        async def trace(request, call_next):
            request.state.trace = Trace("test")
            return await call_next(request)

        self.actor = Actor("alice", "alice", frozenset({"riskList:audit"}), "token")
        app.dependency_overrides[get_service] = lambda: SimpleNamespace(actor=self.actor)
        self.client = TestClient(app)
        self.store = SourceStore()
        self.patch = patch.object(materials, "state_store", self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.client.close)

    def test_metadata_and_original_have_same_digest_and_no_binary_in_json(self):
        detail = self.client.get("/api/sources/proof").json()["data"]
        self.assertNotIn("content", detail)
        original = self.client.get(detail["original_path"])
        self.assertEqual(original.status_code, 200)
        self.assertEqual(hashlib.sha256(original.content).hexdigest(), detail["sha256"])
        self.assertEqual(original.headers["cache-control"], "no-store")
        self.assertIn("filename*=UTF-8''", original.headers["content-disposition"])

    def test_both_routes_reject_other_owner_or_revoked_permission(self):
        for actor, status in ((Actor("bob", "bob", frozenset({"riskList:audit"}), "token"), 404),
                              (Actor("alice", "alice", frozenset(), "token"), 403)):
            self.actor = actor
            for path in ("/api/sources/proof", "/api/sources/proof/original"):
                with self.subTest(path=path, status=status):
                    self.assertEqual(self.client.get(path).status_code, status)

    def test_legacy_attachment_has_explicit_original_unavailable(self):
        self.assertFalse(self.client.get("/api/sources/legacy").json()["data"]["original_available"])
        self.assertEqual(self.client.get("/api/sources/legacy/original").status_code, 404)


if __name__ == "__main__":
    unittest.main()
