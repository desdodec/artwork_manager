"""Street Face held-collection publisher. No Prodigi, Stripe or public checkout actions.

Uploads the selected, validated release and invokes the secured Supabase catalogue
transaction only after every advertised R2 object is verified. Reuse original R2
object keys; never delete. The current implementation targets Hebden Bridge v2.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from botocore.exceptions import ClientError

MAX_PUBLISH_DESIGNS = 1000
DATASET = 'street-face/hebden-bridge/v2'
DEFAULT_ENDPOINT = 'https://ngmdsmmvdnbnxeozpfej.supabase.co/functions/v1/street-face-publish-v2'
LOCAL_SLOTS = ('front_mockup', 'rear_mockup', 'production_png', 'production_svg')
SLOTS = LOCAL_SLOTS[:3]
ROLES = dict(zip(('front', 'rear', 'production'), SLOTS))


def _api(endpoint: str, token: str, body: dict, timeout: int = 35) -> dict:
    payload = json.dumps(body, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    request = Request(endpoint, data=payload, headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
    }, method='POST')
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(8388609)
            if len(raw) > 8388608:
                raise RuntimeError('Ownership response limit exceeded; uploads blocked')
            return json.loads(raw.decode('utf-8'))
    except HTTPError as error:
        detail = error.read(3000).decode('utf-8', errors='replace')
        raise RuntimeError(f'Publish API HTTP {error.code}: {detail}') from None
    except URLError as error:
        raise RuntimeError(f'Publish API not reachable: {error.reason}') from None


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _r2_head(client, bucket: str, key: str):
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        code = str(error.response.get('Error', {}).get('Code', ''))
        if code in ('404', 'NoSuchKey', 'NotFound'):
            return None
        raise


def _remote_sha(client, bucket: str, key: str, head) -> str:
    stored = (head.get('Metadata') or {}).get('sha256')
    if stored and len(stored) == 64:
        return stored
    # Historical objects may lack sha256 metadata. ETag is NOT the SHA-256.
    stream = client.get_object(Bucket=bucket, Key=key)['Body']
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(block)
    return h.hexdigest()


def _verify_artwork(client, bucket, op, head, stop):
    """Hash actual bytes, binding the stream to the observed object version."""
    key = op['key']
    etag = str((head or {}).get('ETag', '')).strip('"')
    if head is None or head.get('ContentLength') != op['local_bytes'] or not re.fullmatch(
            r'[A-Za-z0-9_-]{8,128}', etag):
        raise RuntimeError(f'Upload verification failed: {key}')
    response = client.get_object(Bucket=bucket, Key=key, IfMatch=head['ETag'])
    stream = response['Body']
    digest = hashlib.sha256()
    size = 0
    try:
        if response.get('ContentLength') != op['local_bytes'] or \
                str(response.get('ETag', '')).strip('"') != etag:
            raise RuntimeError(f'Upload verification failed: {key}')
        while True:
            if stop.is_set():
                raise RuntimeError('Stopped during verification; database publication not attempted.')
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
        if stop.is_set():
            raise RuntimeError('Stopped during verification; database publication not attempted.')
        if size != op['local_bytes'] or digest.hexdigest() != op['local_sha256']:
            raise RuntimeError(f'Upload verification failed: {key}')
    finally:
        stream.close()
    return {'bytes': size, 'sha256': digest.hexdigest(), 'etag': etag}


def ownership_for(items, server):
    """Require all snapshot rows and one exact valid owner for each selected PNG."""
    if server.get('ownership_snapshot_version') != 1 or server.get('ownership_complete') is not True or not isinstance(server.get('png_ownership'), list):
        raise RuntimeError('PNG ownership inventory is missing or incomplete')
    if len(server['png_ownership']) > 6000:
        raise RuntimeError('Ownership snapshot limit exceeded; uploads blocked')
    owners = {}
    for row in server['png_ownership']:
        if not isinstance(row, dict) or not isinstance(row.get('key'), str) or not row['key']:
            raise RuntimeError('Malformed PNG ownership assignment')
        owners.setdefault(row['key'], []).append(row)
    selected = {}
    identities = {}
    for item in items:
        for role in ROLES:
            key = item[role + '_key']
            folder = 'mockups/' if role in ('front', 'rear') else 'print_candidates/'
            if not isinstance(key, str) or not key.startswith(DATASET + '/' + folder) or not key.endswith('.png') or '..' in key or '\\' in key:
                raise RuntimeError(f'Unsafe PNG destination: {key}')
            matches = owners.get(key, [])
            if key in selected or len(matches) != 1:
                raise RuntimeError(f'PNG destination collision or missing ownership: {key}')
            owner = matches[0]
            if any(not isinstance(owner.get(k), str) or not owner[k].strip() for k in
                   ('dataset_id', 'street_key', 'design_id', 'asset_id', 'role', 'product_sku', 'status')) or type(owner.get('version')) is not int:
                raise RuntimeError('Malformed selected PNG ownership assignment')
            if (owner['dataset_id'], owner['street_key'], owner['role'], owner['product_sku'],
                    owner['version'], owner['status']) != (DATASET, item['street_key'], role, 'GLOBAL-MUG-W', 1, 'hold'):
                raise RuntimeError(f'PNG destination has a different owner or asset role: {key}')
            identity = (owner['design_id'], owner['asset_id'])
            if item['street_key'] in identities and identities[item['street_key']] != identity:
                raise RuntimeError('Conflicting selected permanent identity or asset assignment')
            identities[item['street_key']] = identity
            selected[key] = dict(owner)
    return selected


def prepare(release, mapped: list[dict], geo: dict, client, bucket: str,
            endpoint: str, token: str, mode: str, report_dir: Path, log, stop,
            *, upload_only: bool = False) -> dict:
    # This is a request limit, independent of the full ownership inventory.
    if not upload_only and len(mapped) > MAX_PUBLISH_DESIGNS:
        raise ValueError(
            f'Publish Collection allows at most {MAX_PUBLISH_DESIGNS:,} selected designs per request; '
            f'{len(mapped):,} selected. Select fewer designs and prepare again.')
    if mode not in ('complete', 'partial'):
        raise ValueError('Choose complete or partial update explicitly')
    if release.issues or not mapped or len(mapped) != len(release.rows):
        raise ValueError('Artwork and geographical validation must pass first')
    if release.prefix != 'street-face/hebden-bridge/v2':
        raise ValueError('v10 publishing is currently enabled only for held Hebden Bridge v2')
    if not token or len(token) < 32:
        raise ValueError('Configure the separate Street Face publish token')
    if stop.is_set():
        raise RuntimeError('Cancelled')
    server = _api(endpoint, token, {'mode': 'preflight', 'dataset_id': DATASET})
    if not server.get('safe_to_upload'):
        raise RuntimeError('Supabase did not confirm that this held release is safe to update')
    by_key = {(f.get('properties') or {}).get('street_key'): f
              for f in geo.get('features', [])}
    by_object = {key: path for path, key in release.files}
    operations = []
    items = []
    file_keys = set()
    for row in mapped:
        key = row['street_key']
        feature = by_key.get(key)
        if not feature or not feature.get('geometry'):
            raise RuntimeError(f'Missing geometry for {key}')
        for slot in LOCAL_SLOTS:
            if row.get(slot) not in by_object:
                raise RuntimeError(f'Missing local source asset: {slot} for {key}')
        asset_keys = {slot: row[slot] for slot in SLOTS}
        for key_in_bucket in asset_keys.values():
            if key_in_bucket in file_keys:
                raise RuntimeError(f'Duplicate R2 destination: {key_in_bucket}')
            file_keys.add(key_in_bucket)
            if key_in_bucket not in by_object:
                raise RuntimeError(f'Artwork not present locally: {key_in_bucket}')
        items.append({
            'street_key': key, 'street_name': row['street_name'],
            'postcode_district': row.get('postcode_district', ''),
            'geometry': feature['geometry'],
            'front_key': asset_keys['front_mockup'],
            'rear_key': asset_keys['rear_mockup'],
            'production_key': asset_keys['production_png'],
        })
    if {key for key in by_object if key.endswith('.png')} != file_keys:
        raise RuntimeError('Unexplained PNG keys in local publication files')
    ownership = ownership_for(items, server)
    for warning in server.get('ownership_warnings', []):
        log('Ownership warning: ' + str(warning))
    by_object = {item[role + '_key']: by_object[item[role + '_key']] for item in items for role in ROLES}
    for index, (key, source) in enumerate(by_object.items(), 1):
        if stop.is_set():
            raise RuntimeError('Cancelled before publishing')
        size = source.stat().st_size
        local_sha = _digest(source)
        remote = _r2_head(client, bucket, key)
        remote_etag = str(remote.get('ETag', '')).strip('"') if remote else None
        remote_sha = _remote_sha(client, bucket, key, remote) if remote else None
        status = 'NEW' if remote is None else ('SAME' if remote_sha == local_sha and
            remote.get('ContentLength') == size else 'REPLACE')
        operations.append({'source': str(source), 'key': key, 'action': status,
                           'local_sha256': local_sha, 'local_bytes': size,
                           'old_etag': remote_etag, 'old_sha256': remote_sha})
        if index % 40 == 0:
            log(f'Compared {index}/{len(by_object)} R2 objects')
    listed_keys = set(server.get('currently_listed_keys') or [])
    selected = {row['street_key'] for row in mapped}
    excluded = sorted(listed_keys - selected) if mode == 'complete' else []
    plan = {
        'operation_id': str(uuid.uuid4()), 'dataset_id': DATASET,
        'mode': mode, 'generated_utc': datetime.now(timezone.utc).isoformat(),
        'design_count': len(items), 'object_count': len(operations),
        'summary': dict(Counter(o['action'] for o in operations)),
        'listed_before': len(listed_keys), 'excluded_keys': excluded,
        'items': items, 'operations': operations, 'ownership': ownership,
        'warning': 'A complete release unlists every previously listed street not included. '
                   'In-place overwrites are safe only while all designs are held and have no checkout orders.',
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / 'publication_plan.json').write_text(json.dumps(plan, indent=2,ensure_ascii=False),encoding='utf-8')
    with (report_dir / 'publication_changes.csv').open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.DictWriter(stream,fieldnames=['action','key','source','local_bytes','old_etag'])
        writer.writeheader()
        for op in operations:
            writer.writerow({k:op.get(k) for k in writer.fieldnames})
    log(f"Publish plan: {len(items)} streets; {plan['summary']}; "
        f"{len(excluded)} existing listings to unlist (complete mode only)")
    return plan


def _backup_existing_objects(plan: dict, client, bucket: str, audit: dict, save, log, stop, recheck=None):
    """Create and verify a full recoverable snapshot before any in-place overwrite.

    Backups are retained in R2. A failure during this phase aborts publication;
    no production keys are modified. The local audit maps each source to its backup.
    """
    if any(not op['key'].endswith('.png') for op in plan['operations']):
        raise RuntimeError('Publication backups accept only PNG destinations')
    changes = [op for op in plan['operations'] if op['action'] == 'REPLACE']
    audit['backup_state'] = 'in_progress'
    audit['backup_total'] = len(changes)
    save()
    prefix = DATASET + '/_publication_backups/' + plan['operation_id'] + '/'
    for index, op in enumerate(changes, 1):
        if stop.is_set():
            raise RuntimeError('Stopped during backup. No production object was overwritten.')
        source_key = op['key']
        if not source_key.startswith(DATASET + '/') or '..' in source_key:
            raise RuntimeError(f'Unsafe original object key: {source_key}')
        backup_key = prefix + source_key[len(DATASET) + 1:]
        current = _r2_head(client, bucket, source_key)
        if current is None or str(current.get('ETag', '')).strip('"') != op['old_etag']:
            raise RuntimeError(f'Original R2 object changed before backup: {source_key}')
        if _r2_head(client, bucket, backup_key) is not None:
            raise RuntimeError(f'Backup destination unexpectedly exists: {backup_key}')
        if recheck:
            recheck()
        if stop.is_set():
            raise RuntimeError('Stopped during backup. No production object was overwritten.')
        client.copy_object(Bucket=bucket, Key=backup_key,
                           CopySource={'Bucket': bucket, 'Key': source_key})
        copied = _r2_head(client, bucket, backup_key)
        if copied is None or copied.get('ContentLength') != current.get('ContentLength'):
            raise RuntimeError(f'R2 backup size verification failed: {backup_key}')
        # Hash the actual backed-up bytes, not just copied (potentially stale) metadata.
        stream = client.get_object(Bucket=bucket, Key=backup_key)['Body']
        digest = hashlib.sha256()
        try:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        finally:
            stream.close()
        if digest.hexdigest() != op['old_sha256']:
            raise RuntimeError(f'R2 backup contents differ from preview: {backup_key}')
        audit['backups'].append({
            'original_key': source_key, 'backup_key': backup_key,
            'sha256': op['old_sha256'], 'bytes': copied['ContentLength'],
            'original_etag': op['old_etag'],
        })
        save()
        if index % 30 == 0 or index == len(changes):
            log(f'Backed up and verified {index}/{len(changes)} existing R2 objects')
    audit['backup_state'] = 'complete'
    save()
    log('Full R2 backup verified. No existing objects have been overwritten yet.')


def publish(plan: dict, client, bucket: str, endpoint: str, token: str,
            report_dir: Path, log, stop, progress=None) -> dict:
    return _run_artwork(plan, client, bucket, endpoint, token, report_dir, log, stop,
                        commit_catalogue=True, progress=progress)


def upload_artwork_only(plan: dict, client, bucket: str, endpoint: str, token: str,
                        report_dir: Path, log, stop, progress=None) -> dict:
    """Upload and verify artwork; the shared runner exits before catalogue commit."""
    return _run_artwork(plan, client, bucket, endpoint, token, report_dir, log, stop,
                        commit_catalogue=False, progress=progress)


def _run_artwork(plan, client, bucket, endpoint, token, report_dir, log, stop,
                 *, commit_catalogue, progress=None):
    if commit_catalogue and len(plan['items']) > MAX_PUBLISH_DESIGNS:
        raise ValueError(
            f'Publish Collection allows at most {MAX_PUBLISH_DESIGNS:,} selected designs per request; '
            f'{len(plan["items"]):,} selected. Select fewer designs and prepare again.')
    # The user has already confirmed the exact plan in the graphical interface.
    # Recheck held/zero-orders immediately BEFORE any R2 overwrite.
    def recheck():
        guard = _api(endpoint, token, {'mode':'preflight','dataset_id':DATASET})
        if not guard.get('safe_to_upload'):
            raise RuntimeError('Pre-upload safety check failed. No subsequent upload performed.')
        if ownership_for(plan['items'], guard) != plan.get('ownership'):
            raise RuntimeError('PNG ownership changed since preview. Prepare a fresh plan.')
    recheck()
    operations=plan['operations']
    expected = set(plan['ownership'])
    if len(operations) != len(expected) or {op['key'] for op in operations} != expected:
        raise RuntimeError('Unexplained publication object keys; prepare a fresh plan.')
    if any(not op['key'].endswith('.png') for op in operations):
        raise RuntimeError('Only PNG publication destinations are permitted')
    started = datetime.now(timezone.utc).isoformat()
    audit={'operation_id':plan['operation_id'],'started_utc':started,
           'complete':False,'files':[],'backups':[], 'backup_state':'not_started',
           'database_result':None}
    audit_file=report_dir / ('publication_run_' + plan['operation_id'] + '.json')
    def save():
        # Atomic local audit replacement avoids a truncated recovery manifest.
        temporary = audit_file.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(audit, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, audit_file)
    counts = {'uploaded': 0, 'skipped': 0, 'failed': 0, 'verified': 0}
    audit['action'] = 'publish' if commit_catalogue else 'upload_artwork_only'
    audit['counts'] = counts
    save()
    try:
        if progress:
            progress(0, len(operations))
        _backup_existing_objects(plan, client, bucket, audit, save, log, stop, recheck)
        recheck()
        for index, op in enumerate(operations,1):
            if stop.is_set():
                raise RuntimeError('Stopped; database publication not attempted. Some uploads may exist.')
            source=Path(op['source']); key=op['key']
            if not source.is_file() or source.stat().st_size!=op['local_bytes'] or _digest(source)!=op['local_sha256']:
                raise RuntimeError(f'Local artwork changed since preview: {source}')
            old=_r2_head(client,bucket,key)
            if op['action']=='NEW':
                if old is not None:
                    raise RuntimeError(f'New destination unexpectedly exists: {key}')
            elif old is None or str(old.get('ETag','')).strip('"')!=op['old_etag']:
                raise RuntimeError(f'R2 changed since preview: {key}')
            if op['action']=='REPLACE':
                # Persist intent first: a crash during upload still appears in recovery.
                backup = next((b for b in audit['backups'] if b['original_key'] == key), None)
                if backup is None:
                    raise RuntimeError(f'No verified backup for overwrite: {key}')
                audit['overwrite_in_progress'] = {'original_key': key,
                                                   'backup_key': backup['backup_key']}
                save()
            if op['action']!='SAME':
                audit['upload_in_progress'] = {'key': key, 'action': op['action'], 'outcome': 'unknown until verified'}
                save()
                recheck()
                if stop.is_set():
                    raise RuntimeError('Stopped before upload; no subsequent write initiated.')
                client.upload_file(str(source),bucket,key,ExtraArgs={
                    'ContentType': 'image/png',
                    'Metadata': {'sha256':op['local_sha256']},
                })
                counts['uploaded'] += 1
            else:
                counts['skipped'] += 1
                log(f'Skipped upload (identical in preview; verifying bytes): {key}')
            after=_r2_head(client,bucket,key)
            verified = _verify_artwork(client, bucket, op, after, stop)
            audit['files'].append({'key':key,'status':op['action'], **verified})
            counts['verified'] += 1
            audit.pop('overwrite_in_progress', None)
            audit.pop('upload_in_progress', None)
            save()
            if progress:
                progress(index, len(operations))
            if index%30==0 or index==len(operations):
                log(f'Verified R2 {index}/{len(operations)} objects')
        if not commit_catalogue:
            if stop.is_set():
                raise RuntimeError('Stopped after artwork verification; database publication not attempted.')
            audit['artwork_complete'] = True
            save()
            log(f"Artwork upload finished: {counts}. No Supabase catalogue changes requested.")
            return dict(counts)
        log('All R2 objects verified. Committing catalogue transaction...')
        # Only independently verified records can supply commit evidence.
        by_key = {record['key']: record for record in audit['files']}
        commit_items = []
        for item in plan['items']:
            expected = dict(item)
            for field in ('front', 'rear', 'production'):
                verified = by_key[item[field + '_key']]
                expected[field + '_size'] = verified['bytes']
                expected[field + '_sha256'] = verified['sha256']
                expected[field + '_etag'] = verified['etag']
            commit_items.append(expected)
        if stop.is_set():
            raise RuntimeError('Stopped before commit; database publication not attempted.')
        result=_api(endpoint,token,{
            'mode':'commit','dataset_id':DATASET,
            'release_mode':plan['mode'],'operation_id':plan['operation_id'],
            'confirmation':'PUBLISH HELD HEBDEN BRIDGE V2','items':commit_items,
        },timeout=180)
        audit['database_result']=result
        audit['complete']=result.get('db_result',{}).get('status') in ('published','already_published')
        save()
        if not audit['complete']:
            raise RuntimeError(f'Catalogue did not confirm publication. Inspect {audit_file.name}')
        log('PUBLISHED to read-only collection inventory. Checkout and Prodigi remain blocked.')
        return result
    except Exception:
        counts['failed'] += 1
        if audit.get('upload_in_progress'):
            log(f"Remote write may have succeeded; outcome unverified: {audit['upload_in_progress']['key']}")
        log(f"Artwork operation failed: {counts}; "
            f"{len(operations) - counts['verified']} files remain unverified. "
            f"Recovery journal: {audit_file}. Retry with a fresh preview.")
        # Restore requirements are explicit: only completed overwrites need recovery.
        # Never auto-restore while a concurrent writer might be active.
        audit['restore_needed'] = [
            {'original_key': row['key'],
             'backup_key': next((b['backup_key'] for b in audit['backups']
                 if b['original_key'] == row['key']), None)}
            for row in audit['files'] if row['status'] == 'REPLACE'
        ]
        if audit.get('overwrite_in_progress') and \
                audit['overwrite_in_progress'] not in audit['restore_needed']:
            audit['restore_needed'].append(audit['overwrite_in_progress'])
        save()
        raise
