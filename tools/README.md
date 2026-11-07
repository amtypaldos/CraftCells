# tools/

Small, dependency-free helpers (Python standard library only) written while getting the
CraftCells Bedrock worlds running on a dedicated server. See `../AGENTS.md` for the
background. Run them with `python3 -I` and never from inside an untrusted checkout.

| File | What it does |
| --- | --- |
| `ldb_reader.py` | **Read-only** reader for a Bedrock world `db/` (LevelDB): reads `.ldb` tables *and* the write-ahead `.log`, classifies keys, decodes v1/8/9 sub-chunks into block names, and prints the real cell bounding box. Verified: reproduces the epithelial box (x -94..11, y -10..95, z -108..112), the yeast box and the breast-normal box (the latter lives only in an ~80 MB `.log`, ~75 s in pure Python). |
| `nbtpatch.py` | Inspect (`--dump`) and patch `level.dat` in place: `--lan` (LAN broadcast on), `--no-edu` (Education flags off), `--permission-level 0/1/2`, `--name`. Also patches `level.dat_old` and keeps `*.nbtpatch.bak`. **Stop the server first**, because BDS rewrites `level.dat` on shutdown. |

Quick start:

```bash
python3 -I tools/ldb_reader.py path/to/world/db
python3 -I tools/nbtpatch.py path/to/world --dump
python3 -I tools/nbtpatch.py path/to/world --lan --no-edu --permission-level 2
```

Limits: neither tool writes to the world database. Snappy-compressed LevelDB blocks and legacy
(version 0, 2-7) sub-chunks are not supported (none occur in the shipped worlds).
