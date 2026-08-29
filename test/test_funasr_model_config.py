import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from get_transcripts import (  # noqa: E402
    FUNASR_MODEL_CACHE_NAMES,
    get_funasr_model_kwargs,
)


class FunASRModelConfigTests(unittest.TestCase):
    def _create_cache(self, root, complete=True):
        for directory_name in FUNASR_MODEL_CACHE_NAMES.values():
            model_dir = Path(root) / directory_name
            model_dir.mkdir(parents=True)
            (model_dir / "config.yaml").write_text("model: test\n", encoding="utf-8")
            if complete or directory_name != FUNASR_MODEL_CACHE_NAMES["vad_model"]:
                (model_dir / "model.pt").touch()

    def test_uses_local_models_when_all_cache_files_exist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._create_cache(temp_dir)

            result = get_funasr_model_kwargs(temp_dir)

            self.assertTrue(result["check_latest"] is False)
            self.assertTrue(result["model"].startswith(temp_dir))
            self.assertTrue(result["vad_model"].startswith(temp_dir))
            self.assertTrue(result["punc_model"].startswith(temp_dir))

    def test_keeps_remote_aliases_when_cache_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._create_cache(temp_dir, complete=False)

            self.assertEqual(
                get_funasr_model_kwargs(temp_dir),
                {
                    "model": "paraformer-zh",
                    "vad_model": "fsmn-vad",
                    "punc_model": "ct-punc-c",
                },
            )


if __name__ == "__main__":
    unittest.main()
