import asyncio
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "douyin_playwright" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from publish_video import _type_desc_text  # noqa: E402


class _FakeKeyboard:
    def __init__(self):
        self.events = []

    async def type(self, value, delay=0):
        self.events.append(("type", value, delay))

    async def press(self, key):
        self.events.append(("press", key))

    async def insert_text(self, value):
        self.events.append(("insert_text", value))


class _FakePage:
    def __init__(self):
        self.keyboard = _FakeKeyboard()


class TopicTypingTests(unittest.TestCase):
    def test_topics_are_typed_character_by_character_with_space_key(self):
        page = _FakePage()

        asyncio.run(_type_desc_text(page, "摘要#话题一 #话题二"))

        self.assertEqual(
            page.keyboard.events,
            [
                ("insert_text", "摘要"),
                ("type", "#", 30),
                ("type", "话", 30),
                ("type", "题", 30),
                ("type", "一", 30),
                ("press", "Space"),
                ("type", "#", 30),
                ("type", "话", 30),
                ("type", "题", 30),
                ("type", "二", 30),
                ("press", "Space"),
            ],
        )
        self.assertEqual(
            [event for event in page.keyboard.events if event[0] == "insert_text"],
            [("insert_text", "摘要")],
        )

    def test_plain_text_can_be_pasted_as_one_block(self):
        page = _FakePage()

        asyncio.run(_type_desc_text(page, "没有话题的简介"))

        self.assertEqual(page.keyboard.events, [("insert_text", "没有话题的简介")])

    def test_topic_without_trailing_separator_gets_space(self):
        page = _FakePage()

        asyncio.run(_type_desc_text(page, "#话题"))

        self.assertEqual(page.keyboard.events[-1], ("press", "Space"))


if __name__ == "__main__":
    unittest.main()
