const $ = id => document.getElementById(id);
const state = {token: sessionStorage.getItem('closeready_token') || '', cases: [], selected: null, data: null};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when = value => value ? new Date(value).toLocaleString() : '—';
const pill = value => `<span class="pill ${esc(value)}">${esc((value || 'unknown').replaceAll('_',' '))}</span>`;
const uid = () => crypto.randomUUID();
function notice(message){$('toast').textContent=message;$('toast').classList.add('show');setTimeout(()=>$('toast').classList.remove('show'),5000)}
async function api(path, options={}){
  const headers={Authorization:`Bearer ${state.token}`,...options.headers};
  if(options.body && !(options.body instanceof FormData)) headers['Content-Type']='application/json';
  const response=await fetch(`/api/v1${path}`,{...options,headers});
  let result;try{result=await response.json()}catch{throw Error(`HTTP ${response.status}`)}
  if(!response.ok) throw Error(`${result.error?.code || response.status}: ${result.error?.message || 'Request failed'}`);
  return result;
}
async function pages(path){let items=[],cursor=null;do{const q=new URLSearchParams({limit:'100'});if(cursor!==null)q.set('cursor',cursor);const page=await api(`${path}?${q}`);items.push(...page.items);cursor=page.next_cursor}while(cursor!==null);return items}
async function loadCases(){if(!state.token)return;try{state.cases=await pages('/cases');$('cases').innerHTML=state.cases.map(c=>`<button class="case ${state.selected===c.case_id?'active':''}" data-case="${esc(c.case_id)}"><b>${esc(c.client_id)}</b><small>${esc(c.accounting_period)} · ${esc(c.readiness_status)}</small></button>`).join('')||'<p class="muted">No cases in your scope.</p>'}catch(e){notice(e.message)}}
async function loadCase(id){state.selected=id;await loadCases();try{
 const base=`/cases/${encodeURIComponent(id)}`;
 const [caseData,reviews,outbox,audit,findings,commitments,reminders,replies,mailbox]=await Promise.all([
 api(base),pages(base+'/review-tasks'),pages(base+'/outbox'),pages(base+'/audit-events'),pages(base+'/findings'),pages(base+'/commitments'),pages(base+'/reminders'),pages(base+'/replies'),pages(base+'/mailbox').then(items=>({items})).catch(e=>({error:e.message}))
 ]);
 const runIds=[...new Set([...audit.map(x=>x.run_id),...reviews.map(x=>x.run_id)].filter(Boolean))];
 const runs=await Promise.all(runIds.map(runId=>api(`/runs/${encodeURIComponent(runId)}`).catch(()=>null)));
 state.data={caseData,reviews,outbox,audit,findings,commitments,reminders,replies,mailbox,runs:runs.filter(Boolean)};render();
}catch(e){notice(e.message)}}
const documentCategories = [
  ['bank_statement', 'Bank statements'],
  ['invoice', 'Invoices'],
  ['receipt', 'Receipts'],
  ['other_supporting_document', 'Other supporting documents'],
];
function renderChecklist(requirements) {
  return documentCategories.map(([type, title]) => {
    const items = requirements.filter(r => r.document_type === type);
    if (!items.length) return `<div class="checklist-item unconfigured"><span class="checkmark" aria-hidden="true"></span><div><b>${title}</b><div class="detail">Not configured for this case</div></div></div>`;
    return items.map(r => {
      const accepted = r.status === 'accepted';
      const attention = ['missing', 'needs_clarification'].includes(r.status);
      const labels = {accepted:'Accepted', missing:'Missing — please submit', needs_clarification:'Correction needed', received:'Received — verification pending', awaiting_review:'Received — awaiting review', waived:'Waived — submission not required'};
      return `<div class="checklist-item ${accepted ? 'complete' : attention ? 'attention' : 'pending'}"><span class="checkmark" aria-hidden="true">${accepted ? '✓' : ''}</span><div class="checklist-content"><div class="row-head"><b>${title}</b></div><div class="checklist-status">${esc(labels[r.status] || r.status)}</div><div class="detail">${esc(r.scope.entity_id)}${r.scope.account_ref ? ' · ' + esc(r.scope.account_ref) : ''} · ${esc(r.accounting_period)}</div>${r.description ? `<div class="detail">${esc(r.description)}</div>` : ''}<div class="detail">Reviewer: ${esc(r.reviewer_status)} · Evidence: ${r.evidence_refs.length ? r.evidence_refs.map(x => esc(x.document_id || x.ref || JSON.stringify(x))).join(', ') : 'none'}</div></div></div>`;
    }).join('');
  }).join('');
}
function render(){const {caseData:c,reviews,outbox,audit,findings,commitments,reminders,replies,mailbox,runs}=state.data;
 $('workspace').innerHTML=`<div class="top"><div><h1>${esc(c.client_id)} · ${esc(c.accounting_period)}</h1><p class="meta">${esc(c.case_id)} · version ${c.state_version} · owner ${esc(c.owner_user_id)} · due ${when(c.due_at)}</p></div>${pill(c.readiness_status)}</div>
 <div class="grid"><div class="panel"><h2>Document checklist</h2><p class="muted">A tick means the required documents have been accepted.</p>${renderChecklist(c.requirements)}</div>
 <div class="panel"><h2>Human review <span class="muted">${reviews.filter(x=>x.status==='open').length} open</span></h2>${reviews.map(t=>`<div class="row"><div class="row-head"><b>${esc(t.reason_code)}</b>${pill(t.status)}</div><div class="detail">${esc(t.reason)} · assigned ${esc(t.assigned_to)} · ${when(t.created_at)}</div>${t.draft?`<pre>Subject: ${esc(t.draft.subject)}\n\n${esc(t.draft.body)}</pre>`:''}${t.status==='open'?`<div class="actions">${t.draft?`<button data-review="approve_draft" data-id="${esc(t.review_task_id)}">Approve draft</button><button class="secondary" data-review="edit_and_approve" data-id="${esc(t.review_task_id)}">Edit & approve</button><button class="secondary" data-review="reject_draft" data-id="${esc(t.review_task_id)}">Reject</button>`:`<button class="secondary" data-review="dismiss_error" data-id="${esc(t.review_task_id)}">Dismiss error</button>`}</div>`:''}</div>`).join('')||'<p class="muted">No review tasks.</p>'}<p class="muted">Evidence correction, pause and final readiness confirmation are not available in the current API.</p></div>
 <div class="panel"><h2>Reviewed outbox</h2>${outbox.map(o=>`<div class="row"><div class="row-head"><b>${esc(o.subject)}</b>${pill(o.delivery_status)}</div><div class="detail">${esc(o.requirement_ids.join(', '))} · ${when(o.created_at)}</div><pre>${esc(o.body)}</pre>${o.delivery_status==='not_attempted'?`<button data-deliver="${esc(o.outbox_id)}">Send to approved sandbox contact</button>`:''}</div>`).join('')||'<p class="muted">No approved drafts.</p>'}</div>
 <div class="panel"><h2>Client follow-up</h2><button id="new-reply">Record sandbox reply</button><div class="row"><h3>Replies</h3>${replies.map(r=>`<div class="row"><div class="row-head"><b>${esc(r.sender_contact_id)}</b><small>${when(r.received_at)}</small></div><pre>${esc(r.body)}</pre><button class="secondary" data-assess="${esc(r.reply_id)}">Assess reply</button></div>`).join('')||'<p class="muted">No replies.</p>'}</div><div class="row"><h3>Commitments</h3>${commitments.map(x=>`<div class="detail">${pill(x.status)} ${esc(x.requirement_id)} · promised ${when(x.promised_at)}</div>`).join('')||'<p class="muted">None.</p>'}</div><div class="row"><h3>Reminders</h3>${reminders.map(x=>`<div class="detail">${pill(x.status)} ${esc(x.requirement_ids.join(', '))} · ${when(x.scheduled_at)}</div>`).join('')||'<p class="muted">None.</p>'}</div><button id="dispatch" class="secondary">Dispatch due sandbox reminders</button></div>
 <div class="panel"><h2>Sandbox mailbox</h2>${Array.isArray(mailbox.items)?mailbox.items.map(m=>`<div class="row"><div class="row-head"><b>${esc(m.subject)}</b>${pill(m.source)}</div><div class="detail">To ${esc(m.to_email)} · ${when(m.sent_at)} · simulated delivery</div></div>`).join('')||'<p class="muted">Empty.</p>':`<p class="muted">${esc(mailbox.error)}</p>`}</div>
 <div class="panel"><h2>Findings</h2>${findings.map(f=>`<div class="row"><div class="detail">${esc(f.finding_id || 'Reply assessment')} · ${esc(f.result || f.intent || '')}</div><pre>${esc(JSON.stringify(f,null,2))}</pre></div>`).join('')||'<p class="muted">No findings.</p>'}</div>
 <div class="panel wide"><h2>Run traces</h2>${runs.map(r=>`<div class="row"><div class="row-head"><b>${esc(r.run_id)}</b>${pill(r.status)}</div><div class="detail">Mode ${esc(r.run_mode)} · ${esc(r.provider)} / ${esc(r.model)} · ${r.live?'live inference':'scripted inference'} · start version ${r.start_state_version} · ${when(r.started_at)}</div>${r.traces.map(t=>`<div class="detail">Step ${t.step}: ${esc(t.tool_names.join(', ') || 'model response')} · ${t.latency_ms} ms · ${esc(t.outcomes.join(', '))}${t.error_code?' · '+esc(t.error_code):''}</div>`).join('')}</div>`).join('')||'<p class="muted">No case analysis runs.</p>'}</div><div class="panel wide"><h2>Audit timeline</h2>${audit.slice().reverse().map(a=>`<div class="row"><div class="row-head"><b>${esc(a.action)}</b>${pill(a.outcome)}</div><div class="detail">${when(a.occurred_at)} · actor ${esc(a.actor_user_id)} · run ${esc(a.run_id || '—')} · state ${esc(a.old_state_version ?? '—')} → ${esc(a.new_state_version ?? '—')} · policy ${esc(a.policy_id)} v${esc(a.policy_version)}</div></div>`).join('')||'<p class="muted">No audit events.</p>'}</div></div>`;
}
async function mutation(path,body){try{await api(path,{method:'POST',headers:{'Idempotency-Key':uid()},body:JSON.stringify(body)});notice('Saved. Current case reloaded.');await loadCase(state.selected)}catch(e){notice(e.message);if(e.message.startsWith('STALE_STATE'))await loadCase(state.selected)}}
function promptFields(title,fields){return new Promise(resolve=>{const d=document.createElement('dialog');d.innerHTML=`<h2>${esc(title)}</h2><form method="dialog">${fields.map(f=>`<label for="f-${esc(f.name)}">${esc(f.label)}</label>${f.multiline?`<textarea id="f-${esc(f.name)}" name="${esc(f.name)}" required>${esc(f.value||'')}</textarea>`:`<input id="f-${esc(f.name)}" name="${esc(f.name)}" value="${esc(f.value||'')}" ${f.type?`type="${f.type}"`:''} required>`}`).join('')}<menu><button type="submit" value="cancel" class="secondary" formnovalidate>Cancel</button><button type="submit" value="ok">Continue</button></menu></form>`;document.body.append(d);d.showModal();d.addEventListener('close',()=>{const values=d.returnValue==='ok'?Object.fromEntries(new FormData(d.querySelector('form'))):null;d.remove();resolve(values)},{once:true})})}
document.addEventListener('click',async e=>{const target=e.target;if(!(target instanceof HTMLElement))return;
 if(target.id==='connect'){state.token=$('token').value.trim();sessionStorage.setItem('closeready_token',state.token);await loadCases();return}
 if(target.id==='disconnect'){state.token='';sessionStorage.removeItem('closeready_token');$('token').value='';$('cases').innerHTML='';$('workspace').innerHTML='<div class="empty">Disconnected.</div>';return}
 if(target.id==='refresh'){await loadCases();if(state.selected)await loadCase(state.selected);return}
 if(target.dataset.case){await loadCase(target.dataset.case);return}
 if(!state.data)return;const id=encodeURIComponent(state.selected),base=`/cases/${id}`;
 if(target.dataset.review){const task=state.data.reviews.find(x=>x.review_task_id===target.dataset.id);const fields=[{name:'reason',label:'Review reason'}];if(target.dataset.review==='edit_and_approve')fields.push({name:'subject',label:'Edited subject',value:task.draft.subject},{name:'body',label:'Edited body',value:task.draft.body,multiline:true});const values=await promptFields(target.dataset.review.replaceAll('_',' '),fields);if(!values)return;const body={expected_state_version:state.data.caseData.state_version,review_task_id:task.review_task_id,decision:target.dataset.review,reason:values.reason};if(body.decision==='edit_and_approve')body.edited_draft={subject:values.subject,body:values.body,requirement_ids:task.draft.requirement_ids};await mutation(base+'/review-decisions',body);return}
 if(target.dataset.deliver){const values=await promptFields('Confirm sandbox delivery',[{name:'confirmation',label:'Type SEND to deliver to the approved sandbox contact'}]);if(values?.confirmation==='SEND')await mutation(base+`/outbox/${encodeURIComponent(target.dataset.deliver)}/deliver`,{});return}
 if(target.id==='new-reply'){const values=await promptFields('Record a trusted sandbox reply',[{name:'sender_email',label:'Approved contact email',type:'email'},{name:'body',label:'Reply body',multiline:true}]);if(values)await mutation(base+'/replies',{expected_state_version:state.data.caseData.state_version,sender_email:values.sender_email,body:values.body,received_at:new Date().toISOString(),provider_message_id:null});return}
 if(target.dataset.assess){await mutation(base+`/replies/${encodeURIComponent(target.dataset.assess)}/assess`,{expected_state_version:state.data.caseData.state_version});return}
 if(target.id==='dispatch')await mutation(base+'/reminders/dispatch-due',{});
});
$('token').value=state.token;if(state.token)loadCases();
