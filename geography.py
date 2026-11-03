"""Fail-closed matching of artwork, street-name CSV, and full GeoJSON geometries."""
from __future__ import annotations
import csv
import json
import math
from pathlib import Path


def _read_csv(path: Path):
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _coordinates_ok(geometry):
    if not isinstance(geometry, dict) or geometry.get('type') not in {'LineString', 'MultiLineString'}:
        return False
    coords = geometry.get('coordinates')
    if not isinstance(coords, list) or not coords:
        return False
    lines = [coords] if geometry['type'] == 'LineString' else coords
    return bool(lines) and all(
        isinstance(line, list) and len(line) >= 2 and all(
            isinstance(pt, list) and len(pt) >= 2 and
            all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in pt[:2]) and
            -180 <= pt[0] <= 180 and -90 <= pt[1] <= 90
            for pt in line
        ) for line in lines
    )


def validate_geography(rows: list[dict], names_csv: Path, geojson_path: Path, identity_csv: Path | None = None):
    issues: list[str] = []
    names = _read_csv(names_csv)
    geo = json.loads(geojson_path.read_text(encoding='utf-8-sig'))
    features = geo.get('features', []) if isinstance(geo, dict) and geo.get('type') == 'FeatureCollection' else []
    # A later generator run may contain only changed designs. The fixed original
    # artwork number is a CSV/GeoJSON INDEX, not the ordinal position in this run.
    # Never try to pair partial artwork with the first N CSV rows.
    if len(names) != len(features):
        issues.append(f'Geography inputs disagree: CSV {len(names)} vs GeoJSON {len(features)}')
    names_by_key, geo_by_key = {}, {}
    for i, item in enumerate(names, 1):
        key = item.get('street_key', '').strip()
        if not key or key in names_by_key: issues.append(f'CSV row {i}: missing/duplicate street_key: {key!r}')
        else: names_by_key[key] = item
    for i, feat in enumerate(features, 1):
        props = feat.get('properties') or {} if isinstance(feat, dict) else {}
        key = props.get('street_key', '')
        if not key or key in geo_by_key: issues.append(f'GeoJSON feature {i}: missing/duplicate street_key: {key!r}')
        else: geo_by_key[key] = feat
        if not _coordinates_ok(feat.get('geometry') if isinstance(feat, dict) else None):
            issues.append(f'GeoJSON feature {i}: missing/invalid LineString or MultiLineString coordinates')
    if set(names_by_key) != set(geo_by_key):
        issues.append(f'CSV/GeoJSON street keys differ: {len(set(names_by_key)^set(geo_by_key))} keys')
    # Prefer the generator's explicit stable identity map. This supports
    # re-numbered partial production runs without guessing by filename or name.
    # Legacy runs without the map remain supported only if original indices
    # have been preserved exactly.
    identity_by_number = None
    if identity_csv and identity_csv.is_file():
        identity_by_number = {}
        used_keys = set()
        for line, item in enumerate(_read_csv(identity_csv), 2):
            number = (item.get('artwork_number') or '').strip()
            street_key = (item.get('street_key') or '').strip()
            if not number.isdecimal() or not street_key or number in identity_by_number or street_key in used_keys:
                issues.append(f'Identity CSV row {line}: missing or repeated artwork number/street_key')
                continue
            identity_by_number[number.zfill(4)] = street_key
            used_keys.add(street_key)
        expected_numbers = {art['artwork_number'] for art in rows}
        if set(identity_by_number) != expected_numbers:
            issues.append(f'Identity CSV must cover exactly this run: {len(expected_numbers ^ set(identity_by_number))} unmatched artwork numbers')

    # Preserve duplicate street names via distinct street keys. A revision's
    # numeric filename prefix MUST still identify exactly the original CSV row.
    result = []
    seen = set()
    for art in sorted(rows, key=lambda r: r['artwork_number']):
        number = art['artwork_number']
        if identity_by_number is not None:
            key = identity_by_number.get(number)
            n = names_by_key.get(key)
            if n is None:
                issues.append(f'Artwork {number}: identity {key!r} not in geographic CSV')
                continue
        else:
            index = int(number)
            if index >= len(names):
                issues.append(f'Artwork {number}: no stable identity file and number exceeds catalogue')
                continue
            n = names[index]
            key = n.get('street_key', '').strip()
        feature = geo_by_key.get(key)
        if not key or not feature:
            issues.append(f'Artwork {number}: missing geographic identity at CSV row {index+1}')
            continue
        expected = n.get('name', '').strip()
        actual = art['street_name'].strip()
        geo_name = (feature.get('properties') or {}).get('name', '').strip()
        if actual != expected or geo_name != expected:
            issues.append(f'Artwork {number}: name mismatch: artwork {actual!r}, original catalogue {expected!r}, GeoJSON {geo_name!r}')
            continue
        if key in seen:
            issues.append(f'Artwork {number}: duplicate geographic identity {key!r}')
            continue
        seen.add(key)
        result.append({**art, 'street_key': key,
            'postcode': n.get('postcode', ''),
            'postcode_status': n.get('postcode_status', ''),
            'postcode_district': n.get('postcode_district', ''),
            'location_review': str(n.get('location_review', '')).lower() == 'true',
            'location_suffix': n.get('location_suffix', ''),
            'highway': n.get('highway', '')})
    return result, issues, geo


def enrich_postcode_report(rows: list[dict], report_path: Path):
    """Join optional postcode evidence by unique street_key, never by name."""
    records = _read_csv(report_path)
    by_key = {}
    issues = []
    for index, record in enumerate(records, 2):
        key = record.get('street_key', '').strip()
        if not key or key in by_key:
            issues.append(f'Postcode report row {index}: missing or duplicate street_key')
        else:
            by_key[key] = record
    required = {row['street_key'] for row in rows}
    missing = required - set(by_key)
    if missing:
        issues.append(f'Postcode report missing {len(missing)} required street keys')
    # Extra postcode rows are expected during a partial artwork revision.
    enriched = []
    for row in rows:
        report = by_key.get(row['street_key'])
        if report is None:
            continue
        district = report.get('postcode_district', '').strip()
        if row.get('postcode_district', '').strip() != district:
            issues.append(f'{row["street_key"]}: postcode district mismatch')
        if report.get('name', '').strip() != row['street_name'].strip():
            issues.append(f'{row["street_key"]}: postcode report street name mismatch')
        enriched.append({**row, 'postcode_report_status': report.get('status', ''),
                         'postcode_report_reason': report.get('reason', ''),
                         'postcode_report_district': district})
    return enriched, issues


def write_geography_reports(rows, issues, geo, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'geographic_issues.txt').write_text('\n'.join(issues), encoding='utf-8')
    (folder / 'geographic_mapping.json').write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding='utf-8')
    if rows:
        with (folder / 'geographic_mapping.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    # Geometry is kept separate as a complete, map-ready GeoJSON export.
    lookup = {r['street_key']: r for r in rows}
    mapped = []
    for feature in geo.get('features', []):
        key = (feature.get('properties') or {}).get('street_key')
        if key in lookup:
            f = json.loads(json.dumps(feature))
            f['properties']['artwork_number'] = lookup[key]['artwork_number']
            f['properties']['area_slug'] = lookup[key]['area_slug']
            f['properties']['release'] = lookup[key]['release']
            mapped.append(f)
    result = {'type': 'FeatureCollection', 'features': mapped}
    (folder / 'geographic_map.geojson').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
