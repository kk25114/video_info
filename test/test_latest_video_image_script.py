import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "mk_video" / "extract_latest_video_images.py"
SPEC = importlib.util.spec_from_file_location("extract_latest_video_images", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LatestVideoImageScriptTests(unittest.TestCase):
    def test_reads_latest_video_id_from_processed_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "processed_videos.log"
            log_path.write_text("oldVideo123\nnewVideo456\n", encoding="utf-8")

            self.assertEqual(
                MODULE.get_latest_video_url(log_path),
                "https://www.youtube.com/watch?v=newVideo456",
            )

    def test_extracts_ids_from_common_youtube_link_forms(self):
        self.assertEqual(MODULE.extract_video_id("abcdefghijk"), "abcdefghijk")
        self.assertEqual(
            MODULE.extract_video_id("https://www.youtube.com/watch?v=abcdefghijk&t=20"),
            "abcdefghijk",
        )
        self.assertEqual(
            MODULE.extract_video_id("https://youtu.be/abcdefghijk?si=demo"),
            "abcdefghijk",
        )

    def test_formats_picture_time_and_total_time_for_build_script(self):
        self.assertEqual(MODULE.format_timestamp_code(5), "005")
        self.assertEqual(MODULE.format_timestamp_code(131), "211")
        self.assertEqual(MODULE.format_timestamp_code(480, total=True), "0800")
        self.assertEqual(MODULE.build_image_filename(5, 600), "005-0800.png")

    def test_uses_real_seconds_for_thirteen_minute_eighty_percent(self):
        self.assertEqual(MODULE.eighty_percent_seconds(13 * 60), 624)
        self.assertEqual(MODULE.build_image_filename(5, 13 * 60), "005-1024.png")

    def test_exports_latest_video_images_and_manifest(self):
        class FakeExtractor:
            def download_youtube_video(self, url, output_dir, proxy, max_height):
                video_dir = output_dir / "下载视频"
                video_dir.mkdir(parents=True, exist_ok=True)
                video_path = video_dir / "abcdefghijk.mp4"
                video_path.write_bytes(b"video")
                return video_path

            def probe_video_resolution(self, video_path):
                return 1920, 1080

            def extract_static_images(self, video_path, output_dir, **kwargs):
                image_name = "001_00m05s_5.0s.png"
                (output_dir / image_name).write_bytes(b"png-data")
                return [
                    {
                        "file": image_name,
                        "start_seconds": 5.0,
                        "end_seconds": 10.0,
                        "duration_seconds": 5.0,
                        "image_resolution": [640, 360],
                    }
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "processed_videos.log"
            log_path.write_text("abcdefghijk\n", encoding="utf-8")
            output_dir = root / "images"
            work_dir = root / "work"

            with (
                mock.patch.object(MODULE, "EXTRACTOR", FakeExtractor()),
                mock.patch.object(MODULE, "probe_duration", return_value=600.0),
            ):
                exported = MODULE.extract_latest_video_images(
                    processed_log=log_path,
                    output_dir=output_dir,
                    work_dir=work_dir,
                )

            self.assertEqual([item["file"] for item in exported], ["005-0800.png"])
            self.assertEqual((output_dir / "005-0800.png").read_bytes(), b"png-data")
            manifest = json.loads((root / "latest_video_images.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_video"], "https://www.youtube.com/watch?v=abcdefghijk")
            self.assertEqual(manifest["eighty_percent_seconds"], 480)


if __name__ == "__main__":
    unittest.main()
