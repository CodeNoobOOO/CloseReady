const $ = id => document.getElementById(id);
const state = {token: sessionStorage.getItem('closeready_token') || '', cases: [], selected: null, data: null, generation: 0, busy: false, timer: null, pending: null};
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
  if(!response.ok) { const error = Error(`${result.error?.code || response.status}: ${result.error?.message || 'Request failed'}`); error.status=response.status; error.code=result.error?.code; throw error; }
  return result;
}
async function pages(path){let items=[],cursor=null;do{const q=new URLSearchParams({limit:'100'});if(cursor!==null)q.set('cursor',cursor);const page=await api(`${path}?${q}`);items.push(...page.items);cursor=page.next_cursor}while(cursor!==null);return items}
async function loadCases(){
  if(!state.token)return;
  const token=state.token;
  try {
    const cases=await pages('/cases');
    if(token!==state.token)return;
    state.cases=cases;
    $('cases').innerHTML=cases.map(c=>`<button class="case ${state.selected===c.case_id?'active':''}" data-case="${esc(c.case_id)}"><b>${esc(c.title || `${c.client_id} · ${c.accounting_period}`)}</b><small>${esc(c.client_id)} · ${esc(c.accounting_period)} · ${esc(c.readiness_status)}</small><small>Case …${esc(c.case_id.slice(-6))}</small></button>`).join('')||'<p class="muted">No cases in your scope.</p>';
  } catch(e){notice(e.message)}
}
async function loadCase(id, quiet=false){
  clearTimeout(state.timer);
  const generation=++state.generation;
  state.selected=id;
  if(!quiet){state.data=null;$('workspace').innerHTML='<div class="empty" role="status">Loading case…</div>';}
  try {
    const base=`/cases/${encodeURIComponent(id)}`;
    const names=['review-tasks','outbox','audit-events','findings','commitments','reminders','replies','documents'];
    const [caseData,collections,mailbox,reference]=await Promise.all([
      api(base),Promise.all(names.map(name=>pages(base+'/'+name))),
      pages(base+'/mailbox').then(items=>({items})).catch(e=>({error:e.message})),
      api(base+'/communication-reference')
    ]);
    const [reviews,outbox,audit,findings,commitments,reminders,replies,documents]=collections;
    const runIds=[...new Set([...audit.map(x=>x.run_id),...reviews.map(x=>x.run_id)].filter(Boolean))];
    const runs=await Promise.all(runIds.map(runId=>api(`/runs/${encodeURIComponent(runId)}`).catch(e=>({run_id:runId,load_error:e.message}))));
    if(generation!==state.generation || !state.token)return;
    state.data={caseData,reviews,outbox,audit,findings,commitments,reminders,replies,mailbox,runs,documents,reference};
    render();loadCases();
    if(documents.some(d=>['queued','processing'].includes(d.status)) || runs.some(r=>['queued','running'].includes(r.status))) {
      scheduleCaseRefresh(id);
    }
  } catch(e){
    if(generation!==state.generation)return;
    state.data=null;$('workspace').innerHTML=`<div class="empty" role="alert">${esc(e.message)}<p>Use Refresh to try again.</p></div>`;
  }
}
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
      const accountLabel=r.scope.masked_account_identifier;
      return `<div class="checklist-item ${accepted ? 'complete' : attention ? 'attention' : 'pending'}"><span class="checkmark" aria-hidden="true">${accepted ? '✓' : ''}</span><div class="checklist-content"><div class="row-head"><b>${title}</b></div><div class="checklist-status">${esc(labels[r.status] || r.status)}</div><div class="detail">${esc(r.scope.entity_id)}${accountLabel ? ' · account ' + esc(accountLabel) : ''} · ${esc(r.accounting_period)}</div>${r.description ? `<div class="detail">${esc(r.description)}</div>` : ''}${r.completion_rule?.expected_item_refs?.length ? `<div class="detail">Required items: ${r.completion_rule.expected_item_refs.map(esc).join(', ')}</div>` : ''}${r.scope.coverage_start ? `<div class="detail">Coverage: ${esc(r.scope.coverage_start)} to ${esc(r.scope.coverage_end)}</div>` : ''}<div class="detail">Reviewer: ${esc(r.reviewer_status)} · Evidence: ${r.evidence_refs.length ? r.evidence_refs.map(x => esc(x.document_id || x.ref || JSON.stringify(x))).join(', ') : 'none'}</div>${evidenceDetails(r.evidence_refs)}</div></div>`;
    }).join('');
  }).join('');
}
function render(){const {caseData:c,reviews,outbox,audit,findings,commitments,reminders,replies,mailbox,runs}=state.data;
 $('workspace').innerHTML=`<div class="top"><div><h1>${esc(c.title || `${c.client_id} · ${c.accounting_period}`)}</h1><p class="meta">${esc(c.client_id)} · ${esc(c.accounting_period)} · ${esc(c.case_id)} · version ${c.state_version} · owner ${esc(c.owner_user_id)} · due ${when(c.due_at)}</p></div>${pill(c.readiness_status)}</div>${renderOverview()}
 <div class="grid"><div class="panel"><h2>Document checklist</h2><p class="muted">A tick means the required documents have been accepted.</p>${renderChecklist(c.requirements)}</div>
 <div class="panel"><h2>Human review <span class="muted">${reviews.filter(x=>x.status==='open').length} open</span></h2>${reviews.map(t=>`<div class="row"><div class="row-head"><b>${esc(t.reason_code)}</b>${pill(t.status)}</div><div class="detail">${esc(t.reason)} · assigned ${esc(t.assigned_to)} · ${when(t.created_at)}</div>${t.draft?`<pre>Subject: ${esc(t.draft.subject)}\n\n${esc(t.draft.body)}</pre>`:''}${t.status==='open'?`<div class="actions">${t.draft?`<button data-review="approve_draft" data-id="${esc(t.review_task_id)}">Approve draft</button><button class="secondary" data-review="edit_and_approve" data-id="${esc(t.review_task_id)}">Edit & approve</button><button class="secondary" data-review="reject_draft" data-id="${esc(t.review_task_id)}">Reject</button>`:`<button class="secondary" data-review="dismiss_error" data-id="${esc(t.review_task_id)}">Dismiss error</button>`}</div>`:''}</div>`).join('')||'<p class="muted">No review tasks.</p>'}<p class="muted">Open reviews pause affected follow-up reminders until a manager resolves them.</p></div>
 ${renderDocuments()}
 <div class="panel"><h2>Reviewed outbox</h2>${outbox.map(o=>`<div class="row"><div class="row-head"><b>${esc(o.subject)}</b>${pill(o.delivery_status)}</div><div class="detail">${esc(o.requirement_ids.join(', '))} · ${when(o.created_at)}${o.reminder_id?' · reminder resend':''}${o.retry_of_outbox_id?' · retry of an earlier attempt':''}</div><pre>${esc(o.body)}</pre>${outboxActions(o)}</div>`).join('')||'<p class="muted">No approved drafts.</p>'}</div>
 <div class="panel"><h2>Client follow-up</h2><button id="new-reply">Record sandbox reply</button><div class="row"><h3>Replies</h3>${replies.map(r=>`<div class="row"><div class="row-head"><b>${esc(r.sender_contact_id)}</b><small>${when(r.received_at)}</small></div><pre>${esc(r.body)}</pre><button class="secondary" data-assess="${esc(r.reply_id)}">Assess reply</button></div>`).join('')||'<p class="muted">No replies.</p>'}</div><div class="row"><h3>Commitments</h3>${commitments.map(x=>`<div class="detail">${pill(x.status)} ${esc(x.requirement_id)} · promised ${when(x.promised_at)}</div>`).join('')||'<p class="muted">None.</p>'}</div><div class="row"><h3>Reminders</h3>${reminders.map(x=>`<div class="detail">${pill(x.status)} ${esc(x.requirement_ids.join(', '))} · ${when(x.scheduled_at)}</div>${reminderActions(x)}`).join('')||'<p class="muted">None.</p>'}</div><button id="dispatch" class="secondary">Dispatch due sandbox reminders</button></div>
 <div class="panel"><h2>Sandbox mailbox</h2>${Array.isArray(mailbox.items)?mailbox.items.map(m=>`<div class="row"><div class="row-head"><b>${esc(m.subject)}</b>${pill(m.source)}</div><div class="detail">To ${esc(m.to_email)} · ${when(m.sent_at)} · ${m.live?'live delivery':'simulated delivery'}</div></div>`).join('')||'<p class="muted">Empty.</p>':`<p class="muted">${esc(mailbox.error)}</p>`}</div>
 <div class="panel"><h2>Findings</h2>${findings.map(f=>`<div class="row"><div class="detail">${esc(f.finding_id || 'Reply assessment')} · ${esc(f.result || f.intent || '')}</div><pre>${esc(JSON.stringify(f,null,2))}</pre></div>`).join('')||'<p class="muted">No findings.</p>'}</div>
 <div class="panel wide"><h2>Run traces</h2>${runs.map(r=>r.load_error?`<p class="muted">${esc(r.run_id)}: ${esc(r.load_error)}</p>`:`<div class="row"><div class="row-head"><b>${esc(r.run_id)}</b>${pill(r.status)}</div><div class="detail">Mode ${esc(r.run_mode)} · ${esc(r.provider)} / ${esc(r.model)} · ${r.live?'live inference':'scripted inference'} · start version ${r.start_state_version} · ${when(r.started_at)}</div>${r.traces.map(t=>`<div class="detail">Step ${t.step}: ${esc(t.tool_names.join(', ') || 'model response')} · ${t.latency_ms} ms · tokens ${esc(t.usage?.total_tokens ?? 'not reported')} · ${esc(t.outcomes.join(', '))}${t.error_code?' · '+esc(t.error_code):''}</div>`).join('')}</div>`).join('')||'<p class="muted">No case analysis runs.</p>'}</div><div class="panel wide"><h2>Audit timeline</h2>${audit.slice().reverse().map(a=>`<div class="row"><div class="row-head"><b>${auditActionLabel(a)}</b>${pill(a.outcome)}</div><div class="detail">${when(a.occurred_at)} · actor ${esc(a.actor_user_id)} · run ${esc(a.run_id || '—')} · state ${esc(a.old_state_version ?? '—')} → ${esc(a.new_state_version ?? '—')} · policy ${esc(a.policy_id)} v${esc(a.policy_version)}</div><div class="detail">${esc(a.reason)}</div></div>`).join('')||'<p class="muted">No audit events.</p>'}</div></div>`;
}
function auditActionLabel(event){
  const labels={reject_document:'Document rejected',accept_document_evidence:'Document evidence accepted',reassign_document_processing:'Document reassigned for processing',prepare_document_correction:'Correction email requested',bind_document_requirement:'Document matched to requirement'};
  const label=labels[event.action] || event.action;
  const filename=event.details?.document_filename;
  return esc(label)+(filename?` · ${esc(filename)}`:'');
}
function resetView(){
  clearTimeout(state.timer);state.generation++;state.selected=null;state.data=null;state.pending=null;
  $('cases').innerHTML='';$('workspace').innerHTML='<div class="empty">Select a case.</div>';
}
async function connectSession(){
  resetView();
  state.token=$('token').value.trim();
  sessionStorage.setItem('closeready_token',state.token);
  await loadCases();
}
async function mutation(path,body,method='POST',{throwOnError=false}={}){
  if(state.busy)return;
  const pending=state.pending;
  if(pending && (pending.path!==path || pending.body!==body)){
    const error=Error('Resolve the previous uncertain request using Retry before making another change.');
    if(throwOnError)throw error;
    notice(error.message);return;
  }
  const op=pending || {path,body,method,key:uid(),caseId:state.data?.caseData.case_id};
  state.pending=op;state.busy=true;
  document.querySelectorAll('button').forEach(b=>{b.dataset.wasDisabled=String(b.disabled);b.disabled=true;});
  let result;
  try{
    result=await api(path,{method:op.method,headers:{'Idempotency-Key':op.key},body:body instanceof FormData?body:JSON.stringify(body)});
    state.pending=null;
    notice(result.associated===false?'Reply quarantined: sender requires review.':'Saved successfully.');
  }catch(e){
    if(e.status && e.status<500)state.pending=null;
    if(throwOnError)throw e;
    notice(e.message+(state.pending?' Outcome uncertain. Retry uses the same request key.':''));
  }finally{
    state.busy=false;
    document.querySelectorAll('button').forEach(b=>{b.disabled=b.dataset.wasDisabled==='true';delete b.dataset.wasDisabled;});
    if(op.caseId && state.token)await loadCase(op.caseId);
    else if(state.token)await loadCases();
  }
  return result;
}
function promptFormMarkup(fields){
  return `<form>${fields.map(f=>{
    const attrs=`id="f-${esc(f.name)}" name="${esc(f.name)}" ${f.optional?'':'required'}${f.maxLength?` maxlength="${esc(f.maxLength)}"`:''}`;
    const input=f.options ? `<select ${attrs}>${f.options.map(o=>`<option value="${esc(o.value)}" ${o.value===f.value?'selected':''}>${esc(o.label)}</option>`).join('')}</select>` : f.multiline ? `<textarea ${attrs}>${esc(f.value||'')}</textarea>` : `<input ${attrs} type="${f.type||'text'}" ${f.type==='file'?'accept="application/pdf,.pdf"':`value="${esc(f.value||'')}"`}>`;
    return `<div class="form-field"><label for="f-${esc(f.name)}">${esc(f.label)}</label>${input}${f.help?`<div class="field-help">${esc(f.help)}</div>`:''}<div class="field-error" data-field-error="${esc(f.name)}" role="alert" hidden></div></div>`;
  }).join('')}<div class="form-error" data-form-error role="alert" hidden></div><menu><button type="button" data-dialog-cancel class="secondary">Cancel</button><button type="submit">Continue</button></menu></form>`;
}
function showPromptErrors(form,errors){
  form.querySelectorAll('[data-field-error]').forEach(node=>{node.textContent='';node.hidden=true});
  form.querySelectorAll('[aria-invalid="true"]').forEach(node=>node.removeAttribute('aria-invalid'));
  const formError=form.querySelector('[data-form-error]');
  if(formError){formError.textContent='';formError.hidden=true}
  let firstInvalid=null;
  for(const [field,message] of Object.entries(errors || {})){
    const errorNode=field==='_form'?formError:form.querySelector(`[data-field-error="${field}"]`);
    if(errorNode){errorNode.textContent=message;errorNode.hidden=false}
    if(field!=='_form'){
      const input=form.querySelector(`[name="${field}"]`);
      if(input){input.setAttribute('aria-invalid','true');firstInvalid ||= input}
    }
  }
  firstInvalid?.focus();
}
function promptFields(title,fields,{validate,submit}={}){
  return new Promise(resolve=>{
    const d=document.createElement('dialog');d.setAttribute('aria-label',title);
    d.innerHTML=`<h2>${esc(title)}</h2>${promptFormMarkup(fields)}`;
    document.body.append(d);
    const form=d.querySelector('form');
    let submittedValues=null;
    form.addEventListener('submit',async event=>{
      event.preventDefault();
      const values=Object.fromEntries(new FormData(form));
      let errors={};
      try{errors=validate?.(values)||{}}catch(error){errors={_form:error.message}}
      if(Object.keys(errors).length){showPromptErrors(form,errors);return}
      if(!submit){submittedValues=values;d.close('ok');return}
      try{
        submittedValues=await submit(values);
        d.close('ok');
      }catch(error){showPromptErrors(form,{_form:error.message})}
    });
    d.querySelector('[data-dialog-cancel]').addEventListener('click',()=>d.close('cancel'));
    d.addEventListener('close',()=>{const values=d.returnValue==='ok'?submittedValues:null;d.remove();resolve(values);},{once:true});
    d.showModal();
  });
}
$('session').addEventListener('submit',async event=>{event.preventDefault();await connectSession()});
document.addEventListener('click',async e=>{const target=e.target instanceof Element ? e.target.closest('button') : null;if(!target)return;
 if(state.busy)return;
 if(target.id==='new-case'){try{await createCaseForm();}catch(error){notice(error.message)}return;}
 if(target.id==='disconnect'){resetView();state.token='';sessionStorage.removeItem('closeready_token');$('token').value='';$('cases').innerHTML='';$('workspace').innerHTML='<div class="empty">Disconnected.</div>';return}
 if(target.id==='refresh'){await loadCases();if(state.selected)await loadCase(state.selected);return}
 if(target.dataset.case){await loadCase(target.dataset.case);return}
 if(!state.data)return;const id=encodeURIComponent(state.data.caseData.case_id),base=`/cases/${id}`;
 if(target.dataset.review){const task=state.data.reviews.find(x=>x.review_task_id===target.dataset.id);const fields=[{name:'reason',label:'Review reason'}];if(target.dataset.review==='edit_and_approve')fields.push({name:'subject',label:'Edited subject',value:task.draft.subject},{name:'body',label:'Edited body',value:task.draft.body,multiline:true});const values=await promptFields(target.dataset.review.replaceAll('_',' '),fields);if(!values)return;const body={expected_state_version:state.data.caseData.state_version,review_task_id:task.review_task_id,decision:target.dataset.review,reason:values.reason};if(body.decision==='edit_and_approve')body.edited_draft={subject:values.subject,body:values.body,requirement_ids:task.draft.requirement_ids};await mutation(base+'/review-decisions',body);return}
 if(target.dataset.deliver){const values=await promptFields('Confirm approved delivery',[{name:'confirmation',label:'Type SEND to deliver (test sink simulates; SMTP sends real email)'}]);if(values?.confirmation==='SEND')await mutation(base+`/outbox/${encodeURIComponent(target.dataset.deliver)}/deliver`,{});return}
 if(target.dataset.retry){const values=await promptFields('Approve a new delivery attempt',[{name:'confirmation',label:'Type RETRY to create a new outbox item. Deliver it separately with a new request key.'}]);if(values?.confirmation==='RETRY')await mutation(base+`/outbox/${encodeURIComponent(target.dataset.retry)}/retry`,{expected_state_version:state.data.caseData.state_version});return}
 if(target.dataset.reconcile){const labels={confirm_delivered:'Confirm this unknown delivery as delivered',retry_delivery:'Confirm it was not delivered and create a new attempt',keep_unresolved:'Leave this unknown delivery unresolved'};const values=await promptFields(labels[target.dataset.reconcile],[{name:'confirmation',label:'Type CONFIRM to continue'}]);if(values?.confirmation==='CONFIRM')await mutation(base+`/outbox/${encodeURIComponent(target.dataset.id)}/reconcile`,{expected_state_version:state.data.caseData.state_version,decision:target.dataset.reconcile});return}
 if(target.dataset.reminderRetry){const values=await promptFields('Approve a new reminder delivery attempt',[{name:'confirmation',label:'Type RETRY to create a new outbox item'}]);if(values?.confirmation==='RETRY')await mutation(base+`/reminders/${encodeURIComponent(target.dataset.reminderRetry)}/retry`,{expected_state_version:state.data.caseData.state_version});return}
 if(target.dataset.reminderReconcile){const values=await promptFields('Reconcile unknown reminder delivery',[{name:'confirmation',label:'Type CONFIRM to continue'}]);if(values?.confirmation==='CONFIRM')await mutation(base+`/reminders/${encodeURIComponent(target.dataset.id)}/reconcile`,{expected_state_version:state.data.caseData.state_version,decision:target.dataset.reminderReconcile});return}
 if(target.id==='new-reply'){const values=await promptFields('Record a trusted sandbox reply',[{name:'sender_email',label:'Approved contact email',type:'email'},{name:'body',label:'Reply body',multiline:true}]);if(values)await mutation(base+'/replies',{expected_state_version:state.data.caseData.state_version,sender_email:values.sender_email,body:values.body,received_at:new Date().toISOString(),provider_message_id:null});return}
 if(target.dataset.assess){await mutation(base+`/replies/${encodeURIComponent(target.dataset.assess)}/assess`,{expected_state_version:state.data.caseData.state_version});return}
 if(target.id==='dispatch')await mutation(base+'/reminders/dispatch-due',{});
});
$('token').value=state.token;if(state.token)loadCases();

function outboxActions(item){
  if(item.delivery_status==='not_attempted') return `<button data-deliver="${esc(item.outbox_id)}">Deliver approved message</button>`;
  if(item.delivery_status==='failed') return `<button data-retry="${esc(item.outbox_id)}">Approve new delivery attempt</button>`;
  if(item.delivery_status==='delivery_unknown') return `<div class="actions"><button data-reconcile="confirm_delivered" data-id="${esc(item.outbox_id)}">Confirm delivered</button><button class="secondary" data-reconcile="retry_delivery" data-id="${esc(item.outbox_id)}">Confirm not delivered and retry</button><button class="secondary" data-reconcile="keep_unresolved" data-id="${esc(item.outbox_id)}">Keep unresolved</button></div>`;
  return '';
}
function reminderActions(item){
  if(item.status==='failed') return `<button class="secondary" data-reminder-retry="${esc(item.reminder_id)}">Approve new delivery attempt</button>`;
  if(item.status==='delivery_unknown') return `<div class="actions"><button data-reminder-reconcile="confirm_delivered" data-id="${esc(item.reminder_id)}">Confirm delivered</button><button class="secondary" data-reminder-reconcile="retry_delivery" data-id="${esc(item.reminder_id)}">Confirm not delivered and retry</button><button class="secondary" data-reminder-reconcile="keep_unresolved" data-id="${esc(item.reminder_id)}">Keep unresolved</button></div>`;
  return '';
}
function evidenceDetails(refs){
  if(!refs?.length)return '';
  return `<details><summary>View evidence (${refs.length})</summary>${refs.map(ref=>`<div class="detail">${esc(ref.document_id)} · page ${esc(ref.page)}<blockquote>${esc(ref.excerpt || ref.quote || ref.text || JSON.stringify(ref))}</blockquote></div>`).join('')}</details>`;
}
async function loadDocumentPdf(path){
  const response=await fetch(`/api/v1${path}/content`,{
    headers:{Authorization:`Bearer ${state.token}`}
  });
  if(!response.ok){
    let result={};try{result=await response.json()}catch{}
    const error=Error(`${result.error?.code || response.status}: ${result.error?.message || 'Original PDF could not be loaded'}`);
    error.status=response.status;throw error;
  }
  return URL.createObjectURL(await response.blob());
}
function documentPreviewMarkup(url){
  return `<div class="document-preview-actions"><a class="pdf-link" href="${esc(url)}" target="_blank" rel="noopener">Open original PDF in new tab</a></div><iframe class="document-preview" title="Original uploaded PDF" src="${esc(url)}"></iframe>`;
}
function shouldOpenDocumentDecision(returnValue){return returnValue==='decide'}
function evidenceDialogActionsMarkup(){
  return `<form method="dialog"><menu><button type="submit" value="close" class="secondary">Close</button><button type="submit" value="decide">Make decision</button></menu></form>`;
}
function renderOverview(){
  const {caseData:c,reviews,documents,commitments,reminders,reference}=state.data;
  const missing=c.requirements.filter(r=>!['accepted','waived'].includes(r.status));
  const open=reviews.filter(r=>r.status==='open');
  const active=commitments.filter(x=>x.status==='active');
  const next=reminders.filter(x=>x.status==='scheduled').sort((a,b)=>a.scheduled_at.localeCompare(b.scheduled_at))[0];
  const ready=c.readiness_status==='ready_for_confirmation'&&!open.length&&!documents.some(d=>['needs_review','queued','processing'].includes(d.status));
  let action=c.readiness_status==='ready'?'Ready for bookkeeping.':open.length?'Manager review is needed.':documents.some(d=>['queued','processing'].includes(d.status))?'Documents are processing.':documents.some(d=>d.status==='needs_review')?'Review the document evidence below.':missing.length?'Collect the outstanding documents.':'Confirm readiness after reviewing the evidence.';
  return `<div class="panel overview"><div class="summary-grid"><div><small>Outstanding requirements</small><strong>${missing.length} / ${c.requirements.length}</strong></div><div><small>Open reviews</small><strong>${open.length + documents.filter(d=>d.status==='needs_review').length}</strong></div><div><small>Next reminder</small><strong class="date">${next?when(next.scheduled_at):'Not scheduled'}</strong></div></div><p>${esc(action)}</p><div class="detail">Owner: ${esc(c.owner_user_id)} · Case reference: ${esc(reference.public_reference || '—')} · Timezone: ${esc(c.timezone)}</div>${active.length?`<p>Client promise: ${active.map(x=>when(x.promised_at)).join(', ')}</p>`:''}<div class="actions"><button data-flow="upload">Upload PDF</button><button data-flow="activate" class="secondary">Generate AI follow-up</button><button data-flow="deadline" class="secondary">Change deadline</button><button data-flow="ready" ${ready?'':'disabled'}>${c.readiness_status==='ready'?'Readiness confirmed':'Confirm ready'}</button>${c.readiness_status==='ready'?'<button data-flow="reopen" class="secondary">Undo ready confirmation</button>':''}${state.pending?'<button data-flow="retry">Retry uncertain request</button>':''}</div><p class="detail">Final confirmation requires all requirements resolved and all reviews closed.</p></div>`;
}
function renderDocuments(){
  const {documents,caseData:c}=state.data;
  const requirementName=id=>{const r=c.requirements.find(x=>x.requirement_id===id);return r?`${r.document_type.replaceAll('_',' ')} · ${r.scope.masked_account_identifier || r.scope.entity_id}`:'Not assigned';};
  return `<div class="panel wide"><div class="row-head"><h2>Documents & evidence</h2><button data-flow="upload">Upload PDF</button></div><p class="muted">Text-based and clearly scanned PDFs, up to 5 MiB. Low-confidence scans require human review.</p>${documents.map(d=>`<div class="row"><div class="row-head"><b>${esc(d.original_filename)}</b>${pill(d.status)}</div><div class="detail">${esc(requirementName(d.requirement_id))} · ${when(d.created_at)} · ${(d.size_bytes/1024).toFixed(1)} KB${d.duplicate_of_document_id?' · Duplicate upload':''}</div><div class="actions">${!['queued','processing'].includes(d.status)?`<button class="secondary" data-flow="evidence" data-document="${esc(d.document_id)}">View finding & review history</button>`:'<span class="muted">Waiting for the document worker. This page refreshes automatically.</span>'}${d.status==='needs_review'?`<button data-flow="doc-review" data-document="${esc(d.document_id)}">Review document</button>`:''}</div></div>`).join('')||'<p class="muted">No documents yet. Upload evidence for an outstanding requirement.</p>'}</div>`;
}
function requirementsOptions(){return state.data.caseData.requirements.map(r=>({value:r.requirement_id,label:`${r.document_type.replaceAll('_',' ')} · ${r.scope.masked_account_identifier||r.scope.entity_id} · ${r.status}`}));}
function documentReviewBody(version,values){
  const rejected=values.decision==='reject_document';
  return {expected_state_version:version,decision:values.decision,
    target_requirement_id:rejected?null:values.target,reason:values.reason,
    prepare_correction_email:rejected && values.follow_up==='prepare'};
}
async function showEvidence(base,documentId){
  const path=base+'/documents/'+encodeURIComponent(documentId);
  const [finding,history,preview]=await Promise.all([
    api(path+'/finding'),
    pages(path+'/review-decisions'),
    loadDocumentPdf(path).then(url=>({url})).catch(error=>({error:error.message}))
  ]);
  const d=document.createElement('dialog');d.setAttribute('aria-label','Document evidence');
  d.className='evidence-dialog';
  const original=preview.url?documentPreviewMarkup(preview.url):`<p class="preview-error" role="alert">Original PDF unavailable: ${esc(preview.error)}</p>`;
  d.innerHTML=`<h2>Document evidence</h2>${original}<section class="finding-summary"><h3>Automated finding</h3>${pill(finding.result)}<p>Detected: ${esc(finding.detected_type || 'Unknown')} · ${esc(finding.detected_period || 'Unknown period')}</p><p>Entity: ${esc(finding.entity_match)} · Account: ${esc(finding.account_match || 'Not applicable')}</p><p>Coverage: ${esc(finding.coverage_start||'—')} to ${esc(finding.coverage_end||'—')}</p><h3>Issues and uncertainty</h3><p>${[...finding.issues,...finding.uncertainty_reasons].map(esc).join('<br>')||'None reported.'}</p>${evidenceDetails(finding.evidence_refs)}<h3>Review history</h3>${history.map(h=>`<p>${esc(h.decision)} · ${esc(h.reviewer_user_id)} · ${when(h.decided_at)}<br>${esc(h.reason)}</p>`).join('')||'<p>No human decision recorded.</p>'}</section>${evidenceDialogActionsMarkup()}`;
  document.body.append(d);
  return new Promise(resolve=>{
    d.addEventListener('close',()=>{
      const proceed=shouldOpenDocumentDecision(d.returnValue);
      if(preview.url)URL.revokeObjectURL(preview.url);
      d.remove();resolve(proceed);
    },{once:true});
    d.showModal();
  });
}
document.addEventListener('click',async event=>{
  const button=event.target instanceof Element?event.target.closest('[data-flow]'):null;
  if(!button || state.busy || !state.data)return;
  const c=state.data.caseData;
  const base='/cases/'+encodeURIComponent(c.case_id);
  const version=c.state_version;
  try{
    switch(button.dataset.flow){
      case 'upload': {
        const values=await promptFields('Upload document',[
          {name:'requirement',label:'Requirement',options:[{value:'',label:'Let the assessor match the document'},...requirementsOptions()],optional:true},
          {name:'file',label:'PDF file (maximum 5 MiB)',type:'file'}]);
        if(!values)return;
        if(!values.file.size || values.file.size>5*1024*1024 || !values.file.name.toLowerCase().endsWith('.pdf'))throw Error('Choose a non-empty PDF no larger than 5 MiB.');
        const body=new FormData();body.set('file',values.file,values.file.name);body.set('expected_state_version',version);
        if(values.requirement)body.set('requirement_id',values.requirement);
        await mutation(base+'/documents',body);break;
      }
      case 'doc-review': {
        const proceed=await showEvidence(base,button.dataset.document);
        if(!proceed)break;
        const values=await promptFields('Review document',[
          {name:'decision',label:'Decision',options:[{value:'accept_for_requirement',label:'Accept evidence for requirement'},{value:'reassign_for_processing',label:'Reassign and process again'},{value:'reject_document',label:'Reject document'}]},
          {name:'target',label:'Target requirement (ignored when rejecting)',options:requirementsOptions()},
          {name:'follow_up',label:'After rejection',options:[{value:'none',label:'Reject only'},{value:'prepare',label:'Reject and prepare correction email'}]},
          {name:'reason',label:'Review reason',multiline:true}]);
        if(values)await mutation(base+'/documents/'+encodeURIComponent(button.dataset.document)+'/review-decisions',documentReviewBody(version,values));
        break;
      }
      case 'evidence':await showEvidence(base,button.dataset.document);break;
      case 'ready': {
        const values=await promptFields('Confirm readiness for bookkeeping',[{name:'reason',label:'Confirm that you reviewed the evidence and resolved all outstanding issues',multiline:true}]);
        if(values)await mutation(base+'/confirm-readiness',{expected_state_version:version,reason:values.reason});break;
      }
      case 'reopen': {
        const values=await promptFields('Undo ready confirmation',[{name:'reason',label:'Reason for reopening (accepted documents and audit history will be preserved)',multiline:true}]);
        if(values)await mutation(base+'/reopen',{expected_state_version:version,reason:values.reason});break;
      }
      case 'deadline': {
        const values=await promptFields('Change case deadline',[{name:'due',label:'New deadline (ISO timestamp with timezone, e.g. 2026-09-25T17:00:00+08:00)',value:c.due_at},{name:'reason',label:'Reason',multiline:true}]);
        if(values)await mutation(base+'/deadline',{expected_state_version:version,due_at:values.due,reason:values.reason},'PATCH');break;
      }
      case 'activate': {
        const values=await promptFields('Generate AI follow-up',[{name:'confirm',label:'Type GENERATE to request a live model analysis'}]);
        if(values?.confirm==='GENERATE')await mutation(base+'/activate',{expected_state_version:version});break;
      }
      case 'retry': if(state.pending)await mutation(state.pending.path,state.pending.body,state.pending.method);break;
    }
  }catch(error){notice(error.message)}
});

function scheduleCaseRefresh(id){
  clearTimeout(state.timer);
  state.timer=setTimeout(()=>{
    if(state.selected!==id || !state.token)return;
    if(state.busy || document.querySelector('dialog[open]'))scheduleCaseRefresh(id);
    else loadCase(id,true);
  },3000);
}

function commaSeparated(value){
  return [...new Set(String(value || '').split(',').map(x=>x.trim()).filter(Boolean))];
}

function caseFormError(field,message){
  const error=Error(message);error.field=field;return error;
}

function buildCaseRequirements(values){
  const requirements=[];
  const [year,month]=values.period.split('-').map(Number);
  const scope={entity_id:values.entity,account_ref:null,coverage_start:null,coverage_end:null};
  const bankAccounts=commaSeparated(values.bank_accounts).map(value=>{
    const match=value.match(/^(?:([A-Za-z0-9 _-]{1,80}):)?(\d{4})$/);
    if(!match)throw caseFormError('bank_accounts','Each bank account entry must be four digits or a label followed by four digits, for example operating:1234.');
    return {accountRef:(match[1]||`bank_account_${match[2]}`).trim(),suffix:match[2]};
  });
  if(new Set(bankAccounts.map(item=>item.accountRef.toLowerCase())).size!==bankAccounts.length)throw caseFormError('bank_accounts','Bank account labels must be unique within the case.');
  for(const {accountRef,suffix} of bankAccounts){
    requirements.push({
      document_type:'bank_statement',accounting_period:values.period,
      scope:{...scope,account_ref:accountRef,masked_account_identifier:`****${suffix}`,coverage_start:values.period+'-01',coverage_end:values.period+'-'+new Date(Date.UTC(year,month,0)).getUTCDate()},
      completion_rule:{kind:'coverage',expected_item_refs:[],allow_multiple_documents:true}
    });
  }
  for(const [field,type] of [['invoices','invoice'],['receipts','receipt']]){
    for(const ref of commaSeparated(values[field])){
      requirements.push({document_type:type,accounting_period:values.period,scope,completion_rule:{kind:'explicit_items',expected_item_refs:[ref],allow_multiple_documents:false}});
    }
  }
  const otherRefs=commaSeparated(values.other_refs);
  if(otherRefs.length){
    requirements.push({document_type:'other_supporting_document',accounting_period:values.period,scope,completion_rule:{kind:'explicit_items',expected_item_refs:otherRefs,allow_multiple_documents:true},description:values.other});
  }
  if(!requirements.length)throw caseFormError('_form','Configure at least one bank account or required item reference.');
  if(String(values.other_refs || '').trim()&&!String(values.other || '').trim())throw caseFormError('other','Other supporting documents need a description.');
  return requirements;
}

function caseFormErrors(values){
  const title=String(values.title || '').trim();
  if(!title)return {title:'Enter a case title.'};
  if(title.length>120)return {title:'Case title must be 120 characters or fewer.'};
  try{buildCaseRequirements(values);return {}}
  catch(error){return {[error.field || '_form']:error.message}}
}

async function createCaseForm(){
  if(!state.token){notice('Connect before creating a case.');return;}
  const created=await promptFields('Create client-period checklist',[
    {name:'title',label:'Case title',maxLength:120},
    {name:'client',label:'Client ID'}, {name:'owner',label:'Manager user ID'},
    {name:'policy',label:'Approved policy ID'}, {name:'period',label:'Accounting period',type:'month'},
    {name:'timezone',label:'Business timezone',value:Intl.DateTimeFormat().resolvedOptions().timeZone},
    {name:'due',label:'Deadline (your local time)',type:'datetime-local'},
    {name:'entity',label:'Entity ID'},
    {name:'bank_accounts',label:'Bank accounts',help:'Required only when collecting bank statements. Enter the final four digits, for example 1234, or a label and final four digits, for example operating:1234. Separate multiple accounts with commas.',optional:true},
    {name:'invoices',label:'Required invoice references, comma-separated (optional)',optional:true},
    {name:'receipts',label:'Required receipt references, comma-separated (optional)',optional:true},
    {name:'other',label:'Other supporting document description (optional)',optional:true},
    {name:'other_refs',label:'Required references for other documents (optional)',optional:true},
  ],{validate:caseFormErrors,submit:async values=>{
    const requirements=buildCaseRequirements(values);
    return mutation('/cases',{title:values.title.trim(),client_id:values.client,owner_user_id:values.owner,policy_id:values.policy,accounting_period:values.period,timezone:values.timezone,due_at:new Date(values.due).toISOString(),requirements},'POST',{throwOnError:true});
  }});
  if(created)await loadCase(created.case_id);
}
