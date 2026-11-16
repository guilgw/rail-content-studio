"""选用脚本后应在当前页展示真实制作进度，而不是立刻跳走。"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProduceDialogTests(unittest.TestCase):
    def text(self, name):
        return (ROOT / name).read_text(encoding="utf-8")

    def test_choose_starts_production_on_the_script_page(self):
        page = self.text("web/template.html")
        self.assertIn("chooseAndProduce(b.dataset.scriptChoice)", page)
        self.assertIn("id=\"produceDialog\"", page)
        self.assertIn("视频制作中", page)
        self.assertIn("id=\"produceRetry\"", page)
        self.assertIn("id=\"produceClose\"", page)
        self.assertIn("fetch('/api/status')", page)
        self.assertIn("location.replace('/#produced')", page)
        self.assertIn("$('screen').scrollIntoView", page)
        self.assertNotIn("setView('create');", page)
        self.assertIn("confirmScriptChoice", page)
        self.assertIn("master.classList.add('on')", page)
        self.assertIn("master.poster=cover", page)
        self.assertIn("$('ph').hidden=true", page)
        for label, step in (("配音", "生成配音"), ("收集素材", "在线素材补缺"), ("匹配镜头", "本地素材库召回"), ("合成成片", "合成粗剪")):
            self.assertIn(label, page)
            self.assertIn(step, page)

    def test_phase_names_match_the_server_log(self):
        page = self.text("web/template.html")
        pipeline = self.text("pipeline.py")
        studio = self.text("studio.py")
        self.assertIn('log("生成配音"', pipeline)
        self.assertIn('log("在线素材补缺"', pipeline)
        self.assertIn('log("本地素材库召回"', pipeline)
        self.assertIn('"step": "合成粗剪"', studio)
        for step in ("生成配音", "在线素材补缺", "本地素材库召回", "合成粗剪"):
            self.assertIn(step, page)

    def test_published_page_includes_the_same_dialog(self):
        built = self.text("index.html")
        self.assertIn("id=\"produceDialog\"", built)
        self.assertIn("chooseAndProduce(b.dataset.scriptChoice)", built)
        self.assertIn("视频制作中", built)
