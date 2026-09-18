const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('closeready/frontend/app.js','utf8');
function sandbox(){
  const elements=new Map();
  const context=vm.createContext({
    sessionStorage:{getItem:()=>'',setItem:()=>{},removeItem:()=>{}},
    document:{getElementById:id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',value:'',classList:{add(){},remove(){}}});return elements.get(id)},addEventListener(){},querySelectorAll:()=>[],querySelector:()=>null},
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

test('undo confirmation is available only for ready cases',()=>{
  const c=sandbox();
  run(c,`state.data=${JSON.stringify({caseData:{case_id:'a',readiness_status:'ready',requirements:[]},reviews:[],documents:[],commitments:[],reminders:[],reference:{}})}`);
  assert.match(run(c,'renderOverview()'),/data-flow="reopen"/);
  run(c,"state.data.caseData.readiness_status='ready_for_confirmation'");
  assert.doesNotMatch(run(c,'renderOverview()'),/data-flow="reopen"/);
  assert.match(run(c,'renderOverview()'),/data-flow="ready" >Confirm ready/);
});
