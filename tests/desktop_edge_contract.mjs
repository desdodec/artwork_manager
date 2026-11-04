import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadDesktopObjects, send} from './edge_artwork.test.mjs';

const fixture=JSON.parse(readFileSync(process.argv[2],'utf8'));
const fields={front:'front_mockup',rear:'rear_mockup',production:'production_png',svg:'production_svg'};

test('actual desktop payload preserves eight distinct artwork records across permanent identities',async()=>{
  const state=loadDesktopObjects(fixture.objects);
  const result=await send(fixture.payload);
  assert.equal(result.status,200,JSON.stringify(result.body));
  assert.equal(result.body.verified_objects,8);
  assert.equal(state.commits.length,1);
  const items=state.commits[0].args.p_items;
  assert.deepEqual(items,fixture.payload.items);
  assert.equal(items.length,2);
  for(const item of items) for(const [field,slot] of Object.entries(fields)) {
    const key=fixture.expected[item.street_key][slot];
    const evidence=fixture.objects[key];
    assert.equal(item[field+'_key'],key);
    assert.equal(item[field+'_size'],evidence.ContentLength);
    assert.equal(item[field+'_etag'],evidence.ETag.replaceAll('"',''));
    assert.equal(item[field+'_sha256'],evidence.Metadata.sha256);
  }
  for(const suffix of ['key','size','etag','sha256'])
    assert.equal(new Set(items.flatMap(item=>Object.keys(fields).map(field=>item[field+'_'+suffix]))).size,8);
});

for(const item of fixture.payload.items) for(const field of Object.keys(fields))
  for(const fault of ['etag','size','missing'])
    test(`desktop contract rejects ${fault}: ${item.street_key} ${field}`,async()=>{
      const state=loadDesktopObjects(fixture.objects);
      const key=item[field+'_key'];
      if(fault==='missing') state.objects.delete(key);
      else if(fault==='size') state.objects.get(key).ContentLength++;
      else state.objects.get(key).ETag='"changed1234567890"';
      const result=await send(fixture.payload);
      assert.equal(result.status,409);
      assert.equal(state.commits.length,0);
      assert.ok(result.body.first_failures.some(f=>f.street_key===item.street_key && f.kind===field));
    });
