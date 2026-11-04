from offline_ownership_fixtures import preflight
"""Offline desktop-to-real-Edge contract; no credentials or SDK connections."""
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, PngImagePlugin
import publisher


class DesktopEdgeContractTests(unittest.TestCase):
    def test_actual_desktop_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            files, rows, features, objects = [], [], [], {}
            for identity in (7, 93):
                row = {'street_key': f'permanent-{identity}', 'street_name': 'Same name',
                       'postcode_district': 'HX7'}
                features.append({'properties': {'street_key': row['street_key']},
                                 'geometry': {'type': 'LineString',
                                              'coordinates': [[0, 0], [1, identity]]}})
                for index, slot in enumerate(publisher.LOCAL_SLOTS):
                    number = identity * 10 + index
                    svg = slot == 'production_svg'
                    path = folder / f'{number}.{"svg" if svg else "png"}'
                    if svg:
                        path.write_text('<svg xmlns="http://www.w3.org/2000/svg"><!--' +
                                        'x' * (20000 + number * 10) + '--></svg>')
                    else:
                        metadata = PngImagePlugin.PngInfo()
                        metadata.add_text('fixture', str(number) + 'x' * (5000 + number * 10))
                        Image.new('RGB', (32, 32), (identity, index, 10)).save(path, pnginfo=metadata)
                        with Image.open(path) as image:
                            image.verify()
                    data = path.read_bytes()
                    self.assertGreaterEqual(len(data), 1000)
                    prefix = 'mockups' if index < 2 else 'print_candidates'
                    key = f'{publisher.DATASET}/{prefix}/{path.name}'
                    row[slot] = key
                    files.append((path, key))
                    objects[key] = data
                rows.append(row)

            class MemoryR2:
                def head_object(self, Bucket, Key):
                    data = objects[Key]
                    return {'ContentLength': len(data),
                            'ETag': '"' + hashlib.md5(data).hexdigest() + '"',
                            'Metadata': {'sha256': hashlib.sha256(data).hexdigest()}}

                def get_object(self, Bucket, Key, IfMatch):
                    head = self.head_object(Bucket, Key)
                    assert IfMatch == head['ETag']
                    return {**head, 'Body': io.BytesIO(objects[Key])}

            captured = []
            def api(endpoint, token, body, **kwargs):
                if body['mode'] == 'commit':
                    captured.append(json.loads(json.dumps(body)))
                    return {'db_result': {'status': 'published'}}
                return preflight(rows, currently_listed_keys=[])

            r2 = MemoryR2()
            release = SimpleNamespace(rows=rows, issues=[], files=files, prefix=publisher.DATASET)
            # Fail closed on accidental Python network access, even if the API mock regresses.
            with patch('socket.socket', side_effect=AssertionError('Network forbidden')), \
                    patch.object(publisher, '_api', side_effect=api):
                args = (r2, 'offline-configured-bucket', 'https://offline.invalid', 'x' * 64)
                plan = publisher.prepare(release, rows, {'features': list(reversed(features))},
                                         *args, 'partial', folder, lambda _: None, threading.Event())
                publisher.publish(plan, *args, folder, lambda _: None, threading.Event())
            self.assertEqual(len(captured), 1)
            evidence = {key: r2.head_object('offline-configured-bucket', key) for key in objects}
            fixture = {'payload': captured[0], 'objects': evidence,
                       'expected': {row['street_key']: {slot: row[slot] for slot in publisher.SLOTS}
                                    for row in rows}}
            fixture_path = folder / 'desktop-contract.json'
            fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
            result = subprocess.run(['node', 'tests/desktop_edge_contract.mjs', str(fixture_path)],
                                    text=True, capture_output=True,
                                    cwd=Path(__file__).resolve().parent, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
