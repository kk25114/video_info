import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from get_transcripts import fetch_youtube_transcript_text  # noqa: E402


class YoutubeTranscriptApiTests(unittest.TestCase):
    @patch('get_transcripts.YouTubeTranscriptApi')
    def test_uses_instance_fetch_and_snippet_text(self, api_class_mock):
        api = api_class_mock.return_value
        api.fetch.return_value = [
            SimpleNamespace(text='第一段'),
            SimpleNamespace(text='第二段'),
        ]

        result = fetch_youtube_transcript_text('video-id')

        self.assertEqual(result, '第一段\n\n第二段')
        api.fetch.assert_called_once_with(
            'video-id',
            languages=['zh-Hans', 'zh-CN', 'zh', 'en'],
        )


if __name__ == '__main__':
    unittest.main()
