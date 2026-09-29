export class Fault extends Error { constructor(status,message){super(message);this.status=status;} }
export const fail=(status,message)=>{throw new Fault(status,message);};
export const uid=()=>crypto.randomUUID();
export const now=()=>Date.now();
export const stamp=()=>new Date(now()+8*3600000).toISOString().slice(0,19);
export const today=()=>stamp().slice(0,10);
export const addDays=(day,n)=>new Date(Date.parse(day+'T00:00:00Z')+n*86400000).toISOString().slice(0,10);
export const sql=(env,q,...args)=>env.DB.prepare(q).bind(...args);
export const one=(env,q,...args)=>sql(env,q,...args).first();
export const all=async(env,q,...args)=>(await sql(env,q,...args).all()).results;
export const run=(env,q,...args)=>sql(env,q,...args).run();
export const json=(value,status=200,headers={})=>Response.json(value,{status,headers});
export const audit=(env,user,action,target)=>sql(env,'INSERT INTO admin_events(actor,action,target) VALUES(?,?,?)',user.username,action,target);
export function fields(body,allowed){if(!body||typeof body!=='object'||Array.isArray(body)||Object.keys(body).some(k=>!allowed.includes(k)))fail(422,'資料格式不正確。');}
export function str(value,min=1,max=200,pattern){if(typeof value!=='string'||value.length<min||value.length>max||(pattern&&!pattern.test(value)))fail(422,'資料格式或長度不正確。');return value;}
export const text=(value,min=1,max=200)=>str(typeof value==='string'?value.trim():value,min,max);
export const uuid=value=>str(value,36,36,/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
export function choice(value,values){if(!values.includes(value))fail(422,'選項不正確。');return value;}
export function date(value){str(value,10,10,/^\d{4}-\d{2}-\d{2}$/);const d=new Date(value+'T00:00:00Z');if(!Number.isFinite(+d)||d.toISOString().slice(0,10)!==value)fail(422,'日期不存在。');return value;}
export function localTime(value){
  str(value,16,40,/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.0{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$/);date(value.slice(0,10));
  const normalized=/Z$|[+-]\d{2}:\d{2}$/.test(value)?value:value+'+08:00';
  const time=Date.parse(normalized);if(!Number.isFinite(time))fail(422,'時間格式不正確。');
  return new Date(time+8*3600000).toISOString().slice(0,19);
}
const encoder=new TextEncoder();
const hex=bytes=>Array.from(new Uint8Array(bytes),b=>b.toString(16).padStart(2,'0')).join('');
const unhex=value=>Uint8Array.from(value.match(/../g)||[],s=>parseInt(s,16));
export const hash=async value=>hex(await crypto.subtle.digest('SHA-256',encoder.encode(value)));
export async function equal(a,b){const [x,y]=await Promise.all([hash(a),hash(b)]);let diff=0;for(let i=0;i<x.length;i++)diff|=x.charCodeAt(i)^y.charCodeAt(i);return diff===0;}
export const random=()=>hex(crypto.getRandomValues(new Uint8Array(32)));
export async function passwordHash(password,salt=random()){
  const key=await crypto.subtle.importKey('raw',encoder.encode(password),'PBKDF2',false,['deriveBits']);
  const bits=await crypto.subtle.deriveBits({name:'PBKDF2',hash:'SHA-256',salt:unhex(salt),iterations:100000},key,256);
  return `pbkdf2:100000:${salt}:${hex(bits)}`;
}
export async function verifyPassword(password,encoded){const parts=encoded.split(':');return parts.length===4&&await equal(await passwordHash(password,parts[2]),encoded);}
async function vaultKey(env){if(!/^[0-9a-f]{64}$/i.test(env.ENCRYPTION_KEY||''))fail(503,'請先設定 64 位十六進位 ENCRYPTION_KEY。');return crypto.subtle.importKey('raw',unhex(env.ENCRYPTION_KEY),'AES-GCM',false,['encrypt','decrypt']);}
export async function encrypt(env,value){const iv=crypto.getRandomValues(new Uint8Array(12));return hex(iv)+':'+hex(await crypto.subtle.encrypt({name:'AES-GCM',iv},await vaultKey(env),encoder.encode(value)));}
export async function decrypt(env,value){const [iv,data]=value.split(':');return new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:unhex(iv)},await vaultKey(env),unhex(data)));}
export async function limit(env,key,max,seconds){
  const t=now();
  const row=await one(env,`INSERT INTO rate_limits(key,hits,expires) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET hits=CASE WHEN expires<=? THEN 1 ELSE hits+1 END, expires=CASE WHEN expires<=? THEN excluded.expires ELSE expires END RETURNING hits`,key,t+seconds*1000,t,t);
  if(row.hits>max)fail(429,'操作次數過多，請稍後再試。');
}
export async function bodyOf(request){
  const reader=request.body?.getReader();if(!reader)return {};
  const chunks=[];let size=0;
  while(true){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>32768){await reader.cancel();fail(413,'請求內容過大。');}chunks.push(value);}
  const bytes=new Uint8Array(size);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.length;}
  try{return JSON.parse(new TextDecoder().decode(bytes)||'{}');}catch{fail(422,'JSON 格式不正確。');}
}
