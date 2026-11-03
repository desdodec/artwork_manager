import json
import sys
import tempfile
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

    def test_complete_and_partial_differ(self):
        reply={'safe_to_upload':True,'currently_listed_keys':['Street 0__abcdef00','removed__foo']}
        with patch.object(publisher,'_api',return_value=reply):
            full=publisher.prepare(self.release,self.items,self.geo,self.r2,'bucket','url','x'*64,
                'complete',self.folder,lambda _:None,type('S',(),{'is_set':lambda s:False})())
            self.assertEqual(len(full['excluded_keys']),1)
            partial=publisher.prepare(self.release,self.items,self.geo,self.r2,'bucket','url','x'*64,
                'partial',self.folder,lambda _:None,type('S',(),{'is_set':lambda s:False})())
            self.assertEqual(partial['excluded_keys'],[])
            self.assertEqual(full['summary']['NEW'],8)

    def test_publish_skips_same_and_calls_server_after_uploads(self):
        stop=type('S',(),{'is_set':lambda s:False})()
        call=[]
        def backend(e,t,body,timeout=35):
            call.append(body['mode'])
            if body['mode']=='preflight':return {'safe_to_upload':True,'currently_listed_keys':[]}
            for _,key in self.release.files:
                self.assertIn(key,self.r2.files)
            return {'db_result':{'status':'published','processed':2}}
        with patch.object(publisher,'_api',side_effect=backend):
            plan=publisher.prepare(self.release,self.items,self.geo,self.r2,'bucket','url','x'*64,
                'complete',self.folder,lambda _:None,stop)
            result=publisher.publish(plan,self.r2,'bucket','url','x'*64,
                self.folder,lambda _:None,stop)
            self.assertEqual(result['db_result']['processed'],2)
            self.assertEqual(call,['preflight','preflight','commit'])
            self.assertEqual(len(self.r2.files),8)
            again=publisher.prepare(self.release,self.items,self.geo,self.r2,'bucket','url','x'*64,
                'partial',self.folder,lambda _:None,stop)
            self.assertEqual(again['summary']['SAME'],8)

if __name__=='__main__':unittest.main()
