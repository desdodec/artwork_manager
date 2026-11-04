from offline_ownership_fixtures import preflight
import json
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import publisher
from dataclasses import dataclass
@dataclass
class Release:
    rows: list
    issues: list
    unrecognised: list
    files: list
    prefix: str

class MemoryR2:
    def __init__(self): self.files={}
    def head_object(self,Bucket,Key):
        if Key not in self.files:
            from botocore.exceptions import ClientError
            raise ClientError({'Error': {'Code':'404','Message':'not found'}},'HeadObject')
        d=self.files[Key]
        return {'ContentLength':len(d),'ETag':'"abcdef1234567890"',
                'Metadata': {'sha256':publisher.hashlib.sha256(d).hexdigest()}}
    def upload_file(self,filename,bucket,key,ExtraArgs):
        self.files[key]=Path(filename).read_bytes()
    def get_object(self, Bucket, Key, IfMatch):
        head = self.head_object(Bucket, Key)
        assert IfMatch == head['ETag']
        return {**head, 'Body': io.BytesIO(self.files[Key])}

class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.r2=MemoryR2()
        self.items=[];files=[];geos=[]
        for n in (0,2):
            num=str(n).zfill(4);name=f'Street {n}';key=f'{name}__abcdef{n:02d}'
            row={'artwork_number':num,'street_key':key,'street_name':name,
                 'postcode_district':'HX7'}
            for slot,suffix,folder in [('front_mockup','_front.png','mockups'),
                ('rear_mockup','_rear.png','mockups'),('production_png','.png','print_candidates'),
                ('production_svg','.svg','print_candidates')]:
                path=self.folder/f'{num}_{name}_{slot}'
                path.write_bytes(('content-'+slot+num).encode()*10)
                objectkey=f'street-face/hebden-bridge/v2/{folder}/{num}_{name}_wrap_print{suffix}'
                row[slot]=objectkey;files.append((path,objectkey))
            self.items.append(row)
            geos.append({'properties':{'street_key':key},'geometry':{'type':'LineString','coordinates':[[0,0],[1,1]]}})
        self.geo={'type':'FeatureCollection','features':geos}
        self.release=Release(self.items,[],[],files,'street-face/hebden-bridge/v2')

    def test_same_files_are_hashed_despite_matching_metadata(self):
        for field in publisher.SLOTS:
            with self.subTest(field=field):
                self.r2.files = {key: path.read_bytes() for path, key in self.release.files}
                stop = threading.Event()
                with patch.object(publisher, '_api', return_value=preflight(self.items)) as api:
                    plan = publisher.prepare(self.release, self.items, self.geo, self.r2,
                        'bucket', 'url', 'x'*64, 'partial', self.folder, lambda _: None, stop)
                    self.assertEqual(plan['summary'], {'SAME': 6})
                    target = self.items[0][field]
                    original_get = self.r2.get_object
                    streams = []
                    def corrupted_get(**kwargs):
                        result = original_get(**kwargs)
                        if kwargs['Key'] == target:
                            data = self.r2.files[target]
                            result['Body'] = io.BytesIO(b'X' + data[1:])
                            streams.append(result['Body'])
                        return result
                    with patch.object(self.r2, 'get_object', side_effect=corrupted_get), \
                            patch.object(self.r2, 'upload_file') as upload:
                        with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                            publisher.publish(plan, self.r2, 'bucket', 'url', 'x'*64,
                                self.folder, lambda _: None, stop)
                        upload.assert_not_called()
                    self.assertTrue(streams[0].closed)
                    self.assertTrue(all(c.args[2]['mode'] != 'commit' for c in api.call_args_list))

    def test_missing_artwork_blocks_commit(self):
        stop = threading.Event()
        with patch.object(publisher, '_api', return_value=preflight(self.items)) as api:
            plan = publisher.prepare(self.release, self.items, self.geo, self.r2,
                'bucket', 'url', 'x'*64, 'partial', self.folder, lambda _: None, stop)
            with patch.object(self.r2, 'upload_file'):
                with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                    publisher.publish(plan, self.r2, 'bucket', 'url', 'x'*64,
                        self.folder, lambda _: None, stop)
            self.assertTrue(all(c.args[2]['mode'] != 'commit' for c in api.call_args_list))

    def test_stream_errors_and_changed_response_headers_block_commit(self):
        for fault in ('size', 'etag', 'truncated', 'read_error'):
            with self.subTest(fault=fault):
                self.r2.files = {key: path.read_bytes() for path, key in self.release.files}
                stop = threading.Event()
                streams = []
                with patch.object(publisher, '_api', return_value=preflight(self.items)) as api:
                    plan = publisher.prepare(self.release, self.items, self.geo, self.r2,
                        'bucket', 'url', 'x'*64, 'partial', self.folder, lambda _: None, stop)
                    original_get = self.r2.get_object
                    def faulty_get(**kwargs):
                        result = original_get(**kwargs)
                        if fault == 'size': result['ContentLength'] += 1
                        if fault == 'etag': result['ETag'] = '"changed1234567890"'
                        if fault == 'truncated':
                            result['Body'].close()
                            result['Body'] = io.BytesIO(b'')
                        if fault == 'read_error':
                            result['Body'].close()
                            class BrokenStream(io.BytesIO):
                                def read(self, size):
                                    raise OSError('interrupted download')
                            result['Body'] = BrokenStream()
                        streams.append(result['Body'])
                        return result
                    with patch.object(self.r2, 'get_object', side_effect=faulty_get):
                        with self.assertRaises((RuntimeError, OSError)):
                            publisher.publish(plan, self.r2, 'bucket', 'url', 'x'*64,
                                self.folder, lambda _: None, stop)
                    self.assertTrue(all(s.closed for s in streams))
                    self.assertTrue(all(c.args[2]['mode'] != 'commit' for c in api.call_args_list))

    def test_publish_again_after_verification_failure_rehashes_without_uploads(self):
        stop = threading.Event()
        with patch.object(publisher, '_api', return_value=preflight(self.items)) as api:
            plan = publisher.prepare(self.release, self.items, self.geo, self.r2,
                'bucket', 'url', 'x'*64, 'partial', self.folder, lambda _: None, stop)
            original_get = self.r2.get_object
            def failed_get(**kwargs):
                if kwargs['Key'] == self.items[-1]['production_png']:
                    raise OSError('interrupted download')
                return original_get(**kwargs)
            with patch.object(self.r2, 'get_object', side_effect=failed_get):
                with self.assertRaises(OSError):
                    publisher.publish(plan, self.r2, 'bucket', 'url', 'x'*64,
                        self.folder, lambda _: None, stop)
            self.assertTrue(all(c.args[2]['mode'] != 'commit' for c in api.call_args_list))
        with patch.object(publisher, '_api', side_effect=lambda e,t,b,**kw:
                preflight(self.items) if b['mode']=='preflight' else {'db_result': {'status':'published'}}):
            again = publisher.prepare(self.release, self.items, self.geo, self.r2,
                'bucket', 'url', 'x'*64, 'partial', self.folder, lambda _: None, stop)
            self.assertNotEqual(plan['operation_id'], again['operation_id'])
            self.assertEqual(again['summary'], {'SAME': 6})
            with patch.object(self.r2, 'upload_file') as upload, \
                    patch.object(self.r2, 'get_object', wraps=original_get) as get:
                publisher.publish(again, self.r2, 'bucket', 'url', 'x'*64,
                    self.folder, lambda _: None, stop)
                upload.assert_not_called()
                self.assertEqual(get.call_count, 6)
            audit = json.loads((self.folder / ('publication_run_' + again['operation_id'] + '.json')).read_text())
            self.assertEqual(len(audit['files']), 6)
            for record in audit['files']:
                self.assertEqual(record['sha256'], publisher.hashlib.sha256(self.r2.files[record['key']]).hexdigest())

    def test_cancellation_during_stream_or_after_last_verification_blocks_commit(self):
        for during_stream in (True, False):
            with self.subTest(during_stream=during_stream):
                stop = threading.Event()
                with patch.object(publisher, '_api', return_value=preflight(self.items)) as api:
                    plan = publisher.prepare(self.release, self.items, self.geo, self.r2,
                        'bucket', 'url', 'x'*64, 'partial', self.folder, lambda _: None, stop)
                    original_get = self.r2.get_object
                    streams = []
                    def cancelling_get(**kwargs):
                        result = original_get(**kwargs)
                        if during_stream:
                            stream = result['Body']
                            class CancellingStream(io.BytesIO):
                                def read(self, size):
                                    self_outer.assertEqual(size, 1024 * 1024)
                                    stop.set()
                                    return super().read(size)
                            self_outer = self
                            result['Body'] = CancellingStream(stream.getvalue())
                            stream.close()
                            streams.append(result['Body'])
                        return result
                    def log(message):
                        if not during_stream and message.startswith('All R2 objects verified'):
                            stop.set()
                    with patch.object(self.r2, 'get_object', side_effect=cancelling_get):
                        with self.assertRaisesRegex(RuntimeError, 'Stopped'):
                            publisher.publish(plan, self.r2, 'bucket', 'url', 'x'*64,
                                self.folder, log, stop)
                    self.assertTrue(all(s.closed for s in streams))
                    self.assertTrue(all(c.args[2]['mode'] != 'commit' for c in api.call_args_list))

    def test_complete_and_partial_differ(self):
        reply=preflight(self.items, currently_listed_keys=['Street 0__abcdef00','removed__foo'])
        with patch.object(publisher,'_api',return_value=reply):
            full=publisher.prepare(self.release,self.items,self.geo,self.r2,'bucket','url','x'*64,
                'complete',self.folder,lambda _:None,type('S',(),{'is_set':lambda s:False})())
            self.assertEqual(len(full['excluded_keys']),1)
            partial=publisher.prepare(self.release,self.items,self.geo,self.r2,'bucket','url','x'*64,
                'partial',self.folder,lambda _:None,type('S',(),{'is_set':lambda s:False})())
            self.assertEqual(partial['excluded_keys'],[])
            self.assertEqual(full['summary']['NEW'],6)

    def test_publish_skips_same_and_calls_server_after_uploads(self):
        stop=type('S',(),{'is_set':lambda s:False})()
        call=[]
        def backend(e,t,body,timeout=35):
            call.append(body['mode'])
            if body['mode']=='preflight':return preflight(self.items, currently_listed_keys=[])
            for _,key in self.release.files:
                if key.endswith(".svg"): continue
                self.assertIn(key,self.r2.files)
            for item in body['items']:
                for field in ('front', 'rear', 'production'):
                    data = self.r2.files[item[field + '_key']]
                    self.assertEqual(item[field + '_size'], len(data))
                    self.assertEqual(item[field + '_sha256'], publisher.hashlib.sha256(data).hexdigest())
                    self.assertEqual(item[field + '_etag'], 'abcdef1234567890')
            return {'db_result':{'status':'published','processed':2}}
        with patch.object(publisher,'_api',side_effect=backend):
            plan=publisher.prepare(self.release,self.items,self.geo,self.r2,'bucket','url','x'*64,
                'complete',self.folder,lambda _:None,stop)
            result=publisher.publish(plan,self.r2,'bucket','url','x'*64,
                self.folder,lambda _:None,stop)
            self.assertEqual(result['db_result']['processed'],2)
            self.assertEqual(call,['preflight'] * 9 + ['commit'])
            self.assertEqual(len(self.r2.files),6)
            again=publisher.prepare(self.release,self.items,self.geo,self.r2,'bucket','url','x'*64,
                'partial',self.folder,lambda _:None,stop)
            self.assertEqual(again['summary']['SAME'],6)

if __name__=='__main__':unittest.main()
