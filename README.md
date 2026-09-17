# Photo Scanner

(`photo_scanner_banana` / `banana` is the repository and package naming convention, not the product name.)

Offline triage pipeline for Epson FF-680W photo scans → Synology NAS → Immich.

Front/back pairing, duplicate detection (dHash + DINOv3), offline handwriting reading (TrOCR),
a review UI, and export as EXIF + XMP sidecars that an Immich External Library imports natively.
Design and phases: see the approved plan (kept outside the repo) and `docs/`.

## Status

| Phase | State |
|---|---|
| 1 Skeleton: config, DB, job queue, `banana_core` (dHash, blank metrics, decode) | done |
| 2 Export engine: layout, ExifTool writer + readback verify, `banana export --manual-json` | done — verify in Immich (`docs/manual-export.md`) |
| Ingest (pairing, blank backs, dHash rescans) + review web UI | done (basic) |
| 3 Analysis (watcher, DINOv3, TrOCR) | next |
| 5 Immich stacks/albums · 6 profiling | later |

## Docs

- [docs/operators.md](docs/operators.md): scanning and review workflow for operators
- [docs/specifications.md](docs/specifications.md): data model, API, export contract, config
- [docs/diagnostics&bugs.md](docs/diagnostics&bugs.md): troubleshooting, known bugs and limitations
- [docs/manual-export.md](docs/manual-export.md): Immich smoke test

Local test without NAS/Immich: `wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/serve_local.sh`

## Layout

```
banana/          Python app (FastAPI, jobs, export, analysis)
banana/core/     hot-path dispatcher: C++ banana_core if installed, NumPy reference otherwise
native/          C++20 nanobind extension (CMake + scikit-build-core)
docker/          Ubuntu/CUDA image and compose file for the server
tests/
```

## Development

Linux (or WSL2 Ubuntu 24.04) matches production:

```bash
sudo apt install build-essential cmake libopencv-dev libimage-exiftool-perl python3-venv
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" ./native
pytest
```

From Windows with WSL Ubuntu 24.04 (build tools installed as above), one command syncs, builds and tests:

```
wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/wsl_build.sh
```

On Windows without a C++ toolchain, skip `./native`: everything runs on the NumPy reference, and
the native-equality tests are skipped. The exporter tests need `exiftool` on PATH.

```
banana doctor          # shows native core / exiftool / path availability
banana export --manual-json batch.json
banana serve           # API on :8000
banana worker          # job worker
```

## Server deployment (Ubuntu + NVIDIA)

1. NVIDIA driver, Docker, nvidia-container-toolkit.
2. NFS-mount the Synology share at `/mnt/photo_vault` (with `inbox/`, `archive/`, `sorted/`).
3. `cp config.example.toml docker/config.toml` and edit it.
4. `docker compose -f docker/compose.yaml up -d --build`

Immich side: mount `/volume1/photo_vault/sorted` into the Immich containers **read-only**, create an
External Library using the in-container path, and add the exclusion pattern `**/.staging/**`.
