#!/usr/bin/env python3
"""Minimal, dependency-free, READ-ONLY reader for Minecraft Bedrock world databases.

A Bedrock world's ``db/`` folder is a LevelDB database written by Mojang's fork of
LevelDB.  This module reads it with the standard library only (``zlib``/``struct``),
because ``amulet-leveldb`` did not build from source in the Linux container we tried
(untested on macOS, where it may install fine from a wheel).

What it can do
    * read ``*.ldb`` tables (blocks are raw-deflate or zlib compressed; Snappy is NOT
      supported and raises) and the write-ahead ``*.log`` files
    * merge them (highest sequence number wins, deletions honoured)
    * classify keys (chunk tags, sub-chunks, ``digp``, other dimensions, global keys)
    * decode version 1/8/9 sub-chunks (first block layer) into block *names*
    * summarise a world: key kinds, platform-only sub-chunks, the *real* cell bounding box

What it cannot do: write anything.  It never modifies the database.

Usage (never run it from inside an untrusted checkout; use -I)::

    python3 -I tools/ldb_reader.py path/to/world/db

The sample worlds in ``Worlds/Bedrock_Education/*.zip`` are LevelDB databases:
  * ``breast_*_bedrock.zip``: only ``MANIFEST-000002`` + a ~80-100 MB ``000003.log``
    (everything is still in the write-ahead log, so the log MUST be read);
  * the others contain ``*.ldb`` tables plus a ``*.log``.

Key layout (little-endian), overworld::

    chunk-level   x:int32 z:int32 tag:u8                      (9 bytes)
    sub-chunk     x:int32 z:int32 0x2f y:int8                 (10 bytes)
    entity digest b"digp" x:int32 z:int32                     (12 bytes)
    other dims    x:int32 z:int32 dim:int32 tag:u8 [y:int8]   (13 / 14 bytes)

Block index inside a decoded sub-chunk is ``x*256 + z*16 + y``.
"""
import collections
import glob
import os
import struct
import sys
import zlib

SUBCHUNK_TAG = 0x2F
# Chunk-level tags seen in the CraftCells worlds (0x40 only appears in the breast worlds).
CHUNK_TAGS = {0x2B, 0x2C, 0x2D, 0x2E, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
              0x38, 0x39, 0x3A, 0x3B, 0x3D, 0x40, 0x76}
AIR = "minecraft:air"
# In the shipped *void* worlds the only 'minecraft:grass' blocks are the 9-block spawn
# platform near y=-63; no real cell block uses this name.
PLATFORM_BLOCK = "minecraft:grass"


# --------------------------------------------------------------------------- low level
def _varint(buf, pos):
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, pos


def _decompress(raw, ctype):
    if ctype == 0:
        return raw
    if ctype == 1:
        raise NotImplementedError("Snappy-compressed LevelDB block (not supported)")
    for wbits in (-15, 15):  # raw deflate (Mojang) or zlib
        try:
            return zlib.decompress(raw, wbits)
        except zlib.error:
            pass
    raise ValueError("cannot decompress block (compression type %d)" % ctype)


def _block_entries(block):
    """Yield (key, value) from one LevelDB block (prefix-compressed entries)."""
    n_restarts = struct.unpack("<I", block[-4:])[0]
    end = len(block) - 4 - 4 * n_restarts
    pos, key = 0, b""
    while pos < end:
        shared, pos = _varint(block, pos)
        unshared, pos = _varint(block, pos)
        vlen, pos = _varint(block, pos)
        key = key[:shared] + block[pos:pos + unshared]
        pos += unshared
        yield key, block[pos:pos + vlen]
        pos += vlen


def _read_block(fh, offset, size):
    fh.seek(offset)
    data = fh.read(size + 5)  # 1 byte compression type + 4 byte crc follow the block
    return _decompress(data[:size], data[size])


def read_table(path):
    """Yield (user_key, seq, type, value) for every entry of one ``.ldb`` table."""
    with open(path, "rb") as fh:
        fh.seek(-48, 2)
        footer = fh.read(48)
        _, p = _varint(footer, 0)            # metaindex handle (offset, size): unused
        _, p = _varint(footer, p)
        index_off, p = _varint(footer, p)
        index_size, p = _varint(footer, p)
        for _, handle in _block_entries(_read_block(fh, index_off, index_size)):
            off, q = _varint(handle, 0)
            size, q = _varint(handle, q)
            for ikey, value in _block_entries(_read_block(fh, off, size)):
                trailer = int.from_bytes(ikey[-8:], "little")
                yield ikey[:-8], trailer >> 8, trailer & 0xFF, value


def read_log(path):
    """Yield (user_key, seq, type, value) from a write-ahead ``.log`` file.

    type 1 = put, 0 = delete.  Standard LevelDB log framing: 32 KiB blocks, 7-byte
    record header (crc32c:4, length:2, type:1), FULL/FIRST/MIDDLE/LAST fragments.
    """
    data = open(path, "rb").read()
    pos, fragment, records = 0, b"", []
    while pos + 7 <= len(data):
        if 32768 - (pos % 32768) < 7:          # block trailer padding
            pos += 32768 - (pos % 32768)
            continue
        length = struct.unpack_from("<H", data, pos + 4)[0]
        rtype = data[pos + 6]
        chunk = data[pos + 7:pos + 7 + length]
        pos += 7 + length
        if rtype == 0 and length == 0:
            continue
        if rtype == 1:
            records.append(chunk)
        elif rtype == 2:
            fragment = chunk
        elif rtype == 3:
            fragment += chunk
        elif rtype == 4:
            records.append(fragment + chunk)
            fragment = b""
    for rec in records:                         # each record is one WriteBatch
        seq = struct.unpack_from("<Q", rec, 0)[0]
        count = struct.unpack_from("<I", rec, 8)[0]
        pos = 12
        for i in range(count):
            if pos >= len(rec):
                break
            etype = rec[pos]
            pos += 1
            klen, pos = _varint(rec, pos)
            key = rec[pos:pos + klen]
            pos += klen
            value = b""
            if etype == 1:
                vlen, pos = _varint(rec, pos)
                value = rec[pos:pos + vlen]
                pos += vlen
            yield key, seq + i, etype, value


def load_db(db_dir):
    """Return {key: value} for the whole database (tables + logs, newest wins)."""
    best = {}
    sources = sorted(glob.glob(os.path.join(db_dir, "*.ldb"))) + \
        sorted(glob.glob(os.path.join(db_dir, "*.log")))
    for path in sources:
        reader = read_table if path.endswith(".ldb") else read_log
        for key, seq, etype, value in reader(path):
            if key not in best or seq >= best[key][0]:
                best[key] = (seq, etype, value)
    return {k: v for k, (_, etype, v) in best.items() if etype == 1}


# --------------------------------------------------------------------------- keys
def classify(key):
    """Name the kind of a database key (see the module docstring for the layouts)."""
    if len(key) == 10 and key[8] == SUBCHUNK_TAG:
        return "subchunk"
    if len(key) == 9 and key[8] in CHUNK_TAGS:
        return "chunk:%02x" % key[8]
    if len(key) == 12 and key[:4] == b"digp":
        return "digp"
    if len(key) in (13, 14):
        return "other-dimension"
    return "global"


def subchunk_key(cx, cz, sy):
    return struct.pack("<ii", cx, cz) + bytes([SUBCHUNK_TAG]) + struct.pack("<b", sy)


def parse_subchunk_key(key):
    cx, cz = struct.unpack("<ii", key[:8])
    return cx, cz, struct.unpack("<b", key[9:10])[0]


# --------------------------------------------------------------------------- NBT (little endian)
def _nbt_payload(buf, pos, tag):
    if tag == 1:
        return buf[pos], pos + 1
    if tag == 2:
        return struct.unpack_from("<h", buf, pos)[0], pos + 2
    if tag == 3:
        return struct.unpack_from("<i", buf, pos)[0], pos + 4
    if tag == 4:
        return struct.unpack_from("<q", buf, pos)[0], pos + 8
    if tag == 5:
        return struct.unpack_from("<f", buf, pos)[0], pos + 4
    if tag == 6:
        return struct.unpack_from("<d", buf, pos)[0], pos + 8
    if tag == 7:
        n = struct.unpack_from("<i", buf, pos)[0]
        return buf[pos + 4:pos + 4 + n], pos + 4 + n
    if tag == 8:
        n = struct.unpack_from("<H", buf, pos)[0]
        return buf[pos + 2:pos + 2 + n].decode("utf-8", "replace"), pos + 2 + n
    if tag == 9:
        etype = buf[pos]
        n = struct.unpack_from("<i", buf, pos + 1)[0]
        pos += 5
        out = []
        for _ in range(n):
            val, pos = _nbt_payload(buf, pos, etype)
            out.append(val)
        return out, pos
    if tag == 10:
        out = {}
        while True:
            ttype = buf[pos]
            pos += 1
            if ttype == 0:
                return out, pos
            nlen = struct.unpack_from("<H", buf, pos)[0]
            name = buf[pos + 2:pos + 2 + nlen].decode("utf-8", "replace")
            pos += 2 + nlen
            out[name], pos = _nbt_payload(buf, pos, ttype)
    if tag == 11:
        n = struct.unpack_from("<i", buf, pos)[0]
        return struct.unpack_from("<%di" % n, buf, pos + 4), pos + 4 + 4 * n
    if tag == 12:
        n = struct.unpack_from("<i", buf, pos)[0]
        return struct.unpack_from("<%dq" % n, buf, pos + 4), pos + 4 + 8 * n
    raise ValueError("unknown NBT tag %d" % tag)


def read_nbt(buf, pos=0):
    """Read one named root tag (little-endian, as stored on disk). -> (name, value, pos)."""
    tag = buf[pos]
    nlen = struct.unpack_from("<H", buf, pos + 1)[0]
    name = buf[pos + 3:pos + 3 + nlen].decode("utf-8", "replace")
    value, pos = _nbt_payload(buf, pos + 3 + nlen, tag)
    return name, value, pos


# --------------------------------------------------------------------------- sub-chunks
def decode_subchunk(value):
    """Decode the first block layer of a sub-chunk value -> list of 4096 block names.

    Supports storage versions 1, 8 and 9 (everything in the CraftCells worlds is 9).
    Index = x*256 + z*16 + y.  Layout: version, [layer count], [y index (v9)], then per
    layer: header byte (bits-per-block << 1 | runtime flag), packed 32-bit words,
    int32 palette size, and that many NBT compounds ({name, states, version}).
    Raises NotImplementedError for legacy (0, 2..7) sub-chunks.
    """
    version = value[0]
    if version not in (1, 8, 9):
        raise NotImplementedError("legacy sub-chunk version %d" % version)
    pos = {1: 1, 8: 2, 9: 3}[version]
    header = value[pos]
    pos += 1
    if header & 1:
        raise NotImplementedError("runtime (network) palette in on-disk data?")
    bits = header >> 1
    if bits == 0:
        indices = [0] * 4096
    else:
        per_word = 32 // bits
        n_words = -(-4096 // per_word)
        words = struct.unpack_from("<%dI" % n_words, value, pos)
        pos += n_words * 4
        mask = (1 << bits) - 1
        indices = [(words[i // per_word] >> ((i % per_word) * bits)) & mask for i in range(4096)]
    count = struct.unpack_from("<i", value, pos)[0]
    pos += 4
    palette = []
    for _ in range(count):
        _, entry, pos = read_nbt(value, pos)
        palette.append(entry.get("name", "?"))
    return [palette[i] if i < len(palette) else "?" for i in indices]


# --------------------------------------------------------------------------- analysis
def analyze(db_dir):
    """Summarise a world: key kinds, sub-chunk versions, platform-only sub-chunks and the
    real cell bounding box (air and the spawn-platform grass ignored)."""
    items = load_db(db_dir)
    kinds = collections.Counter(classify(k) for k in items)
    versions = collections.Counter()
    platform, unsupported = [], 0
    bbox = [10**9, 10**9, 10**9, -10**9, -10**9, -10**9]  # x0 y0 z0 x1 y1 z1
    nonempty = 0
    for key, value in items.items():
        if classify(key) != "subchunk":
            continue
        versions[value[0]] += 1
        cx, cz, sy = parse_subchunk_key(key)
        try:
            names = decode_subchunk(value)
        except NotImplementedError:
            unsupported += 1
            continue
        present = set(names) - {AIR}
        if not present:
            continue
        if present == {PLATFORM_BLOCK}:
            platform.append((cx, cz, sy))
            continue
        nonempty += 1
        for i, name in enumerate(names):
            if name == AIR or name == PLATFORM_BLOCK:
                continue
            x, z, y = cx * 16 + (i >> 8), cz * 16 + ((i >> 4) & 15), sy * 16 + (i & 15)
            bbox[0], bbox[1], bbox[2] = min(bbox[0], x), min(bbox[1], y), min(bbox[2], z)
            bbox[3], bbox[4], bbox[5] = max(bbox[3], x), max(bbox[4], y), max(bbox[5], z)
    return {"kinds": dict(kinds), "subchunk_versions": dict(versions),
            "platform_only_subchunks": sorted(platform), "nonempty_subchunks": nonempty,
            "unsupported_subchunks": unsupported,
            "bbox": tuple(bbox) if nonempty else None}


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    info = analyze(argv[1])
    print("key kinds:", info["kinds"])
    print("sub-chunk versions:", info["subchunk_versions"])
    print("platform-only sub-chunks (cx, cz, sy):", info["platform_only_subchunks"])
    if info["bbox"]:
        x0, y0, z0, x1, y1, z1 = info["bbox"]
        print("real cell bbox: x %d..%d  y %d..%d  z %d..%d  (%d non-empty sub-chunks)"
              % (x0, x1, y0, y1, z0, z1, info["nonempty_subchunks"]))
    else:
        print("no non-empty sub-chunks found")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
