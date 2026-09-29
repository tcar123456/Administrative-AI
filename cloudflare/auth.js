import {all,one,sql,run,fail,fields,str,text,choice,uid,random,hash,equal,passwordHash,verifyPassword,limit,now,json,audit} from './core.js';

export const accountData=row=>({id:row.id,username:row.username,employee_id:row.employee_id,name:row.name,department:row.department,role:row.role,disabled:!!row.disabled});
export async function authenticate(request,env){
  const token=request.headers.get('Cookie')?.match(/(?:^|;\s*)daywork_session=([^;]+)/)?.[1]||'';
  const row=await one(env,'SELECT a.*, s.csrf FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=? AND s.expires>? AND a.disabled=0',await hash(token),now());
  if(!row)fail(401,'登入已過期，請重新登入。');
  return {...accountData(row),csrf:row.csrf,token_hash:await hash(token)};
}
export function admin(user){if(user.role!=='admin')fail(403,'此操作僅限管理員。');}
function loginFields(body){return {username:str(body.username,3,80,/^[a-zA-Z0-9_.@-]+$/).toLowerCase(),password:str(body.password,1,256)};}
async function session(env,user){
  const token=random(),csrf=random();
  return {csrf, statements:[sql(env,'DELETE FROM sessions WHERE expires<=?',now()),sql(env,'INSERT INTO sessions VALUES(?,?,?,?)',await hash(token),user.id,csrf,now()+8*3600000)],cookie:`daywork_session=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800${env.ENVIRONMENT==='local'?'':'; Secure'}`};
}
function accountStatements(env,user,annual,compensatory){return [sql(env,'INSERT INTO accounts(id,username,employee_id,name,department,role,password_hash) VALUES(?,?,?,?,?,?,?)',user.id,user.username,user.employee_id,user.name,user.department,user.role,user.password_hash),...[['annual',annual],['compensatory',compensatory]].map(([kind,hours])=>sql(env,'INSERT INTO balances VALUES(?,?,?)',user.employee_id,kind,hours))];}
export async function authRoutes({request,env,path,method,body,user}){
  if(path==='/api/auth/status'&&method==='GET'){const needed=!await one(env,'SELECT id FROM accounts LIMIT 1');return {setup_required:needed,setup_available:needed&&!!env.SETUP_TOKEN,environment:env.ALLOW_DEMO==='true'?'cloudflare-demo':'production'};}
  if(path==='/api/auth/setup'&&method==='POST'){
    fields(body,['username','password','name','token']);const values=loginFields(body);str(values.password,12,128);text(body.name,1,80);str(body.token,32,256);
    await limit(env,'setup',5,900);
    if(await one(env,'SELECT id FROM accounts LIMIT 1'))fail(409,'初始化已完成，請登入。');
    if(!env.SETUP_TOKEN||env.SETUP_TOKEN.length<32||!await equal(body.token,env.SETUP_TOKEN))fail(403,'初始化代碼不正確。');
    const row={id:uid(),username:values.username,employee_id:'ADMIN',name:body.name.trim(),department:'管理',role:'admin',password_hash:await passwordHash(values.password)};
    const login=await session(env,row);
    await env.DB.batch([sql(env,"INSERT INTO options VALUES('initialized','1')"),...accountStatements(env,row,env.ALLOW_DEMO==='true'?36:0,env.ALLOW_DEMO==='true'?12:0),...login.statements,audit(env,row,'setup',row.id)]);
    return json({...accountData(row),csrf:login.csrf},200,{'Set-Cookie':login.cookie});
  }
  if(path==='/api/auth/login'&&method==='POST'){
    fields(body,['username','password']);const values=loginFields(body);
    await limit(env,'login-ip:'+await hash(request.headers.get('CF-Connecting-IP')||'local'),60,900);
    await limit(env,'login-user:'+values.username,10,900);
    const row=await one(env,'SELECT * FROM accounts WHERE username=?',values.username);
    const valid=await verifyPassword(values.password,row?.password_hash||`pbkdf2:100000:${'0'.repeat(64)}:${'0'.repeat(64)}`);
    if(!valid||!row||row.disabled)fail(401,'帳號或密碼不正確，或帳號已停用。');
    const login=await session(env,row);await env.DB.batch(login.statements);
    return json({...accountData(row),csrf:login.csrf},200,{'Set-Cookie':login.cookie});
  }
  if(path==='/api/auth/me'&&method==='GET')return {...accountData(user),csrf:user.csrf};
  if(path==='/api/auth/logout'&&method==='POST'){await run(env,'DELETE FROM sessions WHERE token_hash=?',user.token_hash);return json({ok:true},200,{'Set-Cookie':'daywork_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'});}
  if(path==='/api/auth/password'&&method==='POST'){
    fields(body,['current_password','new_password']);str(body.current_password,1,256);str(body.new_password,12,128);await limit(env,'password:'+user.id,5,900);
    const row=await one(env,'SELECT password_hash FROM accounts WHERE id=?',user.id);
    if(!await verifyPassword(body.current_password,row.password_hash))fail(403,'目前密碼不正確。');
    await env.DB.batch([sql(env,'UPDATE accounts SET password_hash=? WHERE id=?',await passwordHash(body.new_password),user.id),sql(env,'DELETE FROM sessions WHERE account_id=?',user.id),audit(env,user,'password_changed',user.id)]);return {ok:true};
  }
  if(path==='/api/admin/accounts'&&method==='GET')return (await all(env,'SELECT * FROM accounts ORDER BY username')).map(accountData);
  if(path==='/api/admin/accounts'&&method==='POST'){
    fields(body,['username','password','employee_id','name','department','role','annual_hours','compensatory_hours']);const values=loginFields(body);str(values.password,12,128);
    const row={id:uid(),username:values.username,password_hash:await passwordHash(values.password),employee_id:str(body.employee_id,1,32,/^[a-zA-Z0-9_-]+$/),name:text(body.name,1,80),department:text(body.department,1,80),role:choice(body.role||'employee',['admin','employee'])};
    const hours=[body.annual_hours??0,body.compensatory_hours??0];if(hours.some(h=>typeof h!=='number'||!Number.isFinite(h)||h<0||h>2000||h*2%1))fail(422,'額度需為 0–2000 小時，以半小時為單位。');
    await env.DB.batch([...accountStatements(env,row,...hours),audit(env,user,'account_created',row.id)]);return accountData(row);
  }
  const account=path.match(/^\/api\/admin\/accounts\/([\w-]+)$/);
  if(account&&method==='PATCH'){
    fields(body,['disabled']);if(typeof body.disabled!=='boolean')fail(422,'狀態格式不正確。');
    if(account[1]===user.id)fail(409,'不可停用自己的帳號。');
    if(!await one(env,'SELECT id FROM accounts WHERE id=?',account[1]))fail(404,'找不到帳號。');
    await env.DB.batch([sql(env,'UPDATE accounts SET disabled=? WHERE id=?',+body.disabled,account[1]),sql(env,'DELETE FROM sessions WHERE account_id=?',account[1]),audit(env,user,body.disabled?'account_disabled':'account_enabled',account[1])]);return accountData(await one(env,'SELECT * FROM accounts WHERE id=?',account[1]));
  }
  if(path==='/api/admin/events'&&method==='GET')return all(env,'SELECT actor,action,target,at FROM admin_events ORDER BY id DESC LIMIT 100');
  return undefined;
}
