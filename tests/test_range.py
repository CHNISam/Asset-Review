import http.client
import os
import tempfile
import threading
import unittest
from pathlib import Path

from asset_viewer.app import AssetViewerHandler, ThreadingHTTPServer, byte_range, scan_collection
from asset_viewer.storage import add_collection


class ByteRangeTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(byte_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(byte_range("bytes=90-", 100), (90, 99))
        self.assertEqual(byte_range("bytes=-10", 100), (90, 99))
        self.assertIsNone(byte_range(None, 100))
        self.assertIsNone(byte_range("bytes=0-1,5-6", 100))
        self.assertFalse(byte_range("bytes=200-", 100))


class RangeHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        root = Path(self.tmp.name) / "clips"
        root.mkdir()
        clip = root / "clip.mp4"
        clip.write_bytes(bytes(range(256)) * 16)
        self.size = clip.stat().st_size
        add_collection(str(root), "Clips")
        scan_collection("clips", force=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AssetViewerHandler)
        self.server.trusted_hosts = {"127.0.0.1", "localhost", "::1"}
        self.server.csrf_token = "t"
        self.server.auth_password = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("ASSET_VIEWER_HOME", None)
        os.environ.pop("ASSET_VIEWER_CACHE", None)
        self.tmp.cleanup()

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_originals_support_ranges_so_video_can_seek(self):
        response, body = self.get("/asset/file/clips/clip.mp4", {"Range": "bytes=100-199"})
        self.assertEqual((response.status, body), (206, (bytes(range(256)) * 16)[100:200]))
        self.assertEqual(response.getheader("Content-Range"), f"bytes 100-199/{self.size}")
        response, body = self.get("/asset/file/clips/clip.mp4")
        self.assertEqual((response.status, len(body), response.getheader("Accept-Ranges")), (200, self.size, "bytes"))
        response, _ = self.get("/asset/file/clips/clip.mp4", {"Range": f"bytes={self.size + 5}-"})
        self.assertEqual(response.status, 416)


if __name__ == "__main__":
    unittest.main()
