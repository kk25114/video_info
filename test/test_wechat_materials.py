"""公众号素材流程回归：验证失败不能混入封面，成功时保留原有叠图约定。"""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from mk_video import search_materials as materials


class WechatMaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.article = self.root / "news.md"
        self.article.write_text("# 乡村消亡\n\n正文\n", encoding="utf-8")
        self.output = self.root / "images"
        self.candidate = {
            "title": "乡村消亡与留守老人",
            "description": "村庄人口减少的实地调查",
            "source": "wechat",
            "article_url": "https://mp.weixin.qq.com/s/test-article",
            "page_url": "https://weixin.sogou.com/link?url=test",
            "image_url": "https://mmbiz.qpic.cn/cover.jpg",
        }

    def run_search(self, browser):
        with patch.object(materials, "_wechat_images", return_value=[copy.deepcopy(self.candidate)]), \
                patch.object(materials, "_WechatBodyBrowser", return_value=browser):
            return materials.search_and_download(
                article=self.article,
                output_dir=self.output,
                count_per_query=1,
                max_materials=1,
                duration=610.0,
                clean=True,
                max_bytes=1024 * 1024,
                use_wechat_body=True,
            )

    def test_verification_does_not_download_cover_or_make_success_manifest(self):
        browser = Mock()
        browser.start.return_value = browser
        browser.extract.return_value = ([], "verification_required")
        with patch.object(materials, "_download_image") as download:
            with self.assertRaisesRegex(RuntimeError, "没有下载到"):
                self.run_search(browser)
            download.assert_not_called()
        self.assertFalse((self.output / "materials.json").exists())
        self.assertEqual(list(self.output.glob("*.jpg")), [])
        report = json.loads((self.output / "search_results.json").read_text())
        self.assertEqual(report["searches"][0]["results"][0]["article_fetch_status"], "verification_required")

    def test_success_uses_body_image_and_original_timecode_placement(self):
        raw = io.BytesIO()
        Image.new("RGB", (640, 480), "gray").save(raw, "JPEG")
        browser = Mock()
        browser.start.return_value = browser
        browser.extract.return_value = (["https://mmbiz.qpic.cn/body.jpg"], "ok")
        browser.download.return_value = raw.getvalue()
        manifest = json.loads(self.run_search(browser).read_text())
        image = manifest["materials"][0]
        self.assertEqual(image["source"], "wechat_body")
        self.assertEqual(image["image_url"], "https://mmbiz.qpic.cn/body.jpg")
        self.assertRegex(image["file"], r"^\d+-1010\.jpg$")
        self.assertEqual(image["duration_seconds"], 10.0)
        self.assertEqual(image["position"], "center")
        with Image.open(self.output / image["file"]) as saved:
            self.assertEqual(saved.size, (640, 480))

    def test_right_click_image_save_keeps_source_and_cleans_its_temp_file(self):
        temp_parent = self.root / "Temp"
        save_directory = temp_parent / "VideoInfoWechatImages-test"
        save_directory.mkdir(parents=True)
        image_path = save_directory / "body.jpg"
        raw = io.BytesIO()
        Image.new("RGB", (640, 480), "gray").save(raw, "JPEG")
        image_path.write_bytes(raw.getvalue())
        browser = Mock()
        browser.start.return_value = browser
        browser.open_article.return_value = {
            "status": "ok",
            "address": "https://mp.weixin.qq.com/s/article",
            "images": [{"index": 2, "width": 640, "height": 480}],
        }
        browser.save_image.return_value = {"file_path": str(image_path), "image_index": 2}
        reader = materials._WechatBodyBrowser(browser=browser).start()
        manifest = json.loads(self.run_search(reader).read_text())
        image = manifest["materials"][0]
        self.assertEqual(image["source_url"], "https://mp.weixin.qq.com/s/article")
        self.assertEqual(image["source_image_index"], 2)
        self.assertEqual(image["image_url"], "")
        self.assertEqual(image["capture_method"], "windows_image_save_as")
        browser.save_image.assert_called_once_with(2)
        self.assertFalse(save_directory.exists())

    def test_image_save_failure_does_not_fall_back_to_cover(self):
        browser = Mock()
        browser.open_article.return_value = {
            "status": "ok", "address": self.candidate["article_url"],
            "images": [{"index": 2}],
        }
        browser.save_image.side_effect = RuntimeError("目标文章已切换")
        reader = materials._WechatBodyBrowser(browser=browser)
        with patch.object(materials, "_download_image") as download:
            with self.assertRaisesRegex(RuntimeError, "没有下载到"):
                self.run_search(reader)
            download.assert_not_called()
        report = json.loads((self.output / "search_results.json").read_text())
        self.assertEqual(report["searches"][0]["results"][0]["article_fetch_status"], "image_save_failed")

    def test_empty_http_search_uses_default_browser_results(self):
        raw = io.BytesIO()
        Image.new("RGB", (640, 480), "gray").save(raw, "JPEG")
        windows = Mock()
        windows.start.return_value = windows
        windows.search_wechat.return_value = [copy.deepcopy(self.candidate)]
        body = Mock()
        body.start.return_value = body
        body.extract.return_value = (["https://mmbiz.qpic.cn/body.jpg"], "ok")
        body.download.return_value = raw.getvalue()
        with patch.object(materials, "_wechat_images", return_value=[]), \
                patch.object(materials, "WindowsBrowser", return_value=windows), \
                patch.object(materials, "_WechatBodyBrowser", return_value=body) as reader:
            materials.search_and_download(self.article, self.output, 1, 1, 610, True, 1024 * 1024, True)
        reader.assert_called_once_with(browser=windows)
        windows.search_wechat.assert_called_once()
        report = json.loads((self.output / "search_results.json").read_text())
        self.assertEqual(report["searches"][0]["selected_search_source"], "windows_default_browser")

    def test_searches_once_and_opens_only_the_top_relevant_article(self):
        second = dict(
            self.candidate,
            title="乡村消亡现状调查",
            article_url="https://mp.weixin.qq.com/s/second-article",
            page_url="https://weixin.sogou.com/link?url=second",
        )
        browser = Mock()
        browser.start.return_value = browser
        browser.extract.return_value = (["https://mmbiz.qpic.cn/body.jpg"], "ok")
        raw = io.BytesIO()
        Image.new("RGB", (640, 480), "gray").save(raw, "JPEG")
        browser.download.return_value = raw.getvalue()
        with patch.object(materials, "_query_terms", return_value=["乡村消亡", "留守老人"]), \
                patch.object(materials, "_wechat_images", return_value=[copy.deepcopy(self.candidate), second]) as search, \
                patch.object(materials, "_WechatBodyBrowser", return_value=browser):
            materials.search_and_download(self.article, self.output, 6, 1, 610, True, 1024 * 1024, True)
        search.assert_called_once()
        browser.extract.assert_called_once()
        self.assertEqual(browser.extract.call_args.args[0]["article_url"], self.candidate["article_url"])
        report = json.loads((self.output / "search_results.json").read_text())
        self.assertEqual(len(report["searches"]), 1)
        self.assertEqual(report["searches"][0]["query"], "乡村消亡")
        self.assertNotIn("article_fetch_status", report["searches"][0]["results"][1])

    def test_search_results_do_not_open_each_article_before_selection(self):
        response = Mock()
        response.url = "https://weixin.sogou.com/weixin?type=2&query=%E4%B9%A1%E6%9D%91%E6%B6%88%E4%BA%A1"
        response.text = """<ul class="news-list">
          <li id="sogou_vr_1">
            <h3><a href="/link?url=one">乡村消亡与留守老人</a></h3>
            <div class="txt-info">村庄人口减少的实地调查</div>
            <div class="all-time-y2">公众号甲</div>
          </li>
          <li id="sogou_vr_2">
            <h3><a href="/link?url=two">乡村消亡现状调查</a></h3>
            <div class="txt-info">人口变化</div>
            <div class="all-time-y2">公众号乙</div>
          </li>
        </ul>"""
        session = Mock()
        session.get.return_value = response
        with patch.object(materials, "_resolve_wechat_article") as resolve:
            results = materials._wechat_images(session, "乡村消亡", 10)
        resolve.assert_not_called()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["page_url"], "https://weixin.sogou.com/link?url=one")
        self.assertEqual(results[0]["article_url"], "")
        self.assertEqual(results[0]["article_status"], "resolve_in_windows_browser")

    def test_orphan_surrogates_from_search_results_do_not_break_manifests(self):
        self.candidate["description"] = "村庄人口减少调查 \ud83d"
        raw = io.BytesIO()
        Image.new("RGB", (640, 480), "gray").save(raw, "JPEG")
        browser = Mock()
        browser.start.return_value = browser
        browser.extract.return_value = (["https://mmbiz.qpic.cn/body.jpg"], "ok")
        browser.download.return_value = raw.getvalue()

        manifest_path = self.run_search(browser)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = json.loads((self.output / "search_results.json").read_text(encoding="utf-8"))
        description = manifest["materials"][0]["description"]
        report_description = report["searches"][0]["results"][0]["description"]
        self.assertEqual(description, "村庄人口减少调查 ?")
        self.assertEqual(report_description, description)

    def test_automotive_queries_do_not_expand_to_generic_stock_photos(self):
        queries = materials._query_terms("懂车帝这下麻烦了", "尊界汽车刹车踏板断裂", ["#尊界", "#刹车踏板"], [])
        self.assertTrue(queries)
        self.assertTrue(all(any("\u4e00" <= char <= "\u9fff" for char in query) for query in queries))

    def test_copied_query_does_not_make_unrelated_article_relevant(self):
        unrelated = dict(self.candidate, title="农村信贷员存款纠纷", description="账户资金问题", keywords="乡村消亡 老人 村庄")
        self.assertFalse(materials._candidate_relevance(unrelated, "乡村消亡 老人 村庄"))

    def test_public_image_site_cannot_enter_wechat_materials(self):
        unrelated = dict(self.candidate, title="尊界刹车测试", source="wikimedia")
        self.assertFalse(materials._candidate_relevance(unrelated, "尊界 刹车"))


if __name__ == "__main__":
    unittest.main()
