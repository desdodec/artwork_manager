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

