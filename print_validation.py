"""Read-only local print-file preflight; not a Prodigi proof or fulfilment approval."""
from __future__ import annotations
import csv
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from PIL import Image, UnidentifiedImageError

PRINT_WIDTH_MM = 229
PRINT_HEIGHT_MM = 95
# Confirmed against Prodigi GLOBAL-MUG-W editor, 2026-10-31:
# "Uploaded image: 2670x1110px", "Recommended: 2670x1110px",
# "Print quality: Excellent". This is the PRINT-IMAGE profile,
# not a physical proof or a universal Prodigi product specification.
PRODUCT_SKU = 'GLOBAL-MUG-W'
RECOMMENDED_WIDTH = 2670
RECOMMENDED_HEIGHT = 1110
TARGET_RATIO = RECOMMENDED_WIDTH / RECOMMENDED_HEIGHT
RATIO_TOLERANCE = 0.015


def audit_release(release, output_dir: Path, log=lambda _: None, stop=None):
    """Check every local production PNG and SVG in validated Release; never modify files."""
    if release.issues:
        raise ValueError('Validate the artwork release before running print checks.')
    paths = {key: path for path, key in release.files}
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for index, row in enumerate(release.rows, 1):
        if stop is not None and stop.is_set():
            log('Print audit cancelled. No incomplete report was saved.')
            return None
        code, name = row['artwork_number'], row['street_name']
        png = paths.get(row.get('production_png', ''))
        svg = paths.get(row.get('production_svg', ''))
        errors, warnings = [], []
        width = height = 0
        mode = ''
        bytes_png = 0
        if png is None or not png.is_file():
            errors.append('Production PNG missing')
        else:
            bytes_png = png.stat().st_size
            if not bytes_png:
                errors.append('Production PNG empty')
            else:
                try:
                    with Image.open(png) as img:
                        if img.format != 'PNG':
                            errors.append(f'Image is {img.format}, not PNG')
                        width, height = img.size
                        mode = img.mode
                        img.verify()
                    if width <= 0 or height <= 0:
                        errors.append('Invalid PNG dimensions')
                    else:
                        if width < RECOMMENDED_WIDTH or height < RECOMMENDED_HEIGHT:
                            errors.append(
                                f'Below Prodigi {PRODUCT_SKU} recommended image size '
                                f'({RECOMMENDED_WIDTH} x {RECOMMENDED_HEIGHT}px)'
                            )
                        ratio = width / height
                        if abs(ratio - TARGET_RATIO) / TARGET_RATIO > RATIO_TOLERANCE:
                            errors.append('Aspect ratio differs from Prodigi 2670:1110 template by over 1.5%')
                    if mode not in ('RGB', 'RGBA'):
                        warnings.append(f'PNG colour mode {mode}; inspect export colour profile')
                    if mode == 'RGBA':
                        warnings.append('PNG has alpha channel; confirm transparency/background with Prodigi')
                except (UnidentifiedImageError, OSError, ValueError) as exc:
                    errors.append('PNG cannot be decoded: ' + str(exc)[:120])
        svg_bytes = 0
        if svg is None or not svg.is_file():
            errors.append('Source SVG missing')
        else:
            svg_bytes = svg.stat().st_size
            if not svg_bytes:
                errors.append('Source SVG empty')
            else:
                try:
                    root = ET.parse(svg).getroot()
                    if root.tag.split('}')[-1] != 'svg':
                        errors.append('Source SVG root element invalid')
                except (OSError, ET.ParseError) as exc:
                    errors.append('Source SVG cannot be parsed: ' + str(exc)[:120])
        outcome = 'FAIL' if errors else ('REVIEW' if warnings else 'TECHNICAL_PASS')
        results.append({
            'artwork_number': code, 'street_name': name,
            'street_key': row.get('street_key', ''),
            'production_png_key': row.get('production_png', ''),
            'production_svg_key': row.get('production_svg', ''),
            'width_px': width, 'height_px': height, 'colour_mode': mode,
            'effective_dpi_x': round(width / PRINT_WIDTH_MM * 25.4, 1) if width else '',
            'effective_dpi_y': round(height / PRINT_HEIGHT_MM * 25.4, 1) if height else '',
            'png_bytes': bytes_png, 'svg_bytes': svg_bytes,
            'result': outcome, 'errors': '; '.join(errors), 'warnings': '; '.join(warnings),
        })
        if index % 25 == 0 or index == len(release.rows):
            log(f'Print validation: {index}/{len(release.rows)} designs checked')
    report_csv = output_dir / 'print_validation.csv'
    with report_csv.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    totals = {name: sum(r['result'] == name for r in results) for name in ('TECHNICAL_PASS', 'REVIEW', 'FAIL')}
    report_json = output_dir / 'print_validation_summary.json'
    report_json.write_text(json.dumps({
        'checked': len(results), 'results': totals,
        'product_sku': PRODUCT_SKU,
        'print_area_mm': [PRINT_WIDTH_MM, PRINT_HEIGHT_MM],
        'recommended_pixels': [RECOMMENDED_WIDTH, RECOMMENDED_HEIGHT],
        'profile_evidence': 'Prodigi GLOBAL-MUG-W editor screenshot: recommended 2670x1110px, print quality Excellent (2026-10-31)',
        'aspect_ratio_tolerance': RATIO_TOLERANCE,
        'limitations': 'Local PNG and SVG checks only. Does not verify R2 contents, visual composition, safe zones, colour fidelity, or Prodigi approval. No status updates or fulfilment actions.',
    }, indent=2), encoding='utf-8')
    return report_csv, report_json, totals
