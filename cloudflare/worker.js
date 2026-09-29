import {Fault,fail,bodyOf,equal,json,one,run,now} from './core.js';
import {authenticate,admin,authRoutes} from './auth.js';
import {dataRoutes} from './data.js';
import {providerRoutes} from './providers.js';
import {chatRoutes,documents} from './agent.js';

async function route(request,env){
  const url=new URL(request.url),path=url.pathname,method=request.method;
  if(!env.PUBLIC_ORIGIN||url.origin!==env.PUBLIC_ORIGIN.replace(/\/$/,''))fail(400,'網站來源不正確，請確認 PUBLIC_ORIGIN。');
  if(!path.startsWith('/api/'))return env.ASSETS.fetch(request);
  if(env.ENVIRONMENT!=='local'&&(!env.PUBLIC_ORIGIN.startsWith('https://')||!/^[0-9a-f]{64}$/i.test(env.ENCRYPTION_KEY||'')||(env.SETUP_TOKEN||'').length<32))fail(503,'請先完成 PUBLIC_ORIGIN、ENCRYPTION_KEY 與 SETUP_TOKEN 部署設定。');
  const unsafe=!['GET','HEAD','OPTIONS'].includes(method);
  if(unsafe&&request.headers.get('Origin')!==url.origin)fail(403,'請從本站操作。');
  const body=unsafe?await bodyOf(request):{};
  const publicRoute=['/api/health','/api/auth/status','/api/auth/setup','/api/auth/login'].includes(path);
  const user=publicRoute?null:await authenticate(request,env);
  if(user&&unsafe&&!await equal(request.headers.get('X-CSRF-Token')||'',user.csrf))fail(403,'驗證已過期，請重新整理頁面。');
  if(path.startsWith('/api/admin/'))admin(user);
  if(path==='/api/health'&&method==='GET'){await one(env,'SELECT 1');return {status:'ok',mode:env.ALLOW_DEMO==='true'?'demo':'configured',checks:{database:true,knowledge:documents.length>0}};}
  const context={request,env,path,method,body,user,url};
  for(const handler of [authRoutes,providerRoutes,dataRoutes,chatRoutes]){const response=await handler(context);if(response!==undefined)return response;}
  fail(404,'找不到此操作。');
}
export default {
  async fetch(request,env){
    let response;
    try{const result=await route(request,env);response=result instanceof Response?result:json(result);}
    catch(error){
      // SQL constraint names are mapped to safe messages, never expose SQL, payloads or provider errors.
      const known=[['insufficient_balance','假期餘額不足。'],['leave_overlap','此時段與已提交的請假重疊。'],['leave_started','請假時段已開始，無法操作。'],['invalid_transition','申請狀態已變更，請重新整理。'],['invalid_reviewer','無法審核這筆申請。'],['UNIQUE constraint','資料已存在或由另一個操作更新。']].find(([key])=>String(error.message).includes(key));
      response=json({detail:error instanceof Fault?error.message:known?.[1]||'服務暫時無法處理，本次操作未完成。請稍後重試。'},error instanceof Fault?error.status:known?409:503);
    }
    response=new Response(response.body,response);
    const headers={'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'same-origin','Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"};
    if(env.ENVIRONMENT!=='local')headers['Strict-Transport-Security']='max-age=31536000';
    if(response.status===429)headers['Retry-After']='60';
    for(const [key,value] of Object.entries(headers))response.headers.set(key,value);
    return response;
  },
  async scheduled(_controller,env){
    await env.DB.batch([
      env.DB.prepare('DELETE FROM sessions WHERE expires<=?').bind(now()),
      env.DB.prepare('DELETE FROM rate_limits WHERE expires<=?').bind(now()),
      env.DB.prepare('DELETE FROM locks WHERE expires<=?').bind(now())
    ]);
  }
};
