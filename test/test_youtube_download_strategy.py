import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from get_transcripts import (  # noqa: E402
    build_youtube_audio_download_cmd,
    build_youtube_extractor_args,
)


class YoutubeDownloadStrategyTests(unittest.TestCase):
    def test_cookie_strategy_adds_cookie_file(self):
        command = build_youtube_audio_download_cmd(
            'https://www.youtube.com/watch?v=Sotw8i4nlgU',
            '/tmp/audio.mp3',
            ['--js-runtimes', 'node:/opt/node'],
            ['--cookies', '/tmp/cookies.txt'],
            with_cookies=True,
        )

        self.assertIn('--cookies', command)
        self.assertIn('/tmp/cookies.txt', command)
        self.assertNotIn('--extractor-args', command)

    @patch('get_transcripts.get_youtube_po_token', return_value=None)
    def test_mweb_fallback_is_a_distinct_format(self, _token_mock):
        command = build_youtube_audio_download_cmd(
            'https://www.youtube.com/watch?v=Sotw8i4nlgU',
            '/tmp/audio.mp3',
            [],
            ['--cookies', '/tmp/cookies.txt'],
            player_client='mweb',
            format_selector='18',
        )

        format_index = command.index('-f')
        extractor_index = command.index('--extractor-args')
        self.assertEqual(command[format_index + 1], '18')
        self.assertEqual(
            command[extractor_index + 1],
            'youtube:player_client=mweb',
        )
        self.assertNotIn('--cookies', command)

    @patch('get_transcripts.get_youtube_po_token', return_value='token-value')
    def test_po_token_uses_client_and_gvs_scope(self, _token_mock):
        self.assertEqual(
            build_youtube_extractor_args('mweb'),
            'youtube:player_client=mweb;po_token=mweb.gvs+token-value',
        )


if __name__ == '__main__':
    unittest.main()
