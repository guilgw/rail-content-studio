"""Pixabay 补素材与粗剪闭环。外部请求全部用假响应，不访问 Pixabay 或脚本模型。"""
import io
import json
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
import tempfile

import llm
import pipeline
import stock_media
import studio


SECRET = "super-secret-pixabay"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1200
JPEG = b"\xff\xd8\xff" + b"\x00" * 32


def hit(video_id=125, portrait=True, hd=True, page="https://pixabay.com/videos/night-train-125/"):
    if portrait:
        large = {"url": "https://cdn.pixabay.com/video/125-large.mp4", "width": 1080, "height": 1920, "size": 2000000,
                 "thumbnail": "https://cdn.pixabay.com/video/125.jpg"}
    else:
        large = {"url": "https://cdn.pixabay.com/video/125-large.mp4", "width": 1920, "height": 1080, "size": 2000000,
                 "thumbnail": "https://cdn.pixabay.com/video/125.jpg"}
    if not hd:
        large = {**large, "width": 480, "height": 854 if portrait else 270, "size": 400000}
    return {"id": video_id, "pageURL": page, "tags": "night train, station", "duration": 8, "user": "Ada",
            "videos": {"large": large, "tiny": {"url": "https://evil.example/steal.mp4", "width": 320, "height": 240, "size": 1000}}}


class Response:
    def __init__(self, payload, url, headers=None):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.url = url
        self.headers = EmailMessage()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        if n is None or n < 0 or n >= len(self.payload):
            data, self.payload = self.payload, b""
            return data
        data, self.payload = self.payload[:n], self.payload[n:]
        return data

    def geturl(self):
        return self.url


def opener_for(body, final=None):
    seen = []

    def opener(request, timeout=30):
        seen.append(request.full_url)
        target = final or request.full_url
        if "cdn.pixabay.com/video/125.jpg" in target:
            return Response(JPEG, target)
        if "cdn.pixabay.com" in target:
            return Response(body, target, {"Content-Length": str(len(body))})
        return Response({"hits": [hit()]}, target)

    opener.seen = seen
    return opener


class StockMediaTests(unittest.TestCase):
    def test_english_query_uses_model_and_falls_back_without_leaking_secrets(self):
        with patch.object(llm, "model_config", return_value={"api_key": "model-secret"}), patch.object(
                llm, "script_json", return_value=({"query": "night sleeper train"}, {})) as model:
            self.assertEqual(stock_media.english_query("夜间卧铺列车", ["train"]), "night sleeper train")
        self.assertIn("夜间卧铺列车", model.call_args.args[1])
        with patch.object(llm, "model_config", return_value={"api_key": ""}), patch.object(llm, "script_json") as model:
            self.assertEqual(stock_media.english_query("夜间卧铺", ["train", "night"]), "train night")
            model.assert_not_called()
        self.assertEqual(stock_media.english_query("alpine window"), "alpine window")

    def test_search_prefers_portrait_hd_and_hides_the_key(self):
        landscape = hit(7, portrait=False)
        portrait = hit(9, portrait=True)
        opener = opener_for(MP4)

        def search_opener(request, timeout=30):
            opener.seen.append(request.full_url)
            self.assertIn("key=" + SECRET, request.full_url)
            return Response({"hits": [landscape, portrait, {"id": "bad"}, {"id": 3, "videos": {"tiny": {"url": "http://cdn.pixabay.com/a.mp4", "width": 1080, "height": 1920, "size": 10}}}]}, request.full_url)

        opener.seen.clear()
        with patch.object(llm, "load_env", return_value={"PIXABAY_API_KEY": SECRET}):
            result = stock_media.search("night train", search_opener)
        self.assertEqual([item["id"] for item in result["hits"]], [9, 7])
        self.assertTrue(result["hits"][0]["portrait"] and result["hits"][0]["hd"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn("人物和商标", result["notice"])
        self.assertNotIn("url", result["hits"][0])

    def test_search_failure_does_not_reveal_the_request(self):
        def opener(request, timeout=30):
            raise RuntimeError("boom " + request.full_url)

        with patch.object(llm, "load_env", return_value={"PIXABAY_API_KEY": SECRET}):
            with self.assertRaises(ValueError) as caught:
                stock_media.search("train", opener)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("pixabay.com", str(caught.exception))

    def test_download_rejects_other_hosts_redirects_and_oversized_files(self):
        with self.assertRaisesRegex(ValueError, "允许的下载域名"):
            stock_media._download("https://evil.example/video.mp4", opener_for(MP4))
        with self.assertRaisesRegex(ValueError, "允许的下载域名"):
            stock_media._download("http://cdn.pixabay.com/video.mp4", opener_for(MP4))

        def redirected(request, timeout=30):
            return Response(MP4, "https://evil.example/redirected.mp4")

        with self.assertRaisesRegex(ValueError, "允许范围之外"):
            stock_media._download("https://cdn.pixabay.com/ok.mp4", redirected)
        huge = b"\x00" * (stock_media.MAX_BYTES + 10)

        def oversized(request, timeout=30):
            return Response(huge, request.full_url, {"Content-Length": str(len(huge))})

        with self.assertRaisesRegex(ValueError, "40 MB"):
            stock_media._download("https://cdn.pixabay.com/big.mp4", oversized)

    def test_import_records_source_and_rejects_a_foreign_file_url(self):
        foreign = hit()
        foreign["videos"]["large"]["url"] = "https://evil.example/video.mp4"
        calls = []

        def opener(request, timeout=30):
            calls.append(request.full_url)
            return Response({"hits": [foreign]}, request.full_url)

        with tempfile.TemporaryDirectory() as folder, patch.object(llm, "load_env", return_value={"PIXABAY_API_KEY": SECRET}):
            with self.assertRaisesRegex(ValueError, "没有找到可下载"):
                stock_media.import_video(folder, folder, 125, ["night"], ["苏格兰"], opener)
            self.assertFalse(any(SECRET in path.name for path in Path(folder).rglob("*")))
        self.assertTrue(any("key=" + SECRET in url for url in calls))

    def test_import_writes_library_metadata_without_the_key(self):
        opener = opener_for(MP4)
        with tempfile.TemporaryDirectory() as folder, patch.object(llm, "load_env", return_value={"PIXABAY_API_KEY": SECRET}), \
                patch.object(stock_media, "_probe", return_value=6.5), patch.object(stock_media, "_ffmpeg", return_value=None):
            root, out = Path(folder), Path(folder) / "output"
            clip = stock_media.import_video(str(root), str(out), 125, ["matterhorn", "mountain"], ["苏格兰"], opener)
            again = stock_media.import_video(str(root), str(out), 125, ["night"], ["苏格兰"], opener)
            self.assertEqual(again["id"], "px125")
            self.assertEqual(clip["author"], "Ada")
            self.assertEqual(clip["license"], stock_media.LICENSE)
            self.assertIn("pixabay.com/videos", clip["page"])
            self.assertEqual(clip["places"], ["苏格兰"])
            self.assertEqual(clip["tags"], ["night", "train", "station"])
            self.assertNotIn("matterhorn", clip["tags"])
            self.assertTrue(clip["stock"])
            saved = json.loads((out / "stock-library.json").read_text())
            blob = json.dumps(saved, ensure_ascii=False) + json.dumps(clip)
            self.assertNotIn(SECRET, blob)
            self.assertTrue((out / "stock" / "px125.mp4").is_file())
            self.assertTrue((out / "stock" / "px125.jpg").is_file())
            self.assertEqual(len(opener.seen), 3)  # 检索、视频、缩略图；再次导入直接复用文件

    def test_fill_gaps_skips_network_without_a_key_and_imports_only_weak_lines(self):
        local = [{"id": "m1", "tags": ["train"], "places": [], "quality": 4, "issues": [], "file": "clips/m1.mp4", "title": "local", "pexels": "https://www.pexels.com/video/1/"}]
        lines = [{"zh": "夜车进站", "tags": ["night"], "places": ["伦敦"], "seg_s": 4},
                 {"zh": "列车", "tags": ["train"], "places": [], "seg_s": 3}]

        def retrieve(line, clips, durations):
            if "train" in (line.get("tags") or []):
                return line["tags"], line["places"], [{"id": "m1", "coverage": 0.9, "issues": [], "matched": ["train"]}], []
            return line["tags"], line["places"], [], []

        with patch.object(llm, "load_env", return_value={}):
            clips, durations, report = stock_media.fill_gaps(lines, local, {"m1": 5}, retrieve, ".", ".", importer=None)
        self.assertFalse(report["configured"])
        self.assertEqual(clips, local)
        self.assertIn("只使用本地素材", report["message"])

        imported = []

        def search_fn(query):
            self.assertNotIn("夜", query)
            return [{"id": 125, "portrait": True, "hd": True, "duration": 8}]

        def importer(video_id, tags, places):
            imported.append((video_id, tags, places))
            return {"id": "px125", "title": "night train", "file": "output/stock/px125.mp4", "tags": tags, "places": places,
                    "quality": 4, "clip_s": 8, "author": "Ada", "page": stock_media.LICENSE, "pexels": stock_media.LICENSE, "thumb": ""}

        with patch.object(stock_media, "english_query", return_value="night station"):
            clips, durations, report = stock_media.fill_gaps(lines, local, {"m1": 5}, retrieve, ".", ".", search_fn=search_fn, importer=importer)
        self.assertEqual(imported, [(125, [], ["伦敦"])])
        self.assertIn("px125", durations)
        self.assertEqual(report["imported"][0]["author"], "Ada")
        self.assertIn("人物和商标", report["message"])

    def test_library_load_merges_downloaded_clips(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, "OUT", folder):
            Path(folder, "stock-library.json").write_text(json.dumps({"clips": [{
                "id": "px9", "file": "output/stock/px9.mp4", "title": "lake", "tags": ["lake"], "location": "瑞士",
                "quality": 4, "issues": [], "pexels": stock_media.LICENSE, "places": ["瑞士"], "duration": 4}]}), encoding="utf-8")
            clips = pipeline.load("library.json")["clips"]
        self.assertIn("px9", [clip["id"] for clip in clips])
        self.assertGreater(len(clips), 53)


class StudioStockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.result = {"run_id": "run1", "total_s": 2, "clips": {"m1": {"file": "clips/m1.mp4", "title": "本地", "tags": ["train"], "location": "", "quality": 4, "issues": [], "pexels": "https://www.pexels.com/video/1/", "clip_s": 3}},
                       "segments": [{"start": 0, "dur": 2, "subtitle": "Hello", "shots": [{"clip": "m1"}], "need": ["night"], "places": ["伦敦"]}]}
        (self.out / "result.json").write_text(json.dumps(self.result), encoding="utf-8")
        self.server = studio.ThreadingHTTPServer(("127.0.0.1", 0), studio.H)
        for name, value in [("OUT", str(self.out)), ("PORT", self.server.server_port), ("job", {"running": False, "log": [], "error": None})]:
            patch.object(studio, name, value).start()
        self.addCleanup(patch.stopall)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, origin=None):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Origin": origin or f"http://localhost:{self.server.server_port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        parsed = json.loads(raw.decode()) if raw and response.getheader("Content-Type", "").startswith("application/json") else raw
        return response.status, parsed

    def test_cross_origin_missing_key_and_secret_are_not_exposed(self):
        self.assertEqual(self.request("POST", "/api/stock/search", {"query": "train"}, "https://example.org")[0], 403)
        with patch.object(llm, "load_env", return_value={}):
            status, payload = self.request("POST", "/api/stock/search", {"query": "train"})
        self.assertEqual(status, 400)
        self.assertIn("只使用本地素材", payload["error"])
        self.assertNotIn("super-secret", json.dumps(payload))
        with patch.object(llm, "load_env", return_value={"PIXABAY_API_KEY": SECRET, "DEEPSEEK_API_KEY": "other-secret"}):
            status, payload = self.request("GET", "/api/keys")
        self.assertEqual(status, 200)
        self.assertTrue(payload["pixabay"])
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertNotIn("other-secret", json.dumps(payload))

    def test_search_and_import_update_the_open_cut(self):
        def fake_search(query):
            self.assertEqual(query, "夜车站")
            return {"query": "night station", "configured": True, "notice": stock_media.NOTICE, "hits": [
                {"id": 125, "title": "night train", "duration": 8, "author": "Ada", "page": "https://pixabay.com/videos/night-train-125/",
                 "thumbnail": "https://cdn.pixabay.com/video/125.jpg", "portrait": True, "hd": True, "provider": "Pixabay", "license": stock_media.LICENSE}]}

        def fake_import(root, out, video_id, tags, places, opener=None):
            self.assertEqual((video_id, tags, places), (125, ["night"], ["伦敦"]))
            return {"id": "px125", "title": "night train", "file": "output/stock/px125.mp4", "tags": ["night"], "places": ["伦敦"],
                    "quality": 4, "issues": [], "pexels": stock_media.LICENSE, "clip_s": 8, "provider": "Pixabay", "author": "Ada",
                    "license": stock_media.LICENSE, "thumb": "", "stock": True, "page": stock_media.LICENSE}

        with patch.object(llm, "load_env", return_value={"PIXABAY_API_KEY": SECRET}), patch.object(stock_media, "search", fake_search), \
                patch.object(stock_media, "import_video", fake_import):
            status, payload = self.request("POST", "/api/stock/search", {"query": "夜车站"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["hits"][0]["author"], "Ada")
            self.assertNotIn(SECRET, json.dumps(payload))
            status, payload = self.request("POST", "/api/stock/import", {"video_id": 125, "tags": ["night"], "places": ["伦敦"]})
        self.assertEqual(status, 200)
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["clips"]["px125"]["author"], "Ada")
        self.assertNotIn(SECRET, json.dumps(saved))

    def test_preview_render_keeps_a_safe_error(self):
        with patch.object(studio.roughcut_export, "render", side_effect=ValueError("失败 https://pixabay.com/api/?key=" + SECRET)):
            note = studio.attach_preview()
        saved = json.loads((self.out / "result.json").read_text())
        self.assertNotIn("preview_mp4", saved)
        self.assertNotIn(SECRET, note["msg"])
        self.assertNotIn(SECRET, saved["preview_error"])
        with patch.object(studio.roughcut_export, "render", return_value={"url": "/exports/roughcut-" + "a" * 32 + ".mp4", "filename": "a.mp4", "duration": 2}):
            note = studio.attach_preview()
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["preview_mp4"]["duration"], 2)
        self.assertEqual(note["step"], "合成粗剪")
