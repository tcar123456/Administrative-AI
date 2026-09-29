import {all,one,sql,run,fail,fields,text,choice,uuid,uid,today,stamp,addDays,date,localTime,audit} from './core.js';
export const labels={annual:'特休',compensatory:'補休'};
export const leaveData=r=>({...r,label:labels[r.leave_type]});
export async function balances(env,eid){return (await all(env,'SELECT leave_type,hours FROM balances WHERE employee_id=?',eid)).map(b=>({...b,label:labels[b.leave_type],days:b.hours/8}));}
export async function calendar(env,eid,start=today(),end=addDays(start,30)){
  date(start);date(end);if(end<start||Date.parse(end)-Date.parse(start)>90*86400000)fail(422,'行程查詢區間需為 0–90 天。');
  return all(env,'SELECT id,title,start,end,location FROM calendar WHERE employee_id=? AND start<? AND end>? ORDER BY start',eid,addDays(end,1)+'T00:00:00',start+'T00:00:00');
}
export function leaveInput(body){
  fields(body,['leave_type','start_time','end_time','reason']);const kind=choice(body.leave_type,['annual','compensatory']);
  const start=localTime(body.start_time),end=localTime(body.end_time),day=start.slice(0,10),weekday=new Date(day+'T00:00:00Z').getUTCDay();
  if(start<=stamp()||day>addDays(today(),365))fail(422,'請選擇未來一年內的工作時段。');
  if(day!==end.slice(0,10)||start>=end||weekday===0||weekday===6)fail(422,'目前支援同一工作日（週一至週五）的請假。');
  const s=start.slice(11),e=end.slice(11);
  if(!/:([03]0):00$/.test(s)||!/:([03]0):00$/.test(e))fail(422,'請以 30 分鐘為單位申請。');
  if(s<'09:00:00'||e>'18:00:00'||(s>='12:00:00'&&s<'13:00:00')||(e>'12:00:00'&&e<='13:00:00'))fail(422,'工作時段為 09:00–12:00、13:00–18:00。');
  const hours=(Date.parse(end+'+08:00')-Date.parse(start+'+08:00'))/3600000-(s<'12:00:00'&&e>'13:00:00'?1:0);
  return {leave_type:kind,start,end,hours,reason:text(body.reason,1,200)};
}
export async function draft(env,eid,body){
  const value=leaveInput(body);
  const account=await one(env,'SELECT hours FROM balances WHERE employee_id=? AND leave_type=?',eid,value.leave_type);
  if(!account||account.hours<value.hours)fail(422,'假期餘額不足，請查詢其他假別。');
  if(await one(env,"SELECT id FROM leaves WHERE employee_id=? AND status IN ('pending','approved') AND start<? AND end>?",eid,value.end,value.start))fail(422,'此時段與已提交的請假申請重疊。');
  const existing=await one(env,"SELECT * FROM leaves WHERE employee_id=? AND status='draft' AND leave_type=? AND start=? AND end=? AND reason=?",eid,value.leave_type,value.start,value.end,value.reason);
  return leaveData(existing||{id:uid(),employee_id:eid,...value,status:'draft'});
}
export const insertDraft=(env,row)=>sql(env,"INSERT INTO leaves(id,employee_id,leave_type,start,end,hours,reason) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",row.id,row.employee_id,row.leave_type,row.start,row.end,row.hours,row.reason);
async function ownLeave(env,user,id){const row=await one(env,'SELECT * FROM leaves WHERE id=? AND employee_id=?',uuid(id),user.employee_id);if(!row)fail(404,'找不到此申請。');return row;}
function eventInput(body){
  fields(body,['id','title','start','end','location']);const start=localTime(body.start),end=localTime(body.end);
  if(end<=start||Date.parse(end+'+08:00')-Date.parse(start+'+08:00')>7*86400000)fail(422,'行程結束時間必須晚於開始，且最長為七天。');
  return {title:text(body.title,1,120),start,end,location:text(body.location??'',0,200)};
}
export async function dataRoutes({env,user,path,method,body,url}){
  if(path==='/api/dashboard'&&method==='GET')return {employee:{id:user.employee_id,name:user.name,department:user.department},today:today(),mode:await one(env,'SELECT id FROM providers WHERE enabled=1 AND tested=1')?'configured':env.ALLOW_DEMO==='true'?'demo':'unconfigured',environment:'cloudflare-demo',seeded_demo:env.ALLOW_DEMO==='true',balances:await balances(env,user.employee_id),leaves:(await all(env,'SELECT * FROM leaves WHERE employee_id=? ORDER BY start DESC',user.employee_id)).map(leaveData),events:await calendar(env,user.employee_id)};
  if(path==='/api/calendar'&&method==='GET')return calendar(env,user.employee_id,url.searchParams.get('start_date')||today(),url.searchParams.get('end_date')||undefined);
  if(path==='/api/calendar'&&method==='POST'){
    const value=eventInput(body),id=body.id?uuid(body.id):uid();const existing=await one(env,'SELECT * FROM calendar WHERE id=?',id);
    if(existing){if(existing.employee_id===user.employee_id&&Object.keys(value).every(k=>existing[k]===value[k]))return {id,...value};fail(409,'行程編號已使用，請重新開啟新增視窗。');}
    await run(env,'INSERT INTO calendar VALUES(?,?,?,?,?,?)',id,user.employee_id,value.title,value.start,value.end,value.location);return {id,...value};
  }
  const event=path.match(/^\/api\/calendar\/([\w-]+)$/);
  if(event&&['PUT','DELETE'].includes(method)){
    const id=uuid(event[1]);if(!await one(env,'SELECT id FROM calendar WHERE id=? AND employee_id=?',id,user.employee_id))fail(404,'找不到此行程。');
    if(method==='DELETE'){await run(env,'DELETE FROM calendar WHERE id=? AND employee_id=?',id,user.employee_id);return {ok:true};}
    fields(body,['title','start','end','location']);const value=eventInput(body);
    await run(env,'UPDATE calendar SET title=?,start=?,end=?,location=? WHERE id=? AND employee_id=?',value.title,value.start,value.end,value.location,id,user.employee_id);return {id,...value};
  }
  if(path==='/api/leaves/draft'&&method==='POST'){
    const row=await draft(env,user.employee_id,body);await insertDraft(env,row).run();
    return leaveData(await one(env,"SELECT * FROM leaves WHERE employee_id=? AND status='draft' AND leave_type=? AND start=? AND end=? AND reason=?",user.employee_id,row.leave_type,row.start,row.end,row.reason));
  }
  const leave=path.match(/^\/api\/leaves\/([\w-]+)\/(confirm|cancel|withdraw|events)$/);
  if(leave){
    const row=await ownLeave(env,user,leave[1]),action=leave[2];
    if(action==='events'&&method==='GET')return all(env,'SELECT action,hours,note,at FROM leave_events WHERE leave_id=? ORDER BY id',row.id);
    if(method==='POST'&&action!=='events'){
      const [before,after]={confirm:['draft','pending'],cancel:['draft','cancelled'],withdraw:['pending','withdrawn']}[action];
      if(row.status===after)return leaveData(row);
      if(row.status!==before)fail(409,'申請狀態已變更，請重新整理。');
      const changed=await run(env,'UPDATE leaves SET status=? WHERE id=? AND employee_id=? AND status=?',after,row.id,user.employee_id,before);
      const current=await ownLeave(env,user,row.id);
      if(!changed.meta.changes&&current.status!==after)fail(409,'申請已由另一個操作更新。');
      return leaveData(current);
    }
  }
  if(path==='/api/admin/leaves/summary'&&method==='GET'){
    const counts=Object.fromEntries((await all(env,'SELECT status,count(*) AS n FROM leaves GROUP BY status')).map(r=>[r.status,r.n]));
    return {pending:counts.pending||0,approved:counts.approved||0,rejected:counts.rejected||0,actionable:(await one(env,"SELECT count(*) AS n FROM leaves WHERE status='pending' AND employee_id!=?",user.employee_id)).n};
  }
  if(path==='/api/admin/leaves'&&method==='GET'){
    const status=choice(url.searchParams.get('status')||'pending',['pending','approved','rejected']);
    const rows=await all(env,`SELECT l.*,a.name AS employee_name,a.department,r.note AS review_note,b.username AS reviewer FROM leaves l JOIN accounts a ON a.employee_id=l.employee_id LEFT JOIN reviews r ON r.leave_id=l.id LEFT JOIN accounts b ON b.id=r.reviewer_id WHERE l.status=? ORDER BY l.start ${status==='pending'?'ASC':'DESC'} LIMIT 100`,status);
    return rows.map(r=>({...leaveData(r),review_note:r.review_note||'',reviewer:r.reviewer||'',can_review:status==='pending'&&r.employee_id!==user.employee_id}));
  }
  const review=path.match(/^\/api\/admin\/leaves\/([\w-]+)\/review$/);
  if(review&&method==='POST'){
    fields(body,['decision','note']);choice(body.decision,['approved','rejected']);const note=text(body.note,1,200);
    const row=await one(env,'SELECT * FROM leaves WHERE id=?',uuid(review[1]));if(!row)fail(404,'找不到申請。');if(row.employee_id===user.employee_id)fail(403,'自己的申請必須由另一位管理員審核。');
    const previous=await one(env,'SELECT * FROM reviews WHERE leave_id=?',row.id);
    if(previous){if(previous.decision===body.decision&&previous.note===note&&previous.reviewer_id===user.id)return leaveData(row);fail(409,'申請已由管理員處理。');}
    if(row.status!=='pending')fail(409,'只能審核待審核的申請。');
    await env.DB.batch([sql(env,'INSERT INTO reviews VALUES(?,?,?,?)',row.id,user.id,body.decision,note),audit(env,user,'leave_'+body.decision,row.id)]);
    return leaveData(await one(env,'SELECT * FROM leaves WHERE id=?',row.id));
  }
  return undefined;
}
