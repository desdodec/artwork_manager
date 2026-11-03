// Supabase Edge Function: street-face-publish-v2
// Operator-only publish endpoint; no order submission, Stripe or Prodigi calls.
// Deploy with legacy JWT verification OFF, behind STREET_FACE_PUBLISH_TOKEN.
import { createClient } from 'npm:@supabase/supabase-js@2';
import { S3Client, HeadObjectCommand } from 'npm:@aws-sdk/client-s3@3';

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
  front_key:string; rear_key:string; production_key:string;svg_key:string;
  front_size?:number;rear_size?:number;production_size?:number;
  front_etag?:string;rear_etag?:string;production_etag?:string;
};
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
  const {data:designs,error:de}=await db.from('designs').select('id,street_id,status')
    .eq('dataset_id',DATASET).limit(2001);
  if(de||!designs) return json(503,{error:'Catalogue safety check failed'});
  if(designs.some(d=>d.status!=='hold')) return json(409,{error:'Non-held design exists: release blocked'});
  if(designs.length) {
    const {data:lines,error:le}=await db.from('checkout_order_lines')
      .select('design_id').in('design_id',designs.map(d=>d.id)).limit(1);
    if(le) return json(503,{error:'Cannot verify checkout safety'});
    if(lines?.length) return json(409,{error:'Customer order lines exist: release blocked'});
  }
  if(body.mode==='preflight') {
    const {data:listed,error:ie}=await db.from('street_face_published_items')
      .select('street_key').eq('dataset_id',DATASET).eq('listed',true).limit(2001);
    if(ie) return json(503,{error:'Publication SQL not installed'});
    return json(200,{mode:'preflight',safe_to_upload:true,
      existing_held_designs:designs.length,
      currently_listed_keys:(listed??[]).map(row=>row.street_key),
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
  if(!Array.isArray(items)||items.length<1||items.length>1000)
    return json(400,{error:'Invalid design count'});
  const keys=new Set<string>();
  const checks:Array<{item:Art; field:string; key:string}>=[];
  for(const item of items as Art[]) {
    if(!item||typeof item.street_key!=='string'||keys.has(item.street_key)||
       !item.street_name||!item.geometry || !['LineString','MultiLineString'].includes(item.geometry.type))
      return json(400,{error:'Duplicate or malformed geographic identity'});
    keys.add(item.street_key);
    const fields:[string,string][]=[['front',item.front_key],['rear',item.rear_key],
      ['production',item.production_key],['svg',item.svg_key]];
    for(const [field,key] of fields) {
      if(typeof key!=='string'||!key.startsWith(PREFIX)||key.includes('..')||key.includes('\\'))
        return json(400,{error:'Unsafe R2 key',street_key:item.street_key});
      const folder = field==='front'||field==='rear' ? 'mockups/' : 'print_candidates/';
      const ext = field==='svg' ? '.svg' : '.png';
      if(!key.startsWith(PREFIX+folder)||!key.endsWith(ext))
        return json(400,{error:'Wrong R2 file path',street_key:item.street_key});
      checks.push({item,field,key});
    }
  }
  const r2=new S3Client({region:'auto',
    endpoint:`https://${Deno.env.get('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com`,
    credentials:{accessKeyId:Deno.env.get('R2_ACCESS_KEY_ID')!,
      secretAccessKey:Deno.env.get('R2_SECRET_ACCESS_KEY')!},maxAttempts:2});
  const bucket=Deno.env.get('R2_BUCKET')!;
  const failures:Array<{street_key:string;kind:string}>=[];
  for(let start=0;start<checks.length;start+=20) {
    const batch=await Promise.all(checks.slice(start,start+20).map(async check=>{
      try {
        const head=await r2.send(new HeadObjectCommand({Bucket:bucket,Key:check.key}));
        const etag=(head.ETag??'').replace(/^"|"$/g,'');
        if(!head.ContentLength||head.ContentLength<100||!/^[A-Za-z0-9_-]{8,128}$/.test(etag))
          return {street_key:check.item.street_key,kind:check.field};
        if(check.field!=='svg') {
          (check.item as unknown as Record<string,unknown>)[check.field+'_size']=head.ContentLength;
          (check.item as unknown as Record<string,unknown>)[check.field+'_etag']=etag;
        }
        return null;
      }catch{return {street_key:check.item.street_key,kind:check.field};}
    }));
    for(const error of batch) if(error) failures.push(error);
  }
  if(failures.length) return json(409,{error:'R2 verification failed, Supabase unchanged',
    count:failures.length,first_failures:failures.slice(0,20)});
  const {data,error}=await db.rpc('street_face_publish_held_v2',{
    p_operation_id:operation,p_mode:body.release_mode,p_items:items});
  if(error) return json(409,{error:'Supabase rejected atomic publication',reason:error.message.slice(0,250)});
  return json(200,{verified_objects:checks.length,db_result:data,
    checkout_enabled:false,prodigi_enabled:false});
});
