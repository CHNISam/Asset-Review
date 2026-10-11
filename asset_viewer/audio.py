"""Audio support without new dependencies: WAV peaks for waveforms and thumbnails.

Browsers play the original file in place (WAV, MP3, OGG, FLAC, M4A, Opus).
For WAV we read the RIFF data once, keep a few hundred min/max peaks and
cache them, so grids and A/B players draw instantly without decoding whole
recordings in the browser. Other formats still play; they just have no
waveform until a decoder is available.
"""

from __future__ import annotations

import json
import mimetypes
import struct
from array import array
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .storage import AUDIO_EXTS, _render_cache_path  # noqa: F401  (re-exported)
for _suffix, _type in ((".wav", "audio/wav"), (".mp3", "audio/mpeg"), (".ogg", "audio/ogg"), (".oga", "audio/ogg"),
                      (".flac", "audio/flac"), (".m4a", "audio/mp4"), (".aac", "audio/aac"), (".opus", "audio/ogg")):
    mimetypes.add_type(_type, _suffix)  # Windows registries disagree; browsers need a real audio type

PEAK_BUCKETS = 800
MAX_PEAK_SOURCE_BYTES = 512 * 1024 * 1024
_CHUNK_FRAMES = 65536


def _wav_format(handle: Any) -> tuple[dict[str, int], int, int]:
    """Return (fmt, data_offset, data_size) for a RIFF/WAVE file."""
    header = handle.read(12)
    if len(header) < 12 or header[:4] not in (b"RIFF", b"RF64") or header[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt: dict[str, int] | None = None
    while True:
        chunk = handle.read(8)
        if len(chunk) < 8:
            break
        name, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
        if name == b"fmt ":
            body = handle.read(size)
            tag, channels, rate, _, align, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:  # WAVE_FORMAT_EXTENSIBLE: real tag is the GUID prefix
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = {"tag": tag, "channels": channels, "rate": rate, "align": align, "bits": bits}
        elif name == b"data":
            if not fmt:
                raise ValueError("WAV data before fmt chunk")
            return fmt, handle.tell(), size
        else:
            handle.seek(size + (size & 1), 1)
        if name == b"fmt " and size & 1:
            handle.seek(1, 1)
    raise ValueError("WAV has no data chunk")


def _amplitudes(raw: bytes, tag: int, bits: int) -> tuple[array, float]:
    """Decode a PCM/float byte run into numbers plus the full-scale value."""
    if tag == 3 and bits == 32:
        return array("f", raw[: len(raw) - len(raw) % 4]), 1.0
    if tag == 1 and bits == 16:
        return array("h", raw[: len(raw) - len(raw) % 2]), 32768.0
    if tag == 1 and bits == 32:
        return array("i", raw[: len(raw) - len(raw) % 4]), 2147483648.0
    if tag == 1 and bits == 24:
        return array("b", raw[2::3]), 128.0  # most significant byte is plenty for a waveform
    if tag == 1 and bits == 8:
        return array("b", bytes((b - 128) & 0xFF for b in raw)), 128.0
    raise ValueError(f"unsupported WAV encoding (format {tag}, {bits}-bit)")


def wav_peaks(path: Path, buckets: int = PEAK_BUCKETS) -> dict[str, Any]:
    if path.stat().st_size > MAX_PEAK_SOURCE_BYTES:
        raise ValueError("audio file too large for waveform")
    with path.open("rb") as handle:
        fmt, offset, size = _wav_format(handle)
        align = max(1, fmt["align"])
        frames = size // align
        if not frames or not fmt["rate"]:
            raise ValueError("empty WAV")
        per_bucket = max(1, -(-frames // buckets))
        peaks: list[float] = []
        handle.seek(offset)
        current = 0.0
        filled = 0
        remaining = frames
        stride = max(1, per_bucket // 512) * fmt["channels"]  # sample long buckets; transients still show
        while remaining > 0:
            take = min(_CHUNK_FRAMES, remaining)
            raw = handle.read(take * align)
            if not raw:
                break
            remaining -= take
            values, full = _amplitudes(raw, fmt["tag"], fmt["bits"])
            per_bucket_values = per_bucket * fmt["channels"]
            index = 0
            while index < len(values):
                room = per_bucket_values - filled
                window = values[index:index + room:stride] if stride > 1 else values[index:index + room]
                if window:
                    current = max(current, max(window) / full, -min(window) / full)
                consumed = min(room, len(values) - index)
                filled += consumed
                index += consumed
                if filled >= per_bucket_values:
                    peaks.append(round(min(1.0, current), 4))
                    current, filled = 0.0, 0
        if filled:
            peaks.append(round(min(1.0, current), 4))
    return {"duration": round(frames / fmt["rate"], 3), "rate": fmt["rate"], "channels": fmt["channels"],
            "bits": fmt["bits"], "peaks": peaks}


def peaks_for(path: Path) -> dict[str, Any]:
    """Cached peaks for any audio file; non-WAV formats report no waveform."""
    cache = _render_cache_path(path, "peaks").with_suffix(".json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    if path.suffix.lower() != ".wav":
        payload: dict[str, Any] = {"duration": None, "peaks": [], "error": "waveform needs WAV"}
    else:
        try:
            payload = wav_peaks(path)
        except (ValueError, OSError, struct.error) as exc:
            payload = {"duration": None, "peaks": [], "error": str(exc)}
    cache.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return payload


def render_waveform(path: Path, destination: Path, size: tuple[int, int]) -> bytes:
    """A calm waveform card used as the thumbnail/preview of an audio asset."""
    width = 640 if size[0] <= 640 else 1280
    height = 300 if width == 640 else 520
    image = Image.new("RGB", (width, height), (245, 242, 237))
    draw = ImageDraw.Draw(image)
    data = peaks_for(path)
    peaks = data.get("peaks") or []
    middle = height // 2
    if peaks:
        bars = min(len(peaks), width // 4)
        step = len(peaks) / bars
        bar_w = width / bars
        for i in range(bars):
            chunk = peaks[int(i * step): max(int((i + 1) * step), int(i * step) + 1)]
            amp = max(chunk) if chunk else 0
            h = max(2, amp * (height * 0.42))
            x = i * bar_w + bar_w * 0.2
            draw.rounded_rectangle((x, middle - h, x + bar_w * 0.6, middle + h), radius=bar_w * 0.3, fill=(46, 42, 38))
    else:
        draw.line((width * 0.1, middle, width * 0.9, middle), fill=(160, 152, 144), width=3)
    label = path.suffix.lower().lstrip(".").upper()
    if data.get("duration"):
        label += f" · {data['duration']:.1f}s"
    draw.text((16, 12), label, fill=(120, 112, 104))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "JPEG", quality=88)
    return destination.read_bytes()
