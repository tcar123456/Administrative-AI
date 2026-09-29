import {all,one,sql,run,fail,fields,str,text,choice,uid,encrypt,decrypt,limit,audit} from './core.js';

export class ProviderSession{
  constructor(provider,model,key,prompt,messages,tools){Object.assign(this,{provider,model,key,prompt,messages:[...messages],tools});}
  async step(force=false,timeout=25000){
    const {provider,model,key,prompt,messages,tools}=this;
    let url,headers={'Content-Type':'application/json'},body;
    if(provider==='openai'){
      url='https://api.openai.com/v1/responses';headers.Authorization='Bearer '+key;
      body={model,instructions:prompt,input:messages,tools,parallel_tool_calls:false,store:false,include:['reasoning.encrypted_content'],max_output_tokens:2048};
      if(force)body.tool_choice={type:'function',name:tools[0].name};
    }else if(provider==='gemini'){
      url='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions';headers.Authorization='Bearer '+key;
      body={model,messages:[{role:'system',content:prompt},...messages],tools:tools.map(({name,description,parameters})=>({type:'function',function:{name,description,parameters}})),max_tokens:2048};
      if(force)body.tool_choice={type:'function',function:{name:tools[0].name}};
    }else{
      url='https://api.anthropic.com/v1/messages';headers['x-api-key']=key;headers['anthropic-version']='2023-06-01';
      body={model,system:prompt,messages,tools:tools.map(({name,description,parameters})=>({name,description,input_schema:parameters})),max_tokens:2048};
      if(force)body.tool_choice={type:'tool',name:tools[0].name};
    }
    let data;
    try{const response=await fetch(url,{method:'POST',headers,body:JSON.stringify(body),signal:AbortSignal.timeout(Math.max(1,Math.min(25000,timeout)))});if(!response.ok){await response.body?.cancel();fail(502,'AI 連線失敗，請確認模型、Key 與額度。');}data=await response.json();}
    catch{fail(502,'AI 連線或回應逾時，請稍後重試。');}
    if(provider==='openai'){
      this.messages.push(...data.output);
      return {answer:data.output.filter(x=>x.type==='message').flatMap(x=>x.content||[]).filter(x=>x.type==='output_text').map(x=>x.text).join('\n'),calls:data.output.filter(x=>x.type==='function_call').map(x=>({id:x.call_id,name:x.name,args:x.arguments}))};
    }
    if(provider==='gemini'){
      const message=data.choices[0].message;this.messages.push(message);
      return {answer:message.content||'',calls:(message.tool_calls||[]).map(x=>({id:x.id,name:x.function.name,args:x.function.arguments}))};
    }
    this.messages.push({role:'assistant',content:data.content});
    return {answer:data.content.filter(x=>x.type==='text').map(x=>x.text).join('\n'),calls:data.content.filter(x=>x.type==='tool_use').map(x=>({id:x.id,name:x.name,args:JSON.stringify(x.input)}))};
  }
  results(values){
    if(this.provider==='anthropic')this.messages.push({role:'user',content:values.map(([id,value])=>({type:'tool_result',tool_use_id:id,content:JSON.stringify(value),is_error:!!value.error}))});
    else for(const [id,value] of values)this.messages.push(this.provider==='gemini'?{role:'tool',tool_call_id:id,content:JSON.stringify(value)}:{type:'function_call_output',call_id:id,output:JSON.stringify(value)});
  }
}
export async function probe(provider,model,key){
  const tool={type:'function',name:'connection_check',description:'Return the supplied nonce.',parameters:{type:'object',properties:{nonce:{type:'string'}},required:['nonce'],additionalProperties:false},strict:true};
  const session=new ProviderSession(provider,model,key,'Call connection_check with nonce daywork. After the result say OK.',[{role:'user',content:'Check tool calling.'}],[tool]);
  const result=await session.step(true);
  if(result.calls.length!==1||result.calls[0].name!=='connection_check'||JSON.parse(result.calls[0].args).nonce!=='daywork')fail(502,'工具呼叫測試未通過。');
  session.results([[result.calls[0].id,{ok:true}]]);const final=await session.step();if(!final.answer||final.calls.length)fail(502,'工具往返測試未通過。');
}
function providerData(row,defaultId,admin=false){const value={id:row.id,name:row.name,provider:row.provider,model:row.model,default:row.id===defaultId};return admin?{...value,key_mask:'••••••••'+row.key_suffix,tested:!!row.tested,enabled:!!row.enabled}:value;}
export async function providerRoutes({env,user,path,method,body}){
  if((path==='/api/models'||path==='/api/admin/providers')&&method==='GET'){
    const isAdmin=path.includes('/admin/');const defaultId=(await one(env,"SELECT value FROM options WHERE key='default_provider'"))?.value;
    return (await all(env,'SELECT * FROM providers'+(isAdmin?'':' WHERE enabled=1 AND tested=1')+' ORDER BY name')).map(r=>providerData(r,defaultId,isAdmin));
  }
  const match=path.match(/^\/api\/admin\/providers(?:\/([\w-]+))?(?:\/(test|activate))?$/);if(!match)return undefined;
  const id=match[1],action=match[2];let row=id?await one(env,'SELECT * FROM providers WHERE id=?',id):null;
  if(id&&!row)fail(404,'找不到模型設定。');
  if((!id&&method==='POST')||(id&&!action&&method==='PUT')){
    fields(body,['name','provider','model','api_key']);const name=text(body.name,1,80),provider=choice(body.provider,['openai','anthropic','gemini']),model=str(body.model,1,120,/^[a-zA-Z0-9_.:/-]+$/),key=str(body.api_key,16,1024,/^\S+$/),pid=id||uid();
    const ciphertext=await encrypt(env,key);
    await env.DB.batch([id?sql(env,'UPDATE providers SET name=?,provider=?,model=?,encrypted_key=?,key_suffix=?,revision=revision+1,enabled=0,tested=0 WHERE id=?',name,provider,model,ciphertext,key.slice(-4),pid):sql(env,'INSERT INTO providers(id,name,provider,model,encrypted_key,key_suffix) VALUES(?,?,?,?,?,?)',pid,name,provider,model,ciphertext,key.slice(-4)),sql(env,"DELETE FROM options WHERE key='default_provider' AND value=?",pid),audit(env,user,id?'provider_rotated':'provider_saved',pid)]);
    return providerData(await one(env,'SELECT * FROM providers WHERE id=?',pid),null,true);
  }
  if(action==='test'&&method==='POST'){
    await limit(env,'probe:'+user.id,6,60);
    try{await probe(row.provider,row.model,await decrypt(env,row.encrypted_key));}catch{fail(502,'連線或工具測試未通過，請確認 Key、模型 ID 與額度。');}
    const result=await run(env,'UPDATE providers SET tested=1 WHERE id=? AND revision=?',id,row.revision);if(!result.meta.changes)fail(409,'設定已變更，請重新測試。');
    await audit(env,user,'provider_test_passed',id).run();return {ok:true,message:'連線與工具呼叫往返測試成功，可啟用模型。'};
  }
  if(action==='activate'&&method==='POST'){
    fields(body,['enabled','make_default']);if(typeof body.enabled!=='boolean'||(body.make_default!==undefined&&typeof body.make_default!=='boolean'))fail(422,'狀態格式不正確。');if(body.enabled&&!row.tested)fail(409,'請先通過連線及工具呼叫測試。');
    const statements=[sql(env,'UPDATE providers SET enabled=? WHERE id=? AND revision=? AND (tested=1 OR ?=0)',+body.enabled,id,row.revision,+body.enabled)];
    if(body.enabled){statements.push(sql(env,`INSERT INTO options(key,value) SELECT 'default_provider',id FROM providers WHERE id=? AND enabled=1 AND tested=1 ON CONFLICT(key) DO UPDATE SET value=CASE WHEN ?=1 THEN excluded.value ELSE value END`,id,+(body.make_default||false)));}
    else statements.push(sql(env,"DELETE FROM options WHERE key='default_provider' AND value=?",id));
    statements.push(audit(env,user,body.enabled?'provider_enabled':'provider_disabled',id));const results=await env.DB.batch(statements);if(!results[0].meta.changes)fail(409,'設定已變更，請重新整理。');
    return providerData(await one(env,'SELECT * FROM providers WHERE id=?',id),(await one(env,"SELECT value FROM options WHERE key='default_provider'"))?.value,true);
  }
  if(id&&!action&&method==='DELETE'){
    if(row.enabled)fail(409,'請先停用再刪除。');
    const results=await env.DB.batch([sql(env,'DELETE FROM providers WHERE id=? AND enabled=0',id),sql(env,"DELETE FROM options WHERE key='default_provider' AND value=? AND NOT EXISTS(SELECT 1 FROM providers WHERE id=?)",id,id),audit(env,user,'provider_deleted',id)]);
    if(!results[0].meta.changes)fail(409,'設定已變更，請重新整理。');return {ok:true};
  }
  return undefined;
}
