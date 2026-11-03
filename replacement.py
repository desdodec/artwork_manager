"""Explicit, audited R2 in-place replacement for *held/unpublished* releases only.

This module does not access Supabase, assume order safety or delete R2 keys.
"""
from __future__ import annotations
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from botocore.exceptions import ClientError


def checksum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _head(client, bucket: str, key: str):
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if str(exc.response.get('Error', {}).get('Code', '')) in ('404', 'NoSuchKey', 'NotFound'):
            return None
        raise


def prepare(client, bucket, release, log, stop):
    """Read-only preflight. For unknown remote hashes, refuse blind overwrite."""
    if release.issues or not release.files:
        raise ValueError('Validate the full release first.')
    operations = []
    for source, key in release.files:
        if stop.is_set():
            raise RuntimeError('Replacement preflight cancelled.')
        local_hash = checksum(source)
        remote = _head(client, bucket, key)
        if remote is None:
            raise RuntimeError(f'Replacement requires existing R2 object: {key}. Use normal upload for missing objects.')
        size = source.stat().st_size
        remote_hash = (remote.get('Metadata') or {}).get('sha256')
        etag = str(remote.get('ETag', '')).strip('"')
        # Original R2 releases can lack sha256 metadata. A SHA-256 comparison
        # against remote bytes is necessary then; ETags are not SHA-256 hashes.
        if not remote_hash:
            remote_content = client.get_object(Bucket=bucket, Key=key)['Body']
            digest = hashlib.sha256()
            for chunk in iter(lambda: remote_content.read(1024 * 1024), b''):
                digest.update(chunk)
            remote_hash = digest.hexdigest()
        changed = remote_hash != local_hash or remote.get('ContentLength') != size
        operations.append(dict(source=str(source), key=key, local_sha256=local_hash,
                               local_bytes=size, old_sha256=remote_hash, old_etag=etag,
                               old_bytes=remote.get('ContentLength'), action='REPLACE' if changed else 'UNCHANGED'))
    log(f'Compared {len(operations)} objects: {sum(o["action"] == "REPLACE" for o in operations)} changed.')
    return operations


def apply(client, bucket, operations, report_dir: Path, log, stop):
    """Recheck remote state before every overwrite, then verify uploaded SHA-256."""
    changed = [o for o in operations if o['action'] == 'REPLACE']
    if not changed:
        log('No changes to upload.')
        return None
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report = report_dir / f'r2_replacement_{timestamp}.csv'
    fields = ['key','status','old_bytes','new_bytes','old_etag','new_etag','old_sha256','new_sha256']
    rows = []
    # Write a report after each file, including partial success on errors.
    def save():
        with report.open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    try:
        for index, item in enumerate(changed, 1):
            if stop.is_set():
                raise RuntimeError('Stopped before the next replacement; earlier uploads were not rolled back.')
            src = Path(item['source'])
            if checksum(src) != item['local_sha256'] or src.stat().st_size != item['local_bytes']:
                raise RuntimeError(f'Local file changed after preflight: {src}')
            before = _head(client, bucket, item['key'])
            if not before or str(before.get('ETag','')).strip('"') != item['old_etag']:
                raise RuntimeError(f'R2 object changed during confirmation: {item["key"]}')
            content_type = 'image/svg+xml' if src.suffix.lower() == '.svg' else 'image/png'
            # PUT replaces the existing object, no explicit DELETE, no duplicate key.
            client.upload_file(str(src), bucket, item['key'], ExtraArgs={
                'ContentType': content_type, 'Metadata': {'sha256': item['local_sha256']},
            })
            after = _head(client, bucket, item['key'])
            if not after or (after.get('Metadata') or {}).get('sha256') != item['local_sha256'] or after.get('ContentLength') != item['local_bytes']:
                raise RuntimeError(f'Post-upload verification failed: {item["key"]}')
            rows.append(dict(key=item['key'],status='REPLACED',old_bytes=item['old_bytes'],
                             new_bytes=item['local_bytes'],old_etag=item['old_etag'],
                             new_etag=str(after.get('ETag','')).strip('"'),old_sha256=item['old_sha256'],
                             new_sha256=item['local_sha256']))
            save()
            log(f'Replaced and verified {index}/{len(changed)}: {item["key"]}')
    finally:
        save()
        log(f'Replacement audit saved: {report}')
        log('IMPORTANT: Supabase production_size/production_etag are now STALE for replaced PNGs.')
        log('Do not run fulfilment or mark assets valid until Supabase metadata has been reconciled.')
    return report
