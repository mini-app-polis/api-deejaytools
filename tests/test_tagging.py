"""Golden cases for song tagging, ported from deejaytools-api tagger.test.ts (ADR-009).

Every case in tagger.test.ts appears here with the same inputs and expected
fields; the test names follow the TypeScript ``it`` titles. Fixtures are built
byte-for-byte the way the TypeScript builds them, and tags are read back with
mutagen (or by walking bytes, where the TypeScript walks bytes). Cases after
the ports cover the fix the spec asks a reimplementation to make.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Sequence

import pytest
from mutagen.flac import VCFLACDict
from mutagen.id3 import COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1
from mutagen.mp4 import Atoms, MP4Tags

from api_deejaytools.services import tagging
from api_deejaytools.services.tagging import tag_song_bytes

SOURCE_ONLY_COMMENT = "final mix, ignore the intro before 0:08"


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the tagger's warning messages (the TS tests mock the logger)."""
    captured: list[str] = []

    class _Logger:
        def warning(self, message: str, *args: object, **kwargs: object) -> None:
            captured.append(message)

    monkeypatch.setattr(tagging, "logger", _Logger())
    return captured


# ------------------------------------------------------------------ ID3 helpers


def id3_tag(
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    comment: str | None = None,
    date: str | None = None,
    version: int = 3,
) -> bytes:
    """An ID3v2 tag on its own, as ``NodeID3.create`` makes it (v2.3, UTF-16)."""
    tags = ID3()
    if title is not None:
        tags.add(TIT2(encoding=1, text=[title]))
    if artist is not None:
        tags.add(TPE1(encoding=1, text=[artist]))
    if album is not None:
        tags.add(TALB(encoding=1, text=[album]))
    if genre is not None:
        tags.add(TCON(encoding=1, text=[genre]))
    if comment is not None:
        tags.add(COMM(encoding=1, lang="eng", desc="", text=[comment]))
    if date is not None:
        tags.add(TDRC(encoding=1, text=[date]))
    if version == 3:
        tags.update_to_v23()
    out = io.BytesIO()
    tags.save(out, v1=0, v2_version=version, padding=lambda _: 0)
    return out.getvalue()


def minimal_mp3(genre: str | None = None) -> bytes:
    return id3_tag(title="Original Title", artist="Original Artist", genre=genre)


def frame_sync_mp3() -> bytes:
    return bytes([0xFF, 0xFB]) + bytes([0xAA]) * 100


def read_id3(buf: bytes) -> ID3:
    """The ID3v2 tag at byte 0, frames exactly as stored (no v2.4 upgrade)."""
    return ID3(io.BytesIO(buf), translate=False)


def _text(buf: bytes, frame_id: str) -> str | None:
    frame = read_id3(buf).get(frame_id)
    return str(frame.text[0]) if frame is not None and frame.text else None


def read_mp3_title(buf: bytes) -> str | None:
    return _text(buf, "TIT2")


def read_mp3_artist(buf: bytes) -> str | None:
    return _text(buf, "TPE1")


def read_mp3_genre(buf: bytes) -> str | None:
    return _text(buf, "TCON")


def read_mp3_year(buf: bytes) -> str | None:
    return _text(buf, "TYER")


def read_mp3_comment(buf: bytes) -> str | None:
    frames = read_id3(buf).getall("COMM")
    if not frames:
        return None
    text = "".join(str(t) for t in frames[0].text)
    return text or None


# ------------------------------------------------------------------ tagSongBytes


def test_returns_original_bytes_unchanged_for_unsupported_format() -> None:
    data = b"not audio"
    result = tag_song_bytes(
        data, title="New Title", artist="New Artist", mime_type="audio/ogg"
    )
    assert result is data


def test_returns_original_bytes_unchanged_when_no_mime_type_provided() -> None:
    data = b"test"
    assert tag_song_bytes(data, title="Title", artist="Artist") is data


def test_tags_mp3_bytes_and_updates_title_and_artist() -> None:
    result = tag_song_bytes(
        minimal_mp3(), title="New Title", artist="New Artist", mime_type="audio/mpeg"
    )
    assert read_mp3_title(result) == "New Title"
    assert read_mp3_artist(result) == "New Artist"
    assert read_mp3_genre(result) == "_Routine_"


def test_sets_mp3_genre_to_routine_and_overwrites_an_existing_genre() -> None:
    data = minimal_mp3(genre="Rock")
    assert read_mp3_genre(data) == "Rock"
    result = tag_song_bytes(
        data, title="New Title", artist="New Artist", mime_type="audio/mpeg"
    )
    assert read_mp3_title(result) == "New Title"
    assert read_mp3_artist(result) == "New Artist"
    assert read_mp3_genre(result) == "_Routine_"


def test_writes_provenance_comment_with_populated_fields_only() -> None:
    result = tag_song_bytes(
        minimal_mp3(), title="New Title", artist="New Artist", mime_type="audio/mpeg"
    )
    comment = read_mp3_comment(result)
    assert comment == "title=Original Title,artist=Original Artist"
    assert "prev[" not in comment
    assert "album=" not in comment


def test_writes_title_only_provenance_when_only_a_previous_title_exists() -> None:
    result = tag_song_bytes(
        id3_tag(title="Old Title Only"),
        title="New Title",
        artist="New Artist",
        mime_type="audio/mpeg",
    )
    assert read_mp3_comment(result) == "title=Old Title Only"


def test_writes_no_comment_frame_for_an_untagged_mp3() -> None:
    result = tag_song_bytes(
        frame_sync_mp3(), title="New Title", artist="New Artist", mime_type="audio/mpeg"
    )
    assert read_mp3_title(result) == "New Title"
    assert read_mp3_artist(result) == "New Artist"
    assert read_mp3_comment(result) is None


def test_clears_a_source_only_mp3_comment_when_provenance_is_empty() -> None:
    result = tag_song_bytes(
        id3_tag(comment=SOURCE_ONLY_COMMENT),
        title="New Title",
        artist="New Artist",
        mime_type="audio/mpeg",
    )
    assert read_mp3_comment(result) is None
    assert SOURCE_ONLY_COMMENT not in (read_mp3_comment(result) or "")


def test_replaces_a_source_mp3_comment_with_provenance_rather_than_appending() -> None:
    result = tag_song_bytes(
        id3_tag(title="Old Title", comment="personal note from entrant"),
        title="New Title",
        artist="New Artist",
        mime_type="audio/mpeg",
    )
    assert read_mp3_comment(result) == "title=Old Title"
    assert "personal note" not in (read_mp3_comment(result) or "")


def test_writes_year_when_new_year_is_provided_and_omits_it_otherwise() -> None:
    data = minimal_mp3()
    with_year = tag_song_bytes(
        data,
        title="New Title",
        artist="New Artist",
        year="2026",
        mime_type="audio/mpeg",
    )
    assert read_mp3_year(with_year) == "2026"
    assert read_mp3_genre(with_year) == "_Routine_"

    without_year = tag_song_bytes(
        data, title="New Title", artist="New Artist", mime_type="audio/mpeg"
    )
    assert read_mp3_year(without_year) is None
    assert read_mp3_title(without_year) == "New Title"
    assert read_mp3_artist(without_year) == "New Artist"


def test_preserves_existing_tags_in_comment_field_for_mp3() -> None:
    result = tag_song_bytes(
        minimal_mp3(), title="New Title", artist="New Artist", mime_type="audio/mpeg"
    )
    assert "Original Title" in (read_mp3_comment(result) or "")


def test_returns_original_bytes_gracefully_when_tagging_fails() -> None:
    result = tag_song_bytes(
        bytes(10), title="Title", artist="Artist", mime_type="audio/mpeg"
    )
    assert isinstance(result, bytes)


# ------------------------------------------------------------------ WAV helpers

FMT_CHUNK = bytes(
    [
        *(0x66, 0x6D, 0x74, 0x20),
        *(0x10, 0x00, 0x00, 0x00),
        *(0x01, 0x00, 0x01, 0x00),
        *(0x40, 0x1F, 0x00, 0x00),
        *(0x40, 0x1F, 0x00, 0x00),
        *(0x01, 0x00, 0x08, 0x00),
    ]
)


def riff(body: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", len(body)) + body


def build_minimal_wav(extra_chunks: Sequence[tuple[bytes, bytes]] = ()) -> bytes:
    data = b"data" + struct.pack("<I", 4) + bytes([0x80] * 4)
    extras = b"".join(
        chunk_id + struct.pack("<I", len(payload)) + payload + bytes(len(payload) & 1)
        for chunk_id, payload in extra_chunks
    )
    return riff(b"WAVE" + FMT_CHUNK + data + extras)


def walk_chunks(buf: bytes) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    pos = 12
    while pos + 8 <= len(buf):
        chunk_id = buf[pos : pos + 4]
        (size,) = struct.unpack_from("<I", buf, pos + 4)
        if pos + 8 + size > len(buf):
            break
        out.append((chunk_id, buf[pos + 8 : pos + 8 + size]))
        pos += 8 + size + (size & 1)
    return out


def chunk(buf: bytes, chunk_id: bytes) -> bytes:
    return next(payload for cid, payload in walk_chunks(buf) if cid == chunk_id)


def tag_wav(data: bytes, year: str | None = None) -> bytes:
    return tag_song_bytes(
        data, title="New Title", artist="New Artist", year=year, mime_type="audio/wav"
    )


# ------------------------------------------------------------------ WAV tagging


def test_wav_preserves_the_riff_wave_container_on_a_fresh_wav() -> None:
    result = tag_wav(build_minimal_wav())
    assert result[:4] == b"RIFF"
    assert result[8:12] == b"WAVE"
    assert struct.unpack_from("<I", result, 4)[0] == len(result) - 8


def test_wav_writes_a_new_id3_chunk_containing_the_new_title_and_artist() -> None:
    result = tag_wav(build_minimal_wav())
    ids = [cid for cid, _ in walk_chunks(result)]
    assert b"fmt " in ids
    assert b"data" in ids
    id3 = chunk(result, b"id3 ")
    assert read_mp3_title(id3) == "New Title"
    assert read_mp3_artist(id3) == "New Artist"
    assert read_mp3_genre(id3) == "_Routine_"


def test_wav_sets_genre_to_routine_and_overwrites_an_existing_genre() -> None:
    old = id3_tag(title="Old Title", artist="Old Artist", genre="Jazz")
    result = tag_wav(build_minimal_wav([(b"id3 ", old)]))
    id3_chunks = [p for cid, p in walk_chunks(result) if cid == b"id3 "]
    assert len(id3_chunks) == 1
    assert read_mp3_title(id3_chunks[0]) == "New Title"
    assert read_mp3_artist(id3_chunks[0]) == "New Artist"
    assert read_mp3_genre(id3_chunks[0]) == "_Routine_"


def test_wav_writes_year_and_lean_provenance_comment() -> None:
    old = id3_tag(title="Old Title", artist="Old Artist")
    result = tag_wav(build_minimal_wav([(b"id3 ", old)]), year="2026")
    id3 = chunk(result, b"id3 ")
    assert read_mp3_year(id3) == "2026"
    assert read_mp3_comment(id3) == "title=Old Title,artist=Old Artist"


def test_wav_writes_no_comment_for_an_untagged_file() -> None:
    id3 = chunk(tag_wav(build_minimal_wav()), b"id3 ")
    assert read_mp3_comment(id3) is None
    assert read_mp3_genre(id3) == "_Routine_"


def test_wav_replaces_an_existing_id3_chunk_and_captures_prev_tags_in_comment() -> None:
    old = id3_tag(title="Old Title", artist="Old Artist")
    result = tag_wav(build_minimal_wav([(b"id3 ", old)]))
    id3_chunks = [p for cid, p in walk_chunks(result) if cid == b"id3 "]
    assert len(id3_chunks) == 1
    assert read_mp3_title(id3_chunks[0]) == "New Title"
    assert read_mp3_artist(id3_chunks[0]) == "New Artist"
    assert read_mp3_comment(id3_chunks[0]) == "title=Old Title,artist=Old Artist"


def test_wav_leaves_audio_data_bytes_intact() -> None:
    audio = bytes([0xDE, 0xAD, 0xBE, 0xEF])
    data_chunk = b"data" + struct.pack("<I", len(audio)) + audio
    result = tag_wav(riff(b"WAVE" + FMT_CHUNK + data_chunk))
    assert chunk(result, b"data") == audio


def test_wav_returns_original_bytes_unchanged_for_non_riff_input() -> None:
    data = bytes(100)
    assert tag_wav(data) is data


def wav_with_overrunning_data(
    declared_size: int, audio: bytes, before: Sequence[bytes] = ()
) -> bytes:
    fmt = build_minimal_wav()[12 : 12 + 24]
    data_header = b"data" + struct.pack("<I", declared_size)
    return riff(b"WAVE" + fmt + b"".join(before) + data_header + audio)


def test_wav_keeps_the_audio_when_the_data_chunk_declared_size_overruns() -> None:
    audio = bytes([0x7F]) * 100
    result = tag_wav(wav_with_overrunning_data(1000, audio))
    chunks = walk_chunks(result)
    assert [cid for cid, _ in chunks] == [b"fmt ", b"data", b"id3 "]
    assert chunk(result, b"data") == audio
    assert struct.unpack_from("<I", result, 4)[0] == len(result) - 8
    assert read_mp3_title(chunk(result, b"id3 ")) == "New Title"


def test_wav_keeps_the_audio_of_a_streamed_wav_with_data_size_0xffffffff() -> None:
    audio = bytes([0x40]) * 51  # odd length: the rewritten chunk needs a pad byte
    list_chunk = b"LIST" + bytes([4, 0, 0, 0]) + b"INFO"
    result = tag_wav(wav_with_overrunning_data(0xFFFFFFFF, audio, [list_chunk]))
    chunks = walk_chunks(result)
    assert [cid for cid, _ in chunks] == [b"fmt ", b"LIST", b"data", b"id3 "]
    assert chunk(result, b"data") == audio
    assert struct.unpack_from("<I", result, 4)[0] == len(result) - 8


def test_wav_handles_odd_length_id3_payload_with_correct_riff_padding() -> None:
    result = tag_wav(build_minimal_wav())
    pos = 12
    while pos + 8 <= len(result):
        (size,) = struct.unpack_from("<I", result, pos + 4)
        nxt = pos + 8 + size + (size & 1)
        assert nxt <= len(result)
        if nxt == pos:
            break
        pos = nxt


# ------------------------------------------------------------------ M4A helpers

M4A_CONTAINERS = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"stbl",
    b"udta",
    b"meta",
    b"ilst",
}
NAM, ART, GEN, DAY, CMT = (b"\xa9nam", b"\xa9ART", b"\xa9gen", b"\xa9day", b"\xa9cmt")


def m4a_header_len(name: bytes) -> int:
    return 12 if name == b"meta" else 8


def build_raw_atom(name: bytes, payload: bytes) -> bytes:
    total = m4a_header_len(name) + len(payload)
    meta_flags = bytes(4) if name == b"meta" else b""
    return struct.pack(">I", total) + name + meta_flags + payload


def build_64bit_atom(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4sQ", 1, name, 16 + len(payload)) + payload


class Bounds:
    def __init__(self, name: bytes, start: int, size: int, header_len: int) -> None:
        self.name = name
        self.start = start
        self.size = size
        self.header_len = header_len
        self.payload_start = start + header_len
        self.payload_end = start + size


def read_atom_bounds(buf: bytes, pos: int, container_end: int) -> Bounds | None:
    if pos + 8 > container_end:
        return None
    size32, name = struct.unpack_from(">I4s", buf, pos)
    if size32 == 1:
        if pos + 16 > container_end:
            return None
        (real_size,) = struct.unpack_from(">Q", buf, pos + 8)
        header_len = 16
    elif size32 < 8:
        return None
    else:
        real_size, header_len = size32, m4a_header_len(name)
    if pos + real_size > container_end:
        return None
    return Bounds(name, pos, real_size, header_len)


def build_ilst_data_atom(value: bytes, data_type: int) -> bytes:
    inner = struct.pack(">II", data_type, 0) + value
    return struct.pack(">I", 8 + len(inner)) + b"data" + inner


def build_ilst_text_entry(name: bytes, text: str) -> tuple[bytes, bytes]:
    return name, build_ilst_data_atom(text.encode("utf-8"), 1)


def _walk_for(
    buf: bytes, start: int, end: int, want: bytes, min_payload: int
) -> Bounds | None:
    pos = start
    while pos + 8 <= end:
        atom = read_atom_bounds(buf, pos, end)
        if atom is None:
            return None
        if atom.name == want and atom.payload_end - atom.payload_start >= min_payload:
            return atom
        if atom.name in M4A_CONTAINERS:
            found = _walk_for(buf, atom.payload_start, atom.payload_end, want, 0)
            if found is not None and (
                found.payload_end - found.payload_start >= min_payload
            ):
                return found
        pos += atom.size
    return None


def find_top_level_atom(buf: bytes, name: bytes) -> Bounds | None:
    pos = 0
    while pos + 8 <= len(buf):
        atom = read_atom_bounds(buf, pos, len(buf))
        if atom is None:
            break
        if atom.name == name:
            return atom
        pos += atom.size
    return None


def find_moov_range(buf: bytes) -> Bounds | None:
    return find_top_level_atom(buf, b"moov")


def patch_sample_offset_table(buf: bytearray, offset: int, table: bytes) -> bool:
    moov = find_moov_range(bytes(buf))
    if moov is None:
        return False
    atom = _walk_for(
        bytes(buf),
        moov.payload_start,
        moov.payload_end,
        table,
        12 if table == b"stco" else 16,
    )
    if atom is None:
        return False
    fmt = ">I" if table == b"stco" else ">Q"
    struct.pack_into(fmt, buf, atom.payload_start + 8, offset)
    return True


def find_ilst_entries(buf: bytes) -> list[tuple[bytes, bytes]]:
    moov = find_moov_range(buf)
    if moov is None:
        return []
    ilst = _walk_for(buf, moov.payload_start, moov.payload_end, b"ilst", 0)
    if ilst is None:
        return []
    entries: list[tuple[bytes, bytes]] = []
    pos = ilst.payload_start
    while pos + 8 <= ilst.payload_end:
        entry = read_atom_bounds(buf, pos, ilst.payload_end)
        if entry is None:
            break
        entries.append((entry.name, buf[entry.payload_start : entry.payload_end]))
        pos += entry.size
    return entries


def entry(buf: bytes, name: bytes) -> bytes:
    return next(payload for n, payload in find_ilst_entries(buf) if n == name)


def entry_names(buf: bytes) -> list[bytes]:
    return [n for n, _ in find_ilst_entries(buf)]


def read_ilst_entry_text(payload: bytes) -> str:
    if len(payload) < 16 or payload[4:8] != b"data":
        return ""
    if struct.unpack_from(">I", payload, 8)[0] != 1:
        return ""
    return payload[16:].decode("utf-8")


def read_stco_first_entry(buf: bytes) -> int | None:
    moov = find_moov_range(buf)
    atom = moov and _walk_for(buf, moov.payload_start, moov.payload_end, b"stco", 12)
    return struct.unpack_from(">I", buf, atom.payload_start + 8)[0] if atom else None


def read_co64_first_entry(buf: bytes) -> int | None:
    moov = find_moov_range(buf)
    atom = moov and _walk_for(buf, moov.payload_start, moov.payload_end, b"co64", 16)
    return struct.unpack_from(">Q", buf, atom.payload_start + 8)[0] if atom else None


def mdat_payload_offset(buf: bytes) -> int:
    mdat = find_top_level_atom(buf, b"mdat")
    assert mdat is not None
    return mdat.payload_start


MDAT_PAYLOAD_LENGTH = 64


def build_minimal_m4a(
    *,
    existing_ilst_entries: Sequence[tuple[bytes, bytes]] = (),
    moov_after_mdat: bool = False,
    use_co64: bool = False,
    mdat_64bit: bool = False,
    moov_64bit: bool = False,
) -> tuple[bytes, int]:
    """The TS ``buildMinimalM4a``: returns the file and its mdat payload offset."""
    mdat_payload = bytes([0xAB]) * MDAT_PAYLOAD_LENGTH
    ftyp = build_raw_atom(
        b"ftyp",
        b"M4A " + bytes(4) + b"isom" + bytes([0, 0, 2, 0]) + b"mp42" + bytes(4),
    )
    mvhd = build_raw_atom(b"mvhd", bytes(100))
    tkhd = build_raw_atom(b"tkhd", bytes(84))
    mdhd = build_raw_atom(b"mdhd", bytes(24))
    hdlr = build_raw_atom(b"hdlr", bytes(8) + b"soun" + bytes(13) + bytes(1))
    if use_co64:
        sample_table = build_raw_atom(b"co64", struct.pack(">IIQ", 0, 1, 0))
    else:
        sample_table = build_raw_atom(b"stco", struct.pack(">III", 0, 1, 0))
    stbl = build_raw_atom(b"stbl", sample_table)
    minf = build_raw_atom(b"minf", stbl)
    mdia = build_raw_atom(b"mdia", mdhd + hdlr + minf)
    trak = build_raw_atom(b"trak", tkhd + mdia)

    moov_body = mvhd + trak
    if existing_ilst_entries:
        ilst = build_raw_atom(
            b"ilst", b"".join(build_raw_atom(n, d) for n, d in existing_ilst_entries)
        )
        meta_hdlr = build_raw_atom(b"hdlr", bytes(8) + b"mdir" + bytes(12) + bytes(1))
        meta = build_raw_atom(b"meta", meta_hdlr + ilst)
        moov_body += build_raw_atom(b"udta", meta)
    moov = (build_64bit_atom if moov_64bit else build_raw_atom)(b"moov", moov_body)
    mdat = (build_64bit_atom if mdat_64bit else build_raw_atom)(b"mdat", mdat_payload)

    buf = bytearray(ftyp + mdat + moov if moov_after_mdat else ftyp + moov + mdat)
    offset = mdat_payload_offset(bytes(buf))
    table = b"co64" if use_co64 else b"stco"
    if not patch_sample_offset_table(buf, offset, table):
        raise AssertionError(f"fixture missing {table!r}")
    return bytes(buf), offset


def tag_m4a(
    data: bytes,
    title: str = "New Title",
    artist: str = "New Artist",
    year: str | None = None,
) -> bytes:
    return tag_song_bytes(
        data, title=title, artist=artist, year=year, mime_type="audio/mp4"
    )


def assert_mdat_intact(result: bytes, source: bytes, source_offset: int) -> None:
    offset = mdat_payload_offset(result)
    assert read_stco_first_entry(result) == offset
    assert (
        result[offset : offset + MDAT_PAYLOAD_LENGTH]
        == source[source_offset : source_offset + MDAT_PAYLOAD_LENGTH]
    )


# ------------------------------------------------------------------ M4A tagging


def test_m4a_writes_nam_and_art_into_a_file_with_no_pre_existing_ilst() -> None:
    buf, _ = build_minimal_m4a()
    result = tag_m4a(buf)
    names = entry_names(result)
    assert NAM in names
    assert ART in names
    assert GEN in names
    assert CMT not in names
    assert read_ilst_entry_text(entry(result, NAM)) == "New Title"
    assert read_ilst_entry_text(entry(result, ART)) == "New Artist"
    assert read_ilst_entry_text(entry(result, GEN)) == "_Routine_"
    # An independent reader agrees, and finds the created udta/meta/hdlr(mdir).
    stream = io.BytesIO(result)
    tags = MP4Tags(Atoms(stream), stream)
    assert tags["\xa9nam"] == ["New Title"]
    assert tags["\xa9ART"] == ["New Artist"]
    assert tags["\xa9gen"] == ["_Routine_"]


def test_m4a_preserves_existing_non_target_ilst_entries() -> None:
    existing = [
        (b"tmpo", build_ilst_data_atom(bytes([0x00, 0x80]), 21)),
        (b"----", build_ilst_data_atom(bytes([0x42]) * 50, 0)),
        (DAY, build_ilst_data_atom(b"2024", 1)),
    ]
    buf, _ = build_minimal_m4a(existing_ilst_entries=existing)
    result = tag_m4a(buf)
    for name in (b"tmpo", b"----", DAY):
        assert entry(result, name) == entry(buf, name)
    assert read_ilst_entry_text(entry(result, NAM)) == "New Title"
    assert read_ilst_entry_text(entry(result, ART)) == "New Artist"
    assert read_ilst_entry_text(entry(result, GEN)) == "_Routine_"
    assert CMT not in entry_names(result)


def test_m4a_captures_prev_title_and_artist_in_the_new_comment() -> None:
    buf, _ = build_minimal_m4a(
        existing_ilst_entries=[
            build_ilst_text_entry(NAM, "Old Song"),
            build_ilst_text_entry(ART, "Old Band"),
        ]
    )
    result = tag_m4a(buf, title="Fresh", artist="Fresh Band")
    assert read_ilst_entry_text(entry(result, CMT)) == "title=Old Song,artist=Old Band"


def test_m4a_removes_a_source_only_comment_when_provenance_is_empty() -> None:
    buf, _ = build_minimal_m4a(
        existing_ilst_entries=[build_ilst_text_entry(CMT, SOURCE_ONLY_COMMENT)]
    )
    assert CMT not in entry_names(tag_m4a(buf))


def test_m4a_replaces_a_source_comment_with_provenance_rather_than_appending() -> None:
    buf, _ = build_minimal_m4a(
        existing_ilst_entries=[
            build_ilst_text_entry(NAM, "Old Song"),
            build_ilst_text_entry(CMT, "personal note from entrant"),
        ]
    )
    result = tag_m4a(buf, title="Fresh", artist="Fresh Band")
    comment = read_ilst_entry_text(entry(result, CMT))
    assert comment == "title=Old Song"
    assert "personal note" not in comment


def test_m4a_adjusts_stco_when_moov_precedes_mdat_after_removing_cmt() -> None:
    buf, offset = build_minimal_m4a(
        existing_ilst_entries=[build_ilst_text_entry(CMT, SOURCE_ONLY_COMMENT)]
    )
    result = tag_m4a(buf)
    assert CMT not in entry_names(result)
    assert_mdat_intact(result, buf, offset)


def test_m4a_adjusts_stco_when_moov_precedes_mdat_with_minimal_ilst() -> None:
    buf, offset = build_minimal_m4a()
    assert read_stco_first_entry(buf) == offset
    result = tag_m4a(buf)
    assert sorted(entry_names(result)) == sorted([ART, GEN, NAM])
    assert read_ilst_entry_text(entry(result, GEN)) == "_Routine_"
    assert_mdat_intact(result, buf, offset)


def test_m4a_adjusts_stco_when_moov_precedes_mdat_with_year_and_comment() -> None:
    buf, offset = build_minimal_m4a(
        existing_ilst_entries=[
            build_ilst_text_entry(NAM, "Old Song"),
            build_ilst_text_entry(ART, "Old Band"),
        ]
    )
    result = tag_m4a(buf, year="2026")
    assert sorted(entry_names(result)) == sorted([ART, CMT, DAY, GEN, NAM])
    assert read_ilst_entry_text(entry(result, DAY)) == "2026"
    assert read_ilst_entry_text(entry(result, CMT)) == "title=Old Song,artist=Old Band"
    assert_mdat_intact(result, buf, offset)


def test_m4a_overwrites_an_existing_genre_with_routine() -> None:
    buf, _ = build_minimal_m4a(
        existing_ilst_entries=[
            build_ilst_text_entry(NAM, "Old Song"),
            build_ilst_text_entry(ART, "Old Band"),
            build_ilst_text_entry(GEN, "Pop"),
        ]
    )
    result = tag_m4a(buf, title="Fresh", artist="Fresh Band")
    assert entry_names(result).count(GEN) == 1
    assert read_ilst_entry_text(entry(result, GEN)) == "_Routine_"
    assert read_ilst_entry_text(entry(result, NAM)) == "Fresh"
    assert read_ilst_entry_text(entry(result, ART)) == "Fresh Band"


def test_m4a_does_not_adjust_stco_when_moov_follows_mdat() -> None:
    buf, _ = build_minimal_m4a(moov_after_mdat=True)
    input_stco = read_stco_first_entry(buf)
    result = tag_m4a(buf)
    assert read_stco_first_entry(result) == input_stco
    before = find_top_level_atom(buf, b"mdat")
    after = find_top_level_atom(result, b"mdat")
    assert before is not None and after is not None
    assert (
        result[after.payload_start : after.payload_end]
        == buf[before.payload_start : before.payload_end]
    )


def test_m4a_returns_input_unchanged_when_atoms_cannot_be_parsed() -> None:
    data = b"not a real m4a"
    assert tag_m4a(data) is data


def test_m4a_handles_co64_by_adjusting_64bit_offsets() -> None:
    buf, offset = build_minimal_m4a(use_co64=True)
    assert read_co64_first_entry(buf) == offset
    result = tag_m4a(buf)
    assert read_co64_first_entry(result) == mdat_payload_offset(result)


def _top_level(buf: bytes) -> list[tuple[bytes, int, int]]:
    """(name, length, header length) of each top-level atom, as mutagen parses them."""
    return [
        (a.name, a.length, a._dataoffset - a.offset)
        for a in Atoms(io.BytesIO(buf)).atoms
    ]


def test_m4a_parses_and_re_serializes_an_m4a_with_a_64bit_mdat_header() -> None:
    buf, offset = build_minimal_m4a(mdat_64bit=True)
    # TS round-trips its own parseAtoms/serializeAtoms; here the parser is
    # mutagen's, so assert it reads the 64-bit header and covers the file.
    parsed = _top_level(buf)
    assert [(n, h) for n, _, h in parsed] == [(b"ftyp", 8), (b"moov", 8), (b"mdat", 16)]
    assert sum(length for _, length, _ in parsed) == len(buf)

    result = tag_m4a(buf)
    assert read_ilst_entry_text(entry(result, NAM)) == "New Title"
    assert read_ilst_entry_text(entry(result, ART)) == "New Artist"
    result_offset = mdat_payload_offset(result)
    assert read_stco_first_entry(result) == result_offset
    assert result[result_offset:] == buf[offset : offset + MDAT_PAYLOAD_LENGTH]
    mdat = find_top_level_atom(result, b"mdat")
    assert mdat is not None and mdat.header_len == 16  # written back 64-bit


def test_m4a_parses_and_re_serializes_an_m4a_with_a_64bit_moov_header() -> None:
    buf, offset = build_minimal_m4a(moov_64bit=True)
    parsed = _top_level(buf)
    assert [(n, h) for n, _, h in parsed] == [(b"ftyp", 8), (b"moov", 16), (b"mdat", 8)]
    assert sum(length for _, length, _ in parsed) == len(buf)

    result = tag_m4a(buf)
    assert read_stco_first_entry(result) == mdat_payload_offset(result)
    assert read_stco_first_entry(buf) == offset
    moov = find_moov_range(result)
    assert moov is not None and moov.header_len == 16  # written back 64-bit


def test_m4a_throws_diagnostic_error_for_size_zero_atoms(warnings: list[str]) -> None:
    base, _ = build_minimal_m4a()
    ftyp = find_top_level_atom(base, b"ftyp")
    moov = find_moov_range(base)
    assert ftyp is not None and moov is not None
    mdat_zero = struct.pack(">I", 0) + b"mdat"
    data = base[: ftyp.size] + base[moov.start : moov.payload_end] + mdat_zero
    assert (
        tag_song_bytes(data, title="Title", artist="Artist", mime_type="audio/mp4")
        is data
    )
    failures = [w for w in warnings if "tagger_m4a_parse_failed" in w]
    assert failures and "size_extends_to_end" in failures[0]


def test_m4a_throws_diagnostic_error_for_truncated_atoms(warnings: list[str]) -> None:
    oversize_skip = struct.pack(">I", 1000) + b"skip"
    base, _ = build_minimal_m4a()
    moov = find_moov_range(base)
    ftyp = find_top_level_atom(base, b"ftyp")
    mdat = find_top_level_atom(base, b"mdat")
    assert moov is not None and ftyp is not None and mdat is not None
    patched = build_raw_atom(
        b"moov", base[moov.payload_start : moov.payload_end] + oversize_skip + bytes(12)
    )
    data = base[: ftyp.start + ftyp.size] + patched + base[mdat.start :]
    assert (
        tag_song_bytes(data, title="Title", artist="Artist", mime_type="audio/mp4")
        is data
    )
    failures = [w for w in warnings if "tagger_m4a_parse_failed" in w]
    assert failures and "size_exceeds_buffer" in failures[0]


# ------------------------------------------------------------------ FLAC helpers


def _flac_block(block_type: int, payload: bytes, *, last: bool) -> bytes:
    return (
        bytes([block_type | (0x80 if last else 0)])
        + len(payload).to_bytes(3, "big")
        + payload
    )


def build_minimal_flac(
    comments: Sequence[str | bytes] = (
        "TITLE=Original Title",
        "ARTIST=Original Artist",
        "GENRE=Rock",
    ),
) -> bytes:
    """STREAMINFO + VORBIS_COMMENT + one frame header, as the TS fixture builds it."""
    stream_info = bytearray(34)
    struct.pack_into(">HH", stream_info, 0, 4096, 4096)
    vendor = b"flac-tagger"
    encoded = [c.encode("utf-8") if isinstance(c, str) else c for c in comments]
    vorbis = (
        struct.pack("<I", len(vendor))
        + vendor
        + struct.pack("<I", len(encoded))
        + b"".join(struct.pack("<I", len(c)) + c for c in encoded)
    )
    return (
        b"fLaC"
        + _flac_block(0, bytes(stream_info), last=False)
        + _flac_block(4, vorbis, last=True)
        + bytes([0xFF, 0xF8, 0x82, 0x88, 0x00, 0x00, 0x00, 0x02])
    )


def flac_blocks(buf: bytes) -> list[tuple[int, bytes]]:
    """(type, payload) of each metadata block (mutagen's FLAC rejects the
    fixture's sample rate of 0, so the blocks are walked here)."""
    assert buf[:4] == b"fLaC"
    blocks, pos, last = [], 4, False
    while not last:
        last = bool(buf[pos] & 0x80)
        length = int.from_bytes(buf[pos + 1 : pos + 4], "big")
        blocks.append((buf[pos] & 0x7F, buf[pos + 4 : pos + 4 + length]))
        pos += 4 + length
    return blocks


def flac_vorbis(buf: bytes) -> VCFLACDict:
    """The Vorbis comment block, parsed by mutagen."""
    (payload,) = [p for t, p in flac_blocks(buf) if t == 4]
    return VCFLACDict(payload)


def flac_comments(buf: bytes) -> list[tuple[str, str]]:
    return [(k, v) for k, v in flac_vorbis(buf)]


def flac_tag_map(buf: bytes) -> dict[str, str | list[str]]:
    """flac-tagger's ``tagMap``: upper-cased keys, repeated keys become lists."""
    grouped: dict[str, list[str]] = {}
    for key, value in flac_comments(buf):
        grouped.setdefault(key.upper(), []).append(value)
    return {k: v[0] if len(v) == 1 else v for k, v in grouped.items()}


def tag_flac(data: bytes, *, title: str, artist: str, year: str | None = None) -> bytes:
    return tag_song_bytes(
        data, title=title, artist=artist, year=year, mime_type="audio/flac"
    )


# ------------------------------------------------------------------ FLAC tagging


def test_flac_sets_genre_to_routine_and_preserves_title_and_artist() -> None:
    result = tag_flac(
        build_minimal_flac(), title="FLAC Title", artist="FLAC Artist", year="2026"
    )
    tags = flac_tag_map(result)
    assert tags["TITLE"] == "FLAC Title"
    assert tags["ARTIST"] == "FLAC Artist"
    assert tags["GENRE"] == "_Routine_"
    assert tags["DATE"] == "2026"
    # The TS test also reads back through music-metadata (case-insensitive common.*).
    vorbis = flac_vorbis(result)
    assert vorbis["title"] == ["FLAC Title"]
    assert vorbis["artist"] == ["FLAC Artist"]
    assert vorbis["genre"] == ["_Routine_"]
    assert int(vorbis["date"][0]) == 2026


def test_flac_writes_lean_provenance_and_omits_year_when_absent() -> None:
    result = tag_flac(
        build_minimal_flac(["TITLE=Old Title", "ARTIST=Old Artist"]),
        title="New Title",
        artist="New Artist",
    )
    tags = flac_tag_map(result)
    assert tags["COMMENT"] == "title=Old Title,artist=Old Artist"
    assert "DATE" not in tags


def test_flac_removes_stale_comment_when_source_had_no_title_or_artist() -> None:
    result = tag_flac(
        build_minimal_flac(["COMMENT=legacy noise", "GENRE=Rock"]),
        title="New Title",
        artist="New Artist",
    )
    tags = flac_tag_map(result)
    assert "COMMENT" not in tags
    assert tags["GENRE"] == "_Routine_"


def test_flac_overwrites_an_existing_genre_with_routine() -> None:
    data = build_minimal_flac(
        ["TITLE=Old Title", "ARTIST=Old Artist", "GENRE=Electronic"]
    )
    assert flac_tag_map(data)["GENRE"] == "Electronic"
    tags = flac_tag_map(tag_flac(data, title="New Title", artist="New Artist"))
    assert tags["TITLE"] == "New Title"
    assert tags["ARTIST"] == "New Artist"
    assert tags["GENRE"] == "_Routine_"
    assert not [
        v
        for v in tags.values()
        if (v == "Electronic" if isinstance(v, str) else "Electronic" in v)
    ]


def test_flac_returns_bytes_for_audio_flac_mime_type_without_throwing() -> None:
    result = tag_flac(bytes(200), title="FLAC Title", artist="FLAC Artist")
    assert isinstance(result, bytes)


def test_flac_handles_audio_x_flac_mime_variant() -> None:
    result = tag_song_bytes(
        bytes(200), title="Title", artist="Artist", mime_type="audio/x-flac"
    )
    assert isinstance(result, bytes)


def test_flac_falls_back_to_original_bytes_when_flac_data_is_invalid() -> None:
    data = b"not a real flac file"
    result = tag_flac(data, title="Title", artist="Artist")
    assert isinstance(result, bytes)
    assert result is data


# ------------------------------------------------------------------ cross-format


def test_clears_a_source_only_comment_consistently_across_formats() -> None:
    fields = {"title": "New Title", "artist": "New Artist"}
    mp3 = tag_song_bytes(
        id3_tag(comment=SOURCE_ONLY_COMMENT), mime_type="audio/mpeg", **fields
    )
    assert read_mp3_comment(mp3) is None

    wav = tag_song_bytes(
        build_minimal_wav([(b"id3 ", id3_tag(comment=SOURCE_ONLY_COMMENT))]),
        mime_type="audio/wav",
        **fields,
    )
    assert read_mp3_comment(chunk(wav, b"id3 ")) is None

    flac = tag_song_bytes(
        build_minimal_flac([f"COMMENT={SOURCE_ONLY_COMMENT}"]),
        mime_type="audio/flac",
        **fields,
    )
    assert "COMMENT" not in flac_tag_map(flac)

    m4a_bytes, _ = build_minimal_m4a(
        existing_ilst_entries=[build_ilst_text_entry(CMT, SOURCE_ONLY_COMMENT)]
    )
    m4a = tag_song_bytes(m4a_bytes, mime_type="audio/mp4", **fields)
    assert CMT not in entry_names(m4a)


# ------------------------------------------------------------------ routing


def test_routes_a_riff_wave_file_through_the_wav_tagger_even_when_mime_is_mpeg() -> (
    None
):
    result = tag_song_bytes(
        build_minimal_wav(),
        title="Tagged Title",
        artist="Tagged Artist",
        mime_type="audio/mpeg",
    )
    assert result[:4] == b"RIFF"
    assert result[8:12] == b"WAVE"


def test_routes_an_id3_prefixed_mp3_through_the_mp3_tagger_even_when_mime_is_wave() -> (
    None
):
    result = tag_song_bytes(
        id3_tag(title="Original", artist="Original"),
        title="Tagged Title",
        artist="Tagged Artist",
        mime_type="audio/wave",
    )
    assert result[:3] == b"ID3"
    assert read_mp3_title(result) == "Tagged Title"


def test_routes_a_frame_sync_mp3_without_id3_header_through_the_mp3_tagger() -> None:
    result = tag_song_bytes(
        frame_sync_mp3(),
        title="Tagged Title",
        artist="Tagged Artist",
        mime_type="audio/mpeg",
    )
    assert isinstance(result, bytes)


def test_routes_ftyp_headed_bytes_through_the_m4a_tagger_even_when_mime_is_flac() -> (
    None
):
    buf, _ = build_minimal_m4a()
    result = tag_song_bytes(
        buf, title="Tagged Title", artist="Tagged Artist", mime_type="audio/flac"
    )
    assert result[4:8] == b"ftyp"


def test_returns_input_unchanged_when_no_signature_and_mime_is_unsupported() -> None:
    data = b"totally random bytes here, not any known format header"
    result = tag_song_bytes(data, title="Title", artist="Artist", mime_type="audio/ogg")
    assert result is data


def test_returns_input_unchanged_when_no_signature_and_mime_is_missing() -> None:
    data = b"totally random bytes here, not any known format header"
    assert tag_song_bytes(data, title="Title", artist="Artist") is data


def test_uses_declared_mime_when_sniff_is_unsupported_but_mime_is_known() -> None:
    result = tag_song_bytes(
        bytes([0xAA]) * 20, title="Title", artist="Artist", mime_type="audio/mpeg"
    )
    assert isinstance(result, bytes)


# ------------------------------------------------------------------ beyond the TS cases


def test_mp3_year_replaces_a_v24_tdrc_rather_than_sitting_beside_it() -> None:
    """Spec fix: node-id3 kept a v2.4 file's TDRC next to the new TYER."""
    data = id3_tag(title="Old", date="2019-05-04T10:30", version=4)
    assert read_id3(data).version[1] == 4
    result = tag_song_bytes(
        data, title="New", artist="Artist", year="2027", mime_type="audio/mpeg"
    )
    raw = read_id3(result)
    assert raw.version[:2] == (2, 3)
    assert read_mp3_year(result) == "2027"
    assert "TDRC" not in raw
    assert "TDAT" not in raw  # the old day/month must not attach to the new year
    assert "TIME" not in raw
    # A v2.4-preferring reader (mutagen upgrades on load) sees only the new year.
    assert str(ID3(io.BytesIO(result))["TDRC"].text[0]) == "2027"


def test_mp3_without_year_keeps_the_existing_date() -> None:
    data = id3_tag(title="Old", date="2019", version=4)
    result = tag_song_bytes(data, title="New", artist="Artist", mime_type="audio/mpeg")
    assert read_mp3_year(result) == "2019"


def test_mp3_audio_after_the_tag_and_id3v1_are_left_untouched() -> None:
    audio = frame_sync_mp3()
    id3v1 = (
        b"TAG"
        + b"Old v1 Title".ljust(30, b"\x00")
        + b"V1 Artist".ljust(30, b"\x00")
        + bytes(65)
    )
    data = id3_tag(title="Old", album="Hits") + audio + id3v1
    result = tag_song_bytes(data, title="New", artist="A", mime_type="audio/mpeg")
    assert result.endswith(audio + id3v1)
    assert read_mp3_comment(result) == "title=Old,album=Hits"


def test_flac_spec_example_encoder_tagged_file() -> None:
    """AUDIO-TAGGING.md's FLAC example: keys upper-cased, positions kept."""
    data = build_minimal_flac(
        [
            "title=Old T",
            "artist=Old A",
            "album=Old Alb",
            "DESCRIPTION=entrant",
            "date=2019",
            "encoder=Lavf60",
        ]
    )
    result = tag_flac(data, title="A & B", artist="Classic | R", year="2027")
    assert flac_comments(result) == [
        ("TITLE", "A & B"),
        ("ARTIST", "Classic | R"),
        ("ALBUM", "Old Alb"),
        ("DESCRIPTION", "entrant"),
        ("DATE", "2027"),
        ("ENCODER", "Lavf60"),
        ("GENRE", "_Routine_"),
        ("COMMENT", "title=Old T,artist=Old A"),
    ]
    assert flac_vorbis(result).vendor == "flac-tagger"


def test_flac_drops_comments_without_an_equals_sign() -> None:
    data = build_minimal_flac(["TITLE=Old", b"NOEQUALS", "unknown1=kept"])
    result = tag_flac(data, title="New", artist="A")
    keys = [k for k, _ in flac_comments(result)]
    assert keys == ["TITLE", "UNKNOWN1", "ARTIST", "GENRE", "COMMENT"]


def test_flac_removes_padding_and_keeps_other_blocks_and_audio() -> None:
    plain = build_minimal_flac()
    stream_info = flac_blocks(plain)[0]
    audio = plain[-8:]
    picture = (6, b"picture-bytes")
    blocks = [
        stream_info,
        (1, bytes(64)),
        flac_blocks(plain)[1],
        picture,
        (1, bytes(8)),
    ]
    data = (
        b"fLaC"
        + b"".join(
            _flac_block(t, p, last=i == len(blocks) - 1)
            for i, (t, p) in enumerate(blocks)
        )
        + audio
    )
    result = tag_flac(data, title="New", artist="A")
    assert [t for t, _ in flac_blocks(result)] == [0, 4, 6]
    assert flac_blocks(result)[0] == stream_info
    assert flac_blocks(result)[2] == picture
    assert result.endswith(audio)


def test_flac_without_a_comment_block_gets_one_appended() -> None:
    plain = build_minimal_flac()
    data = b"fLaC" + _flac_block(0, flac_blocks(plain)[0][1], last=True) + plain[-8:]
    result = tag_flac(data, title="New", artist="A", year="2027")
    assert [t for t, _ in flac_blocks(result)] == [0, 4]
    assert flac_comments(result) == [
        ("TITLE", "New"),
        ("ARTIST", "A"),
        ("GENRE", "_Routine_"),
        ("DATE", "2027"),
    ]


def test_mime_sniff_mismatch_is_logged_and_bytes_win(warnings: list[str]) -> None:
    result = tag_song_bytes(
        build_minimal_wav(), title="T", artist="A", mime_type="audio/flac"
    )
    assert any("tagger_mime_sniff_mismatch" in w for w in warnings)
    assert read_mp3_title(chunk(result, b"id3 ")) == "T"


def test_wav_failure_logs_a_warning(warnings: list[str]) -> None:
    data = bytes(100)
    assert tag_song_bytes(data, title="T", artist="A", mime_type="audio/wav") is data
    assert any("tagger_wav_failed" in w for w in warnings)
