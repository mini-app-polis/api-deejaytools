"""Golden cases for magic-byte detection, ported from deejaytools-api audioFormat.test.ts."""

from __future__ import annotations

from api_deejaytools.services.audio_format import detect_audio_format


def test_detects_mp3_with_id3v2_header() -> None:
    assert detect_audio_format(b"ID3" + bytes(20)) == "audio/mpeg"


def test_detects_mp3_with_raw_mpeg_frame_sync() -> None:
    data = bytes([0xFF, 0xFB, 0x90, 0x00, 0, 0, 0, 0, 0, 0, 0, 0])
    assert detect_audio_format(data) == "audio/mpeg"


def test_detects_wav() -> None:
    assert detect_audio_format(b"RIFF\x00\x00\x00\x00WAVE") == "audio/wav"


def test_detects_flac() -> None:
    assert detect_audio_format(b"fLaC" + bytes(20)) == "audio/flac"


def test_detects_m4a_ftyp_box() -> None:
    data = bytes(
        [0, 0, 0, 0x20, 0x66, 0x74, 0x79, 0x70, 0x4D, 0x34, 0x41, 0x20, 0, 0, 0, 0]
    )
    assert detect_audio_format(data) == "audio/mp4"


def test_returns_none_for_non_audio_plain_text() -> None:
    assert detect_audio_format(b"Hello, world! This is a text file.") is None


def test_returns_none_for_too_short_buffer() -> None:
    assert detect_audio_format(b"hi") is None


def test_returns_none_for_a_pdf() -> None:
    assert detect_audio_format(b"%PDF-1.4" + bytes(20)) is None
