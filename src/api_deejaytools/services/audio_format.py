"""Magic-byte detection of the audio formats uploads may use (deejaytools-api DRIVE.md).

The client's declared MIME type is unreliable (iOS Safari sends
``application/octet-stream`` for valid MP3s), so the bytes decide. The formats
recognised are exactly the ones :func:`~.tagging.tag_song_bytes` can tag.
"""

from __future__ import annotations

from typing import Literal

DetectedAudioFormat = Literal["audio/mpeg", "audio/wav", "audio/flac", "audio/mp4"]


def detect_audio_format(data: bytes) -> DetectedAudioFormat | None:
    """Return the canonical MIME type for ``data``, or ``None`` if not recognised.

    Checked in this order: ``ID3`` at 0 (MP3); ``0xFF`` followed by a byte with
    its top three bits set (MPEG frame sync, MP3); ``RIFF`` at 0 and ``WAVE`` at
    8 (WAV); ``fLaC`` at 0 (FLAC); ``ftyp`` at 4 (M4A/MP4, any brand). Fewer
    than 12 bytes is never recognised.
    """
    if len(data) < 12:
        return None
    if data[:3] == b"ID3":
        return "audio/mpeg"
    if data[0] == 0xFF and data[1] & 0xE0 == 0xE0:
        return "audio/mpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    return None
