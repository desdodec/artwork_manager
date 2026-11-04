"""Street Face Artwork Manager — Windows release publisher.

Publishes verified artwork to R2 and atomically synchronises held v2
catalogue metadata to Supabase after explicit operator confirmation.
Does not turn on checkout or Prodigi fulfilment.
"""
from __future__ import annotations

import csv
import hashlib
import json
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import keyring
from geography import validate_geography, enrich_postcode_report, write_geography_reports
from print_validation import audit_release
from publisher import DEFAULT_ENDPOINT, DATASET, _api, prepare as prepare_publication, publish as apply_publication, upload_artwork_only
from production_report import read_production_report, ProductionReportError
from recovery_assessment import assess_recovery, format_assessment
from remote_recovery_verification import verify_remote_backups, format_remote_verification

SERVICE = "street-face-artwork-manager"
FILE_RE = re.compile(r"^(?P<number>\d{4})_(?P<street>.+?)_wrap_print(?P<side>_front|_rear)?\.(?P<ext>png|svg)$", re.I)
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^v[1-9][0-9]*$")
SLOTS = {"production_png", "production_svg", "front_mockup", "rear_mockup"}

# The folder is identified from the generator's reviews/<area>_streets... path.
# Example: reviews/hebden_bridge_streets_pa_.../production/production_...
AREA_DIR_RE = re.compile(r"^(?P<area>[a-z0-9_]+)_streets(?:_|$)", re.I)
GEO_FILES = {
    "names_csv": "{area}_street_names.csv",
    "geojson": "{area}_streets.geojson",
    "postcode_report": "{area}_street_names_postcode_report.csv",
}


def detect_release_paths(selected: Path) -> dict:
    """Resolve release data without guessing from arbitrary folder names.

    Walk upwards until a directory inside reviews/ with a recognised generator
    suffix is found. Find OS_Mail_Addresses in its ancestor chain; geography
    always comes from its geojson_edited sibling. Fail closed if absent.
    """
    selected = selected.expanduser()
    if not selected.is_dir():
        raise ValueError(f"Folder does not exist: {selected}")
    for candidate in (selected, *selected.parents):
        if candidate.parent.name.lower() != "reviews":
            continue
        match = AREA_DIR_RE.match(candidate.name)
        if not match:
            continue
        area = match.group("area").lower()
        workspace = next((parent for parent in (candidate, *candidate.parents)
                          if parent.name.lower() == "os_mail_addresses"), None)
        if workspace is None:
            raise ValueError("Cannot find OS_Mail_Addresses in the selected path")
        # Artwork may be in the dated production output, or in the reviews root.
        # Check selected folder first, then the reviews root. Never combine files
        # from separate production runs.
        possible = [selected, candidate] if selected != candidate else [candidate]
        asset_dir = next((path for path in possible
                          if (path / "mockups").is_dir() and (path / "print_candidates").is_dir()), None)
        if asset_dir is None:
            raise ValueError("No mockups/ and print_candidates/ pair found in selected folder "
                             "or its reviews root. Select the folder containing both subfolders.")
        geography_dir = workspace / "geojson_edited"
        paths = {field: geography_dir / filename.format(area=area)
                 for field, filename in GEO_FILES.items()}
        return {"source": asset_dir, "area_designator": area,
                "area_slug": area.replace("_", "-"), "geography": paths}
    raise ValueError("Expected a path under reviews/<area>_streets... (e.g. hebden_bridge_streets_pa_...)")



@dataclass
class Release:
    rows: list[dict]
    issues: list[str]
    unrecognised: list[str]
    files: list[tuple[Path, str]]
    prefix: str


def norm_name(name: str) -> str:
    return " ".join(name.replace("_", " ").split()).casefold()


def validate(source: Path, area: str, version: str) -> Release:
    if not SLUG_RE.fullmatch(area) or not VERSION_RE.fullmatch(version):
        raise ValueError("Use a lowercase area slug (e.g. hebden-bridge) and version (e.g. v2).")
    if not source.is_dir():
        raise ValueError("Choose an existing artwork reviews folder.")
    mockups, prints = source / "mockups", source / "print_candidates"
    if not mockups.is_dir() or not prints.is_dir():
        raise ValueError("Source must contain mockups/ and print_candidates/ folders.")
    prefix = f"street-face/{area}/{version}"
    records: dict[str, dict] = {}
    issues, unrecognised = [], []
    for folder, kind in ((mockups, "mockup"), (prints, "production")):
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            match = FILE_RE.fullmatch(path.name)
            if not match:
                unrecognised.append(str(path))
                continue
            number, street, side, ext = (match.group(k) for k in ("number", "street", "side", "ext"))
            side, ext = (side or "").lower(), ext.lower()
            slot = None
            if kind == "production" and not side:
                slot = f"production_{ext}"
            elif kind == "mockup" and ext == "png" and side in ("_front", "_rear"):
                slot = "front_mockup" if side == "_front" else "rear_mockup"
            if slot is None:
                unrecognised.append(str(path))
                continue
            record = records.setdefault(number, {"number": number, "street_name": street, "assets": {}})
            if norm_name(record["street_name"]) != norm_name(street):
                issues.append(f"NAME MISMATCH {number}: {record['street_name']} / {street}")
            if slot in record["assets"]:
                issues.append(f"DUPLICATE {number} {slot}: {path.name}")
            else:
                record["assets"][slot] = path
                if path.stat().st_size == 0:
                    issues.append(f"EMPTY FILE {number} {slot}: {path}")
    rows, files = [], []
    for number, record in sorted(records.items()):
        missing = SLOTS - record["assets"].keys()
        for slot in sorted(missing):
            issues.append(f"MISSING {number} {record['street_name']}: {slot}")
        row = {"area_slug": area, "release": version, "artwork_number": number,
               "street_name": record["street_name"], "files_complete": not missing}
        for slot in sorted(SLOTS):
            path = record["assets"].get(slot)
            if path:
                folder = "print_candidates" if slot.startswith("production") else "mockups"
                key = f"{prefix}/{folder}/{path.name}"
                row[slot] = key
                files.append((path, key))
            else:
                row[slot] = ""
        rows.append(row)
    if not rows:
        issues.append("No recognised artwork designs found")
    return Release(rows, issues, unrecognised, files, prefix)



def single_design_release(release: Release) -> Release:
    """Select one design with all four local sources, preserving its original keys.

    This does not create a remote upload plan; publication selects three PNGs.
    """
    if release.issues or not release.rows:
        raise ValueError("Validate a complete release before testing uploads.")
    row = min(release.rows, key=lambda item: item["artwork_number"])
    keys = {row[slot] for slot in SLOTS}
    files = [(path, key) for path, key in release.files if key in keys]
    if len(files) != 4 or len({key for _, key in files}) != 4:
        raise ValueError("Selected design does not have exactly four unique artwork files.")
    return Release(rows=[row], issues=[], unrecognised=[], files=files, prefix=release.prefix)


def write_report(release: Release, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "artwork_manifest.json"
    path.write_text(json.dumps({"generated_utc": datetime.now(timezone.utc).isoformat(),
                                "artwork_numbers_are_not_stable_design_ids": True,
                                "items": release.rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    with (folder / "artwork_manifest.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(release.rows[0]) if release.rows else ["artwork_number"])
        writer.writeheader()
        writer.writerows(release.rows)
    (folder / "blocking_issues.txt").write_text("\n".join(release.issues), encoding="utf-8")
    (folder / "unrecognised_files.txt").write_text("\n".join(release.unrecognised), encoding="utf-8")
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_client(account: str, access: str, secret: str):
    if not re.fullmatch(r"[0-9a-f]{32}", account.strip(), re.I):
        raise ValueError("Cloudflare Account ID must be 32 hexadecimal characters.")
    if not access or not secret:
        raise ValueError("Provide the R2 Access Key ID and Secret Access Key.")
    return boto3.client("s3", endpoint_url=f"https://{account.strip()}.r2.cloudflarestorage.com",
                        aws_access_key_id=access, aws_secret_access_key=secret,
                        region_name="auto", config=Config(signature_version="s3v4", retries={"max_attempts": 4}))


def upload_release(client, bucket: str, release: Release, log, stop: threading.Event):
    raise RuntimeError("Use Upload Artwork Only: legacy raw uploads do not verify PNG ownership.")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Street Face Artwork Manager")
        self.geometry("1120x900")
        self.minsize(900, 680)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.release = None
        self.busy = False
        self.source = tk.StringVar()
        self.auto_geography = tk.BooleanVar(value=True)
        self._auto_updating = False
        self.area = tk.StringVar(value="hebden-bridge")
        self.version = tk.StringVar(value="v2")
        self.names_csv = tk.StringVar()
        self.geojson = tk.StringVar()
        self.postcode_report = tk.StringVar()
        self.account = tk.StringVar()
        self.bucket = tk.StringVar()
        self.access = tk.StringVar()
        self.secret = tk.StringVar()
        self.publish_endpoint = tk.StringVar(value=DEFAULT_ENDPOINT)
        self.publish_token = tk.StringVar()
        self.publish_mode = tk.StringVar(value='partial')
        self.mapped_geography = None
        self.full_geography = None
        self._layout()
        self.after(150, self._poll)

    def _field(self, parent, label, variable, browse=False, show=None):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=21).pack(side="left")
        ttk.Entry(row, textvariable=variable, show=show).pack(side="left", fill="x", expand=True, padx=5)
        if browse:
            ttk.Button(row, text="Browse…", command=self._choose).pack(side="left")

    def _layout(self):
        content = ttk.Frame(self, padding=16)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="Street Face Artwork Manager", font=("Segoe UI", 18, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(content, text="1. Select reviews/ folder and validate  ·  2. Check manifest  ·  3. Upload an immutable R2 release").pack(anchor="w")
        source_frame = ttk.LabelFrame(content, text="Artwork release", padding=10)
        source_frame.pack(fill="x", pady=10)
        self._field(source_frame, "Reviews or production folder", self.source, browse=True)
        ttk.Checkbutton(source_frame, text="Auto-detect area and geography from generator path",
                        variable=self.auto_geography, command=self._on_auto_toggle).pack(anchor="w")
        self._field(source_frame, "Area slug", self.area)
        self._field(source_frame, "Release", self.version)
        self._field(source_frame, "Street names CSV", self.names_csv)
        ttk.Button(source_frame, text="Browse CSV…", command=lambda: self._choose_file(self.names_csv, [("CSV files", "*.csv")])).pack(anchor="e")
        self._field(source_frame, "Street GeoJSON", self.geojson)
        ttk.Button(source_frame, text="Browse GeoJSON…", command=lambda: self._choose_file(self.geojson, [("GeoJSON", "*.geojson")])).pack(anchor="e")
        self._field(source_frame, "Postcode report CSV (optional)", self.postcode_report)
        ttk.Button(source_frame, text="Browse postcode report…", command=lambda: self._choose_file(self.postcode_report, [("CSV files", "*.csv")])).pack(anchor="e")
        ttk.Label(source_frame, text="Expected folders: mockups/ and print_candidates/. Numbers identify this export only.").pack(anchor="w")
        credentials = ttk.LabelFrame(content, text="R2 connection (saved in Windows Credential Manager via keyring)", padding=10)
        credentials.pack(fill="x", pady=5)
        self._field(credentials, "Cloudflare Account ID", self.account)
        self._field(credentials, "Bucket name", self.bucket)
        self._field(credentials, "R2 Access Key ID", self.access)
        self._field(credentials, "R2 Secret Access Key", self.secret, show="*")
        self._field(credentials, "Supabase publish endpoint", self.publish_endpoint)
        self._field(credentials, "Publish admin token", self.publish_token, show="*")
        buttons = ttk.Frame(credentials)
        buttons.pack(fill="x", pady=4)
        ttk.Button(buttons, text="Save credentials securely", command=self._save_credentials).pack(side="left", padx=4)
        ttk.Button(buttons, text="Load saved credentials", command=self._load_credentials).pack(side="left", padx=4)
        ttk.Label(content, text="Select a complete collection or partial update; use Upload Artwork Only for R2, or Publish Collection to also update the catalogue.").pack(anchor="w", pady=5)
        actions = ttk.Frame(content)
        actions.pack(fill="x", pady=8)
        self.validate_button = ttk.Button(actions, text="1. Validate Artwork", command=self._validate)
        self.validate_button.pack(side="left", padx=4)
        self.print_button = ttk.Button(actions, text="Validate Print Files", command=self._print_audit, state="disabled")
        self.print_button.pack(side="left", padx=4)
        self.test_button = ttk.Button(actions, text="Legacy upload (disabled)", command=self._test_upload, state="disabled")
        # Legacy test upload retained in code; use Publish Collection for normal releases.
        self.upload_button = ttk.Button(actions, text="3. Upload entire release to R2", command=self._upload, state="disabled")
        # Legacy raw upload deliberately hidden to avoid bypassing Supabase sync.
        self.replace_button = ttk.Button(actions, text="Replace Changed Artwork", command=self._replace, state="disabled")
        # Legacy raw replacement deliberately hidden; Publish handles sync.
        self.preflight_button = ttk.Button(actions, text="Check Supabase Safety (read-only)", command=self._preflight)
        self.preflight_button.pack(side="left", padx=4)
        ttk.Button(actions, text="Assess Recovery Audit (read-only)",
                   command=self._assess_recovery).pack(side="left", padx=4)
        ttk.Button(content, text="Verify R2 Recovery Backups (read-only)",
                   command=self._verify_recovery_remote).pack(anchor="w", padx=8, pady=3)
        publish_modes = ttk.LabelFrame(content, text="Publish Collection — held designs only")
        publish_modes.pack(fill="x", pady=(6, 4))
        ttk.Radiobutton(publish_modes, text="Partial update — keep other streets listed",
                        variable=self.publish_mode, value="partial").pack(side="left", padx=7)
        ttk.Radiobutton(publish_modes, text="Complete collection — unlist omitted streets",
                        variable=self.publish_mode, value="complete").pack(side="left", padx=7)
        self.artwork_button = ttk.Button(content, text="Upload Artwork Only",
                                         command=lambda: self._publish_plan(upload_only=True), state="disabled")
        self.artwork_button.pack(anchor="w", padx=8, pady=3)
        self.publish_button = ttk.Button(publish_modes, text="2. Preview & Publish Collection",
                                         command=self._publish_plan, state="disabled")
        self.publish_button.pack(side="right", padx=7)
        ttk.Button(actions, text="Stop after current file", command=self.cancel.set).pack(side="left", padx=4)
        self.progress = ttk.Progressbar(content, mode="indeterminate")
        self.progress.pack(fill="x", pady=5)
        self.log_box = tk.Text(content, height=13, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)
        ttk.Label(content, text="Publish Collection updates the held read-only catalogue; checkout and Prodigi remain disabled.").pack(anchor="w", pady=8)
        self.source.trace_add("write", self._on_source_changed)
        for variable in (self.source, self.area, self.version, self.names_csv, self.geojson, self.postcode_report):
            variable.trace_add("write", lambda *_: self._invalidate())

    def _on_auto_toggle(self):
        if self.auto_geography.get():
            self._on_source_changed()
        self._invalidate()

    def _on_source_changed(self, *_):
        if not self.auto_geography.get() or self._auto_updating:
            return
        self._auto_updating = True
        try:
            selected = Path(self.source.get().strip())
            if not selected.is_dir():
                return
            detected = detect_release_paths(selected)
            self.area.set(detected["area_slug"])
            for field, path in detected["geography"].items():
                getattr(self, field).set(str(path))
            self._log("Detected area: " + detected["area_designator"])
            self._log("Artwork folder: " + str(detected["source"]))
            for field, path in detected["geography"].items():
                self._log(f"{field}: {path}" + (" [FOUND]" if path.is_file() else " [MISSING]"))
        except ValueError as exc:
            self._log("Auto-detection: " + str(exc))
        finally:
            self._auto_updating = False

    def _choose(self):
        folder = filedialog.askdirectory(title="Choose artwork reviews folder")
        if folder:
            self.source.set(folder)

    def _choose_file(self, variable, kinds):
        path = filedialog.askopenfilename(filetypes=kinds)
        if path:
            variable.set(path)

    def _invalidate(self):
        self.release = None
        self.mapped_geography = None
        self.full_geography = None
        self.upload_button.configure(state="disabled")
        self.replace_button.configure(state="disabled")
        self.test_button.configure(state="disabled")
        self.print_button.configure(state="disabled")
        self.publish_button.configure(state="disabled")
        self.artwork_button.configure(state="disabled")

    def _log(self, message):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", str(message) + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _save_credentials(self):
        try:
            for field, variable in (("account", self.account), ("bucket", self.bucket),
                                    ("access", self.access), ("secret", self.secret),
                                    ("publish_token", self.publish_token)):
                if not variable.get().strip():
                    raise ValueError(f"Missing {field}")
                keyring.set_password(SERVICE, field, variable.get().strip())
            self._log("R2 credentials saved in OS credential store; not in project files.")
        except Exception as exc:
            messagebox.showerror("Credential storage failed", str(exc))

    def _load_credentials(self):
        try:
            for field, variable in (("account", self.account), ("bucket", self.bucket),
                                    ("access", self.access), ("secret", self.secret),
                                    ("publish_token", self.publish_token)):
                variable.set(keyring.get_password(SERVICE, field) or "")
            self._log("Loaded stored R2 settings.")
        except Exception as exc:
            messagebox.showerror("Credential loading failed", str(exc))

    def _work(self, fn, continuation=False):
        if self.busy and not continuation:
            return
        self.busy = True
        self.validate_button.configure(state="disabled")
        self.upload_button.configure(state="disabled")
        self.replace_button.configure(state="disabled")
        self.test_button.configure(state="disabled")
        self.print_button.configure(state="disabled")
        self.publish_button.configure(state="disabled")
        self.artwork_button.configure(state="disabled")
        self.preflight_button.configure(state="disabled")
        if not continuation:
            self.cancel.clear()
        self._reset_progress()
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        def runner():
            handoff = False
            try:
                handoff = fn() is True
            except Exception as exc:
                self.events.put(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                if not handoff:
                    self.events.put(("done", None))
        threading.Thread(target=runner, daemon=True).start()

    def _validate(self):
        source, area, version = Path(self.source.get()), self.area.get().strip(), self.version.get().strip()
        csv_path, geo_path = Path(self.names_csv.get()), Path(self.geojson.get())
        postcode_path = Path(self.postcode_report.get()) if self.postcode_report.get().strip() else None
        if self.auto_geography.get():
            try:
                detected = detect_release_paths(source)
                source = detected["source"]
                area = detected["area_slug"]
                csv_path = detected["geography"]["names_csv"]
                geo_path = detected["geography"]["geojson"]
                postcode_path = detected["geography"]["postcode_report"]
            except ValueError as exc:
                messagebox.showerror("Cannot auto-detect release", str(exc))
                return
            missing = [str(p) for p in (csv_path, geo_path, postcode_path) if not p.is_file()]
            if missing:
                messagebox.showerror("Missing geographic input", "These expected files are missing:\n" + "\n".join(missing))
                return
        def work():
            release = validate(source, area, version)
            if not csv_path.is_file() or not geo_path.is_file():
                raise ValueError("Select BOTH a street-names CSV and a GeoJSON before validating this release.")
            identity_path = source / 'artwork_identities.csv'
            mapped, geo_issues, geo = validate_geography(release.rows, csv_path, geo_path,
                                                        identity_path if identity_path.is_file() else None)
            self.events.put(('log', 'Using ' + ('stable street_key identities from artwork_identities.csv'
               if identity_path.is_file() else 'original artwork indices (no artwork_identities.csv present)')))
            if postcode_path is not None and not geo_issues:
                mapped, postcode_issues = enrich_postcode_report(mapped, postcode_path)
                geo_issues.extend(postcode_issues)
            release.issues.extend(geo_issues)
            if not geo_issues and len(mapped) == len(release.rows):
                release.rows = mapped

            # Generator selection is authoritative. A complete release may omit
            # deliberately excluded designs; don't infer exclusion from filenames.
            # Check the written street_keys against the validated geography before
            # permitting any R2 upload or catalogue modification.
            release.production_report = None
            report_candidates = [
                source / "print_report.json",
                source / "print_candidates" / "print_report.json",
                source / "reports" / "print_report.json",
                source.parent / "print_report.json",
            ]
            report_path = next((path for path in report_candidates if path.is_file()), None)
            if report_path is not None:
                try:
                    selection = read_production_report(report_path)
                    expected_keys = set(selection["selected_keys"])
                    actual_keys = {row.get("street_key") for row in mapped}
                    if len(actual_keys) != len(mapped) or expected_keys != actual_keys:
                        missing = expected_keys - actual_keys
                        unexpected = actual_keys - expected_keys
                        raise ProductionReportError(
                            f"Generator report differs from validated artwork: "
                            f"{len(missing)} missing, {len(unexpected)} unexpected street_keys"
                        )
                    release.production_report = selection
                    self.events.put(("log", f"Generator report confirmed: "
                        f"{selection['processed']} processed, "
                        f"{selection['written']} production designs, "
                        f"{selection['excluded']} explicitly not for production."))
                except (ValueError, KeyError, OSError) as error:
                    release.issues.append(f"Generator print_report.json: {error}")
            else:
                self.events.put(("log", "No print_report.json found beside this production run. "
                    "Artwork validation uses filenames and geography only."))

            report_dir = source.parent / "artwork_reports" / f"{area}-{version}"
            manifest = write_report(release, report_dir)
            write_geography_reports(mapped, geo_issues, geo, report_dir)
            self.events.put(("log", f"Geographic matches: {len(mapped)}/{len(release.rows)} | Geometry issues: {len(geo_issues)}"))
            self.events.put(("log", f"Geographic exports: {report_dir}"))
            self.events.put(("log", f"Location review flags: {sum(bool(row['location_review']) for row in mapped)} (review separately before catalogue publication)"))
            self.events.put(("log", f"PARTIAL UPDATE SUPPORTED: {len(release.rows)} artwork designs from the original catalogue (not necessarily all 311)."))
            self.events.put(("log", f"Designs: {len(release.rows)} | Local sources (PNG/SVG): {len(release.files)} | Remote PNGs: {len(release.rows) * 3} | Blocking issues: {len(release.issues)} | Unrecognised: {len(release.unrecognised)}"))
            self.events.put(("log", f"Manifest: {manifest}"))
            if release.issues:
                for issue in release.issues[:20]:
                    self.events.put(("log", issue))
                self.events.put(("log", "UPLOAD BLOCKED: inspect blocking_issues.txt"))
            else:
                self.events.put(("log", "VALIDATION PASSED. Review the manifest before uploading."))
            self.events.put(("validated", (release, source, area, version, mapped, geo)))
        self._work(work)

    def _print_audit(self):
        if self.release is None or self.release.issues:
            messagebox.showwarning("Validate first", "Run Validate and export manifest before checking print files.")
            return
        release = self.release
        source = self.validated_source
        report_dir = source.parent / "artwork_reports" / f"{self.validated_area}-{self.validated_version}"
        def work():
            self.events.put(("log", "Read-only local PNG/SVG print audit started; no R2 or Supabase changes."))
            outcome = audit_release(release, report_dir, lambda line: self.events.put(("log", line)), self.cancel)
            if outcome is not None:
                csv_file, summary_file, counts = outcome
                self.events.put(("log", f"PRINT AUDIT: {counts['TECHNICAL_PASS']} technical passes, {counts['REVIEW']} review, {counts['FAIL']} failed"))
                self.events.put(("log", f"Report: {csv_file}"))
                self.events.put(("log", f"Summary: {summary_file}"))
                self.events.put(("log", "Technical validation passed. Artwork meets the checked Prodigi print-file requirements. Physical product proof approval remains outstanding."))
        self._work(work)

    def _test_upload(self):
        messagebox.showwarning("Use publication preview",
            "Use Upload Artwork Only or Publish Collection for verified PNG destinations.")

    def _upload(self):
        messagebox.showwarning("Use publication preview",
            "Use Upload Artwork Only or Publish Collection for verified PNG destinations.")

    def _replace(self):
        messagebox.showwarning("Use publication preview",
            "Use Upload Artwork Only or Publish Collection for verified PNG destinations.")

    def _verify_recovery_remote(self):
        """Explicit user-initiated R2 backup byte reads; never writes or restores."""
        chosen = filedialog.askopenfilename(
            title="Select saved publication_run_<operation_id>.json audit",
            filetypes=[("Publication audit", "publication_run_*.json"), ("JSON files", "*.json")])
        if not chosen:
            return
        account, bucket, access, secret = (x.get().strip() for x in
                                          (self.account, self.bucket, self.access, self.secret))
        if not all((account, bucket, access, secret)):
            messagebox.showerror("R2 connection missing",
                                 "Load your R2 credentials and bucket name first.")
            return
        if bucket != "mug-print-assets":
            messagebox.showerror("Unexpected R2 bucket",
                                 "Recovery verification is restricted to the Hebden Bridge production bucket.")
            return
        if not messagebox.askyesno(
            "READ-ONLY R2 verification",
            "Read backed-up R2 objects in mug-print-assets and verify their real bytes "
            "against the selected journal?\n\n"
            "No uploads, restores, deletions or database changes will occur."):
            return
        def work():
            client = make_client(account, access, secret)
            result = verify_remote_backups(Path(chosen), client, bucket)
            for line in format_remote_verification(result).splitlines():
                self.events.put(("log", line))
            if not result["all_recorded_backups_verified"]:
                self.events.put(("log",
                    "WARNING: One or more recorded backups could not be verified. "
                    "Do not attempt restoration."))
        self._work(work)

    def _assess_recovery(self):
        """Inspect a local publication journal; do not contact R2 or Supabase."""
        chosen = filedialog.askopenfilename(
            title="Select publication_run_<operation_id>.json audit",
            filetypes=[("Publication audit", "publication_run_*.json"), ("JSON files", "*.json")])
        if not chosen:
            return
        try:
            result = assess_recovery(Path(chosen))
            for line in format_assessment(result).splitlines():
                self._log(line)
            if result["missing_backup_keys"] or result["concerns"]:
                messagebox.showwarning("Recovery audit requires attention",
                    "The local recovery journal has missing or inconsistent records. "
                    "Review the log. No recovery actions were attempted.")
        except Exception as exc:
            messagebox.showerror("Recovery assessment failed", str(exc))

    def _preflight(self):
        """Read only the held-catalogue safety state; never upload or publish."""
        endpoint = self.publish_endpoint.get().strip()
        token = self.publish_token.get().strip()
        if not endpoint or len(token) < 32:
            messagebox.showwarning("Missing publishing credentials",
                "Load your saved publishing token and check the endpoint first.")
            return
        def work():
            response = _api(endpoint, token, {"mode": "preflight", "dataset_id": DATASET})
            listed = response.get("currently_listed_keys")
            if not isinstance(listed, list):
                raise RuntimeError("Preflight response omitted currently_listed_keys")
            self.events.put(("log",
                "READ-ONLY SUPABASE PREFLIGHT: "
                f"safe_to_upload={response.get('safe_to_upload')}; "
                f"existing_held_designs={response.get('existing_held_designs')}; "
                f"checkout_order_lines={response.get('checkout_order_lines')}; "
                f"currently_listed={len(listed)}. "
                "No R2 uploads or database changes requested."))
            self.events.put(("log",
                "NOTE: checkout_enabled and prodigi_enabled in this API response "
                "are fixed assertions, not independently verified operational states."))
        self._work(work)

    def _reset_progress(self):
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=1, value=0)

    def _publish_plan(self, upload_only=False):
        if self.release is None or self.release.issues or not self.mapped_geography:
            messagebox.showwarning("Validate first", "Validate the selected production folder and geography first.")
            return
        release=self.release
        mapped=self.mapped_geography
        geo=self.full_geography
        mode="partial" if upload_only else self.publish_mode.get()
        endpoint=self.publish_endpoint.get().strip()
        token=self.publish_token.get().strip()
        account,bucket,access,secret=(x.get().strip() for x in
                                       (self.account,self.bucket,self.access,self.secret))
        report_dir=self.validated_source.parent / "artwork_reports" / f"{self.validated_area}-{self.validated_version}"
        self._reset_progress()
        preview_text = (
            f"Compare artwork for {len(release.rows)} designs in bucket {bucket}?\n\n"
            "This step checks print files, compares R2 objects and reads Supabase safety status.\n"
            "No artwork uploads or catalogue changes yet."
        ) if upload_only else (
            f"Prepare a {mode.upper()} release of {len(release.rows)} designs?\n\n"
            "A COMPLETE release unlists omitted streets. A PARTIAL release keeps other listings.\n"
            "This step compares files and reads Supabase safety status."
        )
        if not messagebox.askyesno("PREVIEW ONLY - no changes yet", preview_text):
            return
        def work():
            self.events.put(("log","Checking local print files first..."))
            result=audit_release(release,report_dir,
                lambda msg:self.events.put(("log",msg)),self.cancel)
            if result is None: raise RuntimeError("Print audit cancelled")
            _,_,counts=result
            if counts["FAIL"] or counts["REVIEW"]:
                raise RuntimeError(f"Print audit blocked release: {counts}")
            client=make_client(account,access,secret)
            plan=prepare_publication(release,mapped,geo,client,bucket,endpoint,token,
                mode,report_dir,lambda msg:self.events.put(("log",msg)),self.cancel,
                upload_only=upload_only)
            self.events.put(("confirm_artwork" if upload_only else "confirm_publish",
                             (plan,client,bucket,endpoint,token,report_dir)))
            return True  # Keep the operation reserved through the confirmation dialog.
        self._work(work)

    def _apply_publish_after_confirmation(self, values, upload_only=False):
        plan,client,bucket,endpoint,token,report_dir=values
        def work():
            action = upload_artwork_only if upload_only else apply_publication
            options = {"progress": lambda current, total: self.events.put(("progress", (current, total)))}
            result=action(plan,client,bucket,endpoint,token,report_dir,
                lambda msg:self.events.put(("log",msg)),self.cancel, **options)
            self.events.put(("log",json.dumps(result,indent=2)))
        self._work(work, continuation=True)

    def _poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "log":
                    self._log(value)
                elif event == "progress":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(1, value[1]), value=value[0])
                elif event == "error":
                    self._reset_progress()
                    self._log("ERROR: " + value)
                    messagebox.showerror("Operation failed", value)
                elif event == "confirm_replace":
                    self._log("Legacy replacement blocked; prepare a fresh PNG publication preview.")
                    self.events.put(("done", None))
                elif event == "confirm_artwork":
                    self._reset_progress()
                    plan, _, bucket, *_ = value
                    summary = plan['summary']
                    approved = messagebox.askyesno("CONFIRM - UPLOAD ARTWORK ONLY",
                        f"Target R2 bucket: {bucket}\n"
                        f"New files to upload: {summary.get('NEW', 0)}\n"
                        f"Existing files to overwrite: {summary.get('REPLACE', 0)}\n"
                        f"Identical files to skip and verify: {summary.get('SAME', 0)}\n\n"
                        "Three PNGs per street are uploaded; the SVG stays local.\n"
                        "Replacements are backed up first. All PNG bytes will be verified.\n"
                        "Supabase catalogue records will NOT change; existing catalogue metadata may become stale.\n"
                        "Publishing remains a separate action. Upload this artwork now?")
                    if approved:
                        self._apply_publish_after_confirmation(value, upload_only=True)
                    else:
                        self._log("Artwork upload cancelled; no artwork uploaded by this action.")
                        self.events.put(("done", None))
                elif event == "confirm_publish":
                    self._reset_progress()
                    plan=value[0]
                    summary=plan['summary']
                    excluded=len(plan['excluded_keys'])
                    generator_report=getattr(self.release, 'production_report', None)
                    generator_line=(f"Generator marked NOT FOR PRODUCTION: "
                        f"{generator_report['excluded']}\n" if generator_report else
                        "Generator exclusion report: not found\n")
                    approved=messagebox.askyesno(
                        "FINAL CONFIRMATION — PUBLISH COLLECTION",
                        f"Mode: {plan['mode'].upper()}\n"
                        f"Designs in this folder: {plan['design_count']}\n"
                        "Three remote PNGs per street; production SVG stays local.\n"
                        f"{generator_line}"
                        f"New R2 files: {summary.get('NEW',0)}\n"
                        f"Changed R2 files: {summary.get('REPLACE',0)}\n"
                        f"Unchanged R2 files: {summary.get('SAME',0)}\n"
                        f"Previously listed streets to unlist: {excluded}\n\n"
                        "This will overwrite changed R2 files and update the Supabase "
                        "read-only catalogue if all verifications pass.\n"
                        "Checkout and Prodigi remain disabled.\n\n"
                        "Proceed with this exact release?"
                    )
                    if approved:
                        self._apply_publish_after_confirmation(value)
                    else:
                        self._log("Publication cancelled; no artwork uploaded by this action.")
                        self.events.put(("done", None))
                elif event == "validated":
                    self.release, self.validated_source, self.validated_area, self.validated_version, self.mapped_geography, self.full_geography = value
                elif event == "done":
                    self.busy = False
                    self._reset_progress()
                    self.validate_button.configure(state="normal")
                    state = "normal" if self.release and not self.release.issues else "disabled"
                    self.upload_button.configure(state=state)
                    self.test_button.configure(state=state)
                    self.print_button.configure(state=state)
                    self.replace_button.configure(state=state)
                    self.publish_button.configure(state=state)
                    self.artwork_button.configure(state=state)
                    self.preflight_button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(150, self._poll)


if __name__ == "__main__":
    App().mainloop()
