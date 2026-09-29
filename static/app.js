const $ = (s) => document.querySelector(s);
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const storage = {
  get(key){try{return sessionStorage.getItem(key);}catch{return null;}},
  set(key,value){try{value===null?sessionStorage.removeItem(key):sessionStorage.setItem(key,value);}catch{/* Session still works if browser storage is unavailable. */}}
};
let interruptedRequest=null;
try{interruptedRequest=JSON.parse(storage.get('daywork-pending-request'));}catch{/* Ignore malformed browser state. */}
const state = {dashboard:null, docs:[], conversation:storage.get('daywork-conversation'), busy:false, failedRequest:interruptedRequest, leaveFilter:'all', withdrawing:null};
const statusText = {draft:'待確認',pending:'待審核',cancelled:'已取消',withdrawn:'已撤回',approved:'已核准',rejected:'已退回'};
const toolLabels = {search_company_knowledge:'搜尋公司知識庫',get_leave_balance:'查詢假期餘額',create_leave_request:'建立請假草稿',get_calendar_events:'查詢工作行程'};
let toastTimer;
function toast(message){$('#toast').textContent=message;$('#toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.add('hidden'),4500);}
async function api(path, options={}) {
  const controller=new AbortController();
  const requestedBy=window.authState?.id;
  const timer=setTimeout(()=>controller.abort(),path==='/api/chat'?125000:(path.endsWith('/test')?65000:20000));
  let response;
  try{response=await fetch(path,{...options,signal:controller.signal,headers:{'Content-Type':'application/json','X-CSRF-Token':window.authState?.csrf||'',...options.headers}});}
  catch(error){throw new Error(error.name==='AbortError'?'等待逾時，操作可能仍在處理。請重試以取得結果。':'無法連線到服務。請確認服務已啟動，再重試。');}
  finally{clearTimeout(timer);}
  let data;try{data=await response.json();}catch{throw new Error('伺服器回應異常，請稍後重試。');}
  if(requestedBy&&requestedBy!==window.authState?.id){const error=new Error('登入帳號已變更，請重新操作。');error.status=401;throw error;}
  if(response.status===401&&!path.startsWith('/api/auth/')){window.showLogin?.();}
  if(!response.ok) {const error=new Error(typeof data.detail==='string'?data.detail:'資料格式不正確，請檢查必填欄位與時間。');error.status=response.status;throw error;}
  return data;
}
const post = (path, body) => api(path,{method:'POST',body:body===undefined?undefined:JSON.stringify(body)});
const period = r => `${r.start.slice(0,10)} · ${r.start.slice(11,16)}–${r.end.slice(11,16)}`;
function welcome(){
  $('#messages').innerHTML=`<div class="welcome"><div class="welcome-emblem">${icon('spark')}</div><span class="welcome-tag">MEET YOUR ADMIN COPILOT</span><h3>今天，想完成什麼？</h3><p>直接描述你的需求，或選一項任務開始。<br>查詢有依據，操作有紀錄。</p><div class="capabilities"><button class="capability" data-prompt="特休規定是什麼？我還剩多少假？">${icon('book')}<span><strong>查公司規章</strong><small>附上可追溯的文件來源</small></span></button><button class="capability" data-prompt="我今年還剩多少特休和補休？">${icon('leaf')}<span><strong>查假期餘額</strong><small>特休與補休，即時掌握</small></span></button><button class="capability" data-prompt="我下週有哪些會議？">${icon('calendar')}<span><strong>查看會議安排</strong><small>快速了解下週行程</small></span></button><button class="capability" data-action="open-leave">${icon('plus')}<span><strong>建立請假申請</strong><small>先核對內容，再確認送出</small></span></button></div></div>`;
}
function draftCard(d){
  const current=state.dashboard?.leaves.find(l=>l.id===d.id)||d;
  return `<div class="draft-card" data-leave-id="${esc(d.id)}"><strong>${esc(d.label)} · ${d.hours} 小時 <span class="status-badge ${esc(current.status)}">${statusText[current.status]||'狀態不明'}</span></strong><p>${period(d)}<br>原因：${esc(d.reason)}</p>${leaveActions(current)}</div>`;
}
function leaveActions(row){
  if(row.status==='draft')return `<div class="leave-actions"><button class="button primary" data-confirm="${esc(row.id)}">確認送出</button><button class="button secondary" data-cancel="${esc(row.id)}">取消草稿</button></div>`;
  if(row.status==='pending'&&new Date(row.start+'+08:00')>new Date())return `<button class="button secondary" data-withdraw="${esc(row.id)}">撤回申請</button>`;
  return '';
}
function addMessage(role,text,result){
  $('#messages .welcome')?.remove();
  const node=document.createElement('div');node.className=`message ${role}`;
  node.innerHTML=`<div class="message-name">${role==='user'?esc(window.authState?.name||'我'):icon('spark')+'日常助理'}</div><div class="message-text">${esc(text)}</div>`;
  if(result?.sources?.length)node.insertAdjacentHTML('beforeend',`<div class="source-list">${result.sources.map(s=>`<button class="source-chip" data-source="${esc(s.source)}" data-source-page="${s.page??''}" data-chunk="${s.chunk}" data-fingerprint="${esc(s.fingerprint||'')}">${esc(s.source)} · ${s.page?'p.'+s.page:'§'+s.chunk}</button>`).join('')}</div>`);
  if(result?.drafts?.length)node.insertAdjacentHTML('beforeend',result.drafts.map(draftCard).join(''));
  $('#messages').append(node);$('#messages').scrollTop=$('#messages').scrollHeight;
}
function showTrace(trace){
  $('#trace-count').textContent=trace.length?`${trace.length} EVENTS`:'DONE';
  $('#trace').innerHTML=trace.length?trace.map(t=>`<details class="trace-event ${t.status}"><summary><span>${t.status==='error'?'!':'✓'} ${esc(toolLabels[t.tool]||t.tool)}</span><span>${t.duration_ms}ms</span></summary><small>${esc(t.tool)}</small><pre>${esc(JSON.stringify({arguments:t.arguments,result:t.result},null,2))}</pre></details>`).join(''):'<div class="trace-empty"><strong>這次不需要呼叫工具</strong><p>助理已回覆說明或詢問補充資訊。</p></div>';
}
function resetTrace(){
  $('#trace-count').textContent='READY';
  $('#trace').innerHTML=`<div class="trace-empty"><div class="orbit">${icon('flow')}</div><strong>準備好開始了</strong><p>傳送訊息後，這裡會顯示<br>工具呼叫、資料來源與執行結果。</p></div>`;
}
function setBusy(value){
  state.busy=value;$('#send').disabled=value;$('#new-chat').disabled=value;$('#history-button').disabled=value;$('#retry-chat').disabled=value;$('#model-select').disabled=value||!!state.failedRequest;
  $('#messages').setAttribute('aria-busy',String(value));
}
function requestNotice(message){
  $('#request-notice-text').textContent=message;
  $('#request-notice').classList.toggle('hidden',!message);
  $('#retry-chat').classList.toggle('hidden',!state.failedRequest);
}
async function syncDashboard(){
  try{await refreshDashboard();}catch{toast('操作結果已收到，但摘要更新失敗；重新整理可取得最新餘額。');}
}
async function sendMessage(text,retry=false){
  if(state.busy||!text.trim())return;
  if(!retry&&state.failedRequest){requestNotice('上次訊息的結果尚未確認。請先重試，或另開新對話。');return;}
  setBusy(true);
  const sender=window.authState?.id;
  const body=retry?state.failedRequest:{message:text,conversation_id:state.conversation||null,request_id:crypto.randomUUID(),provider_id:$('#model-select').value||null};
  storage.set('daywork-pending-request',JSON.stringify(body));
  if(!retry)addMessage('user',text);$('#message-input').value='';requestNotice('');
  const pending=document.createElement('div');pending.className='loading-dots';pending.textContent='日常正在處理你的需求…';$('#messages').append(pending);$('#messages').scrollTop=$('#messages').scrollHeight;
  try{
    const result=await post('/api/chat',body);
    if(sender!==window.authState?.id)return;
    state.conversation=result.conversation_id;storage.set('daywork-conversation',state.conversation);
    state.failedRequest=null;storage.set('daywork-pending-request',null);
    pending.remove();addMessage('assistant',result.answer,result);showTrace(result.trace);
    await syncDashboard();
  }catch(error){pending.remove();if(sender!==window.authState?.id)return;
    if([401,403,404,422].includes(error.status)){state.failedRequest=null;storage.set('daywork-pending-request',null);$('#message-input').value=text;}
    else state.failedRequest=body;
    requestNotice(error.message);
  }
  finally{if(sender===window.authState?.id){setBusy(false);$('#message-input').focus({preventScroll:true});}}
}
function showPage(name){
  if(['settings','reviews'].includes(name)&&window.authState?.role!=='admin')return;
  document.querySelectorAll('.page-view').forEach(el=>el.classList.add('hidden'));$(`#${name}-page`).classList.remove('hidden');
  document.querySelectorAll('.nav-item').forEach(el=>{el.classList.toggle('active',el.dataset.page===name);if(el.dataset.page===name)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  const content={assistant:['行政助理','工作日常，從這裡開始。',`${window.authState?.name||''}，歡迎回來。查詢資訊、安排假期，交給你的 AI 助理。`],knowledge:['公司知識庫','公司資訊，有據可查。','瀏覽內部規章，讓每個回答都有可靠的來源。'],leaves:['我的請假','假期與申請，一次掌握。','核對草稿、確認送出，隨時查看申請狀態。'],settings:['管理設定','把 AI 接進你的工作日常。','管理模型、API Key 與工作空間帳號。'],calendar:['工作行事曆','接下來的安排。','維護自己的工作行程，讓行政助理幫你掌握安排。'],reviews:['審核工作台','讓每份申請，都有回應。','集中查看團隊申請，核對內容並完成審核。']}[name];
  if(name==='settings')window.loadSettings?.();
  if(name==='reviews')window.loadReviews?.();
  if(name==='calendar')window.loadCalendar?.();
  $('#page-title').textContent=content[0];$('#heading').textContent=content[1];$('#subtitle').textContent=content[2];
}
function upcoming(events){return events.length?events.slice(0,2).map(e=>`<div class="upcoming-item"><span class="date-tile"><small>${e.start.slice(5,7)} 月</small>${e.start.slice(8,10)}</span><div><strong>${esc(e.title)}</strong><p>${e.start.slice(11,16)}–${e.end.slice(11,16)} · ${esc(e.location.split(' / ')[0])}</p></div></div>`).join(''):'<div class="side-intro">最近沒有安排會議。</div>';}
async function refreshDashboard(){
  const d=await api('/api/dashboard');state.dashboard=d;
  $('#annual').textContent=Number((d.balances.find(b=>b.leave_type==='annual')?.days??0).toFixed(3));
  $('#annual-hours').textContent=`共 ${d.balances.find(b=>b.leave_type==='annual')?.hours??0} 小時可使用`;
  $('#compensatory').textContent=d.balances.find(b=>b.leave_type==='compensatory')?.hours??0;
  $('#event-count').textContent=d.events.length;$('#upcoming').innerHTML=upcoming(d.events);
  $('#mode-pill').innerHTML=`<i></i> ${d.mode==='demo'?'離線規則模式':d.mode==='unconfigured'?'尚未設定 AI':'AI 已連接'}`;
  $('#mode-note').textContent=d.mode==='demo'?'離線規則模式 · 未使用外部 AI':'AI 回答請以引用文件與工具結果為準';
  $('#today').textContent=d.today.replaceAll('-',' / ');$('#profile-name').textContent=d.employee.name;
  $('.avatar').textContent=d.employee.name.slice(-1);
  $('#data-note').textContent=d.seeded_demo?(d.environment==='cloudflare-demo'?'展示環境：內建政策為虛構，帳號與行程由使用者建立。':'示範資料：政策、原有員工與行程均為虛構。'):'AI 可能有誤，請核對文件來源與實際公司制度。';
  $('#department').textContent=`${d.employee.department} · ${d.employee.id}`;
  $('#leave-form [name=date]').min=d.today;
  renderLeaves();

  document.querySelectorAll('.draft-card').forEach(el=>{const row=d.leaves.find(l=>l.id===el.dataset.leaveId);if(row)el.outerHTML=draftCard(row);});
}
function renderLeaves(){
  if(!state.dashboard)return;
  const leaves=state.dashboard.leaves.filter(l=>state.leaveFilter==='all'||(state.leaveFilter==='closed'?['cancelled','withdrawn','approved','rejected'].includes(l.status):l.status===state.leaveFilter));
  $('#leave-list').innerHTML=leaves.length?leaves.map(l=>`<article class="leave-card"><div><h3>${esc(l.label)} · ${l.hours} 小時 <span class="status-badge ${esc(l.status)}">${statusText[l.status]||'狀態不明'}</span></h3><p>${period(l)}</p><p>${esc(l.reason)}</p><button class="text-button" data-audit="${esc(l.id)}">查看操作紀錄 ↗</button></div>${leaveActions(l)}</article>`).join(''):'<div class="empty-state">目前沒有符合的申請<p>可切換狀態，或從「申請請假」開始。</p></div>';
}
function renderDocuments(query=''){
  const docs=state.docs.filter(d=>JSON.stringify(d).toLowerCase().includes(query.toLowerCase()));
  $('#documents').innerHTML=docs.length?docs.map(d=>`<button class="document-card" data-source="${esc(d.source)}">${icon('book')}<h3>${esc(d.title)}</h3><p>${esc(d.source)} · ${d.chunks.length} 個段落</p></button>`).join(''):'<div class="empty-state">沒有符合的文件。</div>';
}
function openDocument(source,page,chunk,fingerprint){
  const doc=state.docs.find(d=>d.source===source);
  if(!doc){toast('文件已移除或尚未載入，請重新整理知識庫。');return;}
  $('#document-title').textContent=doc.title;
  const content=$('#document-content');content.replaceChildren();
  const citation=chunk!==undefined;
  const changed=citation&&fingerprint&&doc.fingerprint&&fingerprint!==doc.fingerprint;
  let target=null;
  if(changed){const notice=document.createElement('p');notice.className='citation-notice';notice.textContent='文件版本已更新。以下為目前版本，可能與這則歷史回答的引用不同；請重新向助理查詢。';content.append(notice);}
  for(const c of doc.chunks){
    const section=document.createElement('section');section.className='document-chunk';
    const heading=document.createElement('h3');heading.textContent=`${c.page?'第 '+c.page+' 頁 · ':''}段落 ${c.chunk}`;
    const text=document.createElement('p');text.textContent=c.text;section.append(heading,text);content.append(section);
    if(citation&&!changed&&String(c.page??'')===String(page??'')&&String(c.chunk)===String(chunk)){section.classList.add('cited-chunk');section.tabIndex=-1;heading.textContent+=' · 引用來源';target=section;}
  }
  if(citation&&!changed&&!target){const notice=document.createElement('p');notice.className='citation-notice';notice.textContent='原引用段落已無法定位，請核對目前文件或重新查詢。';content.prepend(notice);}
  $('#document-dialog').showModal();
  if(target)requestAnimationFrame(()=>{target.scrollIntoView({block:'center'});target.focus({preventScroll:true});});
  else $('#document-dialog').scrollTop=0;
}
function openLeave(){$('#leave-error').textContent='';$('#leave-dialog').showModal();}
document.addEventListener('click',async e=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.dataset.page)showPage(button.dataset.page);
  if(button.dataset.prompt){showPage('assistant');sendMessage(button.dataset.prompt);}
  if(button.dataset.source)openDocument(button.dataset.source,button.dataset.sourcePage,button.dataset.chunk,button.dataset.fingerprint);
  if(button.dataset.action==='open-leave')openLeave();
  if(button.dataset.filter){state.leaveFilter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});renderLeaves();}
  if(button.dataset.conversation){await restoreConversation(button.dataset.conversation);}
  if(button.dataset.withdraw){
    const row=state.dashboard?.leaves.find(l=>l.id===button.dataset.withdraw);if(!row)return;
    state.withdrawing=row.id;$('#withdraw-summary').textContent=`${row.label} · ${period(row)} · ${row.hours} 小時`;
    $('#withdraw-error').textContent='';$('#withdraw-dialog').showModal();
  }
  if(button.dataset.audit){
    try{const events=await api(`/api/leaves/${button.dataset.audit}/events`);const names={draft_created:'建立草稿',submitted:'送出申請並保留額度',cancelled:'取消草稿',withdrawn:'撤回申請並返還額度',approved:'管理員核准',rejected:'管理員退回並返還額度'};
      $('#document-title').textContent='請假操作紀錄';$('#document-content').textContent=events.length?events.map(e=>`${e.at.replace('T',' ')}\n${names[e.action]||e.action} · ${e.hours} 小時${e.note?'\n審核說明：'+e.note:''}`).join('\n\n'):'這筆申請建立於紀錄功能上線前，沒有歷史事件。';$('#document-dialog').showModal();
    }catch(error){toast(error.message);}
  }
  if(button.classList.contains('close-dialog'))button.closest('dialog').close();
  if(button.dataset.confirm||button.dataset.cancel){
    button.disabled=true;const action=button.dataset.confirm?'confirm':'cancel';const id=button.dataset.confirm||button.dataset.cancel;
    try{const row=await post(`/api/leaves/${id}/${action}`);const local=state.dashboard?.leaves.find(l=>l.id===id);if(local)local.status=row.status;document.querySelectorAll('.draft-card').forEach(el=>{if(el.dataset.leaveId===id&&local)el.outerHTML=draftCard(local);});renderLeaves();toast(action==='confirm'?'申請已送出，等待主管審核；餘額已保留。':'草稿已取消，餘額不受影響。');await syncDashboard();}
    catch(error){toast(error.message);button.disabled=false;}
  }
});
$('#open-leave').addEventListener('click',openLeave);
$('#chat-form').addEventListener('submit',e=>{e.preventDefault();sendMessage($('#message-input').value);});
$('#message-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();sendMessage(e.target.value);}});
$('#new-chat').addEventListener('click',()=>{if(state.busy)return;state.conversation=null;state.failedRequest=null;$('#model-select').disabled=false;storage.set('daywork-conversation',null);storage.set('daywork-pending-request',null);welcome();resetTrace();requestNotice('');$('#message-input').focus({preventScroll:true});});
$('#retry-chat').addEventListener('click',()=>{if(state.failedRequest)sendMessage(state.failedRequest.message,true);});
$('#history-button').addEventListener('click',async()=>{
  if(state.busy)return;$('#history-list').textContent='正在載入…';$('#history-dialog').showModal();
  try{const conversations=await api('/api/conversations');$('#history-list').innerHTML=conversations.length?conversations.map(c=>`<button class="history-item" data-conversation="${esc(c.id)}"><strong>${esc(c.title)}</strong><small>${c.message_count} 則訊息 · ${esc(c.updated_at?c.updated_at.slice(0,16).replace('T',' '):'較早的對話')}${c.id===state.conversation?' · 目前對話':''}</small></button>`).join(''):'還沒有歷史對話。';}
  catch(error){$('#history-list').textContent=error.message;}
});
async function restoreConversation(id){
  if(state.busy)return;
  if(state.failedRequest){toast('請先重試尚未確認的訊息，或另開新對話。');return;}
  setBusy(true);
  try{const history=await api(`/api/conversations/${id}`);state.conversation=id;storage.set('daywork-conversation',id);welcome();resetTrace();requestNotice('');for(const m of history.messages)addMessage(m.role,m.content,m.result);const last=[...history.messages].reverse().find(m=>m.result);if(last)showTrace(last.result.trace);$('#history-dialog').close();showPage('assistant');}
  catch(error){toast(error.message);if(error.status===404){state.conversation=null;storage.set('daywork-conversation',null);}}
  finally{setBusy(false);}
}
$('#withdraw-confirm').addEventListener('click',async()=>{
  const button=$('#withdraw-confirm');button.disabled=true;
  try{const row=await post(`/api/leaves/${state.withdrawing}/withdraw`);const local=state.dashboard?.leaves.find(l=>l.id===row.id);if(local)local.status=row.status;renderLeaves();$('#withdraw-dialog').close();toast('申請已撤回，保留時數已返還。');await syncDashboard();}
  catch(error){$('#withdraw-error').textContent=error.message;}finally{button.disabled=false;}
});
$('#doc-search').addEventListener('input',e=>renderDocuments(e.target.value));
$('#leave-form').addEventListener('submit',async e=>{
  e.preventDefault();const f=new FormData(e.target);const submit=e.target.querySelector('[type=submit]');submit.disabled=true;
  try{const row=await post('/api/leaves/draft',{leave_type:f.get('leave_type'),start_time:`${f.get('date')}T${f.get('start')}:00+08:00`,end_time:`${f.get('date')}T${f.get('end')}:00+08:00`,reason:f.get('reason')});$('#leave-dialog').close();showPage('assistant');addMessage('assistant','請核對請假草稿，確認後再送出。',{drafts:[row]});toast('草稿已建立，可在「我的請假」找到。');await syncDashboard();}
  catch(error){$('#leave-error').textContent=error.message;}finally{submit.disabled=false;}
});
async function init(){
  welcome();setBusy(true);
  const results=await Promise.allSettled([refreshDashboard(),api('/api/documents')]);
  if(results[0].status==='rejected'){toast(results[0].reason.message);$('#mode-pill').textContent='服務連線失敗';}
  if(results[1].status==='fulfilled'){state.docs=results[1].value;$('#doc-count').textContent=state.docs.length;renderDocuments();}
  else $('#documents').textContent='文件暫時無法載入，請重新整理。';
  setBusy(false);
  if(state.failedRequest){addMessage('user',state.failedRequest.message);requestNotice('有一則訊息的結果尚未確認。重試會沿用請求編號，不會重複執行。');}
  else if(state.conversation)await restoreConversation(state.conversation);
}
// Authentication bootstraps the workspace from settings.js.
