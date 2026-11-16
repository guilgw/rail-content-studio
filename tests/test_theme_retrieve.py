"""整条成片要扣住选题里的视觉主题，而不是只在第一镜出现。"""
import unittest
from unittest.mock import patch

import pipeline
import stock_media


def clip(cid, tags, quality=4):
    return {"id": cid, "title": cid, "tags": tags, "places": [], "quality": quality, "issues": [],
            "file": f"clips/{cid}.mp4", "location": "", "pexels": "https://www.pexels.com/video/1/"}


class ThemeTests(unittest.TestCase):
    def test_bees_come_from_the_title_and_the_script(self):
        lines = [{"en": "A swarm of bees once delayed a train.", "tags": ["bee", "train"]},
                 {"en": "The sleeper leaves London.", "tags": ["sleeper", "london"]}]
        self.assertEqual(stock_media.visual_theme("小蜜蜂做火车上学", lines), ["bee"])
        self.assertEqual(stock_media.visual_theme("Bees ride the sleeper", []), ["bee"])

    def test_a_plain_sleeper_story_has_no_extra_theme(self):
        lines = [{"en": "The sleeper leaves London Euston.", "tags": ["sleeper", "london"]},
                 {"en": "Wake up to the highlands.", "tags": ["highland", "mountain"]}]
        self.assertEqual(stock_media.visual_theme("伦敦到苏格兰卧铺", lines), [])

    def test_theme_outranks_a_generic_train_match(self):
        clips = [clip("m41", ["train", "ticket"]), clip("bee", ["bee", "honey"])]
        durations = {"m41": 8, "bee": 6}
        _need, _places, cands, _bad = pipeline.retrieve(
            {"en": "Split the ticket and save.", "tags": ["ticket", "journey"], "places": []},
            clips, durations, ["bee"])
        self.assertEqual(cands[0]["id"], "bee")
        self.assertTrue(cands[0]["theme_hit"])
        self.assertIn("m41", [item["id"] for item in cands])

    def test_theme_clips_rotate_and_a_single_clip_stops_at_two(self):
        bees = [{"id": "px30470", "theme_hit": 1}, {"id": "px367164", "theme_hit": 1}, {"id": "m41", "theme_hit": 0}]
        uses, previous, openings = {}, None, []
        for _ in range(5):
            picked = pipeline.choose_shots(bees, 1, previous, uses, 2)
            openings.append(picked[0]["id"])
            uses[picked[0]["id"]] = uses.get(picked[0]["id"], 0) + 1
            previous = picked[0]["id"]
        self.assertEqual(openings, ["px30470", "px367164", "px30470", "px367164", "m41"])
        self.assertLessEqual(uses["px30470"], 2)
        self.assertEqual(uses["px367164"], 2)

        solo = [{"id": "px30470", "theme_hit": 1}, {"id": "m13", "theme_hit": 0}, {"id": "m41", "theme_hit": 0}]
        uses, previous, openings = {}, None, []
        for _ in range(5):
            picked = pipeline.choose_shots(solo, 1, previous, uses, 2)
            openings.append(picked[0]["id"])
            uses[picked[0]["id"]] = uses.get(picked[0]["id"], 0) + 1
            previous = picked[0]["id"]
        self.assertEqual(openings.count("px30470"), 2)
        self.assertTrue(all(left != right for left, right in zip(openings, openings[1:])))

    def test_downloaded_clip_is_used_even_when_its_tags_do_not_say_bee(self):
        clips = [clip("px30470", ["bee", "honey"]), clip("px367164", ["insect", "macro"]), clip("m13", ["ticket"])]
        clips[1]["for_theme"] = ["bee"]
        durations = {item["id"]: 6 for item in clips}
        _need, _places, cands, _bad = pipeline.retrieve(
            {"en": "Save on the fare.", "tags": ["ticket"], "places": []}, clips, durations, ["bee"])
        self.assertEqual([item["id"] for item in cands if item["theme_hit"]], ["px30470", "px367164"])

    def test_shots_alternate_theme_and_do_not_repeat_the_neighbor(self):
        cands = [{"id": "bee", "theme_hit": 1, "issues": []}, {"id": "train", "theme_hit": 0, "issues": []},
                 {"id": "hill", "theme_hit": 0, "issues": []}]
        first = pipeline.choose_shots(cands, 2, None)
        second = pipeline.choose_shots(cands, 2, first[-1]["id"])
        self.assertEqual([item["id"] for item in first], ["bee", "train"])
        self.assertNotEqual(second[0]["id"], first[-1]["id"])
        self.assertEqual(second[0]["id"], "bee")
        self.assertNotEqual(second[0]["id"], second[1]["id"])

    def test_missing_theme_context_searches_even_when_trains_match(self):
        local = [clip("m41", ["train", "countryside"])]
        lines = [{"zh": "窗外是田野", "tags": ["countryside", "window"], "places": [], "seg_s": 4, "en": "Fields outside."}]
        queries = []

        def retrieve(line, clips, durations):
            return line["tags"], [], [{"id": "m41", "coverage": 0.9, "issues": [], "matched": ["countryside"]}], []

        def search_fn(query):
            queries.append(query)
            return [{"id": 50, "portrait": True, "hd": True, "duration": 7}]

        def importer(video_id, tags, places):
            self.assertEqual(tags, [])
            return {"id": "px50", "title": "bee meadow", "file": "output/stock/px50.mp4", "tags": ["bee", "meadow"],
                    "places": [], "quality": 4, "clip_s": 7, "author": "Ada", "page": stock_media.LICENSE,
                    "pexels": stock_media.LICENSE, "thumb": ""}

        with patch.object(stock_media, "english_query", return_value="countryside window"):
            _clips, _durations, report = stock_media.fill_gaps(
                lines, local, {"m41": 8}, retrieve, ".", ".", search_fn=search_fn, importer=importer, theme=["bee"])
        self.assertTrue(any("bee" in query.split() for query in queries))
        self.assertEqual(report["imported"][0]["id"], "px50")
        self.assertIn("bee", report["message"])

    def test_a_bee_closeup_does_not_cover_a_countryside_line(self):
        local = [clip("bee", ["bee", "honey"]), clip("m22", ["cabin", "window"])]
        lines = [{"zh": "窗外是田野", "tags": ["countryside", "window"], "places": [], "seg_s": 4, "en": "Fields outside."}]
        queries = []

        def retrieve(line, clips, durations):
            return line["tags"], [], [{"id": "m22", "coverage": 0.7, "issues": [], "matched": ["window"]}], []

        def search_fn(query):
            queries.append(query)
            return [{"id": 51, "portrait": True, "hd": True, "duration": 6}]

        def importer(video_id, tags, places):
            return {"id": "px51", "title": "bee field", "file": "output/stock/px51.mp4", "tags": ["bee", "field"],
                    "places": [], "quality": 4, "clip_s": 6, "author": "Ada", "page": stock_media.LICENSE,
                    "pexels": stock_media.LICENSE, "thumb": ""}

        with patch.object(stock_media, "english_query", return_value="countryside"):
            _clips, _durations, report = stock_media.fill_gaps(
                lines, local, {"bee": 5, "m22": 5}, retrieve, ".", ".", search_fn=search_fn, importer=importer, theme=["bee"])
        self.assertIn("bee countryside", queries)
        self.assertEqual(report["imported"][0]["id"], "px51")

    def test_an_existing_theme_clip_is_reused_for_a_generic_line(self):
        local = [clip("m13", ["ticket"]), clip("bee", ["bee", "honey"])]
        lines = [{"zh": "分段买票", "tags": ["ticket", "journey"], "places": [], "seg_s": 4, "en": "Save on the fare."}]

        def retrieve(line, clips, durations):
            return line["tags"], [], [{"id": "m13", "coverage": 0.8, "issues": [], "matched": ["ticket"]}], []

        def search_fn(query):
            raise AssertionError("generic line should reuse the bee clip")

        _clips, _durations, report = stock_media.fill_gaps(
            lines, local, {"m13": 5, "bee": 5}, retrieve, ".", ".", search_fn=search_fn, importer=lambda *a: None, theme=["bee"])
        self.assertEqual(report["imported"], [])

    def test_theme_search_stops_at_the_configured_download_limit(self):
        lines = [{"zh": "票", "tags": ["ticket"], "places": [], "seg_s": 3, "en": "Tickets."} for _ in range(4)]
        queries = []

        def retrieve(line, clips, durations):
            return line["tags"], [], [{"id": "m13", "coverage": 0.8, "issues": [], "matched": ["ticket"]}], []

        def search_fn(query):
            queries.append(query)
            return [{"id": 100 + len(queries), "portrait": True, "hd": True, "duration": 5}]

        def importer(video_id, tags, places):
            self.assertEqual(tags, [])
            return {"id": f"px{video_id}", "title": "bee", "file": f"output/stock/px{video_id}.mp4", "tags": ["bee"],
                    "places": [], "quality": 4, "clip_s": 5, "author": "Ada", "page": stock_media.LICENSE,
                    "pexels": stock_media.LICENSE, "thumb": "", "for_theme": ["bee"]}

        _clips, _durations, report = stock_media.fill_gaps(
            lines, [clip("m13", ["ticket"])], {"m13": 5}, retrieve, ".", ".", search_fn=search_fn, importer=importer,
            theme=["bee"], limit=3)
        self.assertEqual(queries[:3], ["bee flower", "bee flying", "bee hive"])
        self.assertEqual(len(report["imported"]), 3)
        self.assertEqual(stock_media.download_limit(9), 8)
        self.assertEqual(stock_media.download_limit("nope"), 6)
