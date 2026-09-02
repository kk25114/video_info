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

    def test_trims_sustained_unstable_bottom_from_candidate_box(self):
        stable_mask = np.ones((100, 120), dtype=np.uint8)
        stable_mask[80:, :] = 0

        trimmed = MODULE.trim_unstable_bottom_edge((10, 10, 80, 85), stable_mask)

        # 在稳定区结束前留一行安全余量，避免字幕边界的抗锯齿像素被带出。
        self.assertEqual(trimmed, (10, 10, 80, 69))

    def test_keeps_box_when_bottom_is_only_briefly_unstable(self):
        stable_mask = np.ones((100, 120), dtype=np.uint8)
        stable_mask[80:82, :] = 0

        box = (10, 10, 80, 85)
        self.assertEqual(MODULE.trim_unstable_bottom_edge(box, stable_mask), box)

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

    def test_track_keeps_complete_box_after_partial_candidate(self):
        complete = np.full((120, 240, 3), 120, dtype=np.uint8)
        partial = np.full((120, 80, 3), 220, dtype=np.uint8)
        track = MODULE.Track(
            box=(40, 20, 240, 120),
            start_time=1.0,
            last_time=5.0,
            best_frame=complete,
            best_sharpness=20.0,
            last_seen_index=10,
            last_signature=MODULE.image_hash(complete),
            detector="card",
        )
        track.update(
            MODULE.Candidate(
                (200, 20, 80, 120),
                partial,
                5.5,
                999.0,
                signature=MODULE.image_hash(partial),
                detector="card",
            ),
            11,
        )
        self.assertEqual(track.box, (40, 20, 240, 120))
        self.assertEqual(track.best_frame.shape, complete.shape)

    def test_partial_box_matches_complete_box_but_adjacent_map_does_not(self):
        complete = np.full((120, 240, 3), 120, dtype=np.uint8)
        complete_hash = MODULE.image_hash(complete)
        partial = MODULE.Candidate(
            (200, 20, 80, 120),
            np.full((120, 80, 3), 220, dtype=np.uint8),
            5.0,
            20.0,
            signature=np.logical_not(complete_hash),
            detector="card",
        )
        track = MODULE.Track(
            box=(40, 20, 240, 120),
            start_time=1.0,
            last_time=4.0,
            best_frame=complete,
            best_sharpness=20.0,
            last_signature=complete_hash,
            detector="card",
        )
        self.assertTrue(MODULE.candidate_matches_track(track, partial))

        adjacent_map = MODULE.Candidate(
            (120, 10, 130, 130),
            np.full((130, 130, 3), 80, dtype=np.uint8),
            6.0,
            20.0,
            signature=np.logical_not(complete_hash),
            detector="card",
        )
        self.assertFalse(MODULE.candidate_matches_track(track, adjacent_map))

    def test_merges_overlapping_partial_track_with_complete_track(self):
        complete = MODULE.Track(
            box=(47, 51, 544, 197),
            start_time=10.0,
            last_time=30.0,
            best_frame=np.full((197, 544, 3), 120, dtype=np.uint8),
            best_sharpness=20.0,
            detector="card",
        )
        partial = MODULE.Track(
            box=(321, 34, 141, 255),
            start_time=20.0,
            last_time=24.0,
            best_frame=np.full((255, 141, 3), 120, dtype=np.uint8),
            best_sharpness=20.0,
            detector="card",
        )

        self.assertTrue(MODULE.tracks_can_merge(complete, partial))
        merged = MODULE.merge_tracks([complete, partial])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].box, complete.box)


if __name__ == "__main__":
    unittest.main()
