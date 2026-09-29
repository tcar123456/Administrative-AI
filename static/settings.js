window.authState=null;
let demoAvailable=false;
let setupRequired=false, editingProvider=null, providerRows=[];
let reviewRows=[], reviewFilter='pending';
function clearWorkspace(){
  state.conversation=null;state.failedRequest=null;state.dashboard=null;state.docs=[];state.busy=false;
  storage.set('daywork-conversation',null);storage.set('daywork-pending-request',null);
  $('#messages').replaceChildren();$('#provider-list').replaceChildren();$('#account-list').replaceChildren();
  $('#provider-form').reset();$('#account-form').reset();
  editingProvider=null;providerRows=[];reviewRows=[];reviewFilter='pending';
  $('#review-list').replaceChildren();$('#review-search').value='';$('#calendar-list').replaceChildren();$('#document-content').replaceChildren();$('#leave-list').replaceChildren();
  window.resetCalendar?.();
}
window.showLogin=function(){
  window.authState=null;document.body.classList.add('locked');$('#auth-gate').classList.remove('hidden');
  document.querySelectorAll('dialog[open]').forEach(d=>d.close());
  clearWorkspace();
};
async function enterWorkspace(user){
  if(storage.get('daywork-user')!==user.id)clearWorkspace();
  storage.set('daywork-user',user.id);window.authState=user;
  $('#settings-nav').classList.toggle('hidden',user.role!=='admin');
  $('#reviews-nav').classList.toggle('hidden',user.role!=='admin');
  document.body.classList.remove('locked');$('#auth-gate').classList.add('hidden');
  $('#auth-form').reset();$('#login-password').value='';
  await loadModels();await init();showPage('assistant');
  if(user.role==='admin')loadReviewSummary().catch(()=>{});
}
async function bootAuth(){
  try{
    const status=await api('/api/auth/status');setupRequired=status.setup_required;demoAvailable=status.demo_available??status.environment!=='production';
    $('#setup-fields').classList.toggle('hidden',!setupRequired);
    $('#setup-token').required=setupRequired;$('#setup-name').required=setupRequired;
    $('#login-password').minLength=setupRequired?12:1;
    $('#login-password').autocomplete=setupRequired?'new-password':'current-password';
    $('#auth-title').textContent=setupRequired?'建立第一位管理員':'登入工作空間';
    $('#login-submit').textContent=setupRequired?'建立管理員並開始 →':'登入工作空間 →';
    $('#auth-description').textContent=setupRequired?'請使用伺服器上的初始化代碼，設定專屬帳號及至少 12 字元的密碼。':'使用管理員提供的帳號登入。';
    if(setupRequired)return;
    try{await enterWorkspace(await api('/api/auth/me'));}catch(e){if(e.status!==401)throw e;}
  }catch(e){$('#auth-error').textContent=e.message;}
}
$('#auth-form').addEventListener('submit',async e=>{
  e.preventDefault();const button=$('#login-submit');button.disabled=true;$('#auth-error').textContent='';
  const body={username:$('#login-username').value,password:$('#login-password').value};
  if(setupRequired)Object.assign(body,{token:$('#setup-token').value,name:$('#setup-name').value});
  try{const user=await post(setupRequired?'/api/auth/setup':'/api/auth/login',body);setupRequired=false;$('#setup-fields').classList.add('hidden');$('#setup-token').required=false;$('#setup-name').required=false;await enterWorkspace(user);}
  catch(error){$('#auth-error').textContent=error.message;}finally{button.disabled=false;}
});
$('#logout-button').addEventListener('click',async()=>{
  try{await post('/api/auth/logout');showLogin();await bootAuth();}catch(e){toast(e.message);}
});
$('#password-button').addEventListener('click',()=>{$('#password-error').textContent='';$('#password-form').reset();$('#password-dialog').showModal();});
$('#password-form').addEventListener('submit',async e=>{
  e.preventDefault();const button=e.target.querySelector('[type=submit]');button.disabled=true;
  try{await post('/api/auth/password',Object.fromEntries(new FormData(e.target)));e.target.reset();showLogin();await bootAuth();toast('密碼已更新，請重新登入。');}
  catch(error){$('#password-error').textContent=error.message;}finally{button.disabled=false;}
});
async function loadModels(){
  const models=await api('/api/models'), select=$('#model-select'), prior=select.value;
  const chosen=models.find(m=>m.id===prior)||models.find(m=>m.default)||models[0];
  select.innerHTML=models.length?models.map(m=>`<option value="${esc(m.id)}">${esc(m.name)}${m.default?' · 預設':''}</option>`).join(''):'<option value="">尚無可用模型</option>';
  if(chosen)select.value=chosen.id;
  $('#model-description').textContent=models.length?'訊息與必要資料會送至所選 AI 供應商':(demoAvailable?'管理員可到「管理設定」連接 AI；目前可使用離線規則展示。':'尚無可用 AI，請聯絡管理員。');
}
$('#model-select').addEventListener('change',()=>{
  if(state.failedRequest){toast('請先處理上次尚未確認的訊息。');return;}
  $('#new-chat').click();toast('已切換模型並開始新對話。');
});
window.loadSettings=async function(){
  if(window.authState?.role!=='admin')return;
  try{
    const results=await Promise.all([api('/api/admin/providers'),api('/api/admin/accounts')]);
    providerRows=results[0];renderProviders();renderAccounts(results[1]);
  }catch(e){toast(e.message);}
};
function renderProviders(){
  $('#provider-list').innerHTML=providerRows.length?providerRows.map(p=>`<article class="provider-card"><div class="provider-heading"><strong>${esc(p.name)}</strong><span class="tag">${p.enabled?'使用中':p.tested?'已測試':'待測試'}${p.default?' · 預設':''}</span></div><p>${esc(p.provider)} · ${esc(p.model)}</p><code>${esc(p.key_mask)}</code><div class="settings-actions"><button class="text-button" data-provider-action="test" data-id="${p.id}">測試連線</button>${p.tested?`<button class="text-button" data-provider-action="${p.enabled?'disable':'enable'}" data-id="${p.id}">${p.enabled?'停用':'啟用'}</button>`:''}${p.enabled&&!p.default?`<button class="text-button" data-provider-action="default" data-id="${p.id}">設為預設</button>`:''}<button class="text-button" data-provider-action="edit" data-id="${p.id}">更換 Key / 模型</button>${!p.enabled?`<button class="text-button danger-text" data-provider-action="delete" data-id="${p.id}">刪除</button>`:''}</div></article>`).join(''):'<div class="empty-state">尚未連接 AI<p>填寫左側設定，即可開始。</p></div>';
}
function resetProviderForm(){editingProvider=null;$('#provider-form').reset();$('#provider-form-title').textContent='連接一個 AI 模型';$('#cancel-provider-edit').classList.add('hidden');$('#provider-error').textContent='';}
$('#cancel-provider-edit').addEventListener('click',resetProviderForm);
$('#refresh-providers').addEventListener('click',loadSettings);
$('#provider-form').addEventListener('submit',async e=>{
  e.preventDefault();const button=e.target.querySelector('[type=submit]');button.disabled=true;$('#provider-error').textContent='';
  const body=Object.fromEntries(new FormData(e.target));
  try{await api('/api/admin/providers'+(editingProvider?'/'+editingProvider:''),{method:editingProvider?'PUT':'POST',body:JSON.stringify(body)});resetProviderForm();toast('已加密儲存，請測試連線後啟用。');await loadSettings();await loadModels();}
  catch(error){$('#provider-error').textContent=error.message;}finally{e.target.elements.api_key.value='';body.api_key='';button.disabled=false;}
});
$('#provider-list').addEventListener('click',async e=>{
  const b=e.target.closest('[data-provider-action]');if(!b)return;
  const row=providerRows.find(p=>p.id===b.dataset.id), action=b.dataset.providerAction;
  if(action==='edit'){
    editingProvider=row.id;const f=$('#provider-form');f.elements.name.value=row.name;f.elements.provider.value=row.provider;f.elements.model.value=row.model;f.elements.api_key.value='';
    $('#provider-form-title').textContent='更新模型設定';$('#cancel-provider-edit').classList.remove('hidden');$('#provider-error').textContent='儲存後將停用這組設定，需重新測試及啟用。';f.elements.api_key.focus();return;
  }
  b.disabled=true;const original=b.textContent;if(action==='test')b.textContent='測試中…';
  try{
    if(action==='test'){const r=await post(`/api/admin/providers/${row.id}/test`);toast(r.message);}
    else if(action==='delete'){await api(`/api/admin/providers/${row.id}`,{method:'DELETE'});toast('已刪除設定；如需撤銷 Key，請至供應商後台操作。');}
    else{await post(`/api/admin/providers/${row.id}/activate`,{enabled:action!=='disable',make_default:action==='default'});toast('模型使用設定已更新。');}
    await loadSettings();await loadModels();await refreshDashboard();
  }catch(error){toast(error.message);}finally{b.disabled=false;b.textContent=original;}
});
function renderAccounts(rows){
  $('#account-list').innerHTML=rows.map(u=>`<article class="member-card"><div><strong>${esc(u.name)}</strong><small>${esc(u.username)} · ${esc(u.employee_id)}<br>${esc(u.department)} · ${u.role==='admin'?'管理員':'員工'}${u.disabled?' · 已停用':''}</small></div>${u.id===window.authState.id?'<span class="tag">你</span>':`<button class="text-button" data-account="${u.id}" data-disabled="${!u.disabled}">${u.disabled?'啟用':'停用'}</button>`}</article>`).join('');
}
$('#account-form').addEventListener('submit',async e=>{
  e.preventDefault();const b=e.target.querySelector('[type=submit]');b.disabled=true;$('#account-error').textContent='';
  const body=Object.fromEntries(new FormData(e.target));body.annual_hours=Number(body.annual_hours);body.compensatory_hours=Number(body.compensatory_hours);
  try{await post('/api/admin/accounts',body);e.target.reset();toast('帳號已建立，請安全交付帳號與初始密碼。');await loadSettings();}
  catch(error){$('#account-error').textContent=error.message;}finally{body.password='';e.target.elements.password.value='';b.disabled=false;}
});
$('#account-list').addEventListener('click',async e=>{
  const b=e.target.closest('[data-account]');if(!b)return;b.disabled=true;
  try{await api(`/api/admin/accounts/${b.dataset.account}`,{method:'PATCH',body:JSON.stringify({disabled:b.dataset.disabled==='true'})});await loadSettings();toast('帳號狀態已更新，舊的登入工作階段已失效。');}
  catch(error){toast(error.message);}finally{b.disabled=false;}
});
$('#refresh-audit').addEventListener('click',async()=>{
  try{const rows=await api('/api/admin/events');$('#admin-audit-list').replaceChildren();for(const r of rows){const p=document.createElement('p');p.textContent=`${r.at.replace('T',' ')} · ${r.actor} · ${r.action}`;$('#admin-audit-list').append(p);}}
  catch(error){toast(error.message);}
});
async function loadReviewSummary(){
  const summary=await api('/api/admin/leaves/summary');
  for(const key of ['actionable','pending','approved','rejected'])$(`#review-${key}`).textContent=summary[key];
  $('#review-nav-count').textContent=summary.actionable;
}
function renderReviews(){
  const query=$('#review-search').value.trim().toLowerCase();
  const rows=reviewRows.filter(r=>[r.employee_name,r.employee_id,r.department,r.reason].some(v=>v.toLowerCase().includes(query)));
  document.querySelectorAll('[data-review-filter]').forEach(b=>{const active=b.dataset.reviewFilter===reviewFilter;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});
  $('#review-list').innerHTML=rows.length?rows.map(r=>`<article class="panel review-card"><div class="provider-heading"><div><strong>${esc(r.employee_name)}</strong><small>${esc(r.department)} · ${esc(r.employee_id)}</small></div><span class="status-badge ${r.status}">${statusText[r.status]}</span></div><h3>${esc(r.label)} <span>${r.hours} 小時</span></h3><p>${period(r)}</p><p class="review-reason">${esc(r.reason)}</p>${r.can_review?`<form class="review-form" data-id="${r.id}"><label>審核說明<input name="note" required maxlength="200" placeholder="例如：已確認工作交接安排"></label><div class="settings-actions"><button class="button primary" type="submit" value="approved">核准申請</button><button class="button secondary" type="submit" value="rejected">退回並返還額度</button></div><p class="form-error review-form-error" role="alert"></p></form>`:r.status==='pending'?'<p class="subtle">這是你的申請，等待其他管理員審核。</p>':`<div class="review-decision"><strong>${esc(r.reviewer||'管理員')}</strong><p>${esc(r.review_note)}</p></div>`}</article>`).join(''):`<div class="empty-state">${query?'沒有符合搜尋的申請':reviewFilter==='pending'?'目前沒有待審核申請':'目前沒有這個狀態的申請'}<p>${query?'試試其他姓名、部門或原因。':'申請與審核結果會顯示在這裡。'}</p></div>`;
}
let reviewLoad=0;
async function loadReviews(){
  if(window.authState?.role!=='admin')return;
  const version=++reviewLoad;
  $('#review-error').textContent='';$('#review-list').setAttribute('aria-busy','true');
  try{
    const [rows]=await Promise.all([api('/api/admin/leaves?status='+reviewFilter),loadReviewSummary()]);
    if(version!==reviewLoad)return;
    reviewRows=rows;renderReviews();
  }catch(e){if(version===reviewLoad){reviewRows=[];$('#review-list').replaceChildren();$('#review-error').textContent=e.message;}}
  finally{if(version===reviewLoad)$('#review-list').setAttribute('aria-busy','false');}
}
$('#refresh-reviews').addEventListener('click',loadReviews);
$('#review-search').addEventListener('input',renderReviews);
document.querySelectorAll('[data-review-filter]').forEach(b=>b.addEventListener('click',()=>{reviewFilter=b.dataset.reviewFilter;reviewRows=[];renderReviews();loadReviews();}));
$('#review-list').addEventListener('submit',async e=>{
  if(!e.target.matches('.review-form'))return;e.preventDefault();
  const form=e.target,buttons=form.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);
  form.querySelector('.review-form-error').textContent='';
  try{await post(`/api/admin/leaves/${form.dataset.id}/review`,{decision:e.submitter?.value||'approved',note:form.elements.note.value});toast('審核已完成，可切換狀態查看結果。');await loadReviews();}
  catch(error){form.querySelector('.review-form-error').textContent=error.message;}finally{buttons.forEach(b=>b.disabled=false);}
});
// Remove legacy demo browser state; keys are never placed in browser storage.
try{localStorage.removeItem('daywork-conversation');localStorage.removeItem('daywork-pending-request');}catch{}
bootAuth();
