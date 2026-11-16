"""从制作工作台评测当前成片：不把视频再上传一遍，也不许读到导出目录以外的文件。"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import studio


NAME = "roughcut-" + "a" * 32 + ".mp4"
REVIEW = {"dimensions": [{"name": "内容", "max": 1, "criteria": []}], "risks": [], "compliance": {"complete": True}, "draft": {"matched": True}, "usage": {}}


def sample():
    return {"run_id": "run1", "title": "小蜜蜂做火车上学", "total_s": 2,
            "clips": {"m1": {"file": "clips/m1.mp4"}, "m2": {"file": "clips/m2.mp4"}},
            "segments": [{"start": 0, "dur": 2, "shots": [{"clip": "m1", "start": 0, "dur": 2, "src_in": 0}]}]}


class RoughcutPathTests(unittest.TestCase):
    def test_only_named_exports_inside_the_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            exports = out / "exports"
            exports.mkdir()
            (exports / NAME).write_bytes(b"video")
            outside = out / "secret.mp4"
            outside.write_bytes(b"secret")
            with patch.object(studio, "OUT", str(out)):
                self.assertEqual(studio.roughcut_path(NAME), str((exports / NAME).resolve()))
                for bad in ("../secret.mp4", outside.name, NAME + "/../../secret.mp4", "roughcut-" + "a" * 31 + ".mp4", ""):
                    with self.assertRaises(ValueError):
                        studio.roughcut_path(bad)
                link = exports / ("roughcut-" + "b" * 32 + ".mp4")
                link.symlink_to(outside)
                with self.assertRaisesRegex(ValueError, "成片不存在"):
                    studio.roughcut_path(link.name)


class ReviewCurrentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        (self.out / "exports").mkdir()
        (self.out / "exports" / NAME).write_bytes(b"video")
        payload = sample()
        payload["preview_mp4"] = {"url": "/exports/" + NAME, "filename": NAME, "duration": 2}
        (self.out / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        self.server = studio.ThreadingHTTPServer(("127.0.0.1", 0), studio.H)
        self.job = {"running": False, "log": [], "error": None}
        for name, value in [("OUT", str(self.out)), ("PORT", self.server.server_port), ("job", self.job)]:
            patch.object(studio, name, value).start()
        self.addCleanup(patch.stopall)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def post(self, path, origin, payload=None):
        import http.client
        body = json.dumps({} if payload is None else payload).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", path, body, {"Content-Type": "application/json", "Origin": origin})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    def test_rejects_external_origin_missing_key_busy_and_missing_file(self):
        local = f"http://127.0.0.1:{self.server.server_port}"
        status, body = self.post("/api/video-review-current", "https://example.org", {"run_id": "run1"})
        self.assertEqual(status, 403)
        self.job["running"] = True
        status, body = self.post("/api/video-review-current", local, {"run_id": "run1"})
        self.assertEqual(status, 409)
        self.assertIn("正在生成中", body["error"])
        self.job["running"] = False
        with patch.object(studio.llm, "model_config", return_value={"api_key": "", "model": "x", "base_url": "https://example.invalid", "provider": "测试"}):
            status, body = self.post("/api/video-review-current", local, {"run_id": "run1", "filename": NAME})
        self.assertEqual(status, 400)
        self.assertIn("DEEPSEEK_API_KEY", body["error"])
        (self.out / "exports" / NAME).unlink()
        with patch.object(studio.llm, "model_config", return_value={"api_key": "secret-video-key", "model": "fake", "base_url": "https://example.invalid", "provider": "测试"}):
            status, body = self.post("/api/video-review-current", local, {"run_id": "run1", "filename": NAME})
        self.assertEqual(status, 400)
        self.assertIn("成片不存在", body["error"])
        self.assertNotIn("secret-video-key", json.dumps(body))

    def test_runtime_error_keeps_the_chinese_reason(self):
        reason = "缺少本地语音识别组件，请运行 pip install -r requirements.txt。"
        local = f"http://127.0.0.1:{self.server.server_port}"
        with patch.object(studio.llm, "model_config", return_value={"api_key": "secret-video-key", "model": "fake", "base_url": "https://example.invalid", "provider": "测试"}), \
                patch("video_review.review_video", side_effect=RuntimeError(reason)):
            status, body = self.post("/api/video-review-current", local, {"run_id": "run1", "filename": NAME})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], reason)
        self.assertNotIn("评测失败，请检查依赖和服务配置后重试。", body["error"])
        self.assertFalse(self.job["running"])

    def test_reviews_the_export_and_applies_swaps_without_uploading(self):
        seen = {}

        def fake_review(path, api_key, model, draft, base_url):
            seen.update(path=path, key=api_key, model=model, clip=draft["segments"][0]["shots"][0]["clip"], base=base_url)
            return dict(REVIEW, usage={"requests": 0})

        local = f"http://127.0.0.1:{self.server.server_port}"
        with patch.object(studio.llm, "model_config", return_value={"api_key": "secret-video-key", "model": "fake-model", "base_url": "https://example.invalid", "provider": "测试"}), \
                patch("video_review.review_video", fake_review):
            status, body = self.post("/api/video-review-current", local, {"run_id": "run1", "swaps": {"0": {"0": "m2"}}})
        self.assertEqual(status, 200)
        self.assertEqual(seen["clip"], "m2")
        self.assertTrue(seen["path"].endswith(NAME))
        self.assertEqual(os.path.dirname(os.path.realpath(seen["path"])), str((self.out / "exports").resolve()))
        self.assertEqual(seen["key"], "secret-video-key")
        self.assertEqual(body["source"]["title"], "小蜜蜂做火车上学")
        self.assertEqual(body["source"]["run_id"], "run1")
        self.assertEqual(body["source"]["url"], "/exports/" + NAME)
        self.assertEqual(body["review"]["dimensions"][0]["name"], "内容")
        self.assertNotIn("secret-video-key", json.dumps(body))
        self.assertFalse(self.job["running"])

    def test_rejects_a_path_outside_exports(self):
        local = f"http://127.0.0.1:{self.server.server_port}"
        with patch.object(studio.llm, "model_config", return_value={"api_key": "secret-video-key", "model": "fake", "base_url": "https://example.invalid", "provider": "测试"}):
            status, body = self.post("/api/video-review-current", local, {"run_id": "run1", "filename": "../result.json"})
        self.assertEqual(status, 400)
        self.assertIn("只能评测工作台导出的成片", body["error"])

    def test_fresh_render_is_remembered_for_review(self):
        local = f"http://127.0.0.1:{self.server.server_port}"
        fresh = "roughcut-" + "c" * 32 + ".mp4"
        with patch.object(studio.roughcut_export, "render", return_value={"url": "/exports/" + fresh, "filename": fresh, "duration": 2, "run_id": "run1"}):
            status, body = self.post("/api/export-roughcut", local, {"run_id": "run1", "swaps": {}})
        self.assertEqual(status, 200)
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["preview_mp4"]["filename"], fresh)


class ReviewCurrentPageTests(unittest.TestCase):
    def test_workbench_starts_review_and_keeps_manual_upload(self):
        page = Path("web/template.html").read_text(encoding="utf-8")
        self.assertIn('id="reviewCurrentBtn"', page)
        self.assertIn("reviewCurrent()", page)
        self.assertIn("/api/video-review-current", page)
        self.assertIn("镜头已替换，正在按当前时间线合成最新成片", page)
        self.assertIn("正在生成中，请完成后再评测。", page)
        self.assertIn("成片不存在，请先在制作工作台合成后再评测。", page)
        self.assertIn("请先在 .env 中填写 DEEPSEEK_API_KEY（或 VIDEO_API_KEY）。", page)
        self.assertIn('id="currentCutCard"', page)
        self.assertIn('id="reviewFile"', page)
        self.assertIn("id=\"humanReviewForm\"", page)
        self.assertIn("renderReview(v,{name:", page)
        self.assertIn("renderReview(v,file,taskContext)", page)
        self.assertIn('id="reviewCurrentBtn"', Path("index.html").read_text(encoding="utf-8"))
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("av>=11,<16", requirements)
        self.assertIn("metadata_errors", Path("README.md").read_text(encoding="utf-8"))
