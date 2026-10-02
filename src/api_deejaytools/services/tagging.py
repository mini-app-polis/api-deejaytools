"""Tags written into every uploaded song file (deejaytools-api AUDIO-TAGGING.md).

DJ software reads these tags straight from the event folders, so the fields
are part of the contract: title, artist, the constant genre ``_Routine_``, the
season year, and a provenance comment recording the tags the file arrived
with.

mutagen (ADR-009) reads and writes the tags themselves: ID3v2.3 for MP3 and
for WAV's ``id3 `` chunk, and the Vorbis comment block for FLAC. The
containers around them are hand-written where the spec needs behaviour
mutagen does not give: the RIFF chunk list (overrun handling, ``id3 `` only),
the FLAC metadata-block list, and the M4A atom tree (byte-for-byte
preservation of other ``ilst`` entries, the spec's passthrough cases, and
``stco``/``co64`` fixes).

Tagging never fails an upload: any problem logs a warning and the original
bytes are returned unchanged.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Mapping
from typing import Literal

from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix
from mutagen.flac import VCFLACDict
from mutagen.id3 import COMM, ID3, TCON, TIT2, TPE1, TYER, ID3v1SaveOptions

from ..zod_coerce import js_trim

logger = get_logger()

_AudioFormat = Literal["mp3", "wav", "flac", "m4a"]

#: Every file this service tags is a competition routine; DJ software selects
#: an event's tracks as one set on this value. Deliberately not a parameter.
SONG_GENRE = "_Routine_"

_MIME_FORMATS: Mapping[str, _AudioFormat] = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/x-mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "video/mp4": "m4a",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}

_UTF16 = 1  # ID3 text encoding: UTF-16 with BOM, as ID3v2.3 requires for non-Latin-1.


def _warn(event: str, detail: str) -> None:
    logger.warning(with_log_prefix(LOG_WARNING, f"{event}: {detail}"))


def _sniff(data: bytes) -> _AudioFormat | None:
    """Format from the leading bytes, in the spec's order (WAV, FLAC, M4A, MP3)."""
    if len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:3] == b"ID3" or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


def _provenance(**fields: str) -> str:
    """``key=value`` pairs, in argument order, of the fields that are non-blank."""
    return ",".join(
        f"{key}={js_trim(value)}" for key, value in fields.items() if js_trim(value)
    )


# ---------------------------------------------------------------- ID3 (MP3, WAV)


def _id3_text(tags: ID3, frame_id: str) -> str:
    frame = tags.get(frame_id)
    return str(frame.text[0]) if frame is not None and frame.text else ""


def _id3_provenance(tags: ID3) -> str:
    return _provenance(
        title=_id3_text(tags, "TIT2"),
        artist=_id3_text(tags, "TPE1"),
        album=_id3_text(tags, "TALB"),
    )


def _apply_id3_fields(
    tags: ID3, title: str, artist: str, year: str | None, comment: str
) -> None:
    """Set the contract frames on ``tags`` and convert it to ID3v2.3.

    Every ``COMM`` frame is replaced by the provenance (or removed when it is
    empty). When a year is given, ``TYER`` is replaced and the rest of the old
    date (v2.4 ``TDRC``, v2.3 ``TDAT``/``TIME``) is dropped, so no reader can
    show the entrant's year next to the season year.
    """
    tags.setall("TIT2", [TIT2(encoding=_UTF16, text=[title])])
    tags.setall("TPE1", [TPE1(encoding=_UTF16, text=[artist])])
    tags.setall("TCON", [TCON(encoding=_UTF16, text=[SONG_GENRE])])
    tags.delall("COMM")
    if comment:
        tags.add(COMM(encoding=_UTF16, lang="eng", desc="", text=[comment]))
    if year:
        for frame_id in ("TDRC", "TYER", "TDAT", "TIME"):
            tags.delall(frame_id)
    tags.update_to_v23()
    if year:
        tags.add(TYER(encoding=_UTF16, text=[year]))


def _render_id3(tags: ID3) -> bytes:
    """The tag alone, as ID3v2.3 bytes with no padding."""
    out = io.BytesIO()
    tags.save(out, v1=ID3v1SaveOptions.REMOVE, v2_version=3, padding=lambda _: 0)
    return out.getvalue()


def _id3v2_length(data: bytes) -> int:
    """Length of the ID3v2 tag at the start of ``data`` (header and footer included)."""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size + (10 if data[5] & 0x10 else 0)


def _tag_mp3(data: bytes, title: str, artist: str, year: str | None) -> bytes:
    """Merge into the existing ID3v2 tag and rewrite it as v2.3 at byte 0.

    Frames other than the contract ones are kept (mutagen converts v2.4-only
    frames to their v2.3 forms or drops them). An ID3v1 tag at the end of
    the file is neither read nor touched.
    """
    old_length = _id3v2_length(data)
    tags = ID3(io.BytesIO(data), load_v1=False) if old_length else ID3()
    _apply_id3_fields(tags, title, artist, year, _id3_provenance(tags))
    return _render_id3(tags) + data[old_length:]


def _read_riff_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """``(id, payload)`` for each chunk after the ``WAVE`` form type.

    A chunk whose declared size runs past the end of the file keeps the bytes
    actually present (it is rewritten with its real length) and ends the walk.
    Fewer than 8 bytes left after the last whole chunk are dropped.
    """
    chunks: list[tuple[bytes, bytes]] = []
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        if pos + 8 + size > len(data):
            chunks.append((chunk_id, data[pos + 8 :]))
            break
        chunks.append((chunk_id, data[pos + 8 : pos + 8 + size]))
        pos += 8 + size + (size & 1)
    return chunks


def _tag_wav(data: bytes, title: str, artist: str, year: str | None) -> bytes:
    """Write a fresh ID3v2.3 tag as the RIFF ``id3 `` chunk.

    Every existing ``id3 `` chunk is replaced in place, or the new one is
    appended after the last chunk. Provenance comes from the last ``id3 ``
    chunk; an unreadable one contributes nothing.
    """
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    chunks = _read_riff_chunks(data)
    comment = ""
    for chunk_id, payload in chunks:
        if chunk_id == b"id3 ":
            try:
                comment = _id3_provenance(ID3(io.BytesIO(payload), load_v1=False))
            except Exception:
                comment = ""

    tags = ID3()
    _apply_id3_fields(tags, title, artist, year, comment)
    new_tag = _render_id3(tags)

    payloads = [
        (chunk_id, new_tag if chunk_id == b"id3 " else payload)
        for chunk_id, payload in chunks
    ]
    if not any(chunk_id == b"id3 " for chunk_id, _ in chunks):
        payloads.append((b"id3 ", new_tag))

    body = bytearray(b"WAVE")
    for chunk_id, payload in payloads:
        body += chunk_id + struct.pack("<I", len(payload)) + payload
        if len(payload) & 1:
            body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + bytes(body)


# ---------------------------------------------------------------- FLAC

_FLAC_VORBIS_COMMENT = 4
_FLAC_PADDING = 1


def _tag_flac(data: bytes, title: str, artist: str, year: str | None) -> bytes:
    """Rewrite the Vorbis comments, keeping every comment the spec does not set.

    Keys are upper-cased; ``TITLE``, ``ARTIST``, ``GENRE``, ``DATE`` and
    ``COMMENT`` each end up with one value, at their first existing position
    or appended. Provenance reads title and artist only (not album). Every
    ``PADDING`` block is removed; other blocks and the audio are copied as-is.

    The metadata-block walk is hand-written because mutagen's ``FLAC`` refuses
    a STREAMINFO it considers invalid (sample rate 0, as in the golden
    fixture), which the spec keeps untouched. The comment block itself is
    mutagen's ``VCFLACDict``.
    """
    if data[:4] != b"fLaC":
        raise ValueError("no fLaC signature")
    blocks: list[tuple[int, bytes]] = []
    pos, last = 4, False
    while not last:
        if pos + 4 > len(data):
            raise ValueError("truncated metadata block header")
        last = bool(data[pos] & 0x80)
        block_type = data[pos] & 0x7F
        length = int.from_bytes(data[pos + 1 : pos + 4], "big")
        if pos + 4 + length > len(data):
            raise ValueError("truncated metadata block")
        blocks.append((block_type, data[pos + 4 : pos + 4 + length]))
        pos += 4 + length
    audio = data[pos:]

    existing = [p for t, p in blocks if t == _FLAC_VORBIS_COMMENT]
    # errors="ignore" drops comments with no "=", as the spec does.
    vorbis = VCFLACDict(existing[0], errors="ignore") if existing else VCFLACDict()

    def first(key: str) -> str:
        values = vorbis.get(key) or [""]
        return str(values[0])

    comment = _provenance(title=first("TITLE"), artist=first("ARTIST"))
    wanted = {"TITLE": title, "ARTIST": artist, "GENRE": SONG_GENRE}
    if year:
        wanted["DATE"] = year
    if comment:
        wanted["COMMENT"] = comment

    rewritten: list[tuple[str, str]] = []
    for key, value in list(vorbis):
        upper = key.upper()
        if upper in wanted:
            if all(done != upper for done, _ in rewritten):
                rewritten.append((upper, wanted[upper]))
        elif upper != "COMMENT":
            rewritten.append((upper, value))
    rewritten += [
        (k, v) for k, v in wanted.items() if all(d != k for d, _ in rewritten)
    ]
    del vorbis[:]
    vorbis.extend(rewritten)
    new_comment = vorbis.write()

    kept = [
        (t, new_comment if t == _FLAC_VORBIS_COMMENT else p)
        for t, p in blocks
        if t != _FLAC_PADDING
    ]
    if not existing:
        kept.append((_FLAC_VORBIS_COMMENT, new_comment))
    out = bytearray(b"fLaC")
    for index, (block_type, payload) in enumerate(kept):
        flag = 0x80 if index == len(kept) - 1 else 0
        out += bytes([block_type | flag]) + len(payload).to_bytes(3, "big") + payload
    return bytes(out) + audio


# ---------------------------------------------------------------- M4A / MP4

_M4A_CONTAINERS = frozenset(
    {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta", b"ilst"}
)


class _Atom:
    """One MP4 atom: a leaf keeps its payload, a container its children."""

    __slots__ = ("name", "header_len", "payload", "children")

    def __init__(
        self,
        name: bytes,
        header_len: int,
        payload: bytes = b"",
        children: list[_Atom] | None = None,
    ) -> None:
        self.name = name
        self.header_len = header_len  # 8, 16 (64-bit size), or 12 for meta
        self.payload = payload
        self.children = children

    def render(self) -> bytes:
        """The atom as bytes: header, then its children or its payload."""
        body = (
            b"".join(c.render() for c in self.children)
            if self.children is not None
            else self.payload
        )
        size = self.header_len + len(body)
        if self.header_len == 16:
            return struct.pack(">I4sQ", 1, self.name, size) + body
        flags = bytes(4) if self.name == b"meta" else b""
        return struct.pack(">I4s", size, self.name) + flags + body

    def child(self, name: bytes) -> _Atom | None:
        """The first direct child named ``name``, or None."""
        return next((c for c in self.children or [] if c.name == name), None)


def _parse_atoms(data: bytes, start: int, end: int) -> list[_Atom]:
    """Parse ``data[start:end]``; fewer than 8 trailing bytes are dropped.

    Raises :class:`ValueError` for the trees the spec passes through untagged:
    a size-0 atom anywhere, a size below the header, a size past the end of
    the parent, or a 64-bit ``meta``.
    """
    atoms: list[_Atom] = []
    pos = start
    while pos + 8 <= end:
        size, name = struct.unpack_from(">I4s", data, pos)
        label = f"name={name.decode('latin-1')} offset={pos}"
        if size == 0:
            raise ValueError(f"size_extends_to_end ({label})")
        if size == 1:
            if name == b"meta":
                raise ValueError(f"64-bit-with-meta unsupported ({label})")
            if pos + 16 > end:
                raise ValueError(f"size_exceeds_buffer ({label} end={end})")
            (size,) = struct.unpack_from(">Q", data, pos + 8)
            header = 16
        else:
            header = 12 if name == b"meta" else 8
        if size < header:
            raise ValueError(f"size_below_header ({label} size={size})")
        if pos + size > end:
            raise ValueError(f"size_exceeds_buffer ({label} size={size} end={end})")
        if name in _M4A_CONTAINERS:
            children = _parse_atoms(data, pos + header, pos + size)
            atoms.append(_Atom(name, header, children=children))
        else:
            atoms.append(_Atom(name, header, data[pos + header : pos + size]))
        pos += size
    return atoms


def _ilst_text(entry: _Atom | None) -> str:
    """The first ``data`` child's text, if it is type 1 (UTF-8)."""
    p = entry.payload if entry is not None else b""
    if len(p) < 16 or p[4:8] != b"data" or struct.unpack_from(">I", p, 8)[0] != 1:
        return ""
    return p[16 : struct.unpack_from(">I", p)[0]].decode("utf-8", "replace")


def _ilst_entry(name: bytes, text: str) -> _Atom:
    value = text.encode("utf-8")
    return _Atom(name, 8, struct.pack(">I4sII", 16 + len(value), b"data", 1, 0) + value)


def _shift_chunk_offsets(atom: _Atom, delta: int) -> None:
    """Add ``delta`` to every ``stco``/``co64`` entry under ``atom``."""
    for child in atom.children or []:
        width = {b"stco": 4, b"co64": 8}.get(child.name)
        if width is None:
            _shift_chunk_offsets(child, delta)
            continue
        p = child.payload
        if len(p) < 8:
            continue
        count = struct.unpack_from(">I", p, 4)[0]
        if len(p) < 8 + count * width:
            continue  # shorter than its count: left unadjusted, as the spec says
        fmt = f">{count}{'I' if width == 4 else 'Q'}"
        shifted = [o + delta for o in struct.unpack_from(fmt, p, 8)]
        child.payload = p[:8] + struct.pack(fmt, *shifted)


def _tag_m4a(data: bytes, title: str, artist: str, year: str | None) -> bytes:
    """Set the iTunes text atoms in ``moov/udta/meta/ilst``.

    Hand-written rather than mutagen's ``MP4Tags``: mutagen re-renders every
    ``ilst`` entry and cannot load one it does not understand (the golden
    fixture's bare ``----`` atom), where the spec keeps other entries byte for
    byte. Missing containers are created; existing entries are replaced in
    place, new ones appended. When ``moov`` precedes ``mdat`` its size change
    is added to every ``stco``/``co64`` entry. 64-bit atoms stay 64-bit.
    """
    atoms = _parse_atoms(data, 0, len(data))
    names = [a.name for a in atoms]
    if b"moov" not in names or b"mdat" not in names:
        raise ValueError("no top-level moov or mdat")
    moov = atoms[names.index(b"moov")]
    old_size = len(moov.render())

    def container(parent: _Atom, name: bytes, *initial: _Atom) -> _Atom:
        found = parent.child(name)
        if found is None:
            found = _Atom(name, 12 if name == b"meta" else 8, children=list(initial))
            assert parent.children is not None
            parent.children.append(found)
        return found

    hdlr = _Atom(b"hdlr", 8, bytes(8) + b"mdir" + bytes(13))
    ilst = container(container(container(moov, b"udta"), b"meta", hdlr), b"ilst")
    entries = ilst.children
    assert entries is not None

    comment = _provenance(
        title=_ilst_text(ilst.child(b"\xa9nam")),
        artist=_ilst_text(ilst.child(b"\xa9ART")),
        album=_ilst_text(ilst.child(b"\xa9alb")),
    )
    values = {b"\xa9nam": title, b"\xa9ART": artist, b"\xa9gen": SONG_GENRE}
    if year:
        values[b"\xa9day"] = year
    if comment:
        values[b"\xa9cmt"] = comment
    else:
        entries[:] = [e for e in entries if e.name != b"\xa9cmt"]
    for name, text in values.items():
        index = next((i for i, e in enumerate(entries) if e.name == name), None)
        if index is None:
            entries.append(_ilst_entry(name, text))
        else:
            entries[index] = _ilst_entry(name, text)

    delta = len(moov.render()) - old_size
    if delta and names.index(b"moov") < names.index(b"mdat"):
        _shift_chunk_offsets(moov, delta)
    return b"".join(a.render() for a in atoms)


# ---------------------------------------------------------------- entry point

_TAGGERS = {"mp3": _tag_mp3, "wav": _tag_wav, "flac": _tag_flac, "m4a": _tag_m4a}
_FAILURE_EVENTS = {
    "mp3": "tagger_id3_failed",
    "wav": "tagger_wav_failed",
    "flac": "tagger_flac_failed",
    "m4a": "tagger_m4a_parse_failed",
}


def tag_song_bytes(
    data: bytes,
    *,
    title: str,
    artist: str,
    year: str | None = None,
    mime_type: str | None = None,
) -> bytes:
    """Return ``data`` with the song's tags written in, per AUDIO-TAGGING.md.

    The format comes from the leading bytes; ``mime_type`` is only a fallback
    when they are not recognised (a disagreement is logged and the bytes win).
    Writes title, artist, genre ``_Routine_``, ``year`` when given, and the
    provenance comment (``title=…,artist=…,album=…`` from the file's existing
    tags; the comment field is removed when there is nothing to record).

    Never raises: an unsupported format or any error while reading or writing
    tags logs a warning and returns ``data`` itself, unchanged. Synchronous and
    pure (bytes in, bytes out); run it off the event loop.
    """
    sniffed = _sniff(data)
    declared = _MIME_FORMATS.get(mime_type or "")
    if sniffed and declared and sniffed != declared:
        _warn(
            "tagger_mime_sniff_mismatch",
            f"declared={declared} sniffed={sniffed} bytes={len(data)}",
        )
    audio_format = sniffed or declared
    if audio_format is None:
        _warn("tagger_unsupported_format", f"mime={mime_type} bytes={len(data)}")
        return data
    try:
        return _TAGGERS[audio_format](data, title, artist, year)
    except Exception as exc:
        _warn(
            _FAILURE_EVENTS[audio_format],
            f"bytes={len(data)} error={type(exc).__name__}: {exc}",
        )
        return data
