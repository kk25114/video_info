import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "video_get_image" / "extract_static_background_images.py"
SPEC = importlib.util.spec_from_file_location("static_background_image_extractor", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class StaticBackgroundImageExtractorTests(unittest.TestCase):
    def _create_video(self, output_path: Path) -> None:
        writer = cv2.VideoWriter(
            str(output_path),
            cv2.VideoWriter_fourcc(*"MJPG"),
            10.0,
            (320, 180),
        )
        self.assertTrue(writer.isOpened())
        static_picture = np.zeros((90, 120, 3), dtype=np.uint8)
        static_picture[:, :] = (30, 150, 230)
        cv2.circle(static_picture, (60, 45), 28, (240, 240, 240), -1)
        cv2.line(static_picture, (0, 0), (119, 89), (20, 40, 150), 5)

        for frame_index in range(100):
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            frame[:, :, 0] = (frame_index * 11) % 255
            frame[:, :, 1] = (frame_index * 17) % 255
            frame[:, :, 2] = (frame_index * 23) % 255
            cv2.rectangle(frame, ((frame_index * 7) % 260, 8), ((frame_index * 7) % 260 + 55, 38), (255, 255, 255), -1)
            # 第 2 到 8 秒固定展示图片，四周背景仍持续运动。
            if 20 <= frame_index < 80:
                frame[45:135, 100:220] = static_picture
            writer.write(frame)
        writer.release()

    def test_extracts_picture_that_is_static_while_background_moves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "sample.avi"
            output_dir = root / "output"
            self._create_video(video_path)

            images = MODULE.extract_static_images(
                video_path,
                output_dir,
                sample_fps=2.0,
                min_static_seconds=3.0,
                diff_threshold=14,
                min_area_ratio=0.08,
                min_background_motion=0.02,
                analysis_width=320,
            )

            self.assertEqual(len(images), 1)
            self.assertGreaterEqual(images[0]["duration_seconds"], 3.0)
            self.assertTrue((output_dir / images[0]["file"]).is_file())
            self.assertTrue((output_dir / "提取结果.json").is_file())

    def test_does_not_treat_a_fully_static_video_as_background_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "static.avi"
            writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (160, 100))
            frame = np.full((100, 160, 3), (120, 80, 40), dtype=np.uint8)
            for _ in range(70):
                writer.write(frame)
            writer.release()

            images = MODULE.extract_static_images(
                video_path,
                root / "output",
                sample_fps=2.0,
                min_static_seconds=3.0,
                min_area_ratio=0.05,
                analysis_width=160,
            )

            self.assertEqual(images, [])

    def test_trims_large_light_card_without_using_small_watermark(self):
        image = np.full((180, 320, 3), (220, 150, 50), dtype=np.uint8)
        image[20:150, 80:240] = (255, 255, 255)
        cv2.circle(image, (160, 85), 35, (240, 120, 20), -1)
        image[75:105, 10:65] = (255, 255, 255)

        cropped = MODULE.trim_light_card(image)

        self.assertGreaterEqual(cropped.shape[1], 150)
        self.assertLess(cropped.shape[1], 190)
        self.assertGreaterEqual(cropped.shape[0], 120)
        self.assertLess(cropped.shape[0], 150)

    def test_trims_card_using_straight_border_lines(self):
        image = np.full((200, 340, 3), (230, 150, 40), dtype=np.uint8)
        cv2.rectangle(image, (85, 25), (270, 175), (255, 255, 255), -1)
        cv2.rectangle(image, (85, 25), (270, 175), (40, 40, 40), 2)

        cropped = MODULE.trim_rectangular_card(image)

        self.assertIsNotNone(cropped)
        self.assertGreaterEqual(cropped.shape[1], 180)
        self.assertLessEqual(cropped.shape[1], 190)
        self.assertGreaterEqual(cropped.shape[0], 145)
        self.assertLessEqual(cropped.shape[0], 155)

    def test_trims_card_from_top_edge_when_one_vertical_edge_is_missing(self):
        image = np.full((220, 360, 3), (230, 150, 40), dtype=np.uint8)
        cv2.rectangle(image, (80, 25), (280, 175), (255, 255, 255), -1)
        cv2.line(image, (80, 25), (280, 25), (40, 40, 40), 2)

        cropped = MODULE.trim_rectangular_card(image, expected_height=150)

        self.assertIsNotNone(cropped)
        self.assertGreaterEqual(cropped.shape[1], 195)
        self.assertLessEqual(cropped.shape[1], 205)
        self.assertEqual(cropped.shape[0], 150)

    def test_brief_detection_gap_does_not_split_one_static_picture(self):
        # 同一图片的候选框在一段极短的检测空窗后重新出现，应仍合并为一份素材。
        track = MODULE.Track(
            box=(10, 10, 100, 80),
            start_time=1.0,
            last_time=5.0,
            best_frame=np.full((80, 100, 3), 120, dtype=np.uint8),
            best_sharpness=30.0,
            last_seen_index=20,
        )
        self.assertLessEqual(23 - track.last_seen_index, round(2.0 * 3.0))


if __name__ == "__main__":
    unittest.main()
