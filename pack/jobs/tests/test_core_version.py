import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import release_version


class ReleaseVersion(unittest.TestCase):
    """三端同号的唯一来源：发布快照读 engine.json 的 version；开发树取最近 tag 的数字段（期望按手写夹具）"""
    def test_snapshot_reads_engine_json(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "engine.json").write_text(json.dumps({"schema": 1, "version": "0.2.3"}))
            self.assertEqual(release_version(d), "0.2.3")

    def test_dev_tree_uses_latest_tag_numbers(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
            for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"], ["tag", "v0.3.1-beta.2"]):
                subprocess.run(["git", "-C", d, *args], check=True, env=env)
            self.assertEqual(release_version(d), "0.3.1")

    def test_nothing_known_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(release_version(d), "")


if __name__ == "__main__":
    unittest.main()
