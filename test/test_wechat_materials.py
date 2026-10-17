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
