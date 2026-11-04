import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {createHash} from 'node:crypto';

// Execute the real handler. Replace only external SDK imports and Deno runtime;
// no installed SDK, credentials, network, deployed function or database needed.
let handler, commits, objects, heads;
globalThis.fetch = () => { throw new Error('Network forbidden in offline Edge tests'); };
const dataset = 'street-face/hebden-bridge/v2';
const token = 'offline-publish-token-at-least-32-characters';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const command = class { constructor(input) { this.input = input; } };
const query = {select(){return this;},eq(){return this;},limit(){return {data:[]};}};
globalThis.edgeFakes = {
  createClient: () => ({from:()=>query, rpc:async (name,args)=>{
    commits.push({name,args}); return {data:{status:'published'}};
  }}),
  HeadObjectCommand: command,
  S3Client: class {async send(command) {
    assert.equal(command.input.Bucket, 'offline-configured-bucket');
    heads.push(command.input.Key);
    const object = objects.get(command.input.Key);
    if (!object) throw new Error('NoSuchKey');
    assert.equal(command.constructor, globalThis.edgeFakes.HeadObjectCommand);
    return {ContentLength:object.ContentLength, ETag:object.ETag};
  }},
};
globalThis.Deno = {
  serve: fn => {handler=fn;},
  env: {get: name => name==='STREET_FACE_PUBLISH_TOKEN' ? token :
    name==='R2_BUCKET' ? 'offline-configured-bucket' : 'offline-placeholder'},
};
let source = readFileSync(new URL('../collection-publish-edge.ts', import.meta.url), 'utf8');
source = source.replace(/import \{ createClient \} from 'npm:[^']+';/,
  'const {createClient} = globalThis.edgeFakes;')
  .replace(/import \{ S3Client, HeadObjectCommand \} from 'npm:[^']+';/,
    'const {S3Client, HeadObjectCommand} = globalThis.edgeFakes;')
  .replace("'./collection-artwork-verification.mjs'",
    JSON.stringify(new URL('../collection-artwork-verification.mjs', import.meta.url).href));
await import('data:text/javascript;base64,' + Buffer.from(stripTypeScriptTypes(source)).toString('base64'));

function fixture(pngSize=1000, svgSize=100) {
  commits=[]; objects=new Map(); heads=[];
  const item={street_key:'permanent-street-1',street_name:'Example',
    geometry:{type:'LineString',coordinates:[[0,0],[1,1]]}};
  for (const field of ['front','rear','production','svg']) {
    const svg=field==='svg';
    const key=dataset+'/'+(['front','rear'].includes(field)?'mockups/':'print_candidates/')+
      field+(svg?'.svg':'.png');
    const bytes=new Uint8Array(svg?svgSize:pngSize).fill(65);
    item[field+'_etag']='abcdef1234567890';
    item[field+'_key']=key; item[field+'_size']=bytes.length; item[field+'_sha256']=sha(bytes);
    objects.set(key,{bytes,ContentLength:bytes.length,ETag:'"abcdef1234567890"',Metadata:{sha256:sha(bytes)}});
  }
  return {mode:'commit',dataset_id:dataset,operation_id:'00000000-0000-4000-8000-000000000001',
    release_mode:'partial',confirmation:'PUBLISH HELD HEBDEN BRIDGE V2',items:[item]};
}
async function send(body) {
  const response=await handler(new Request('https://offline.invalid', {method:'POST',
    headers:{authorization:'Bearer '+token},body:JSON.stringify(body)}));
  return {status:response.status,body:await response.json()};
}
async function blocked(body, reason) {
  const result=await send(body);
  assert.equal(result.status,409);
  assert.match(result.body.error,/Supabase unchanged/);
  assert.ok(result.body.first_failures.some(f=>f.reason.includes(reason)));
  assert.equal(commits.length,0);
}
export function loadDesktopObjects(evidence) {
  commits=[]; heads=[]; objects=new Map(Object.entries(structuredClone(evidence)));
  return {commits, heads, objects};
}
export {send};
test('matching desktop evidence commits all four files using HEAD only, retaining permanent identity and verified ETag',async()=>{
  const body=fixture(); const result=await send(body);
  assert.equal(result.status,200); assert.equal(result.body.verified_objects,4);
  assert.equal(heads.length,4); assert.equal(commits.length,1);
  assert.equal(commits[0].args.p_items[0].street_key,'permanent-street-1');
  for (const field of ['front','rear','production','svg']) {
    assert.equal(commits[0].args.p_items[0][field+'_etag'],'abcdef1234567890');
    assert.equal(commits[0].args.p_items[0][field+'_sha256'],body.items[0][field+'_sha256']);
  }
});
for (const field of ['front','rear','production','svg']) test(`changed ${field} ETag blocks commit`,async()=>{
  const body=fixture(); objects.get(body.items[0][field+'_key']).ETag='"changed1234567890"';
  await blocked(body,'ETag differs');
});

test('checksum metadata is not used as byte proof by Edge',async()=>{
  const body=fixture(); for (const object of objects.values()) object.Metadata={sha256:'0'.repeat(64)};
  assert.equal((await send(body)).status,200); assert.equal(commits.length,1);
});

for (const field of ['front','rear','production','svg']) test(`missing ${field} ETag blocks commit`,async()=>{
  const body=fixture(); delete body.items[0][field+'_etag']; await blocked(body,'Invalid expected');
});

test('missing object blocks commit',async()=>{
  const body=fixture(); objects.delete(body.items[0].svg_key); await blocked(body,'missing');
});
test('missing expected checksum blocks commit',async()=>{
  const body=fixture(); delete body.items[0].front_sha256; await blocked(body,'Invalid expected');
});
test('a mismatch in a later batch prevents the entire commit',async()=>{
  const body=fixture();
  for (let index=2; index<=6; index++) {
    const next={...body.items[0],street_key:'permanent-street-'+index};
    for (const field of ['front','rear','production','svg']) {
      const oldKey=next[field+'_key'];
      const newKey=oldKey.replace('/'+field+'.','/street'+index+'_'+field+'.');
      next[field+'_key']=newKey;
      objects.set(newKey,{...objects.get(oldKey)});
    }
    body.items.push(next);
  }
  objects.get(body.items[5].svg_key).ETag='changed1234567890';
  await blocked(body,'ETag differs');
  assert.equal(heads.length,24);
});
test('changed object size blocks commit',async()=>{
  const body=fixture(); objects.get(body.items[0].front_key).ContentLength++;
  await blocked(body,'byte count differs');
});
for (const size of [999,1000,1001]) test(`PNG size boundary ${size} agrees with SQL`,async()=>{
  const sql=readFileSync(new URL('../collection_publish.sql',import.meta.url),'utf8');
  for (const field of ['front','rear','production'])
    assert.ok(sql.includes(`(v_item->>'${field}_size')::bigint < 1000`));
  const body=fixture(size);
  if(size<1000) await blocked(body,'Invalid expected');
  else {assert.equal((await send(body)).status,200);assert.equal(commits.length,1);}
});
for (const size of [99,100,101]) test(`SVG size boundary ${size}`,async()=>{
  const body=fixture(1000,size);
  if(size<100) await blocked(body,'Invalid expected');
  else assert.equal((await send(body)).status,200);
});
