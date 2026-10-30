# Audio tagging

What the API writes into every uploaded song file before it goes to Drive.
DJ software reads these tags directly (event folders are played from as-is), so
the output is part of the contract: a reimplementation must produce the same
fields with the same values. It does **not** need to be byte-identical. Frame
order, padding and text encoding are free as long as the fields read back the same.

Source: [`src/api_deejaytools/services/tagging.py`](../src/api_deejaytools/services/tagging.py) (`tag_song_bytes`), called once
per build from [`services/song_builds.py`](../src/api_deejaytools/services/song_builds.py).
Tests: [`tests/unit/test_tagging.py`](../tests/unit/test_tagging.py).
Where the tagged file goes: [DRIVE.md](./DRIVE.md#background-build).

---

## Inputs

| Field | Value | Example |
|-------|-------|---------|
| **Title** | Entity names, first `&` second; second omitted for solo, team and other entries. Same order as the filename, including the ProAm FollowerAm swap ([DRIVE.md](./DRIVE.md#background-build) step 4) | `Ada Lovelace & Bob Jones` |
| **Artist** | Division and routine name joined with ` \| ` (space, pipe, space), blanks dropped. `\|` rather than `-` because division names contain hyphens | `Classic \| Blue Monday`, or just `Classic` |
| **Genre** | Always the constant `_Routine_`, so DJ software can select an event's tracks as one set | `_Routine_` |
| **Year** | Upload season year (October 1 rollover). Always present on uploads; the tagger omits the field if it is ever empty | `2027` |
| **Comment** | Provenance: the tags the file arrived with (below) | `title=Old Song,artist=DJ X` |

The season year is deliberately **not** in the artist string.

### Provenance comment

Records what the entrant's file was tagged with before it was overwritten, so a
mis-attributed upload can be traced back.

- Read from the file's existing tags: title, artist and album (**FLAC: title and
  artist only**).
- Each value is trimmed; empty values are dropped.
- Joined as `key=value` pairs with `,`, in the order title, artist, album. No
  escaping: a comma or `=` inside a value is written as-is.
- If nothing remains, the comment is **removed** from the output, including
  any comment the entrant's file had **in the field listed for its format below**
  (`COMM`, `COMMENT`, `©cmt`). Comments stored elsewhere survive, e.g. ffmpeg
  writes MP3 comments as `TXXX:comment` and FLAC comments as `DESCRIPTION`.

| Original tags | Comment written |
|---------------|-----------------|
| title `Old Song`, artist `DJ X`, album `Hits` | `title=Old Song,artist=DJ X,album=Hits` |
| title `  Old Song `, artist empty | `title=Old Song` |
| none | *(no comment field)* |

---

## Format selection

The format is taken from the file's leading bytes; the MIME type is only a
fallback when the bytes are not recognised. In the upload path the MIME type
always comes from the same byte check (`detectAudioFormat`), so the two agree.

| Signature (checked in this order) | Format |
|-----------------------------------|--------|
| `RIFF` at 0 and `WAVE` at 8 | WAV |
| `fLaC` at 0 | FLAC |
| `ftyp` at 4 | M4A / MP4 |
| `ID3` at 0, or `0xFF` followed by a byte with the top 3 bits set | MP3 |

Shorter than 12 bytes or no match → MIME fallback (`audio/mpeg`, `audio/mp3`,
`audio/x-mp3` → MP3; `audio/wav`, `audio/x-wav`, `audio/wave` → WAV;
`audio/mp4`, `audio/x-m4a`, `video/mp4` → M4A; `audio/flac`, `audio/x-flac` →
FLAC) → otherwise unsupported. A disagreement between bytes and MIME type is
logged (`tagger_mime_sniff_mismatch`) and the bytes win.

**Tagging never fails an upload.** An unsupported format, or any error while
reading or writing tags, returns the original bytes untouched and the file is
uploaded untagged. MP3, FLAC, M4A and unsupported formats log a warning when this
happens; WAV logs nothing.

---

## Per-format output

### MP3 — ID3v2.3 (`node-id3` `update`)

The existing ID3v2 tag is read, merged and rewritten as **ID3v2.3** (text frames
UTF-16 with BOM) at the start of the file. node-id3 removes the first valid `ID3`
header it finds anywhere in the buffer, not only at byte 0.

Frames not listed in the table are kept **only if node-id3 0.2.x recognises them**
(album, composer, cover art and the like), and only the first instance of
`COMM`, `APIC`, `USLT` and `POPM` survives. Frames it doesn't know (`RVA2`,
`PCNT`, `MCDI`, …) and encrypted frames are dropped. ID3v2.4-only frames from a
v2.4 input are copied across unchanged, so a v2.4 file keeps its old `TDRC`
date next to the new `TYER` (readers that prefer `TDRC` show the old year). A
reimplementation should replace or drop `TDRC` as well.

| Field | Frame | Notes |
|-------|-------|-------|
| Title | `TIT2` | replaced |
| Artist | `TPE1` | replaced |
| Genre | `TCON` | replaced |
| Year | `TYER` | replaced; left alone if year is empty |
| Comment | `COMM`, language `eng`, empty description | replaced; **removed** when provenance is empty |

Provenance reads `TIT2`, `TPE1`, `TALB` from the existing ID3v2 tag. ID3v1 is
neither read nor touched.

### WAV — ID3v2.3 in a RIFF `id3 ` chunk

The tag is stored as a RIFF sub-chunk with the id `id3 ` (lowercase, trailing
space), not prepended to the file.

- A **fresh** ID3v2.3 tag is built containing only: `TIT2` title, `TPE1` artist,
  `TCON` genre, `TYER` year (if set), and `COMM` (`eng`, empty description) only
  when provenance is non-empty. Nothing from the old `id3 ` tag survives except
  as provenance.
- If the file has an `id3 ` chunk, it is replaced in place (every one, if
  there are several); otherwise the new chunk is appended after the last chunk.
- All other chunks (`fmt `, `data`, `LIST`/`INFO`, `bext`, …) are copied
  through unchanged, padded to even length. The RIFF size header is rewritten.
- Provenance reads `TIT2`, `TPE1`, `TALB` from the existing `id3 ` chunk (the
  last one, if several). `LIST/INFO` tags are not read. A chunk named `ID3 `
  (uppercase) is not recognised: it is kept, and a second tag is added as `id3 `.
- **Oversized last chunk:** a chunk whose declared size runs past the end of the
  file (a truncated file, or a streaming writer that left the `data` size at
  `0xFFFFFFFF`) is kept with the bytes actually present and re-written with its
  real length, so the audio survives. The new `id3 ` chunk goes after it.
- Fewer than 8 bytes left over after the last whole chunk are dropped.

### FLAC — Vorbis comments

- Existing comments are read into a map with **upper-cased keys**; repeated keys
  become multi-valued. Every comment not listed below is kept (including
  `DESCRIPTION`, which some encoders use for comments), re-emitted with its key
  upper-cased.
- Set, each to a single value: `TITLE`, `ARTIST`, `GENRE`, and `DATE` (if year is
  set). Existing keys keep their position; new keys are appended at the end.
- `COMMENT` is set to the provenance, or removed when provenance is empty.
- The vendor string is kept. If the file had no `VORBIS_COMMENT` block, one is
  appended after the existing metadata blocks with a default vendor string.
- Comments without an `=` are dropped. Quirk of the reader: if a repeated key's
  first value is empty, the next value replaces it instead of being added.
- **All `PADDING` blocks are removed.** Other blocks (`STREAMINFO`, `SEEKTABLE`,
  `PICTURE`, …) are kept.
- Provenance reads title and artist from the Vorbis comments (album is not read).

Example — an encoder-tagged file in, tagged out:

```
before: title=Old T  artist=Old A  album=Old Alb  DESCRIPTION=entrant  date=2019  encoder=Lavf…
after:  TITLE=A & B  ARTIST=Classic | R  ALBUM=Old Alb  DESCRIPTION=entrant  DATE=2027
        ENCODER=Lavf…  GENRE=_Routine_  COMMENT=title=Old T,artist=Old A
```

### M4A / MP4 — iTunes `ilst` atoms (hand-written parser)

Tags live in `moov/udta/meta/ilst`. Missing containers are created and appended:
`udta` to `moov`, `meta` (a full box: 4 zero bytes of version/flags) to `udta`
with an `hdlr` child of handler type `mdir`, and `ilst` to `meta`.

| Field | Atom | Notes |
|-------|------|-------|
| Title | `©nam` | |
| Artist | `©ART` | |
| Genre | `©gen` | free-text genre, not the numeric `gnre` |
| Year | `©day` | only if year is set |
| Comment | `©cmt` | **removed** when provenance is empty |

(`©` is byte `0xA9`.) Each value is a `data` child: type `1` (UTF-8), locale `0`,
then the UTF-8 bytes. An existing entry is replaced in place; a new one is
appended to the end of `ilst`. Other `ilst` entries (album, cover art, …) are kept.

- **Chunk offsets:** when `moov` comes before `mdat` (fast-start files), the
  size change of `moov` is added to every entry of every `stco` and `co64` table
  inside it, so the audio still plays. When `mdat` comes first, offsets are
  unchanged.
- **Provenance** reads `©nam`, `©ART`, `©alb`, only from type-1 (UTF-8) `data`
  atoms.
- **Untagged passthrough** (original bytes returned) when there is no top-level
  `moov` or `mdat`, or the atom tree cannot be parsed: an atom with size `0`
  ("extends to end of file") anywhere in the walk, a size below its header
  length, a size past the end of its parent, or a 64-bit-sized `meta`. 64-bit
  (`size = 1`) atoms are otherwise supported and written back 64-bit.
- **Silently lost or ignored:** fewer than 8 trailing bytes inside any container
  or at the end of the file are dropped (e.g. the 4-byte QuickTime `udta`
  terminator); `stco`/`co64` tables shorter than their declared count are left
  unadjusted; tags under `moov/meta/ilst` (rather than `moov/udta/meta/ilst`) are
  not read, and a second `udta/meta/ilst` is created alongside them.

---

## Checking a reimplementation

Read the output back with an independent reader (`ffprobe -show_format`, `mutagen`,
`music-metadata`) and compare fields, not bytes. `tests/unit/test_tagging.py`
has fixtures for each format, including provenance, missing containers,
fast-start offsets and 64-bit atoms.
