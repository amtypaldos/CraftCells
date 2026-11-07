#!/usr/bin/env python3
"""Inspect and patch a Bedrock ``level.dat`` IN PLACE (standard library only).

Why this exists.  The Bedrock worlds shipped in ``Worlds/Bedrock_Education`` were saved
from Minecraft Education Edition with single-player settings.  A regular Bedrock
dedicated server (BDS) loads them, but:

* ``educationFeaturesEnabled`` / ``eduOffer`` = 1  -> a normal Bedrock client lists the
  world as "incompatible version".  Set both to 0.
* ``LANBroadcast`` = 0  -> the server never answers LAN discovery.  Set it to 1.
* ``XBLBroadcastIntent`` = 3 (we set it to 0 to match a known-good server world).
* ``playerPermissionsLevel`` = 0 (visitor) in the epithelial world -> players cannot
  build or break blocks.  Set it to 2 (operator).  BDS in offline mode cannot persist
  ``/op`` (log: "Missing id, won't persist permissions on disk"), so this world setting
  is the reliable fix.

ALWAYS stop the server first: BDS rewrites ``level.dat`` on shutdown and would undo the
patch.  The tool also patches ``level.dat_old`` (BDS can fall back to it) and keeps a
``level.dat.nbtpatch.bak`` copy of the original.

File layout: 4-byte storage version + 4-byte payload length (both little-endian), then
a little-endian NBT root compound.  Numeric edits keep the size.  Editing a string
(``LevelName``) changes the length, so the header length is rewritten.

Examples::

    python3 -I tools/nbtpatch.py WORLD_DIR --dump
    python3 -I tools/nbtpatch.py WORLD_DIR --lan --no-edu --permission-level 2 \\
        --name "Human Epithelial Cell"
"""
import argparse
import os
import shutil
import struct
import sys

SHOW_KEYS = ("LevelName", "GameType", "Generator", "LANBroadcast", "XBLBroadcastIntent",
             "MultiplayerGame", "eduOffer", "educationFeaturesEnabled",
             "playerPermissionsLevel", "commandsEnabled", "cheatsEnabled",
             "SpawnX", "SpawnY", "SpawnZ", "MinimumCompatibleClientVersion",
             "lastOpenedWithVersion", "NetworkVersion", "InventoryVersion")


# --------------------------------------------------------------------------- reading
def _payload(buf, pos, tag):
    if tag == 1:
        return buf[pos], pos + 1
    if tag == 2:
        return struct.unpack_from("<h", buf, pos)[0], pos + 2
    if tag == 3:
        return struct.unpack_from("<i", buf, pos)[0], pos + 4
    if tag == 4:
        return struct.unpack_from("<q", buf, pos)[0], pos + 8
    if tag == 5:
        return round(struct.unpack_from("<f", buf, pos)[0], 4), pos + 4
    if tag == 6:
        return struct.unpack_from("<d", buf, pos)[0], pos + 8
    if tag == 7:
        n = struct.unpack_from("<i", buf, pos)[0]
        return "<%d bytes>" % n, pos + 4 + n
    if tag == 8:
        n = struct.unpack_from("<H", buf, pos)[0]
        return buf[pos + 2:pos + 2 + n].decode("utf-8", "replace"), pos + 2 + n
    if tag == 9:
        etype, n = buf[pos], struct.unpack_from("<i", buf, pos + 1)[0]
        pos += 5
        out = []
        for _ in range(n):
            val, pos = _payload(buf, pos, etype)
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
            out[name], pos = _payload(buf, pos, ttype)
    if tag == 11:
        n = struct.unpack_from("<i", buf, pos)[0]
        return "<%d ints>" % n, pos + 4 + 4 * n
    if tag == 12:
        n = struct.unpack_from("<i", buf, pos)[0]
        return "<%d longs>" % n, pos + 4 + 8 * n
    raise ValueError("unknown NBT tag %d" % tag)


def load(path):
    """Parse ``level.dat`` into a dict (read-only; byte arrays/int arrays are summarised)."""
    buf = open(path, "rb").read()[8:]
    assert buf[0] == 10, "root tag is not a compound"
    nlen = struct.unpack_from("<H", buf, 1)[0]
    return _payload(buf, 3 + nlen, 10)[0]


# --------------------------------------------------------------------------- patching
def _find(buf, tag_type, name):
    key = bytes([tag_type]) + struct.pack("<H", len(name)) + name.encode()
    n = buf.count(key)
    return buf.index(key) + len(key) if n == 1 else None if n == 0 else -1


def set_number(buf, name, new):
    """Set a byte (tag 1) or int (tag 3) tag in place and return its old value."""
    for tag_type, fmt in ((1, "<B"), (3, "<i")):
        pos = _find(buf, tag_type, name)
        if pos == -1:
            raise ValueError("%s: tag name is not unique" % name)
        if pos is not None:
            old = struct.unpack_from(fmt, buf, pos)[0]
            struct.pack_into(fmt, buf, pos, new)
            return old
    raise KeyError("%s: no byte/int tag with this name" % name)


def set_string(buf, name, new):
    """Replace a string tag (tag 8), fix the header length, return the old value."""
    pos = _find(buf, 8, name)
    if pos in (None, -1):
        raise KeyError("%s: string tag not found / not unique" % name)
    old_len = struct.unpack_from("<H", buf, pos)[0]
    old = bytes(buf[pos + 2:pos + 2 + old_len]).decode("utf-8", "replace")
    encoded = new.encode("utf-8")
    buf[pos:pos + 2 + old_len] = struct.pack("<H", len(encoded)) + encoded
    struct.pack_into("<i", buf, 4, len(buf) - 8)   # header: storage version, payload length
    return old


def patch_world(world_dir, *, lan=False, edu_off=False, permission_level=None, name=None):
    """Patch ``level.dat`` and ``level.dat_old`` in ``world_dir`` (server must be stopped).

    lan:              LANBroadcast=1 and XBLBroadcastIntent=0
    edu_off:          eduOffer=0 and educationFeaturesEnabled=0
    permission_level: playerPermissionsLevel (0 visitor, 1 member, 2 operator)
    name:             LevelName (also rewrites levelname.txt)
    """
    for fname in ("level.dat", "level.dat_old"):
        path = os.path.join(world_dir, fname)
        if not os.path.exists(path):
            continue
        backup = path + ".nbtpatch.bak"
        if not os.path.exists(backup):
            shutil.copyfile(path, backup)
        buf, changes = bytearray(open(path, "rb").read()), []
        if lan:
            changes += [("LANBroadcast", set_number(buf, "LANBroadcast", 1)),
                        ("XBLBroadcastIntent", set_number(buf, "XBLBroadcastIntent", 0))]
        if edu_off:
            changes += [("eduOffer", set_number(buf, "eduOffer", 0)),
                        ("educationFeaturesEnabled", set_number(buf, "educationFeaturesEnabled", 0))]
        if permission_level is not None:
            changes.append(("playerPermissionsLevel",
                            set_number(buf, "playerPermissionsLevel", permission_level)))
        if name is not None:
            changes.append(("LevelName", set_string(buf, "LevelName", name)))
        open(path, "wb").write(buf)
        print(fname, "(old values):", changes)
    if name is not None:
        with open(os.path.join(world_dir, "levelname.txt"), "w", encoding="utf-8") as fh:
            fh.write(name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("world_dir", help="world folder containing level.dat (server STOPPED)")
    ap.add_argument("--dump", action="store_true", help="print the interesting level.dat keys")
    ap.add_argument("--lan", action="store_true", help="LANBroadcast=1, XBLBroadcastIntent=0")
    ap.add_argument("--no-edu", action="store_true", help="eduOffer=0, educationFeaturesEnabled=0")
    ap.add_argument("--permission-level", type=int, choices=(0, 1, 2),
                    help="playerPermissionsLevel: 0 visitor, 1 member, 2 operator")
    ap.add_argument("--name", help="new LevelName (shown in the world list)")
    args = ap.parse_args(argv)
    path = os.path.join(args.world_dir, "level.dat")
    if args.lan or args.no_edu or args.permission_level is not None or args.name:
        patch_world(args.world_dir, lan=args.lan, edu_off=args.no_edu,
                    permission_level=args.permission_level, name=args.name)
    if args.dump or not (args.lan or args.no_edu or args.permission_level is not None or args.name):
        data = load(path)
        for key in SHOW_KEYS:
            if key in data:
                print("%-32s %s" % (key, data[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
