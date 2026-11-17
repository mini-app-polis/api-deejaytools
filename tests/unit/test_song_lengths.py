"""Reading a song's length from its bytes (services/song_lengths.py)."""

from __future__ import annotations

import io
import wave

from api_deejaytools.services.song_lengths import read_duration_ms


def _wav(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def test_a_wav_reads_its_length_in_whole_milliseconds() -> None:
    assert read_duration_ms(_wav(1.5)) == 1500


def test_bytes_that_are_not_audio_have_no_length() -> None:
    assert read_duration_ms(b"not audio at all, just some text") is None
    assert read_duration_ms(b"") is None


def test_audio_with_no_samples_has_no_length() -> None:
    assert read_duration_ms(_wav(0)) is None
