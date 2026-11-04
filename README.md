# Street Face Artwork Manager

Windows desktop application for validating, reviewing and publishing Street Face print-on-demand mug artwork to Cloudflare R2 and a held Supabase catalogue.

## Project relationship

- **Image generator**: creates print PNG, SVG, front/rear mockups and machine-readable review reports.
- **Artwork Manager** (this repository): reads the latest output, validates it, safely uploads selected production assets, and synchronises the read-only catalogue.
- **Shop** (`desdodec/shop`): displays the current catalogue. Payment and live fulfilment approvals remain separate.

## Source of truth

The generator's `print_report.json` contains `processed`, `written`, the explicit `skipped` decisions, and `streets[].location.street_key`. Artwork names and four-digit artwork numbers are presentation details, **not primary keys**.

For the 3 November 2026 Hebden Bridge production run, the report says **311 processed, 290 written, 21 explicitly skipped as not_for_production**. The selection contains 290 designs; all 311 permanent design identities remain preserved, including the 21 excluded designs held. Full release mode unlists previously listed omitted keys; that count need not be 21. A partial release should preserve omitted catalogue entries.

## Important safety constraints

- Publication must never enable checkout, alter historical orders or initiate Prodigi fulfilment.
- Hold/pending gates apply until the physical proof has been approved.
- A complete release cannot be inferred merely because some files are absent; use explicit release mode and review decisions.
- Never delete or clear the R2 bucket just to publish.
- Never store credentials, password-manager exports, `.env` files, generated artwork or run-specific production reports in Git.
- Production updates must have a preflight, a file-level audit and an independently verifiable database result.

## Setup

Use the existing Windows Python environment and `requirements.txt`; launch `app.py`. Application source is tracked here.

## Release workflow

1. Generate a production folder and report files.
2. Review selection from `print_report.json` and validate artwork.
3. Preview a complete or partial release, including excluded street keys.
4. Upload required R2 objects, verify and commit the held catalogue transaction.
5. Confirm the read-only preview; separately approve physical samples and sales.

## Artwork verification

Before requesting the catalogue commit, the desktop streams and SHA-256-hashes
every required R2 PNG object, including unchanged PNGs. It records
the actual size, hash and ETag in the publication audit and sends those values
to the Edge Function. The Edge Function uses HEAD requests to reject missing
objects or changed sizes/ETags before calling SQL; it downloads no artwork bodies.
Remote PNG evidence requires at least 1,000 bytes. SVG remains part of local print validation.

This moves the download cost to the desktop: a collection containing roughly
1 GB of artwork still requires roughly 1 GB of verification downloads per publish
attempt, even when no uploads are needed. Preview comparisons without checksum
metadata and replacement-backup verification can add downloads. Streaming uses
1 MiB chunks rather than buffering the collection. Edge still makes one HEAD
request per artwork object (870 for a 290-design collection).

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


## Local sources and remote publication

Each selected street requires four local sources: front mockup PNG, rear mockup
PNG, production wrap PNG and production SVG. Publication plans only the three
PNGs: 290 streets produce 870 destinations, while 1,160 local sources are validated.
SVG is never uploaded, backed up, overwritten or remotely verified by publication;
existing remote SVG objects are retained. Legacy raw write actions are blocked.

The authenticated Edge Function uses the read-only
`street_face_upload_snapshot_v1()` PostgreSQL RPC to obtain a consistent
statement-level ownership snapshot. The desktop verifies selected destination
ownership against that snapshot. The snapshot does not prevent concurrent
catalogue changes between verification and an R2 write. Each PNG destination must
have exactly one owner matching the permanent street, held design, asset role,
GLOBAL-MUG-W SKU and version 1. The desktop saves those assignments and rechecks
them after confirmation before backups/uploads. A missing assignment blocks even
a new destination; filenames never establish ownership. Commit checks ownership too.

Publish Collection allows at most **1,000 selected designs per request**, checked
locally during preparation before publication confirmation or any remote calls.
This is separate from the 2,000-record ownership snapshot ceiling: the inventory
may include more designs than the current selection.

Upload Artwork Only intentionally has no 1,000-design publication request limit
because it never sends a catalogue commit. Its full ownership checks and snapshot
ceiling still apply.

Upload Artwork Only uses the same preview, confirmation, backups, crash journal
and byte verification, but sends only read-only Supabase preflight requests; it
never requests a catalogue transaction. Existing catalogue metadata may become
stale after overwrites. A failed client upload can have written remotely: journals
retain upload intent and report the uncertain destination, including new objects.
Retry always requires a fresh preview. The preview reservation spans confirmation
and upload, and cancellation is retained across that handoff.

The shop uses three PNG assets, but end-to-end Prodigi submission is not implemented.
This change does not establish fulfilment readiness. Before deploying the updated
Edge Function or using the new ownership checks, `proposed_upload_snapshot.sql`
must be reviewed, installed and its permissions verified in the target Supabase
project.

## Shop importer key compatibility

Compared offline with the owner-supplied `keys` and `safe_name` source from
`desdodec/shop/catalogue_import/catalogue_import_validator.py`, commit
`1dc8ba49ac9916f1ce2b95a5276b6cf022487974`. No shop checkout or GitHub access was used.
For the same dataset argument, **all three destination conventions differ**:

| Role | Artwork Manager | Supplied shop importer |
| --- | --- | --- |
| Front PNG | `<dataset>/mockups/NNNN_<street>_wrap_print_front.png` | `front/<dataset>_<street_id>_<safe_name>_front.png` |
| Rear PNG | `<dataset>/mockups/NNNN_<street>_wrap_print_rear.png` | `rear/<dataset>_<street_id>_<safe_name>_rear.png` |
| Production PNG | `<dataset>/print_candidates/NNNN_<street>_wrap_print.png` | `prodigi_h_mug_w/<dataset>_<street_id>_<safe_name>.png` |

Artwork Manager preserves the original filename, including its artwork number,
spaces and punctuation. The importer uses permanent `street_id`, replaces
whitespace with underscores, strips trailing periods and removes characters
outside ASCII letters/digits, underscore, ASCII apostrophe and hyphen. Slashes
inside `dataset_id` remain unchanged in the importer's generated key. Its
production filename also omits `_wrap_print`.

For dataset `street-face/hebden-bridge/v2`, street ID `permanent-street-93`,
artwork number `0007` and name `St. George's Road.`, the front keys are:

- Artwork Manager: `street-face/hebden-bridge/v2/mockups/0007_St. George's Road._wrap_print_front.png`
- Supplied importer: `front/street-face/hebden-bridge/v2_permanent-street-93_St_George's_Road_front.png`

No destinations are renamed to resolve this discrepancy. Existing stored PNG
ownership assignments must match the proposed Manager keys exactly; incompatible
assignments block publication before backups/uploads. The supplied key function
alone does not establish whether other shop code uses stored asset columns instead
of these generated paths. Reconciliation is required before claiming compatibility
with this importer; it is outside the authorised key-preserving implementation.
The offline comparison regression tests exercise actual Manager folder validation
against the supplied reference and require that Manager keys remain unchanged.
# Local review prerequisite: consistent upload ownership

The upload safety checks now require the authenticated preflight response to
include `ownership_snapshot_version: 1`. Older Edge responses are blocked before
any R2 write. There is no paginated-query fallback.

`proposed_upload_snapshot.sql` is an **unexecuted proposal**. Before enabling this
version, separately authorise, review and install its service-role-only read RPC
`public.street_face_upload_snapshot_v1()` against the existing catalogue schema,
then deploy the matching Edge source. The RPC returns ownership, held/order safety
and listed keys in one SQL statement and one scalar JSON result. Local fake-client
tests do not validate the actual database schema, permissions or deployed service.

Uploads recheck the authenticated snapshot before each backup copy, after backups,
and immediately before each artwork upload. These checks do not lock independently
operating catalogue writers. The existing publication transaction can create
designs and create/update asset assignments; its transaction lock does not span
desktop R2 uploads. Legacy replacement code can also change R2 bytes, though it
does not change database ownership. An ownership or object change can still race
between a check and a write. Stop prevents initiating subsequent writes after it
is observed at the final write boundary; an already-started request may finish.


Snapshot review corrections (local proposal): all four required production tables
and service_role SELECT permissions have been verified externally. No further
schema investigation is required for this proposal. Deployment still requires
separate authorisation. The installer must be able to assign ownership to the
trusted postgres administrative role, revoke historical grants, and verify its
schema USAGE, SELECT on all four tables and BYPASSRLS/superuser execution context.
SECURITY DEFINER executes as postgres, not service_role. The proposal verifies
owner, definer flag, empty search_path and direct ACL allowlist in its transaction.
It clears all historical direct grantees (including cascading delegated grants)
before granting only postgres and service_role; role memberships and superuser
access must remain restricted to trusted administrators. No credentials belong
in the SQL. Static offline tests cannot execute or certify these SQL assertions.

The snapshot ceiling is 2,000 designs, 2,000 asset records and 2,000 listed keys,
plus 8 MiB of JSON. This allows growth from 311 designs. Bounded aggregates read
at most 2,001 records while full counts detect overflow; overflow raises SQLSTATE
54000 instead of returning a truncated success. Edge rejects overflow or malformed
snapshots with HTTP 503; desktop also bounds response bytes and 6,000 ownership
assignments and refuses uploads on errors. Raising limits needs coordinated review.

`ownership_complete` describes database snapshot coverage only. It does not
certify a globally consistent catalogue. Every selected destination must resolve
to exactly one valid owner with matching permanent street, role, held status,
product and version; all three roles must share a design and asset identity.
Unrelated designs without assets are allowed. Conflicting unrelated ownership
is returned as a warning and logged during preview; selected conflicts block.
Malformed transport data blocks safely. These checks do not prove that no other
catalogue inconsistencies exist. The ownership-check-to-R2-write interval remains
non-atomic: another writer can change ownership after a check. Backups, repeated
checks, cancellation boundaries, journals and recovery evidence remain necessary.
The three-PNG upload and local SVG validation workflow is retained.
