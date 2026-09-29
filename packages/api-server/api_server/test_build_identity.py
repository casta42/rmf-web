"""F4.2 (FR-30): the build identity is read from what the IMAGE recorded —
the file first, the environment second, and "dev" (with the reason) when a
server is not running from a release image at all. Pure: no app, no DB."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api_server.build_identity import UNKNOWN, build_identity


class TestBuildIdentity(unittest.TestCase):
    def _file(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / "VERSION"
        p.write_text(text)
        return p

    def test_the_image_record_is_read_line_by_line(self):
        p = self._file(
            "f1-n54\n5f6a8ee4b0f5\nrmf_pin_sha256=55f51dce\npatches=f106,f132\n"
        )
        got = build_identity(p)
        self.assertEqual("f1-n54", got["version"])
        self.assertEqual("5f6a8ee4b0f5", got["git_sha"])
        self.assertEqual("55f51dce", got["rmf_pin_sha256"])
        self.assertEqual("f106,f132", got["patches"])
        self.assertEqual(str(p), got["source"])

    def test_a_short_record_says_what_it_does_not_know(self):
        got = build_identity(self._file("f1-n54\n"))
        self.assertEqual("f1-n54", got["version"])
        self.assertEqual(UNKNOWN, got["git_sha"])
        self.assertEqual(UNKNOWN, got["patches"])

    def test_the_environment_is_the_fallback(self):
        with mock.patch.dict(
            os.environ, {"GF_VERSION": "f1-n54", "GF_GIT_SHA": "abc"}, clear=False
        ):
            got = build_identity(Path("/nonexistent/VERSION"))
        self.assertEqual("f1-n54", got["version"])
        self.assertEqual("abc", got["git_sha"])
        self.assertIn("GF_VERSION", got["source"])

    def test_no_record_at_all_is_dev_and_says_why(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GF_")}
        with mock.patch.dict(os.environ, env, clear=True):
            got = build_identity(Path("/nonexistent/VERSION"))
        self.assertEqual("dev", got["version"])
        self.assertIn("not running from a release image", got["source"])
        self.assertEqual(UNKNOWN, got["git_sha"])


if __name__ == "__main__":
    unittest.main()
