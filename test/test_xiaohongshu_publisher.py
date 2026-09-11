import importlib.util
import sys
import unittest
from pathlib import Path


PUBLISHER_DIR = Path(__file__).resolve().parents[1] / "xiaohongshu_playwright"
sys.path.insert(0, str(PUBLISHER_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "xiaohongshu_publish_video_test_module",
    PUBLISHER_DIR / "publish_video.py",
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("无法加载小红书发布器测试模块")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

build_caption = _MODULE.build_caption
trim_xhs_text = _MODULE.trim_xhs_text
xhs_text_length = _MODULE.xhs_text_length


class XiaohongshuPublisherTests(unittest.TestCase):
    def test_chinese_title_is_trimmed_to_xhs_limit(self):
        title = "这是一个超过小红书标题长度限制的中文标题"
        result = trim_xhs_text(title, 20)

        self.assertLessEqual(xhs_text_length(result), 20)
        self.assertEqual(result, title[:20])

    def test_explicit_caption_does_not_need_auto_description(self):
        title, desc = build_caption(
            Path("example.mp4"),
            "手工标题",
            "手工正文 #测试",
            auto_desc=False,
            auto_desc_max_chars=50,
            title_limit=20,
            desc_limit=1000,
        )

        self.assertEqual(title, "手工标题")
        self.assertEqual(desc, "手工正文 #测试")


if __name__ == "__main__":
    unittest.main()
