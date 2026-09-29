import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import worker from '../worker.js';
import {today,addDays,encrypt,decrypt} from '../core.js';
import {search} from '../agent.js';
import {ProviderSession,probe} from '../providers.js';

// Exercise the actual migration and SQL triggers using SQLite, with the D1 binding contract.
class D1 {
  constructor(){this.db=new DatabaseSync(':memory:');this.db.exec('PRAGMA foreign_keys=ON');this.db.exec(readFileSync('cloudflare/migrations/0001_initial.sql','utf8'));}
  prepare(query){
    const db=this.db;
    const make=args=>({bind:(...values)=>make(values),first:async()=>db.prepare(query).get(...args)||null,
      all:async()=>({results:db.prepare(query).all(...args)}),run:async()=>{const info=db.prepare(query).run(...args);return {meta:{changes:Number(info.changes)},results:[]};}});
    return make([]);
  }
  async batch(statements){this.db.exec('BEGIN');try{const result=[];for(const s of statements)result.push(await s.run());this.db.exec('COMMIT');return result;}catch(e){this.db.exec('ROLLBACK');throw e;}}
}
function fixture(){
  const env={DB:new D1(),ENVIRONMENT:'local',PUBLIC_ORIGIN:'http://localhost:8787',ALLOW_DEMO:'true',SETUP_TOKEN:'test-setup-'.padEnd(40,'x'),ENCRYPTION_KEY:'a'.repeat(64),ASSETS:{fetch:async()=>new Response('assets')}};
  const client=()=>({cookie:'',csrf:'',async call(path,method='GET',body,headers={}){
    const r=await worker.fetch(new Request(env.PUBLIC_ORIGIN+path,{method,headers:{Origin:env.PUBLIC_ORIGIN,Cookie:this.cookie,'X-CSRF-Token':this.csrf,'Content-Type':'application/json',...headers},body:body===undefined?undefined:JSON.stringify(body)}),env);
    const cookie=r.headers.get('set-cookie');if(cookie)this.cookie=cookie.split(';')[0];
    let data;try{data=await r.json();}catch{data=null;}if(data?.csrf)this.csrf=data.csrf;return {status:r.status,data,headers:r.headers};
  }});
  return {env,client};
}
const monday=()=>addDays(today(),7-(new Date(today()+'T00:00:00Z').getUTCDay()+6)%7);
const leave=()=>({leave_type:'annual',start_time:monday()+'T13:00:00+08:00',end_time:monday()+'T18:00:00+08:00',reason:'測試私人事務'});
const event=()=>({id:crypto.randomUUID(),title:'展示彩排',start:monday()+'T10:00:00+08:00',end:monday()+'T11:00:00+08:00',location:'會議室 A'});
async function setup(f){const c=f.client();const r=await c.call('/api/auth/setup','POST',{username:'admin',password:'test-password-123',name:'測試管理員',token:f.env.SETUP_TOKEN});assert.equal(r.status,200,JSON.stringify(r.data));return c;}
async function employee(f,admin){const created=await admin.call('/api/admin/accounts','POST',{username:'staff',password:'staff-password-123',name:'測試員工',employee_id:'STAFF001',department:'產品部',annual_hours:24});assert.equal(created.status,200);const c=f.client();assert.equal((await c.call('/api/auth/login','POST',{username:'staff',password:'staff-password-123'})).status,200);return c;}

test('bootstrap, authentication, CSRF, role isolation and session revocation',async()=>{
  const f=fixture(),admin=await setup(f),staff=await employee(f,admin);
  assert.equal((await f.client().call('/api/documents')).status,401);
  assert.equal((await staff.call('/api/admin/providers')).status,403);
  assert.equal((await admin.call('/api/calendar','POST',event(),{Origin:'https://evil.example'})).status,403);
  assert.equal((await admin.call('/api/calendar','POST',event(),{'X-CSRF-Token':'bad'})).status,403);
  assert.equal((await admin.call('/api/auth/setup','POST',{username:'other',password:'test-password-123',name:'Other',token:f.env.SETUP_TOKEN})).status,409);
  const me=(await staff.call('/api/auth/me')).data;
  assert.equal((await admin.call('/api/admin/accounts/'+me.id,'PATCH',{disabled:true})).status,200);
  assert.equal((await staff.call('/api/dashboard')).status,401);
  assert.equal((await admin.call('/api/auth/password','POST',{current_password:'test-password-123',new_password:'updated-password-123'})).status,200);
  assert.equal((await admin.call('/api/dashboard')).status,401);
});

test('calendar CRUD, replay, timezone validation and ownership',async()=>{
  const f=fixture(),admin=await setup(f),staff=await employee(f,admin),body=event();
  const r=await admin.call('/api/calendar','POST',body);assert.equal(r.status,200);assert.match(r.data.start,/10:00:00$/);
  assert.deepEqual((await admin.call('/api/calendar','POST',body)).data,r.data);
  assert.equal((await staff.call('/api/calendar')).data.length,0);
  const update={...body,title:'展示正式會議'};delete update.id;
  assert.equal((await staff.call('/api/calendar/'+body.id,'PUT',update)).status,404);
  assert.equal((await staff.call('/api/calendar/'+body.id,'DELETE')).status,404);
  assert.equal((await admin.call('/api/calendar','POST',{...event(),employee_id:'STAFF001'})).status,422);
  assert.equal((await admin.call('/api/calendar','POST',{...event(),end:body.start})).status,422);
  assert.equal((await admin.call('/api/calendar/'+body.id,'PUT',update)).status,200);
  assert.match((await admin.call('/api/chat','POST',{message:'我下週有哪些會議？'})).data.answer,/展示正式會議/);
  assert.equal((await admin.call('/api/calendar/'+body.id,'DELETE')).status,200);
  assert.equal((await admin.call('/api/calendar')).data.length,0);
});

test('leave transaction triggers prevent overlap/double deductions and keep review history',async()=>{
  const f=fixture(),admin=await setup(f),staff=await employee(f,admin);
  const row=(await staff.call('/api/leaves/draft','POST',leave())).data;
  assert.equal(row.status,'draft');
  for(let i=0;i<2;i++)assert.equal((await staff.call('/api/leaves/'+row.id+'/confirm','POST')).status,200);
  assert.equal((await staff.call('/api/dashboard')).data.balances.find(b=>b.leave_type==='annual').hours,19);
  assert.equal((await staff.call('/api/leaves/draft','POST',leave())).status,422);
  assert.equal((await admin.call('/api/admin/leaves/summary')).data.actionable,1);
  const decision={decision:'rejected',note:'請調整日期'};
  for(let i=0;i<2;i++)assert.equal((await admin.call('/api/admin/leaves/'+row.id+'/review','POST',decision)).status,200);
  assert.equal((await staff.call('/api/dashboard')).data.balances.find(b=>b.leave_type==='annual').hours,24);
  assert.equal((await admin.call('/api/admin/leaves?status=rejected')).data[0].review_note,decision.note);
  assert.deepEqual((await staff.call('/api/leaves/'+row.id+'/events')).data.map(e=>e.action),['draft_created','submitted','rejected']);
  const own=(await admin.call('/api/leaves/draft','POST',leave())).data;
  await admin.call('/api/leaves/'+own.id+'/confirm','POST');
  assert.equal((await admin.call('/api/admin/leaves/'+own.id+'/review','POST',{decision:'approved',note:'self'})).status,403);
});

test('D1 SQL guards serialize stale competing requests and roll back batch failures',async()=>{
  const f=fixture(),admin=await setup(f);
  const a=(await admin.call('/api/leaves/draft','POST',leave())).data;
  const b=(await admin.call('/api/leaves/draft','POST',{...leave(),leave_type:'compensatory'})).data;
  await admin.call('/api/leaves/'+a.id+'/confirm','POST');
  assert.equal((await admin.call('/api/leaves/'+b.id+'/confirm','POST')).status,409);
  assert.deepEqual((await admin.call('/api/dashboard')).data.balances.map(b=>b.hours).sort((a,b)=>a-b),[12,31]);
  await admin.call('/api/leaves/'+a.id+'/withdraw','POST');await admin.call('/api/leaves/'+a.id+'/withdraw','POST');
  assert.equal((await admin.call('/api/dashboard')).data.balances.find(b=>b.leave_type==='annual').hours,36);
  await assert.rejects(()=>f.env.DB.batch([f.env.DB.prepare("UPDATE balances SET hours=0 WHERE employee_id='ADMIN'"),f.env.DB.prepare("INSERT INTO options VALUES('initialized','duplicate')")]));
  assert.equal((await admin.call('/api/dashboard')).data.balances.find(b=>b.leave_type==='annual').hours,36);
});

test('demo follow-ups, citations, chat receipts and tenant isolation',async()=>{
  const f=fixture(),admin=await setup(f),staff=await employee(f,admin);
  assert.equal(search('國外出差一天餐費可以報多少？')[0].source,'travel_policy.md');assert.equal(search('恐龍為何滅絕？').length,0);
  const first=(await admin.call('/api/chat','POST',{message:'幫我請下週五下午特休'})).data;assert.ok(first.pending_leave);
  const body={message:'原因：私人事務',conversation_id:first.conversation_id,request_id:crypto.randomUUID()};
  const result=await admin.call('/api/chat','POST',body);assert.equal(result.status,200);assert.equal(result.data.drafts.length,1);
  assert.deepEqual((await admin.call('/api/chat','POST',body)).data,result.data);
  assert.equal((await admin.call('/api/chat','POST',{...body,message:'changed'})).status,409);
  assert.equal((await staff.call('/api/conversations/'+first.conversation_id)).status,404);
  assert.equal((await staff.call('/api/chat','POST',body)).status,409);
  assert.equal((await admin.call('/api/dashboard')).data.leaves.length,1);
  const policy=(await admin.call('/api/chat','POST',{message:'特休規定是什麼？我還剩多少假？'})).data;
  const docs=(await admin.call('/api/documents')).data;assert.equal(policy.sources[0].fingerprint,docs.find(d=>d.source===policy.sources[0].source).fingerprint);
});

test('vault and provider configuration lifecycle redact secrets and require testing',async()=>{
  const f=fixture(),admin=await setup(f),key='unit-test-only-never-a-real-key';
  const ciphertext=await encrypt(f.env,key);assert.ok(!ciphertext.includes(key));assert.equal(await decrypt(f.env,ciphertext),key);
  const body={name:'Test model',provider:'openai',model:'test-model',api_key:key};const r=await admin.call('/api/admin/providers','POST',body);
  assert.equal(r.status,200);assert.ok(!JSON.stringify(r.data).includes(key));
  const id=r.data.id;assert.equal((await admin.call('/api/admin/providers/'+id+'/activate','POST',{enabled:true})).status,409);
  f.env.DB.db.prepare('UPDATE providers SET tested=1 WHERE id=?').run(id);
  assert.equal((await admin.call('/api/admin/providers/'+id+'/activate','POST',{enabled:true,make_default:true})).status,200);
  assert.equal((await admin.call('/api/models')).data.length,1);
  assert.equal((await admin.call('/api/admin/providers/'+id,'PUT',{...body,api_key:key+'-rotated'})).status,200);
  assert.equal((await admin.call('/api/models')).data.length,0);
  assert.equal((await admin.call('/api/admin/providers/'+id,'DELETE')).status,200);
});

test('three provider protocols round-trip tool calls with fixed endpoints',async()=>{
  const original=globalThis.fetch;
  try{for(const provider of ['openai','anthropic','gemini']){
    let step=0;
    globalThis.fetch=async(url,options)=>{
      const body=JSON.parse(options.body);assert.equal(body.model,'test-model');const first=step++===0;
      if(provider==='openai'){assert.equal(url,'https://api.openai.com/v1/responses');return Response.json({output:first?[{type:'function_call',call_id:'1',name:'connection_check',arguments:'{"nonce":"daywork"}'}]:[{type:'message',content:[{type:'output_text',text:'OK'}]}]});}
      if(provider==='anthropic'){assert.equal(url,'https://api.anthropic.com/v1/messages');return Response.json({content:first?[{type:'tool_use',id:'1',name:'connection_check',input:{nonce:'daywork'}}]:[{type:'text',text:'OK'}]});}
      assert.equal(url,'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions');return Response.json({choices:[{message:first?{role:'assistant',tool_calls:[{id:'1',type:'function',function:{name:'connection_check',arguments:'{"nonce":"daywork"}'},extra_content:{google:{thought_signature:'test-signature'}}}]}:{role:'assistant',content:'OK'}}]});
    };
    await probe(provider,'test-model','mock-key');assert.equal(step,2);
  }}finally{globalThis.fetch=original;}
});

test('cloud deployment fails closed without secrets and adds Secure cookies',async()=>{
  const f=fixture();f.env.ENVIRONMENT='production';f.env.PUBLIC_ORIGIN='https://demo.example.com';
  const c=await setup(f);assert.match((await c.call('/api/auth/login','POST',{username:'admin',password:'test-password-123'})).headers.get('Set-Cookie'),/Secure/);
  f.env.ENCRYPTION_KEY='';assert.equal((await c.call('/api/auth/status')).status,503);
});

test('failed live model turn leaves no drafts, conversation or receipt, and releases its lock',async()=>{
  const f=fixture(),admin=await setup(f),key='unit-test-only-never-a-real-key';
  const provider=(await admin.call('/api/admin/providers','POST',{name:'Failing model',provider:'openai',model:'test-model',api_key:key})).data;
  f.env.DB.db.prepare('UPDATE providers SET tested=1,enabled=1 WHERE id=?').run(provider.id);
  const original=globalThis.fetch;let step=0;
  globalThis.fetch=async()=>step++===0?Response.json({output:[{type:'function_call',call_id:'draft-1',name:'create_leave_request',arguments:JSON.stringify(leave())}]}):new Response('upstream-secret-must-never-escape',{status:500});
  try{
    const result=await admin.call('/api/chat','POST',{message:'請假',provider_id:provider.id,request_id:crypto.randomUUID()});
    assert.equal(result.status,502);assert.ok(!JSON.stringify(result.data).includes('upstream-secret'));
    for(const table of ['leaves','conversations','receipts','locks'])assert.equal(f.env.DB.db.prepare(`SELECT count(*) AS n FROM ${table}`).get().n,0);
  }finally{globalThis.fetch=original;}
});

test('calendar rejects normalized invalid clocks and chat rejects oversized input',async()=>{
  const f=fixture(),admin=await setup(f);
  for(const clock of ['24:00:00','10:60:00','10:00:00.500']){
    assert.equal((await admin.call('/api/calendar','POST',{...event(),start:monday()+'T'+clock+'+08:00'})).status,422);
  }
  assert.equal((await admin.call('/api/chat','POST',{message:'x'.repeat(33000)})).status,413);
});
