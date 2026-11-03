"""Street Face held-collection publisher. No Prodigi, Stripe or public checkout actions.

Uploads the selected, validated release and invokes the secured Supabase catalogue
transaction only after every advertised R2 object is verified. Reuse original R2
object keys; never delete. The current implementation targets Hebden Bridge v2.
"""
from __future__ import annotations

import csv
import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from botocore.exceptions import ClientError

DATASET = 'street-face/hebden-bridge/v2'
DEFAULT_ENDPOINT = 'https://ngmdsmmvdnbnxeozpfej.supabase.co/functions/v1/street-face-publish-v2'
SLOTS = ('front_mockup', 'rear_mockup', 'production_png', 'production_svg')


def _api(endpoint: str, token: str, body: dict, timeout: int = 35) -> dict:
    payload = json.dumps(body, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    request = Request(endpoint, data=payload, headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
    }, method='POST')
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
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


def prepare(release, mapped: list[dict], geo: dict, client, bucket: str,
            endpoint: str, token: str, mode: str, report_dir: Path, log, stop) -> dict:
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
            'svg_key': asset_keys['production_svg'],
        })
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
        'items': items, 'operations': operations,
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


def publish(plan: dict, client, bucket: str, endpoint: str, token: str,
            report_dir: Path, log, stop) -> dict:
    # The user has already confirmed the exact plan in the graphical interface.
    # Recheck held/zero-orders immediately BEFORE any R2 overwrite.
    guard = _api(endpoint, token, {'mode':'preflight','dataset_id':DATASET})
    if not guard.get('safe_to_upload'):
        raise RuntimeError('Pre-upload safety check failed. No upload performed.')
    operations=plan['operations']
    started = datetime.now(timezone.utc).isoformat()
    audit={'operation_id':plan['operation_id'],'started_utc':started,
           'complete':False,'files':[],'database_result':None}
    audit_file=report_dir / 'publication_run.json'
    def save():
        audit_file.write_text(json.dumps(audit,indent=2,ensure_ascii=False),encoding='utf-8')
    save()
    try:
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
            if op['action']!='SAME':
                client.upload_file(str(source),bucket,key,ExtraArgs={
                    'ContentType': 'image/svg+xml' if source.suffix.lower()=='.svg' else 'image/png',
                    'Metadata': {'sha256':op['local_sha256']},
                })
            after=_r2_head(client,bucket,key)
            if after is None or after.get('ContentLength')!=op['local_bytes'] or \
                    _remote_sha(client,bucket,key,after)!=op['local_sha256']:
                raise RuntimeError(f'Upload verification failed: {key}')
            audit['files'].append({'key':key,'status':op['action'],'bytes':op['local_bytes'],
                                   'etag':str(after.get('ETag','')).strip('"')})
            save()
            if index%30==0 or index==len(operations):
                log(f'Verified R2 {index}/{len(operations)} objects')
        log('All R2 objects verified. Committing catalogue transaction...')
        result=_api(endpoint,token,{
            'mode':'commit','dataset_id':DATASET,
            'release_mode':plan['mode'],'operation_id':plan['operation_id'],
            'confirmation':'PUBLISH HELD HEBDEN BRIDGE V2','items':plan['items'],
        },timeout=180)
        audit['database_result']=result
        audit['complete']=result.get('db_result',{}).get('status') in ('published','already_published')
        save()
        if not audit['complete']:
            raise RuntimeError('Catalogue did not confirm publication. Inspect publication_run.json')
        log('PUBLISHED to read-only collection inventory. Checkout and Prodigi remain blocked.')
        return result
    except Exception:
        save()
        raise
