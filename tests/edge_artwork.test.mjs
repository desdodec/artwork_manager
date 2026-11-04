import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {createHash} from 'node:crypto';

// Execute the real handler. Replace only external SDK imports and Deno runtime;
// no installed SDK, credentials, network, deployed function or database needed.
let handler, commits, objects, heads, ownershipItems=[], pageFault=null, inventoryOverride=null, snapshotFault=null;
globalThis.fetch = () => { throw new Error('Network forbidden in offline Edge tests'); };
const dataset = 'street-face/hebden-bridge/v2';
const token = 'offline-publish-token-at-least-32-characters';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const command = class { constructor(input) { this.input = input; } };
function query(table) {return {
  select(){return this;},eq(){return this;},in(){return this;},limit(){return {data:[],count:0};},
  order(){return this;},range(start,end){
    let rows=ownershipItems.map((item,index)=>table==='designs' ?
      {id:'design-'+index,dataset_id:dataset,street_id:item.street_key,status:'hold'} :
      {id:'asset-'+index,design_id:'design-'+index,product_sku:'GLOBAL-MUG-W',version:1,
       front_mockup_key:item.front_key,rear_mockup_key:item.rear_key,production_key:item.production_key});
    if(inventoryOverride) rows=inventoryOverride[table];
    if(pageFault==='query') return {error:{message:'offline failure'},data:null,count:null};
    if(pageFault==='truncated') return {data:[],count:rows.length+1};
    if(pageFault==='count') return {data:rows.slice(start,end+1),count:null};
    if(pageFault==='duplicate' && rows.length>1) rows[1]=rows[0];
    if(pageFault==='later' && start>0) return {error:{message:'later page failed'},data:null,count:null};
    return {data:rows.slice(start,end+1),count:rows.length};
  }
};}
globalThis.edgeFakes = {
  createClient: () => ({from:()=>{throw new Error('Paginated reads forbidden');}, rpc:async (name,args)=>{
    if(name==='street_face_upload_snapshot_v1') {
      const designs=query('designs').range(0,999999);
      const assets=query('design_assets').range(0,999999);
      if(pageFault==='later') return {error:{message:'snapshot failed'}};
      if(snapshotFault==='missing_rpc') return {error:{message:'function unavailable'}};
      const data={snapshot_version:1,designs:designs.data,designs_count:designs.count,
        design_assets:assets.data,design_assets_count:assets.count,
        checkout_order_lines:0,listed_keys:[],listed_count:0};
      if(snapshotFault==='version') delete data.snapshot_version;
      if(snapshotFault==='asset_count') data.design_assets_count++;
      if(snapshotFault==='orders') data.checkout_order_lines=1;
      if(snapshotFault==='listed_count') data.listed_count=1;
      if(snapshotFault==='listed_duplicate') {data.listed_keys=['same','same'];data.listed_count=2;}
      return {data};
    }
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
  for (const field of ['front','rear','production']) {
    const svg=field==='svg';
    const key=dataset+'/'+(['front','rear'].includes(field)?'mockups/':'print_candidates/')+
      field+(svg?'.svg':'.png');
    const bytes=new Uint8Array(svg?svgSize:pngSize).fill(65);
    item[field+'_etag']='abcdef1234567890';
    item[field+'_key']=key; item[field+'_size']=bytes.length; item[field+'_sha256']=sha(bytes);
    objects.set(key,{bytes,ContentLength:bytes.length,ETag:'"abcdef1234567890"',Metadata:{sha256:sha(bytes)}});
  }
  ownershipItems=[item];
  return {mode:'commit',dataset_id:dataset,operation_id:'00000000-0000-4000-8000-000000000001',
    release_mode:'partial',confirmation:'PUBLISH HELD HEBDEN BRIDGE V2',items:[item]};
}
async function send(body) {
  ownershipItems=body.items ?? ownershipItems;
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
test('matching desktop evidence commits all three PNGs using HEAD only, retaining permanent identity and verified ETag',async()=>{
  const body=fixture(); const result=await send(body);
  assert.equal(result.status,200); assert.equal(result.body.verified_objects,3);
  assert.equal(heads.length,3); assert.equal(commits.length,1);
  assert.equal(commits[0].args.p_items[0].street_key,'permanent-street-1');
  for (const field of ['front','rear','production']) {
    assert.equal(commits[0].args.p_items[0][field+'_etag'],'abcdef1234567890');
    assert.equal(commits[0].args.p_items[0][field+'_sha256'],body.items[0][field+'_sha256']);
  }
});
for (const field of ['front','rear','production']) test(`changed ${field} ETag blocks commit`,async()=>{
  const body=fixture(); objects.get(body.items[0][field+'_key']).ETag='"changed1234567890"';
  await blocked(body,'ETag differs');
});

test('checksum metadata is not used as byte proof by Edge',async()=>{
  const body=fixture(); for (const object of objects.values()) object.Metadata={sha256:'0'.repeat(64)};
  assert.equal((await send(body)).status,200); assert.equal(commits.length,1);
});

for (const field of ['front','rear','production']) test(`missing ${field} ETag blocks commit`,async()=>{
  const body=fixture(); delete body.items[0][field+'_etag']; await blocked(body,'Invalid expected');
});

test('missing object blocks commit',async()=>{
  const body=fixture(); objects.delete(body.items[0].production_key); await blocked(body,'missing');
});
test('missing expected checksum blocks commit',async()=>{
  const body=fixture(); delete body.items[0].front_sha256; await blocked(body,'Invalid expected');
});
test('a mismatch in a later batch prevents the entire commit',async()=>{
  const body=fixture();
  for (let index=2; index<=6; index++) {
    const next={...body.items[0],street_key:'permanent-street-'+index};
    for (const field of ['front','rear','production']) {
      const oldKey=next[field+'_key'];
      const newKey=oldKey.replace('/'+field+'.','/street'+index+'_'+field+'.');
      next[field+'_key']=newKey;
      objects.set(newKey,{...objects.get(oldKey)});
    }
    body.items.push(next);
  }
  objects.get(body.items[5].production_key).ETag='changed1234567890';
  await blocked(body,'ETag differs');
  assert.equal(heads.length,18);
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

test('authenticated preflight returns all ownership roles and no database writes',async()=>{
  fixture();
  const result=await send({mode:'preflight',dataset_id:dataset});
  assert.equal(result.status,200);
  assert.equal(result.body.ownership_complete,true);
  assert.equal(result.body.png_ownership.length,3);
  assert.deepEqual(result.body.png_ownership.map(o=>o.role),['front','rear','production']);
  assert.equal(commits.length,0);assert.equal(heads.length,0);
});
for(const fault of ['query','truncated','count','duplicate','later'])
  test(`incomplete ownership blocks on ${fault}`,async()=>{
    const body=fixture();
    ownershipItems=Array.from({length:501},(_,i)=>({...body.items[0],street_key:'street-'+i}));
    pageFault=fault;
    try {
      const result=await send({mode:'preflight',dataset_id:dataset});
      assert.equal(result.status,503);
      assert.equal(commits.length,0);assert.equal(heads.length,0);
    } finally {pageFault=null;}
  });
test('scalar snapshot includes ownership beyond the default row limit',async()=>{
  const body=fixture();
  ownershipItems=Array.from({length:1001},(_,i)=>({...body.items[0],street_key:'street-'+i}));
  const result=await send({mode:'preflight',dataset_id:dataset});
  assert.equal(result.status,200);assert.equal(result.body.png_ownership.length,3003);
  assert.equal(commits.length,0);
});
test('authentication still blocks preflight and commit',async()=>{
  fixture();
  const response=await handler(new Request('https://offline.invalid',{method:'POST',
    headers:{authorization:'Bearer wrong'},body:JSON.stringify({mode:'preflight',dataset_id:dataset})}));
  assert.equal(response.status,403);assert.equal(commits.length,0);
});

test('preflight includes owners across datasets, statuses, SKUs and versions',async()=>{
  fixture();
  inventoryOverride={designs:[
    {id:'d1',dataset_id:'other-dataset',street_id:'other-street',status:'live'},
    {id:'d2',dataset_id:dataset,street_id:'held-street',status:'hold'}],
    design_assets:[{id:'a1',design_id:'d1',product_sku:'OTHER-SKU',version:7,
      front_mockup_key:'shared/front.png',rear_mockup_key:'shared/rear.png',production_key:'shared/production.png'}]};
  try {
    const result=await send({mode:'preflight',dataset_id:dataset});
    assert.equal(result.status,200);
    const owner=result.body.png_ownership[0];
    assert.equal(owner.dataset_id,'other-dataset');assert.equal(owner.status,'live');
    assert.equal(owner.product_sku,'OTHER-SKU');assert.equal(owner.version,7);
    assert.equal(commits.length,0);assert.equal(heads.length,0);
  } finally {inventoryOverride=null;}
});
test('commit refuses destinations without exact ownership before any R2 checks',async()=>{
  const body=fixture();
  inventoryOverride={designs:[{id:'d',dataset_id:dataset,street_id:'different-street',status:'hold'}],
    design_assets:[{id:'a',design_id:'d',product_sku:'GLOBAL-MUG-W',version:1,
      front_mockup_key:body.items[0].front_key,rear_mockup_key:body.items[0].rear_key,
      production_key:body.items[0].production_key}]};
  try {
    const result=await send(body);
    assert.equal(result.status,409);assert.match(result.body.error,/ownership/);
    assert.equal(commits.length,0);assert.equal(heads.length,0);
  } finally {inventoryOverride=null;}
});

for(const fault of ['missing_rpc','version','asset_count','orders','listed_count','listed_duplicate'])
  test(`snapshot prerequisite or safety failure blocks all writes: ${fault}`,async()=>{
    const body=fixture(); snapshotFault=fault;
    try {
      for(const request of [{mode:'preflight',dataset_id:dataset},body]) {
        const result=await send(request);
        assert.equal(result.status,fault==='orders'?409:503);
        assert.equal(commits.length,0);assert.equal(heads.length,0);
      }
    } finally {snapshotFault=null;}
  });

test('snapshot proposal is one read-only SQL statement with service-role-only access',()=>{
  const sql=readFileSync(new URL('../proposed_upload_snapshot.sql',import.meta.url),'utf8');
  assert.match(sql,/LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ''/);
  assert.match(sql,/REVOKE ALL[^;]+FROM PUBLIC, anon, authenticated/);
  assert.match(sql,/GRANT EXECUTE[^;]+TO service_role/);
  assert.doesNotMatch(sql,/\b(INSERT|UPDATE|DELETE|LOCK)\b/);
});

for(const kind of ['missing-assets','unrelated-conflict','selected-conflict','malformed-selected','oversized','malformed-response'])
 test(`bounded snapshot and selected ownership: ${kind}`,async()=>{
  const body=fixture();
  const d={id:'design-0',dataset_id:dataset,street_id:body.items[0].street_key,status:'hold'};
  const a={id:'asset-0',design_id:d.id,product_sku:'GLOBAL-MUG-W',version:1,
    front_mockup_key:body.items[0].front_key,rear_mockup_key:body.items[0].rear_key,production_key:body.items[0].production_key};
  inventoryOverride={designs:[d],design_assets:[a]};
  if(kind==='missing-assets') inventoryOverride.designs.push({...d,id:'other',street_id:'other'});
  if(kind==='unrelated-conflict') inventoryOverride.design_assets.push(...['x','y'].map(id=>({...a,id,front_mockup_key:'other.png',rear_mockup_key:null,production_key:null})));
  if(kind==='selected-conflict') inventoryOverride.design_assets.push({...a,id:'duplicate'});
  if(kind==='malformed-selected') a.design_id='missing';
  if(kind==='oversized') inventoryOverride.designs=Array.from({length:2001},(_,i)=>({...d,id:'d'+i}));
  if(kind==='malformed-response') inventoryOverride.designs=[null];
  try {
    const result=await send(body);
    assert.equal(result.status, ['missing-assets','unrelated-conflict'].includes(kind)?200:kind==='oversized'||kind==='malformed-response'?503:409);
    if(result.status!==200) {assert.equal(heads.length,0);assert.equal(commits.length,0);}
    if(kind==='unrelated-conflict') {
      const preflight=await send({mode:'preflight',dataset_id:dataset});
      assert.equal(preflight.body.ownership_complete,true);
      assert.ok(preflight.body.ownership_warnings.length);
    }
  } finally {inventoryOverride=null;}
 });
test('replacement SQL clears historical ACLs and verifies trusted execution context',()=>{
 const sql=readFileSync(new URL('../proposed_upload_snapshot.sql',import.meta.url),'utf8');
 for(const pattern of [/aclexplode/,/FROM %I CASCADE/,/OWNER TO postgres/,/rolbypassrls/,/p.prosecdef/,/p.proconfig/,/8388608/,/> 2000/,/ERRCODE = '54000'/]) assert.match(sql,pattern);
});

test('snapshot byte ceiling blocks before R2 even below record limit',async()=>{
 const body=fixture();
 inventoryOverride={designs:[{id:'d',dataset_id:dataset,street_id:'x'.repeat(8388609),status:'hold'}],design_assets:[]};
 try {assert.equal((await send(body)).status,503);assert.equal(heads.length,0);assert.equal(commits.length,0);}
 finally {inventoryOverride=null;}
});

for (const count of [290, 1000, 1001]) test(`publication selection boundary ${count}`, async()=>{
  const body=fixture();
  const template=body.items[0];
  body.items=Array.from({length:count},(_,i)=>{
    const item={...template,street_key:'boundary-'+i};
    for(const field of ['front','rear','production']) {
      const original=template[field+'_key'];
      const key=original.replace('.png','-'+i+'.png');
      item[field+'_key']=key;
      objects.set(key,objects.get(original));
    }
    return item;
  });
  const result=await send(body);
  assert.equal(result.status,count>1000?400:200,JSON.stringify(result.body));
  if(count>1000) {
    assert.match(result.body.error,/1 to 1,000 selected designs per request/);
    assert.equal(heads.length,0);
    assert.equal(commits.length,0);
  } else assert.equal(commits.length,1);
});
