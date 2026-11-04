# Street Face Artwork Manager: development instructions

## Working with the owner

The owner is a non-technical builder and product manager. Act as lead developer:
handle architecture, code quality, debugging and technical choices independently.
Explain changes in everyday language, focusing on app behaviour and outcomes.
Ask the owner about material UI, workflow and feature-behaviour decisions; make
routine technical decisions yourself and briefly explain the reason.
Keep doing meaningful verification, but describe it plainly. Report actual
results, risks and limitations. At the end of each task give the exact count of
items processed, and name every skipped item with its specific reason. Use the
word "complete" for task status only when every requested item was finished.

## Purpose and scope

This is an existing Windows Python/Tkinter desktop application supporting an
Etsy print-on-demand mug business. Work with the existing code, not a rebuild.
The artwork generator supplies the artwork and review data; this app validates
and publishes it; the separate shop (`desdodec/shop`) displays the catalogue.

Preserve this simple operator workflow:

1. Select a folder containing artwork and associated CSV/GeoJSON data.
2. Validate the files, permanent street identities, geography and print profile.
3. Show a clear summary of designs and proposed publication changes.
4. Publish after explicit operator confirmation.
5. After failure, Publish Again creates a fresh comparison and skips files that
   are already uploaded and verified, finishing the remaining work.
6. Synchronise Supabase only after all required R2 objects have been verified.

Prefer simplicity and reliability over extra infrastructure. Do not add recovery
frameworks, abstractions, configuration systems or services unless essential to
the authorised task. Preserve working functionality without demonstrated reason
to change it. Keep changes minimal, understandable and testable.

## Safety boundaries

- Never upload to, overwrite or delete objects in production R2 bucket
  `mug-print-assets` without explicit authorisation. Creating backup copies there
  is also a production cloud write and requires authorisation.
- Never execute a live Supabase publication transaction, install publication SQL,
  deploy the Edge Function or otherwise change production database records during
  ordinary development/testing without explicit authorisation.
- Never activate Stripe checkout or live Prodigi fulfilment. Physical proof,
  sales and fulfilment approvals are separate from catalogue publication.
- Never reveal, print, export or commit credentials, tokens or secrets. Do not
  inspect credential values or dump environment variables/credential stores for
  routine diagnosis. Keep errors and reports free of secrets.
- Prefer offline tests and mocks. Never run live cloud writes merely because a
  helper or test script supports them. Do not run `--execute` integration-test
  commands without explicit approval; do not bypass interactive safeguards.
- Never automatically restore production artwork. A recovery assessment or
  verified backup is not restoration authorisation.
- Never clear a bucket to publish a release. Do not add automatic deletion or
  cleanup of retained backups or test objects.
- Do not make destructive Git changes, discard the owner's changes, or force-push.
  Do not commit or push unless instructed.
- Permission to edit code or run offline tests does not authorise cloud activity.
  Read-only cloud tools still contact live services; do not invoke them when the
  task is local-only or prohibits cloud operations.

Production and test credentials must stay separate in Windows Credential Manager
through `keyring`. Production service name is `street-face-artwork-manager`;
the dedicated test service is `street-face-artwork-manager-r2-tests`. These names
are configuration identifiers, not secret values. Never substitute production
credentials for missing test credentials.

## Current project facts

- Publisher dataset: `street-face/hebden-bridge/v2`. Publishing is currently
  restricted to held Hebden Bridge v2 designs, not a general multi-area service.
- Production R2 bucket: `mug-print-assets`. The normal app accepts its bucket from
  operator configuration; this is not a universal hard-coded bucket guard.
- Integration-test bucket: `street-face-integration-tests`; test root prefix:
  `street-face-integration-tests/v1/`, isolated further by run UUID.
- Known Hebden Bridge selection: 311 candidates processed, 290 selected designs,
  21 intentionally `not_for_production`; four artwork files per selected design
  gives 1,160 files. These are release-specific expectations, not general limits
  or a reason to reject legitimate partial updates.
- Those release counts are recorded in the brief and README, not independently
  established by an artwork audit in this repository inspection. Confirm the
  actual selected folder and generator report before any authorised publication.

## Source map

| File | Responsibility and constraints |
| --- | --- |
| `app.py` | Tkinter UI, folder detection, four-slot artwork validation, local manifests, worker threads/event queue, credential UI, print checks and confirmed publication. Keep long-running work off the UI thread. Legacy raw upload/replacement handlers remain present but their buttons are hidden. |
| `publisher.py` | `prepare` checks the held catalogue, maps assets, compares local/remote objects and writes a plan. `publish` rechecks safety, backs up replacements, uploads/verifies files, journals progress and calls the catalogue transaction last. |
| `collection-publish-edge.ts` | Deno Supabase Edge Function, `street-face-publish-v2`. POST-only, separate bearer publish token, service-role database access, held/no-order checks, R2 HEAD checks and RPC coordination. Source comments specify legacy JWT verification off with the separate publish-token guard; never weaken that guard. |
| `collection_publish.sql` | Publication tables, service-role-only `street_face_publish_held_v2` transaction and public read-only `street_face_current_v2` RPC. Preserve transaction locking, idempotency, identity and held/pending gates. This is live schema-changing SQL if executed. |
| `geography.py` | Fail-closed CSV/GeoJSON matching, stable identity mapping, coordinate checks, postcode enrichment and geographic reports. |
| `production_report.py` | Validates generator `print_report.json` counts, selected permanent keys, four file references and explicit exclusion reasons; previews unlisting by stable key. |
| `print_validation.py` | Local PNG/SVG audit and CSV/JSON reports. Current `GLOBAL-MUG-W` image profile is 2670 x 1110 px, 229 x 95 mm, 1.5% ratio tolerance. This is an existing project profile, not a universal product specification or physical proof approval. |
| `replacement.py` | Legacy audited in-place R2 replacement. Does not check Supabase safety, create the publisher's backup set or reconcile the catalogue; warns that database asset metadata becomes stale. Do not use it as the normal publishing path. |
| `recovery_assessment.py` | Reads local publication journals only. Identifies recorded/potential changes and missing backup evidence; cannot establish current remote state or restore anything. |
| `remote_recovery_verification.py` | Validates all journal records before cloud reads, then uses only HEAD/GET on recorded backups and hashes actual bytes. Does not check current originals or Supabase, establish journal trust or authorise restoration. |
| `r2_isolated_test.py` | Offline plan-only harness; no credentials, cloud operations or execute option. |
| `r2_backup_integration.py` | Dry-run by default; opt-in live probe creates, backs up, replaces and restores disposable objects only in a fresh dedicated test prefix, retaining objects without deletion. Its live mode needs explicit approval. |
| `r2_test_bucket_check.py` | Live read-only test-bucket connectivity/isolation helper, restricted to the dedicated test bucket. Its unit tests use fakes. |
| `r2_test_credentials.py` | Interactive helper storing separate test credentials. Do not run it during ordinary offline testing. |

## Identity, inputs and selection

- `street_key` is permanent. Filenames, four-digit artwork numbers and street
  names are presentation/export details, never database primary identities.
  Preserve distinct streets with the same name and existing design IDs/codes.
- Each design has front and rear PNG mockups under `mockups/`, and production PNG
  and SVG under `print_candidates/`. Names use
  `NNNN_<street>_wrap_print[_front|_rear].png` and production `.svg`.
- Prefer the run's `artwork_identities.csv` mapping to permanent keys. Legacy
  fallback uses original zero-based CSV indices; never pair a partial run with
  the first N rows or guess identities by name. Never silently renumber identities.
- Geography requires matching CSV/GeoJSON key sets, valid LineString or
  MultiLineString coordinates and consistent names. Postcode evidence joins by
  key. Auto-detection expects `OS_Mail_Addresses/.../reviews/<area>_streets...`
  and the `geojson_edited` sibling data; it does not combine production runs.
- The generator's `print_report.json` is authoritative selection evidence when
  present. Counts must balance; selected keys must match validated artwork;
  skipped entries must have names and explicit `not_for_production` status.
- A full collection release (code mode `complete`) unlists all currently listed
  keys omitted from selection; a `partial` update preserves omitted listings.
  Default UI mode is partial. Never infer full-release intent or exclusions from
  absent files. The 21 generator exclusions and number of existing listings to
  unlist are different counts, especially on a first publication.

## Publication and recovery invariants

- Preserve `NEW`, `SAME`, `REPLACE` comparisons using byte counts and SHA-256.
  ETags are change checks, not SHA-256 hashes. Reuse existing object keys and skip
  matching verified files; a retry must prepare a fresh plan rather than reuse a
  stale interrupted plan.
- Validate print files before publication. The UI blocks both `FAIL` and `REVIEW`
  print outcomes. Local technical checks do not approve visual quality, safe
  zones, colour fidelity or physical production.
- Require held designs and zero checkout-order references before overwrites and
  again in the database transaction. Refuse unsafe or changed local/remote state.
- Back up every planned replacement before any artwork overwrite, under
  `<dataset>/_publication_backups/<operation_id>/...`. Verify backup size and
  actual-byte SHA-256; refuse existing backup destinations and retain backups.
- Keep atomic local journal writes (temporary file, flush/fsync, replace), backup
  records, file results and overwrite intent saved before each overwrite. A
  process crash can bypass exception handling; preserve pending intent evidence.
- Verify every required R2 object before the commit request. R2 writes and the
  database transaction are not a distributed transaction: a failure can leave
  uploaded artwork with an unchanged catalogue. Report uncertainty honestly and
  preserve evidence for reconciliation; never claim automatic rollback.
- Preserve SQL advisory transaction locking, unique operation IDs, duplicate-key
  rejection and `(dataset_id, street_key)` inventory identity. Keep design codes
  independent of filenames, assets pending and designs held. All catalogue
  changes must commit together or roll back together.
- API `checkout_enabled: false` and `prodigi_enabled: false` are fixed assertions,
  not an independent audit of operational checkout/fulfilment configuration.

## Local setup and verification

Use the existing `.venv` interpreter where available. Dependencies in
`requirements.txt` are boto3, keyring and Pillow; Tkinter is part of the Python
installation. Local setup/launch commands (installation may require network;
launch only when useful for an authorised UI task):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Run both offline suites from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

Root discovery currently does not descend into `tests/`; do not omit the second
command. Inspect new/changed tests for cloud access before running them. Current
tests use in-memory clients and mocked APIs, including integration-named tests.

Coverage map: `test_publisher_backup.py` covers backup and failure journals;
`test_publication_failure_audit.py` covers failures through local assessment;
`test_publish_again.py` covers fresh-plan retry and upload skipping;
`test_recovery_assessment.py` and `test_remote_recovery_verification.py` cover
local/remote assessment; the four `test_r2_*.py` modules cover test isolation,
credential storage, bucket checks and the simulated live probe.
`tests/test_publisher.py` covers selection mode and upload-before-commit;
`tests/test_production_report.py` covers selection counts, keys and exclusions.
Add meaningful offline regression coverage for behavioural changes. Do not claim
Python unit tests validate deployed SQL/TypeScript, real cloud access or full UI
behaviour. Report commands, passed/failed/skipped counts and untested boundaries.

## Repository hygiene and inspection baseline

Before editing, inspect Git status, current source and applicable instructions.
Keep owner changes intact. Do not treat ignored legacy copies or archives as the
canonical app, and do not overwrite the working installation with a scaffold.
Keep generated artwork, releases, audits, reports, `.env` files and credentials
out of Git. `.gitignore` covers common outputs but not every possible report
filename; inspect proposed additions individually.

Inspection on 3 November 2026, before this file was added:

- HEAD was `0463e2f` (retryable Publish Again tests), matching the supplied brief.
- All 11 `test_*.py` files were inspected. Actual offline results: root suite
  50 passed, `tests/` suite 7 passed; total 57 passed, 0 failed, 0 skipped.
- Root `AGENTS.md` was absent. Existing `instructions.txt` contains local install
  and launch commands. `README.md` has stale scaffold/setup wording claiming app
  source is not checked in; root application source is present and tracked.
- Existing untracked `README.local.backup` and `instructions.txt` were preserved.
- Known limitations: the UI allows validation without `print_report.json`;
  ordinary publication trusts stored 64-character SHA-256 metadata if present,
  hashing remote bytes only when absent; Edge verification uses HEAD size/ETag
  checks rather than independent byte hashing; legacy raw write handlers remain.
  Do not describe these as stronger safeguards than they are or change them
  without an authorised task.
- No cloud operations or credential reads were performed. Live bucket contents,
  stored credential availability, deployed SQL/Edge state and the 290-design
  artwork set were not independently verified.

This baseline is dated evidence, not a permanent guarantee. Recheck the current
repository and actual test results in future sessions.
