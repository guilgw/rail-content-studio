"""剪映工程要带可编辑轨道，替换镜头要写进去，压缩包换位置后仍指向素材。"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import jianying_export
import pipeline
import studio


def media(folder):
    folder = Path(folder)
    (folder / "clips").mkdir()
    (folder / "tts").mkdir()
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x568:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(folder / "clips" / "m1.mp4")],
                   check=True, capture_output=True)
    shutil.copy(folder / "clips" / "m1.mp4", folder / "clips" / "m2.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "pcm_s16le", str(folder / "tts" / "line1.wav")],
                   check=True, capture_output=True)


def result():
    skill = json.loads(Path("data/skill.json").read_text(encoding="utf-8"))
    return {"run_id": "run1", "title": "小蜜蜂做火车上学😭@LNER", "total_s": 2.0, "skill": skill,
            "clips": {"m1": {"file": "clips/m1.mp4", "title": "火车"}, "m2": {"file": "clips/m2.mp4", "title": "蜜蜂"}},
            "segments": [{"start": 0, "dur": 2.0, "voice_s": 1.8, "audio": "tts/line1", "subtitle": "Bees on a train",
                          "stickers": [{"text": "BEES", "start": 0.2, "dur": 1.0}],
                          "shots": [{"clip": "m1", "start": 0, "dur": 2.0, "src_in": 0, "title": "火车",
                                     "quality": 4, "issues": [], "coverage": 1, "excluded": None}],
                          "candidates": []}]}


class JianyingExportTests(unittest.TestCase):
    def test_name_drops_emoji_and_symbols(self):
        self.assertTrue(pipeline.draft_name_for("小蜜蜂做火车上学😭@LNER").startswith("AI粗剪_小蜜蜂做火车上学LNER_"))
        self.assertNotIn("😭", pipeline.draft_name_for("thepaw😭@LNER"))
        self.assertNotIn("@", pipeline.draft_name_for("thepaw😭@LNER"))

    def test_draft_tracks_match_the_timeline_and_swaps(self):
        with tempfile.TemporaryDirectory() as folder:
            media(folder)
            with patch.object(pipeline, "ROOT", folder), patch.object(pipeline, "OUT", folder):
                info = jianying_export.export_project(result(), {"0": {"0": "m2"}}, folder, folder, install=True)
            draft = json.loads((Path(folder) / info["name"] / "draft_info.json").read_text(encoding="utf-8"))
            names = {track["name"]: track for track in draft["tracks"]}
            self.assertEqual(set(names), {"画面", "配音", "背景音乐", "字幕", "花字"})
            self.assertEqual(names["画面"]["type"], "video")
            self.assertEqual(names["配音"]["type"], "audio")
            self.assertEqual(names["字幕"]["type"], "text")
            self.assertEqual(names["花字"]["type"], "text")
            self.assertEqual(names["画面"]["segments"][0]["target_timerange"]["duration"], 2_000_000)
            self.assertEqual(names["画面"]["segments"][0]["volume"], 0.0)
            self.assertEqual(names["字幕"]["segments"][0]["target_timerange"]["duration"], 2_000_000)
            self.assertEqual(names["背景音乐"]["segments"], [])
            video_id = names["画面"]["segments"][0]["material_id"]
            videos = {item["id"]: item for item in draft["materials"]["videos"]}
            self.assertTrue(videos[video_id]["path"].endswith("m2.mp4"))
            text_ids = {item["id"] for item in draft["materials"]["texts"]}
            self.assertIn(names["字幕"]["segments"][0]["material_id"], text_ids)
            with zipfile.ZipFile(Path(folder) / "exports" / info["filename"]) as archive:
                packed = archive.namelist()
            self.assertTrue(any(name.endswith("draft_info.json") for name in packed))
            self.assertTrue(any(name.endswith("materials/m2.mp4") for name in packed))
            self.assertTrue(any(name.endswith("README.txt") for name in packed))
            portable = json.loads((Path(folder) / "jianying-portable" / info["name"] / "draft_info.json").read_text(encoding="utf-8"))
            portable_path = next(item["path"] for item in portable["materials"]["videos"] if item["path"].endswith("m2.mp4"))
            self.assertIn("##_draftpath_placeholder_", portable_path)
            self.assertNotIn(folder, portable_path)


class JianyingApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        (self.out / "result.json").write_text(json.dumps({"run_id": "run1", "preview_only": True}), encoding="utf-8")
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

    def post(self, path, origin, payload=b"{}"):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", path, payload, {"Content-Type": "application/json", "Origin": origin})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_export_and_redraft_reject_external_origins(self):
        for path in ("/api/export-jianying", "/api/redraft"):
            status, _body = self.post(path, "https://example.org")
            self.assertEqual(status, 403, path)

    def test_local_export_reaches_the_handler(self):
        status, body = self.post("/api/export-jianying", f"http://127.0.0.1:{self.server.server_port}", b'{"run_id":"run1"}')
        self.assertEqual(status, 400)
        self.assertIn("演示", body.decode())
