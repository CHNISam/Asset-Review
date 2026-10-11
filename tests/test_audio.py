import http.client
import json
import math
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from asset_viewer import production
from asset_viewer.app import AssetViewerHandler, ThreadingHTTPServer, image_rows, make_thumbnail, scan_collection
from asset_viewer.audio import peaks_for, wav_peaks
from asset_viewer.storage import add_collection, collection_review_state, set_review
from asset_viewer.handoff import approved_handoff


def write_wav(path: Path, *, seconds=0.5, rate=8000, bits=16, channels=1, fmt_tag=1, amplitude=0.5, freq=440):
    frames = int(seconds * rate)
    samples = []
    for i in range(frames):
        # Loud first half, quiet second half: peaks must show the envelope.
        level = amplitude if i < frames // 2 else amplitude / 10
        samples.append(level * math.sin(2 * math.pi * freq * i / rate))
    if fmt_tag == 3:
        data = b"".join(struct.pack("<f", v) for v in samples for _ in range(channels))
    elif bits == 16:
        data = b"".join(struct.pack("<h", int(v * 32767)) for v in samples for _ in range(channels))
    elif bits == 24:
        data = b"".join(int(v * 8388607).to_bytes(3, "little", signed=True) for v in samples for _ in range(channels))
    else:
        raise ValueError(bits)
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", fmt_tag, channels, rate, rate * align, align, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"LIST" + struct.pack("<I", 4) + b"INFO" + b"data" + struct.pack("<I", len(data)) + data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.root = Path(self.tmp.name) / "audio"

    def tearDown(self):
        os.environ.pop("ASSET_VIEWER_HOME", None)
        os.environ.pop("ASSET_VIEWER_CACHE", None)
        self.tmp.cleanup()

    def test_peaks_follow_the_envelope_for_common_wav_encodings(self):
        for name, kwargs in (("pcm16.wav", {}), ("pcm24.wav", {"bits": 24}), ("float.wav", {"bits": 32, "fmt_tag": 3}),
                             ("stereo.wav", {"channels": 2})):
            data = wav_peaks(write_wav(self.root / name, **kwargs), buckets=20)
            self.assertAlmostEqual(data["duration"], 0.5, places=2)
            loud, quiet = max(data["peaks"][:9]), max(data["peaks"][11:])
            self.assertGreater(loud, 0.4, name)
            self.assertLess(quiet, 0.1, name)

    def test_non_wav_audio_plays_without_a_waveform(self):
        path = self.root / "theme.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
        self.assertEqual(peaks_for(path)["peaks"], [])
        broken = self.root / "broken.wav"
        broken.write_bytes(b"not riff")
        self.assertIn("error", peaks_for(broken))

    def test_audio_joins_the_catalog_review_and_handoff_unchanged(self):
        write_wav(self.root / "kawai-middle.wav")
        add_collection(str(self.root), "Audition", "Game / Hall / 乐器 / 试听")
        rows = image_rows("audition")
        self.assertEqual([(r["name"], r["media_type"]) for r in rows], [("kawai-middle.wav", "audio")])
        self.assertTrue(make_thumbnail(self.root / "kawai-middle.wav").startswith(b"\xff\xd8"))
        set_review("audition", "kawai-middle.wav", "approved", "温暖，保留")
        handoff = approved_handoff("audition")
        self.assertEqual([item["rel"] for item in handoff["items"]], ["kawai-middle.wav"])

    def test_sample_banks_are_tracked_but_never_pending(self):
        bank = self.root / "bank"
        for note in ("C4", "D4", "E4"):
            write_wav(bank / f"Player_vl1_{note}.wav", seconds=0.05)
        add_collection(str(bank), "Kawai bank", "Game / Hall / 乐器 / 音源库")
        scan_collection("kawai-bank", force=True)
        state = collection_review_state("kawai-bank")
        self.assertEqual(state["unreviewed"], 3)
        self.assertFalse(state["review_required"])
        self.assertFalse(state["pending"])

    def test_instrument_assets_use_audio_words_and_a_source_stage(self):
        write_wav(self.root / "kawai" / "audition" / "middle" / "middle_v1.wav")
        write_wav(self.root / "kawai" / "audition" / "middle" / "middle_v2.wav", amplitude=0.3)
        add_collection(str(self.root), "Audio", "Game / Hall / 乐器 / 试听")
        scan_collection("audio", force=True)
        detail = production.create_asset("Game", "Kawai", key="kawai", kind="instrument", views=["middle"],
                                         source_collection="audio", source_prefix="kawai")
        self.assertEqual([s["stage"] for s in detail["stages"]], ["source", "design", "engine"])
        self.assertEqual([s["label"] for s in detail["stages"]], ["音源", "试听", "实机"])
        self.assertEqual(detail["gates"]["visual"]["label"], "听感批准")
        production.discover("kawai")
        production.attach_evidence("kawai", "source", source="assets/audio/candidates/kawai_candidate · 80 WAV", caption="音源库")
        detail = production.asset_detail("kawai")
        middle = detail["views"][0]
        self.assertEqual([r["label"] for r in middle["revisions"]], ["v1", "v2"])
        self.assertEqual(middle["revisions"][0]["media_type"], "audio")
        self.assertTrue(detail["asset"]["audio"])


class AudioHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        root = Path(self.tmp.name) / "audio"
        self.size = write_wav(root / "clip.wav").stat().st_size
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

    def test_originals_support_ranges_so_players_can_seek(self):
        response, body = self.get("/asset/file/clips/clip.wav", {"Range": "bytes=100-199"})
        self.assertEqual(response.status, 206)
        self.assertEqual(len(body), 100)
        self.assertEqual(response.getheader("Content-Range"), f"bytes 100-199/{self.size}")
        self.assertTrue(response.getheader("Content-Type").startswith("audio/"))
        response, body = self.get("/asset/file/clips/clip.wav")
        self.assertEqual((response.status, len(body), response.getheader("Accept-Ranges")), (200, self.size, "bytes"))
        response, _ = self.get("/asset/file/clips/clip.wav", {"Range": f"bytes={self.size + 5}-"})
        self.assertEqual(response.status, 416)

    def test_peaks_endpoint(self):
        response, body = self.get("/asset/peaks/clips/clip.wav")
        self.assertEqual(response.status, 200)
        self.assertGreater(len(json.loads(body)["peaks"]), 10)
        response, _ = self.get("/asset/peaks/clips/..%2Fsecret.wav")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
