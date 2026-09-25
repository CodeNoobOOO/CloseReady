const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('closeready/frontend/app.js','utf8');
function sandbox(){
  const elements=new Map();
  const context=vm.createContext({
    sessionStorage:{getItem:()=>'',setItem:()=>{},removeItem:()=>{}},
    document:{getElementById:id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',value:'',classList:{add(){},remove(){}},addEventListener(){}});return elements.get(id)},addEventListener(){},querySelectorAll:()=>[],querySelector:()=>null},
    setTimeout:()=>0,clearTimeout(){},FormData,crypto:require('node:crypto').webcrypto,URLSearchParams,
  });
  vm.runInContext(source,context);
  return context;
}
const run=(c,code)=>vm.runInContext(code,c);
test('late response from previous case cannot replace current case',async()=>{
  const c=sandbox();let release;
  const slow=new Promise(resolve=>release=resolve);
  c.api=async path=>path==='/cases/A'?slow:path==='/cases/B'?{case_id:'B'}:{};
  c.pages=async()=>[];
  run(c,"state.token='test'; render=()=>{}; loadCases=async()=>{};");
  const first=run(c,"loadCase('A')");
  await run(c,"loadCase('B')");
  release({case_id:'A'});await first;
  assert.equal(run(c,'state.data.caseData.case_id'),'B');
});
test('disconnect invalidates pending case fetch',async()=>{
  const c=sandbox();let release;
  c.api=async path=>path==='/cases/A'?new Promise(resolve=>release=resolve):{};
  c.pages=async()=>[];
  run(c,"state.token='test';render=()=>{};loadCases=async()=>{};");
  const request=run(c,"loadCase('A')");
  run(c,"resetView();state.token='';");release({case_id:'A'});await request;
  assert.equal(run(c,'state.data'),null);
});
test('retry preserves request body and idempotency key after network failure',async()=>{
  const c=sandbox(),attempts=[];
  c.api=async(path,options)=>{attempts.push({path,...options});if(attempts.length===1)throw Error('connection lost');return {};};
  run(c,"state.token='test';state.data={caseData:{case_id:'A'}};loadCase=async()=>{};loadCases=async()=>{};");
  await run(c,"mutation('/cases/A/documents',{expected_state_version:2})");
  assert.ok(run(c,'state.pending'));
  await run(c,"mutation(state.pending.path,state.pending.body,state.pending.method)");
  assert.equal(attempts[0].headers['Idempotency-Key'],attempts[1].headers['Idempotency-Key']);
  assert.equal(attempts[0].body,attempts[1].body);
  assert.equal(run(c,'state.pending'),null);
});
test('only accepted evidence is ticked, all four categories remain visible',()=>{
  const c=sandbox();
  const row={document_type:'bank_statement',scope:{entity_id:'demo'},accounting_period:'2026-07',reviewer_status:'not_required',evidence_refs:[]};
  const render=status=>run(c,`renderChecklist(${JSON.stringify([{...row,status}])})`);
  assert.match(render('accepted'),/✓/);
  for(const status of ['missing','received','awaiting_review','waived','needs_clarification'])assert.doesNotMatch(render(status),/✓/);
  assert.match(render('missing'),/checklist-item attention/);
  for(const title of ['Bank statements','Invoices','Receipts','Other supporting documents'])assert.ok(render('missing').includes(title));
});
test('ready confirmation disabled with an unresolved review task',()=>{
  const c=sandbox();
  run(c,`state.data=${JSON.stringify({caseData:{case_id:'a',readiness_status:'ready_for_confirmation',requirements:[]},reviews:[{status:'open'}],documents:[],commitments:[],reminders:[],reference:{}})}`);
  assert.match(run(c,'renderOverview()'),/data-flow="ready" disabled/);
});

test('document review blocks final confirmation and evidence excerpts are escaped',()=>{
  const c=sandbox();
  run(c,`state.data=${JSON.stringify({caseData:{case_id:'a',readiness_status:'ready_for_confirmation',requirements:[]},reviews:[],documents:[{status:'needs_review'}],commitments:[],reminders:[],reference:{}})}`);
  assert.match(run(c,'renderOverview()'),/data-flow="ready" disabled/);
  const html=run(c,`evidenceDetails([{document_id:'d',page:1,excerpt:'<script>unsafe</script>'}])`);
  assert.match(html,/&lt;script&gt;unsafe/);
  assert.doesNotMatch(html,/<script>/);
});

test('original PDF preview is fetched with the current manager token',async()=>{
  const c=sandbox();let request;
  c.fetch=async(url,options)=>{
    request={url,options};
    return {ok:true,status:200,blob:async()=>({type:'application/pdf'})};
  };
  c.URL={
    createObjectURL:blob=>blob.type==='application/pdf'?'blob:manager-preview':'',
    revokeObjectURL(){}
  };
  run(c,"state.token='manager-token'");

  const url=await run(c,"loadDocumentPdf('/cases/case-a/documents/doc-a')");

  assert.equal(url,'blob:manager-preview');
  assert.equal(request.url,'/api/v1/cases/case-a/documents/doc-a/content');
  assert.equal(request.options.headers.Authorization,'Bearer manager-token');
  assert.match(run(c,"documentPreviewMarkup('blob:manager-preview')"),/<iframe[^>]+blob:manager-preview/);
});

test('prompt dialogs make Enter confirm while Cancel remains an explicit button',()=>{
  const c=sandbox();
  const html=run(c,"promptFormMarkup([{name:'reason',label:'Reason'}])");
  assert.match(html,/<button type="button"[^>]*data-dialog-cancel[^>]*>Cancel<\/button>/);
  assert.match(html,/<button type="submit"[^>]*>Continue<\/button>/);
});

test('prompt dialog keeps invalid values open and shows the field error',async()=>{
  const c=sandbox();
  const fieldError={textContent:'',hidden:true};
  const input={attributes:{},focused:false,setAttribute(name,value){this.attributes[name]=value},removeAttribute(name){delete this.attributes[name]},focus(){this.focused=true}};
  const form={
    values:{bank_accounts:'12345'},listeners:{},
    addEventListener(name,handler){this.listeners[name]=handler},
    querySelectorAll(selector){return selector==='[data-field-error]'?[fieldError]:selector==='[aria-invalid="true"]'?[input]:[]},
    querySelector(selector){
      if(selector==='[data-field-error="bank_accounts"]')return fieldError;
      if(selector==='[name="bank_accounts"]')return input;
      if(selector==='[data-form-error]')return null;
      return null;
    }
  };
  const cancel={addEventListener(){}};
  const dialog={
    returnValue:'',closeCalls:0,listeners:{},
    setAttribute(){},set innerHTML(value){this.markup=value},
    querySelector(selector){return selector==='form'?form:selector==='[data-dialog-cancel]'?cancel:null},
    addEventListener(name,handler){this.listeners[name]=handler},
    close(value){this.closeCalls+=1;this.returnValue=value;this.listeners.close?.()},
    remove(){},showModal(){}
  };
  c.document={createElement:()=>dialog,body:{append(){}},querySelectorAll:()=>[]};
  c.FormData=class{constructor(element){return new Map(Object.entries(element.values))}};

  const result=run(c,"promptFields('Create case',[{name:'bank_accounts',label:'Bank account'}],{validate:values=>values.bank_accounts==='12345'?{bank_accounts:'Use exactly four digits.'}:{}})");
  form.listeners.submit({preventDefault(){}});

  assert.equal(dialog.closeCalls,0);
  assert.equal(fieldError.textContent,'Use exactly four digits.');
  assert.equal(fieldError.hidden,false);
  assert.equal(input.attributes['aria-invalid'],'true');
  assert.equal(input.focused,true);

  form.values.bank_accounts='1234';
  form.listeners.submit({preventDefault(){}});
  assert.equal(dialog.closeCalls,1);
  assert.deepEqual({...await result},{bank_accounts:'1234'});
});

test('new case validation attaches malformed bank account errors to that input',()=>{
  const c=sandbox();
  const errors=run(c,`caseFormErrors({
    period:'2026-09',entity:'entity_demo',bank_accounts:'12345',
    invoices:'',receipts:'',other:'',other_refs:''
  })`);
  assert.deepEqual({...errors},{bank_accounts:'Each bank account entry must be four digits or a label followed by four digits, for example operating:1234.'});
});

test('bank account is optional when another document requirement is configured',()=>{
  const c=sandbox();
  const errors=run(c,`caseFormErrors({
    period:'2026-09',entity:'entity_demo',bank_accounts:'',
    invoices:'INV-001',receipts:'',other:'',other_refs:''
  })`);
  assert.deepEqual({...errors},{});
  const html=run(c,"promptFormMarkup([{name:'bank_accounts',label:'Bank accounts',optional:true,help:'Required only when collecting bank statements.'}])");
  assert.match(html,/Required only when collecting bank statements\./);
  assert.match(html,/data-field-error="bank_accounts"/);
  assert.match(html,/role="alert"/);
});

test('only an explicit evidence decision action proceeds to document review',()=>{
  const c=sandbox();
  assert.equal(run(c,"shouldOpenDocumentDecision('decide')"),true);
  assert.equal(run(c,"shouldOpenDocumentDecision('close')"),false);
  assert.equal(run(c,"shouldOpenDocumentDecision('')"),false);
  const html=run(c,"evidenceDialogActionsMarkup()");
  assert.match(html,/value="close"[^>]*>Close<\/button>/);
  assert.match(html,/value="decide"[^>]*>Make decision<\/button>/);
});

test('document rejection can request a reviewed correction draft',()=>{
  const c=sandbox();
  const body=run(c,`documentReviewBody(7,{
    decision:'reject_document',target:'req-a',reason:'Wrong period',follow_up:'prepare'
  })`);
  assert.equal(body.expected_state_version,7);
  assert.equal(body.target_requirement_id,null);
  assert.equal(body.prepare_correction_email,true);
  const accept=run(c,`documentReviewBody(7,{
    decision:'accept_for_requirement',target:'req-a',reason:'Verified',follow_up:'prepare'
  })`);
  assert.equal(accept.prepare_correction_email,false);
  const html=run(c,"auditActionLabel({action:'reject_document',details:{document_filename:'july.pdf'}})");
  assert.match(html,/Document rejected/);
  assert.match(html,/july\.pdf/);
});

test('connect session uses the entered token and loads cases',async()=>{
  const c=sandbox();let loaded=0;
  run(c,"$('token').value='manager-token'; loadCases=async()=>{loadedByTest()};");
  c.loadedByTest=()=>{loaded+=1};
  await run(c,'connectSession()');
  assert.equal(run(c,'state.token'),'manager-token');
  assert.equal(loaded,1);
});

test('connect controls are a form so Enter submits the token',()=>{
  const html=fs.readFileSync('closeready/frontend/index.html','utf8');
  assert.match(html,/<form id="session"[^>]*>/);
  assert.match(html,/<button id="connect" type="submit">Connect<\/button>/);
});

test('undo confirmation is available only for ready cases',()=>{
  const c=sandbox();
  run(c,`state.data=${JSON.stringify({caseData:{case_id:'a',readiness_status:'ready',requirements:[]},reviews:[],documents:[],commitments:[],reminders:[],reference:{}})}`);
  assert.match(run(c,'renderOverview()'),/data-flow="reopen"/);
  run(c,"state.data.caseData.readiness_status='ready_for_confirmation'");
  assert.doesNotMatch(run(c,'renderOverview()'),/data-flow="reopen"/);
  assert.match(run(c,'renderOverview()'),/data-flow="ready" >Confirm ready/);
});

test('case form creates one bank requirement per unique account suffix',()=>{
  const c=sandbox();
  const requirements=run(c,`buildCaseRequirements({
    period:'2026-09',entity:'entity_demo',bank_accounts:'operating:1234, payroll:5678, operating:1234',
    invoices:'',receipts:'',other:'',other_refs:''
  })`);
  assert.equal(requirements.length,2);
  assert.deepEqual(
    Array.from(requirements, item=>item.scope.masked_account_identifier),
    ['****1234','****5678']
  );
  assert.deepEqual(
    Array.from(requirements, item=>item.scope.account_ref),
    ['operating','payroll']
  );
});

test('case form preserves separate account labels when last four digits collide',()=>{
  const c=sandbox();
  const requirements=run(c,`buildCaseRequirements({
    period:'2026-09',entity:'entity_demo',bank_accounts:'operating:1234, payroll:1234',
    invoices:'',receipts:'',other:'',other_refs:''
  })`);
  assert.equal(requirements.length,2);
  assert.deepEqual(
    Array.from(requirements, item=>item.scope.account_ref),
    ['operating','payroll']
  );
  assert.deepEqual(
    Array.from(requirements, item=>item.scope.masked_account_identifier),
    ['****1234','****1234']
  );
});

test('case form rejects malformed bank account suffixes',()=>{
  const c=sandbox();
  assert.throws(()=>run(c,`buildCaseRequirements({
    period:'2026-09',entity:'entity_demo',bank_accounts:'12345',
    invoices:'',receipts:'',other:'',other_refs:''
  })`),/four digits/);
});

test('checklist shows masked account identifier instead of internal account reference',()=>{
  const c=sandbox();
  const requirement={
    document_type:'bank_statement',accounting_period:'2026-09',status:'missing',
    reviewer_status:'not_required',evidence_refs:[],
    scope:{entity_id:'entity_demo',account_ref:'internal-account',masked_account_identifier:'****1234'}
  };
  const html=run(c,`renderChecklist(${JSON.stringify([requirement])})`);
  assert.match(html,/\*\*\*\*1234/);
  assert.doesNotMatch(html,/internal-account/);
});

test('audit timeline gives automatic requirement matching a business label',()=>{
  const c=sandbox();
  const label=run(c,"auditActionLabel({action:'bind_document_requirement',details:{}})");
  assert.equal(label,'Document matched to requirement');
});
