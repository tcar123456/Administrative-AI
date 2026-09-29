let calendarRows=[], editingEvent=null, deletingEvent=null, calendarLoad=0;
const eventPeriod=e=>`${e.start.slice(0,10)} ${e.start.slice(11,16)} – ${e.end.slice(0,10)===e.start.slice(0,10)?'':e.end.slice(0,10)+' '}${e.end.slice(11,16)}`;
function resetCalendar(){
  calendarRows=[];editingEvent=null;deletingEvent=null;calendarLoad++;
  $('#calendar-range').reset();$('#event-form').reset();$('#calendar-error').textContent='';
}
function defaultCalendarRange(){
  const start=state.dashboard?.today||new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Taipei'}).format(new Date());
  const end=new Date(start+'T00:00:00Z');end.setUTCDate(end.getUTCDate()+30);
  const form=$('#calendar-range');form.elements.start.value=start;form.elements.end.value=end.toISOString().slice(0,10);
}
async function loadCalendar(){
  const form=$('#calendar-range');if(!form.elements.start.value)defaultCalendarRange();
  const version=++calendarLoad;$('#calendar-error').textContent='';$('#calendar-list').setAttribute('aria-busy','true');
  try{
    const params=new URLSearchParams({start_date:form.elements.start.value,end_date:form.elements.end.value});
    const rows=await api('/api/calendar?'+params);
    if(version!==calendarLoad)return;
    calendarRows=rows;
    $('#calendar-list').innerHTML=rows.length?rows.map(e=>`<article class="calendar-event"><div class="date-tile"><small>${e.start.slice(5,7)} 月</small>${e.start.slice(8,10)}</div><div class="calendar-detail"><h3>${esc(e.title)}</h3><p>${eventPeriod(e)}</p><p>${esc(e.location||'未指定地點')}</p></div><div class="calendar-actions"><button class="text-button" data-edit-event="${e.id}">編輯</button><button class="text-button danger-text" data-delete-event="${e.id}">刪除</button></div></article>`).join(''):'<div class="empty-state">這段期間沒有行程<p>新增一筆會議，再試著問助理「今天有哪些行程？」</p></div>';
  }catch(error){if(version===calendarLoad){calendarRows=[];$('#calendar-list').replaceChildren();$('#calendar-error').textContent=error.message;}}
  finally{if(version===calendarLoad)$('#calendar-list').setAttribute('aria-busy','false');}
}
function openEvent(row){
  const form=$('#event-form');form.reset();editingEvent={id:row?.id||crypto.randomUUID(),existing:!!row};
  $('#event-title').textContent=row?'編輯行程':'新增行程';$('#event-error').textContent='';
  const day=$('#calendar-range').elements.start.value||state.dashboard.today;
  form.elements.title.value=row?.title||'';
  form.elements.start.value=row?.start.slice(0,16)||day+'T10:00';
  form.elements.end.value=row?.end.slice(0,16)||day+'T11:00';
  form.elements.location.value=row?.location||'';
  $('#event-dialog').showModal();form.elements.title.focus();
}
$('#add-event').addEventListener('click',()=>openEvent());
$('#calendar-range').addEventListener('submit',e=>{e.preventDefault();loadCalendar();});
$('#calendar-today').addEventListener('click',()=>{defaultCalendarRange();loadCalendar();});
$('#calendar-list').addEventListener('click',e=>{
  const button=e.target.closest('button');if(!button)return;
  const row=calendarRows.find(r=>r.id===(button.dataset.editEvent||button.dataset.deleteEvent));if(!row)return;
  if(button.dataset.editEvent)openEvent(row);
  else{deletingEvent=row.id;$('#delete-event-summary').textContent=`${row.title} · ${eventPeriod(row)}`;$('#delete-event-error').textContent='';$('#delete-event-dialog').showModal();}
});
$('#event-form').addEventListener('submit',async e=>{
  e.preventDefault();const form=e.target,button=form.querySelector('[type=submit]'),event=editingEvent;
  if(!event)return;
  const values=Object.fromEntries(new FormData(form));
  if(values.end<=values.start){$('#event-error').textContent='結束時間必須晚於開始時間。';return;}
  button.disabled=true;$('#event-error').textContent='';
  const body={...values,start:values.start+':00+08:00',end:values.end+':00+08:00'};
  if(!event.existing)body.id=event.id;
  try{
    await api('/api/calendar'+(event.existing?'/'+event.id:''),{method:event.existing?'PUT':'POST',body:JSON.stringify(body)});
    $('#event-dialog').close();toast('行程已儲存，行政助理也能查到最新安排。');
    const range=$('#calendar-range');range.elements.start.value=values.start.slice(0,10);range.elements.end.value=values.end.slice(0,10);
    await Promise.all([loadCalendar(),syncDashboard()]);
  }catch(error){$('#event-error').textContent=error.message;}finally{button.disabled=false;}
});
$('#delete-event-confirm').addEventListener('click',async()=>{
  const button=$('#delete-event-confirm');button.disabled=true;
  try{await api('/api/calendar/'+deletingEvent,{method:'DELETE'});$('#delete-event-dialog').close();toast('行程已刪除。');await Promise.all([loadCalendar(),syncDashboard()]);}
  catch(error){if(error.status===404){$('#delete-event-dialog').close();await loadCalendar();await syncDashboard();toast('這筆行程已不存在，列表已更新。');}else $('#delete-event-error').textContent=error.message;}
  finally{button.disabled=false;}
});
