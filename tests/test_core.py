# -*- coding: utf-8 -*-
"""Core regression tests - standard library only, no pytest required.

Run from the project root:
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import server  # noqa: E402
import version  # noqa: E402


class FilenameSafety(unittest.TestCase):
    def test_safe_filename_allows_plain_name(self):
        self.assertEqual(server.safe_filename("IMG_001.jpg"), "IMG_001.jpg")

    def test_safe_filename_rejects_any_separator(self):
        # safe_filename deliberately rejects separators rather than stripping
        # them: callers must pass an already-extracted bare filename.
        for bad in ("dir/sub/photo.png", "dir\\photo.png"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                server.safe_filename(bad)

    def test_safe_filename_rejects_traversal(self):
        for bad in ("", ".", "..", "../etc/passwd", "a\\b.txt", "/abs.txt"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                server.safe_filename(bad)


class PathCleaning(unittest.TestCase):
    def test_clean_path_strips_quotes_from_copy_as_path(self):
        self.assertEqual(server._clean_path('  "C:\\My Data\\img"  '), "C:\\My Data\\img")

    def test_clean_path_strips_single_quotes(self):
        self.assertEqual(server._clean_path("'D:/set'"), "D:/set")

    def test_clean_path_handles_empty(self):
        self.assertEqual(server._clean_path(None), "")
        self.assertEqual(server._clean_path("   "), "")


class ConfigAtomicity(unittest.TestCase):
    def test_cfg_update_is_read_modify_write_and_survives_reload(self):
        original = server.CONFIG_PATH.read_bytes() if server.CONFIG_PATH.exists() else None
        try:
            server.cfg_save({})
            out = server.cfg_update(lambda c: c.__setitem__("mirror", "aliyun"))
            self.assertEqual(out["mirror"], "aliyun")
            self.assertEqual(server.cfg_load()["mirror"], "aliyun")
            # a second update must preserve the first key (true RMW)
            server.cfg_update(lambda c: c.__setitem__("device", "cpu"))
            cfg = server.cfg_load()
            self.assertEqual(cfg["mirror"], "aliyun")
            self.assertEqual(cfg["device"], "cpu")
        finally:
            if original is None:
                server.CONFIG_PATH.unlink(missing_ok=True)
            else:
                server.CONFIG_PATH.write_bytes(original)


class VersionSource(unittest.TestCase):
    def test_single_version_source(self):
        self.assertIsInstance(version.APP_VERSION, str)
        self.assertTrue(version.APP_VERSION.count(".") >= 1)
        import installer
        self.assertEqual(installer.VERSION, version.APP_VERSION)


class ModelNameValidation(unittest.TestCase):
    def test_model_name_regex(self):
        self.assertTrue(server.MODEL_NAME_RE.match("yolo26n.pt"))
        self.assertFalse(server.MODEL_NAME_RE.match("../../evil.exe"))
        self.assertFalse(server.MODEL_NAME_RE.match("x.pt/../../y"))
        self.assertFalse(server.MODEL_NAME_RE.match("plain"))


class ApiGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        cls._orig_log = server.Handler.log_message
        server.Handler.log_message = lambda *a: None
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        with urllib.request.urlopen(cls.base + "/", timeout=5) as r:
            import re
            m = re.search(rb'YSTUDIO_TOKEN="([^"]+)"', r.read())
        cls.token = m.group(1).decode() if m else ""

    @classmethod
    def tearDownClass(cls):
        server.Handler.log_message = cls._orig_log
        cls.srv.shutdown()
        cls.srv.server_close()

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_health_and_token(self):
        st, body = self.call("GET", "/api/health")
        self.assertEqual(st, 200)
        self.assertIn("yolo-studio", body)
        self.assertTrue(self.token, "token must be injected into index.html")

    def test_post_without_token_rejected(self):
        st, _ = self.call("POST", "/api/task/stop", {"id": "x"})
        self.assertEqual(st, 403)

    def test_foreign_origin_rejected(self):
        st, _ = self.call("POST", "/api/task/stop", {"id": "x"},
                          {"X-YStudio-Token": self.token, "Origin": "http://evil.example"})
        self.assertEqual(st, 403)

    def test_foreign_host_rejected(self):
        st, _ = self.call("GET", "/api/health", None, {"Host": "evil.example"})
        self.assertEqual(st, 403)

    def test_valid_token_accepted(self):
        st, _ = self.call("POST", "/api/task/stop", {"id": "x"},
                          {"X-YStudio-Token": self.token})
        self.assertNotEqual(st, 403)

    def test_malicious_wheel_index_rejected(self):
        st, body = self.call("POST", "/api/task/start",
                             {"type": "backend", "params": {"backend": "evil",
                                                            "index": "http://evil/simple"}},
                             {"X-YStudio-Token": self.token})
        self.assertEqual(st, 400)
        self.assertIn("accelerator", body)

    def test_model_traversal_rejected(self):
        st, _ = self.call("POST", "/api/task/start",
                          {"type": "download_model", "params": {"model": "../../evil.exe"}},
                          {"X-YStudio-Token": self.token})
        self.assertEqual(st, 400)

    def test_arbitrary_download_host_rejected(self):
        st, body = self.call("POST", "/api/task/start",
                             {"type": "download_model",
                              "params": {"model": "yolo26n.pt", "url": "https://evil.example/x.pt"}},
                             {"X-YStudio-Token": self.token})
        self.assertEqual(st, 400)

    def test_exe_open_blocked(self):
        st, body = self.call("POST", "/api/open",
                             {"path": str(server.WORKSPACE / "payload.exe")},
                             {"X-YStudio-Token": self.token})
        self.assertEqual(st, 400)
        self.assertIn("cannot be opened", body)

    def test_status_exposes_version(self):
        st, body = self.call("GET", "/api/status")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["version"], version.APP_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
