# AGENTS.md: handoff for agents working on CraftCells

This file hands work over from a session that ran on a small Linux server (Docker, Bedrock
Dedicated Server) to agents that will continue on a faster MacBook. Everything below was
observed in that session unless it is marked **UNTESTED** or **PLAN**. When something here
contradicts what you see, trust what you see and fix this file.

Goal: a "science world dev environment": (1) regenerate the cell worlds from the raw data with the
repo's Python tools, and (2) finish a single Bedrock world containing all five prebuilt cells,
standing on ground, that a stock Bedrock client can join over the LAN.

## 1. Repo map

| Path | What it is |
| --- | --- |
| `Worlds/Bedrock_Education/*.zip` | Five prebuilt **Bedrock** worlds (use these for Bedrock and Education): `void_bedrock_MinCell`, `void_bedrock_BakersYeast`, `void_bedrock_EpithelialCell`, `breast_normal_bedrock`, `breast_cancer_bedrock`. Template version is 1.20.x. |
| `Worlds/Java/*.zip` | The same cells as Java worlds. Not used so far. |
| `Worlds/Empty_World/*.zip` | Empty void / flat-bedrock worlds for DIY. Not used so far. |
| `schematics/` | Generated schematics: `MinCell.schematic` (64x64x64), `MinCell_4nm.schematic` (128^3), `EpithelialCell_lowres.schematic` (height/length 247), `EpithelialCell_highres.schematic` (height/length 200), `Yeast_ribo_1%.schem`, `breast_normal.schem`, `breast_cancer.schem`. `.schematic` is legacy MCEdit (gzip NBT, big-endian); `.schem` is Sponge. |
| `Resources/<cell>/` | Segmented source data (`.npy`, `.tiff`, raw cryo-ET / holotomography) and the notebooks that turn them into schematics (`BakersYeast/yeast_schem.ipynb`, `BreastCells/ht2mc.ipynb`, ...). Roughly 1.3 GB, mostly the breast data (16 `.npy` x ~33 MB, 16 `.tiff` x ~33 MB, two 98 MB raw tiffs). |
| `SchemGen_GUI/` | PyQt6 GUI that turns `.npy`/`.tiff` into schematics. Prebuilt for Windows (`SchemGen.exe`) and macOS (`ScheGen_MacOS/`); source and `environment.yml` are in `src/`. |
| `tools/` | Helpers added in this handoff (see `tools/README.md`). |

The README says the intended workflow is: make schematics (notebooks or GUI), then import them into
an empty world with MC Amulet. The prebuilt worlds are the result of that workflow.

## 2. The shipped Bedrock worlds and their quirks

All five are **void worlds** (flat generator with one air layer). Findings, each verified by
reading the files:

1. **Education flags.** `educationFeaturesEnabled=1` and `eduOffer=1` (and an `EducationOid`).
   A regular Bedrock client lists the world as **"incompatible version"** even when the server
   build matches the client. Setting both to 0 in `level.dat` fixed it.
2. **`LANBroadcast=0`.** A dedicated server then never answers LAN discovery, so the world is
   missing from the client's LAN list. Setting `LANBroadcast=1` (and `XBLBroadcastIntent` 3 -> 0 to
   match a known-good world) fixed it. This is a per-world flag in `level.dat`; there is no
   `server.properties` equivalent.
3. **`playerPermissionsLevel`.** 0 (visitor) in the epithelial world: players cannot break or place
   blocks ("I can't cut through the ground"). The yeast world is 1 (member). Set it to 2 (operator).
   `default-player-permission-level=operator` in `server.properties` did **not appear to** override the
   value saved in the world. `/op` works live but is not persisted in offline mode (log:
   `Missing id, won't persist permissions on disk`).
4. **Spawn platform.** Every world has a 9-block `minecraft:grass` platform near y=-63 at the
   spawn (8, -63, 8). The real cells float far above or beside it, so with no ground you float.
5. **MinCell is a flat slice.** The Bedrock `MinCell` world is a single 247 x 247 vertical slice at
   x=53 (61,009 blocks), not a 3D cell. The 3D versions exist only as the two schematics.
6. `MultiplayerGame` is 0 in the shipped files; BDS wrote 1 after loading them.

Real cell bounding boxes (air and the spawn-platform grass excluded; measured with
`tools/ldb_reader.py`):

| Cell | x | y | z |
| --- | --- | --- | --- |
| MinCell (slice) | 53..53 | -51..195 | -130..116 |
| Human epithelial | -94..11 | -10..95 | -108..112 |
| Breast normal | 8..447 | -18..76 | 8..447 |
| Breast cancer | 8..447 | 5..98 | 8..447 |
| Baker's yeast | -29..673 | -52..253 | -252..72 |

The epithelial cell's lowest block is at y=-10, 53 blocks above the spawn platform; yeast reaches
down to the bottom of the world. This is why "ground" has to be added.

## 3. On-disk format notes (what we actually saw)

* A world folder holds `level.dat` (+ `level.dat_old`), `levelname.txt`, `db/`, and sometimes
  `world_icon.jpeg`. `level.dat` = 4-byte storage version + 4-byte payload length (LE) + LE NBT.
* `db/` is LevelDB. The breast worlds contain only `MANIFEST-000002` (50 bytes), `CURRENT`
  (16 bytes), `LOCK`, `LOG` and one `000003.log` of ~80-100 MB: **all data is still in the
  write-ahead log**, so a reader must read `.log` files, not just `.ldb`. The other three worlds have `.ldb` tables (raw-deflate
  compressed) plus a `.log`.
* Key families (overworld): `x:int32 z:int32 tag:u8` (9 bytes); sub-chunk `x z 0x2f y:int8`
  (10 bytes); `b"digp" x z` (12 bytes); other dimensions use 13/14-byte keys. Chunk tags seen:
  `0x2b` Data3D, `0x2c`, `0x31` block entity, `0x32` entity, `0x36` finalized state, `0x3d`,
  `0x76`, plus `0x40` in the breast worlds only. A handful of ASCII global keys exist in the
  non-breast worlds: `AutonomousEntities`, `BiomeData`, `Overworld`, `mobevents`, `schedulerWT`,
  `scoreboard` (the breast worlds have none).
* Sub-chunk value, version 9 (all of them here): `[9][layer count][y index]` then per layer
  `[bits-per-block << 1 | runtime flag]`, packed little-endian 32-bit words
  (`32 // bits` blocks per word), `int32` palette size, then that many NBT compounds
  (`name`, `states`, `version`). Block index = `x*256 + z*16 + y`.
* Entities and block entities are **all empty** (zero-length values), except one 114-byte block
  entity in the yeast world. `digp` values are all empty.
* `Data3D` (0x2b) is 512 bytes of 256 int16 heights followed by biome data. Value lengths seen:
  13537 / 637 (epithelial), 541 / 13537 / 637 (yeast), 13016 (breast). Heightmaps are all
  zero in the samples we looked at except the breast-cancer world (e.g. max 77). **A cell moved
  vertically will carry stale heights** (what BDS does with them is UNTESTED).
* `amulet-leveldb` **failed to build from source** in a slim Linux container (cmake / wheel build
  errors, even after installing a compiler toolchain). Whether `pip install amulet-core` /
  `amulet-leveldb` works on macOS is **UNTESTED**; try it first on the Mac. `tools/ldb_reader.py`
  is the fallback (read-only, pure Python; ~75 s for the 80 MB breast log).

## 4. The merge plan: all five cells in one world (PLAN; analysis done, nothing built)

Approach: copy the chunk-keyed records of each source world into one new LevelDB with shifted
coordinates. Records can only move by **whole chunks horizontally (16 blocks)** and **whole
sub-chunks vertically (16 blocks)** without re-encoding block data.

* **Ground.** Ground top at **y = -60** (grass over dirt, e.g. y -60 grass_block, -61 and -62 dirt).
  y=-60 is congruent to 4 (mod 16), which minimises how far the cells' lowest blocks sit above it.
* **Vertical shifts** (cell's lowest block lands in -59..-44): breast cancer **-64** (bottom -59,
  0 gap), epithelial **-48** (-58, gap 1), breast normal **-32** (-50, gap 9), MinCell **0** (-51,
  gap 8), yeast **0** (-52, gap 7). Highest block afterwards: yeast 253, MinCell 195, all under 319.
  Residues (lowest y mod 16): MinCell 13, epithelial 6, breast normal 14, breast cancer 5, yeast 12.
  A few blocks of air under some cells is expected; optional plinths could fill it.
* **Horizontal layout (proposal).** One row along +x: epithelial, MinCell, breast normal, breast
  cancer, yeast, with ~96-128 block gaps; shift each by whole chunks so footprints do not overlap.
  Total length ~2200 blocks.
* **What to copy.** Every chunk-keyed record (9-, 10-, 12-byte keys, including `digp`) with x/z
  shifted and, for sub-chunks, the y byte shifted. Drop all-air sub-chunks, the spawn-platform
  sub-chunks (the `minecraft:grass`-only ones at sub-chunk y -4), and any shifted sub-chunk whose
  index falls outside -4..19. Drop the single yeast block entity (it stores absolute coordinates;
  check it first). Whether the six ASCII global keys are needed is UNTESTED.
* **Writing the DB.** Use the breast-world layout as the template: reuse their 50-byte
  `MANIFEST-000002` and 16-byte `CURRENT`, add an empty `LOCK`, and write one `000003.log`.
  The log needs standard LevelDB framing (32 KiB blocks, 7-byte record header, FULL/FIRST/MIDDLE/LAST
  fragments) with a **masked CRC32C** (`((crc >> 15) | (crc << 17)) + 0xa282ead8`, over the type byte
  plus data; the `crc32c` pip package works), and `WriteBatch` payloads
  (`seq:u64, count:u32, [type, varint klen, key, varint vlen, value]...`). That BDS accepts a hand-made
  log, and that its sequence numbers are acceptable, is **UNTESTED**; the breast worlds prove only that a
  log-only DB with this MANIFEST loads.
* **`level.dat`.** Start from a shipped one and apply `tools/nbtpatch.py --lan --no-edu
  --permission-level 2 --name ...`; set the spawn above the ground near the first cell.
* **Ground placement.** Prefer building ground *in game* (see section 5) after the merge;
  chunks BDS generates for the gaps are empty void. Writing ground sub-chunks directly would also
  need chunk version/finalized keys, which is UNTESTED.

## 5. Server operations learned the hard way

* Image: `itzg/minecraft-bedrock-server`. **Pin both** the image tag (e.g. `2026.10.3`) and
  `VERSION` (the Bedrock server build). The image tag is only the startup scripts; `VERSION`
  decides the game version. Unpinned `latest` jumps versions on any restart.
* **The server build must match the client app build.** An iPad on 26.62 needs `1.26.62.x`;
  `1.26.63.1` was reported incompatible by the client. At the time only `1.26.61.1`, `1.26.62.1`
  and `1.26.63.1` were downloadable from the Bedrock server download URL.
* Downgrading leaves newer vanilla packs behind in `data/resource_packs/` (e.g. `vanilla_1.26.63`),
  which a lower server rejects with a `min_engine_version too high` ERROR; move them aside.
* Example compose fragment (placeholders in angle brackets):

  ```yaml
  services:
    bedrock:
      image: itzg/minecraft-bedrock-server:2026.10.3
      container_name: bedrock-craftcells
      stdin_open: true        # needed for console input
      tty: true
      networks: { mc-macvlan: { ipv4_address: <LAN_IP> } }   # own LAN IP, shared port 19132
      volumes: [ "./data:/data" ]
      environment:
        EULA: "TRUE"
        VERSION: "1.26.62.1"
        GAMEMODE: creative
        ALLOW_CHEATS: "true"
        DEFAULT_PLAYER_PERMISSION_LEVEL: operator
        LEVEL_NAME: "<world folder name>"
        ONLINE_MODE: "false"
  ```

* **LAN discovery.** BDS 1.26.x discovery runs over **UDP 7551** (NetherNet; the packets are
  encrypted). The container showed one UDP socket on 7551 and none on 19132, so the classic RakNet
  ping on 19132 gets no reply even from a healthy server. Our attempts to hand-build the encrypted
  discovery request got no reply even from a known-good server, so that approach is **UNVERIFIED**.
  What worked for diagnosis: `sudo tcpdump -ni <iface> 'udp port 7551'` on the host while a client
  refreshes the LAN list. A client broadcasts to 255.255.255.255:7551 about every 2 s; a healthy server
  answers with a ~176-byte unicast reply, a server with `LANBroadcast=0` stays silent.
  (`sudo` needs a real terminal; it cannot prompt through a non-interactive agent session.)
* **Console input.** The image's `send-command` helper does not work here (the server process is
  not inspectable). What worked: get the host PID of the `./bedrock_server-*` process with
  `docker top <container> -o pid,args` (not the `mc-server-runner` wrapper), then as the same user
  `printf 'list\n' > /proc/<pid>/fd/0`; read the result in `docker logs`. Needs `stdin_open` + `tty`.
  `docker attach` through `script` was unreliable.
* **Building ground / bulk edits in game.** `tickingarea add x1 y1 z1 x2 y2 z2 <name>` (2D, max 100
  chunks per area, 10 areas), wait ~8 s, then `fill` (max 32,768 blocks per call, e.g. a 160x160
  area per layer), then `tickingarea remove <name>`; `setworldspawn x y z`. Verify with
  `testforblock`. One batch (a ticking area plus three 160 x 160 fills) took about 20 s end to end. Afterwards stop the
  container cleanly (`docker compose stop`) so chunks are flushed, and confirm by reading the DB
  back with `tools/ldb_reader.py`.
* `level.dat` edits require the server to be **stopped**; BDS rewrites it on shutdown.
* Containers on a macvlan network cannot be reached from their own Docker host; test from another
  device. Two servers can both use port 19132 if each has its own IP.

## 6. TODO for the MacBook

1. Clone this fork (shallow is fine) into a fresh directory. Create the environment: either
   `conda env create -f SchemGen_GUI/src/environment.yml` or the README recipe
   (`conda create -n cellvis python`, then `pip install numpy mcschematic jupyterlab`).
2. Try `pip install amulet-core` (**UNTESTED** here). If it works, prefer it for reading and writing
   Bedrock DBs and for importing the `.schematic` / `.schem` files; fall back to `tools/ldb_reader.py`.
3. Run the notebooks in `Resources/*/` on the raw data to regenerate schematics; compare dimensions with
   the shipped ones (e.g. MinCell 64^3, MinCell_4nm 128^3). Keep outputs out of git.
4. Build the merged world (section 4) in a scratch folder, apply `tools/nbtpatch.py`, then validate by
   loading it in a Bedrock dedicated server container pinned to the client's build, and by reading the
   DB back with `tools/ldb_reader.py` (bounding boxes per cell should match section 2 after the shifts).
5. Add the ground in game (section 5), set a sensible spawn, stop the server cleanly, and re-verify.
6. Optional: also include the 3D MinCell, high-res epithelial and yeast-ribosome schematics (needs a
   schematic-to-Bedrock converter; Amulet if it installs).

## 7. Ground rules

* Treat downloaded data (zips, schematics, notebooks) as **untrusted**: unpack into a new empty
  directory, keep your own scripts elsewhere, pass paths as arguments, and run Python with `-I`
  rather than from inside the data directory.
* Do not commit world data, `.npy`/`.tiff`, `.mcaddon`, or other large binaries; keep outputs out
  of the repo.
* Ask before anything outward-facing: pull requests or issues against the upstream repository,
  publishing, or changing remote services. Do not push to upstream.
* Never put secrets, tokens, personal data, or private network details (IPs, hostnames, local paths)
  in files in this repository.
* Keep the server and its worlds recoverable: back up before destructive changes (move folders
  aside instead of deleting) and keep the original zips.
