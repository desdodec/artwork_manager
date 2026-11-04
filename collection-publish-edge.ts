// Supabase Edge Function: street-face-publish-v2
// Operator-only publish endpoint; no order submission, Stripe or Prodigi calls.
// Deploy with legacy JWT verification OFF, behind STREET_FACE_PUBLISH_TOKEN.
import { createClient } from 'npm:@supabase/supabase-js@2';
import { S3Client, HeadObjectCommand } from 'npm:@aws-sdk/client-s3@3';
import { verifyArtwork } from './collection-artwork-verification.mjs';

const MAX_PUBLISH_DESIGNS = 1000;
const DATASET = 'street-face/hebden-bridge/v2';
const PREFIX = 'street-face/hebden-bridge/v2/';
const json = (code: number, body: unknown) => new Response(JSON.stringify(body), {
  status: code, headers: {'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store'}
});
async function digest(value: string) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
}
async function authorized(header: string, secret: string) {
  const [a,b] = await Promise.all([digest(header), digest('Bearer '+secret)]);
  let diff=a.length^b.length;
  for(let i=0;i<Math.min(a.length,b.length);i++) diff|=a[i]^b[i];
  return diff===0;
}
type Art = {
  street_key:string; street_name:string; postcode_district:string;
  geometry:{type:string;coordinates:unknown};
  front_key:string; rear_key:string; production_key:string;
  front_size?:number;rear_size?:number;production_size?:number;
  front_sha256?:string;rear_sha256?:string;production_sha256?:string;
  front_etag?:string;rear_etag?:string;production_etag?:string;
};
// One scalar JSON result avoids PostgREST row limits and cross-page snapshots.
// No fallback: the separately reviewed read-only RPC must be installed first.
async function ownershipSnapshot(db:any) {
  const {data,error}=await db.rpc('street_face_upload_snapshot_v1');
  if(error || !data || data.snapshot_version!==1) throw new Error('Snapshot RPC unavailable');
  if(new TextEncoder().encode(JSON.stringify(data)).length>8388608 ||
     ['designs_count','design_assets_count','listed_count'].some(k=>data[k]>2000))
    throw new Error('Ownership snapshot limit exceeded; uploads blocked');
  for(const table of ['designs','design_assets']) {
    const rows=data[table];
    if(!Array.isArray(rows) || data[table+'_count']!==rows.length ||
       rows.some((r:any)=>!r || typeof r.id!=='string'||!r.id) ||
       new Set(rows.map((r:any)=>r.id)).size!==rows.length)
      throw new Error('Incomplete or duplicate ownership snapshot');
  }
  if(!Number.isSafeInteger(data.checkout_order_lines)||data.checkout_order_lines<0 ||
     !Array.isArray(data.listed_keys)||data.listed_count!==data.listed_keys.length ||
     data.listed_keys.some((k:any)=>typeof k!=='string'||!k)||
     new Set(data.listed_keys).size!==data.listed_keys.length)
    throw new Error('Incomplete safety snapshot');
  return data;
}
function pngOwnership(snapshot:any) {
  const designs=snapshot.designs;
  const assets=snapshot.design_assets;
  const byId=new Map(designs.map(d=>[d.id,d]));
  const assignments:any[]=[];
  for(const a of assets) {
    const d=byId.get(a.design_id);
    // Retain malformed assignments by destination so selected keys fail closed.
    // Missing assets on other designs do not imply an incomplete database read.
    for(const [role,column] of [['front','front_mockup_key'],['rear','rear_mockup_key'],['production','production_key']]) {
      const key=a[column];
      if(key===null || key===undefined) continue;
      if(typeof key!=='string' || !key) throw new Error('Invalid ownership destination');
      assignments.push({key,role,dataset_id:d?.dataset_id,street_key:d?.street_id,
        design_id:d?.id,status:d?.status,asset_id:a.id,product_sku:a.product_sku,version:a.version});
    }
  }
  return assignments;
}
Deno.serve(async request => {
  if(request.method!=='POST') return json(405,{error:'POST required'});
  const token=Deno.env.get('STREET_FACE_PUBLISH_TOKEN')??'';
  if(token.length<32 || !(await authorized(request.headers.get('authorization')??'',token)))
    return json(403,{error:'Forbidden'});
  const required=['SUPABASE_URL','SUPABASE_SERVICE_ROLE_KEY','R2_ACCOUNT_ID',
    'R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_BUCKET'];
  const missing=required.filter(name=>!Deno.env.get(name));
  if(missing.length) return json(503,{error:'Missing configuration',missing_secret_names:missing});
  let body:Record<string,unknown>;
  try {body=await request.json();}catch{return json(400,{error:'Invalid JSON'});}
  if(body.dataset_id!==DATASET || !['preflight','commit'].includes(String(body.mode)))
    return json(400,{error:'Invalid dataset or mode'});
  const db=createClient(Deno.env.get('SUPABASE_URL')!,Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!,
    {auth:{persistSession:false,autoRefreshToken:false}});
  let snapshot, ownership;
  try {snapshot=await ownershipSnapshot(db); ownership=pngOwnership(snapshot);}
  catch {return json(503,{error:'Ownership snapshot unavailable, malformed or limit exceeded; uploads blocked'});}
  const designs=snapshot.designs.filter((d:any)=>d.dataset_id===DATASET);
  if(designs.some((d:any)=>d.status!=='hold')) return json(409,{error:'Non-held design exists: release blocked'});
  if(snapshot.checkout_order_lines) return json(409,{error:'Customer order lines exist: release blocked'});
  if(body.mode==='preflight') {
    return json(200,{mode:'preflight',safe_to_upload:true,
      // ownership_complete means all database rows read, not global consistency.
      ownership_snapshot_version:1,ownership_complete:true,png_ownership:ownership,
      ownership_warnings:ownership.filter(o=>ownership.filter(x=>x.key===o.key).length>1 ||
        ![o.dataset_id,o.street_key,o.design_id,o.asset_id,o.status,o.product_sku].every(v=>typeof v==='string'&&v.trim()) ||
        !Number.isInteger(o.version)).length ? ['Catalogue ownership conflicts exist; selected destinations require separate validation.'] : [],
      existing_held_designs:designs.length,
      currently_listed_keys:snapshot.listed_keys,
      checkout_order_lines:0,checkout_enabled:false,prodigi_enabled:false});
  }
  if(body.confirmation!=='PUBLISH HELD HEBDEN BRIDGE V2')
    return json(400,{error:'Exact operator confirmation required'});
  const operation=String(body.operation_id??'');
  if(!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(operation))
    return json(400,{error:'Invalid operation ID'});
  if(!['complete','partial'].includes(String(body.release_mode)))
    return json(400,{error:'Invalid collection update mode'});
  const items=body.items;
  if(!Array.isArray(items)||items.length<1||items.length>MAX_PUBLISH_DESIGNS)
    return json(400,{error:'Publish Collection requires 1 to 1,000 selected designs per request. Select fewer designs and prepare again.'});
  const keys=new Set<string>();
  const checks:Array<{item:Art; field:string; key:string}>=[];
  for(const item of items as Art[]) {
    if(!item||typeof item.street_key!=='string'||keys.has(item.street_key)||
       !item.street_name||!item.geometry || !['LineString','MultiLineString'].includes(item.geometry.type))
      return json(400,{error:'Duplicate or malformed geographic identity'});
    keys.add(item.street_key);
    const fields:[string,string][]=[['front',item.front_key],['rear',item.rear_key],
      ['production',item.production_key]];
    for(const [field,key] of fields) {
      if(typeof key!=='string'||!key.startsWith(PREFIX)||key.includes('..')||key.includes('\\'))
        return json(400,{error:'Unsafe R2 key',street_key:item.street_key});
      const folder = field==='front'||field==='rear' ? 'mockups/' : 'print_candidates/';
      const ext = '.png';
      if(!key.startsWith(PREFIX+folder)||!key.endsWith(ext))
        return json(400,{error:'Wrong R2 file path',street_key:item.street_key});
      checks.push({item,field,key});
    }
  }
  try {
    // Reuse the single-statement ownership/safety snapshot above.
    const destinations=new Set();
    const identities=new Map();
    for(const {item,field,key} of checks) {
      const owners=ownership.filter(o=>o.key===key);
      const o=owners[0];
      if(!o || ![o.design_id,o.asset_id,o.street_key].every(v=>typeof v==='string'&&v.trim()) || destinations.has(key)||owners.length!==1||o.dataset_id!==DATASET||o.street_key!==item.street_key||
         o.role!==field||o.product_sku!=='GLOBAL-MUG-W'||o.version!==1||o.status!=='hold')
        return json(409,{error:'PNG ownership collision or missing assignment; Supabase unchanged'});
      const identity=JSON.stringify([o.design_id,o.asset_id]);
      if(identities.has(item.street_key)&&identities.get(item.street_key)!==identity)
        return json(409,{error:'Conflicting ownership identity; Supabase unchanged'});
      identities.set(item.street_key,identity);
      destinations.add(key);
    }
  } catch {return json(503,{error:'PNG ownership inventory incomplete; Supabase unchanged'});}
  const r2=new S3Client({region:'auto',
    endpoint:`https://${Deno.env.get('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com`,
    credentials:{accessKeyId:Deno.env.get('R2_ACCESS_KEY_ID')!,
      secretAccessKey:Deno.env.get('R2_SECRET_ACCESS_KEY')!},maxAttempts:2});
  const bucket=Deno.env.get('R2_BUCKET')!;
  const failures=await verifyArtwork(checks, (key:string) =>
    r2.send(new HeadObjectCommand({Bucket:bucket,Key:key})));
  if(failures.length) return json(409,{error:'R2 verification failed, Supabase unchanged',
    count:failures.length,first_failures:failures.slice(0,20)});
  const {data,error}=await db.rpc('street_face_publish_held_v2',{
    p_operation_id:operation,p_mode:body.release_mode,p_items:items});
  if(error) return json(409,{error:'Supabase rejected atomic publication',reason:error.message.slice(0,250)});
  return json(200,{verified_objects:checks.length,db_result:data,
    checkout_enabled:false,prodigi_enabled:false});
});
