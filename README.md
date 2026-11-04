# Street Face Artwork Manager

Windows desktop application for validating, reviewing and publishing Street Face print-on-demand mug artwork to Cloudflare R2 and a held Supabase catalogue.

## Project relationship

- **Image generator**: creates print PNG, SVG, front/rear mockups and machine-readable review reports.
- **Artwork Manager** (this repository): reads the latest output, validates it, safely uploads selected production assets, and synchronises the read-only catalogue.
- **Shop** (`desdodec/shop`): displays the current catalogue. Payment and live fulfilment approvals remain separate.

## Source of truth

The generator's `print_report.json` contains `processed`, `written`, the explicit `skipped` decisions, and `streets[].location.street_key`. Artwork names and four-digit artwork numbers are presentation details, **not primary keys**.

For the 3 November 2026 Hebden Bridge production run, the report says **311 processed, 290 written, 21 explicitly skipped as not_for_production**. A complete release should show 290 designs and explicitly remove the 21 excluded street keys from the current published inventory. A partial release should preserve omitted catalogue entries.

## Important safety constraints

- Publication must never enable checkout, alter historical orders or initiate Prodigi fulfilment.
- Hold/pending gates apply until the physical proof has been approved.
- A complete release cannot be inferred merely because some files are absent; use explicit release mode and review decisions.
- Never delete or clear the R2 bucket just to publish.
- Never store credentials, password-manager exports, `.env` files, generated artwork or run-specific production reports in Git.
- Production updates must have a preflight, a file-level audit and an independently verifiable database result.

## Setup

This repository is being prepared as the canonical code source. The existing v10 Windows application is still local until its source files are checked in. **Do not clone this scaffold over the working installation.** Keep the current app and credentials untouched; we'll migrate the source and then use normal `git pull` updates.

## Release workflow

1. Generate a production folder and report files.
2. Review selection from `print_report.json` and validate artwork.
3. Preview a complete or partial release, including excluded street keys.
4. Upload required R2 objects, verify and commit the held catalogue transaction.
5. Confirm the read-only preview; separately approve physical samples and sales.

## Artwork verification

Before requesting the catalogue commit, the desktop streams and SHA-256-hashes
every required R2 artwork object, including unchanged files and SVGs. It records
the actual size, hash and ETag in the publication audit and sends those values
to the Edge Function. The Edge Function uses HEAD requests to reject missing
objects or changed sizes/ETags before calling SQL; it downloads no artwork bodies.
PNG files must still be at least 1,000 bytes (SVGs at least 100 bytes).

This moves the download cost to the desktop: a collection containing roughly
1 GB of artwork still requires roughly 1 GB of verification downloads per publish
attempt, even when no uploads are needed. Preview comparisons without checksum
metadata and replacement-backup verification can add downloads. Streaming uses
1 MiB chunks rather than buffering the collection. Edge still makes one HEAD
request per artwork object (1,160 for a 290-design collection).

There remains a time-of-check/time-of-use window: an object can change after its
Edge HEAD check and before the database transaction or later use. ETags are change
checks, not SHA-256 proofs; a same-size change retaining the same ETag cannot be
detected by HEAD. The desktop GET uses If-Match and checks response headers and
actual bytes, but R2 and Supabase do not form one atomic transaction. Failure or
cancellation before the commit call blocks that call; an already-sent commit
cannot be cancelled by the desktop. Publish Again prepares a fresh comparison,
skips unnecessary uploads and re-verifies every required object.

Offline verification commands (no credentials or cloud access):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/edge_artwork.test.mjs tests/edge_package.test.mjs
```

The Edge tests run the actual handler with fake SDKs under Node's TypeScript
stripper. They do not validate deployment, Deno/SDK compatibility or live services.
The root Python suite also captures a real desktop publication payload for two
streets and runs it through that handler with mocked R2 and Supabase services.

The deployment source consists of **both** `collection-publish-edge.ts` and
`collection-artwork-verification.mjs` in the same function source directory.
Preserve their filenames and relative layout when preparing the function's entry
point; copying the TypeScript file alone is insufficient. The offline packaging
test copies this pair into an isolated folder and resolves the entry point's
actual relative import. It does not deploy anything or verify Deno/SDK support.

