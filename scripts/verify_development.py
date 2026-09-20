"""Validate development fixtures in a temporary DB. Never opens held-out inputs."""
import argparse, hashlib, json, sys, tempfile, time
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from closeready.api import create_app
from closeready.config import AccessConfig
from closeready.document_processor import DocumentProcessor
from closeready.mail import SandboxMailSink

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Use configured real model (incurs API cost).')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    rows=[json.loads(x) for x in (ROOT/'evaluation/development/scenarios.jsonl').read_text().splitlines()]
    assert all(x['split']=='development' for x in rows)
    cases=[json.loads((ROOT/'evaluation'/r['case_file']).read_text()) for r in rows]
    token='synthetic-development-verification-only'
    config=AccessConfig.model_validate({
      'principals':[{'user_id':'evaluation_manager','token_sha256':hashlib.sha256(token.encode()).hexdigest(),'client_ids':[c['client_id'] for c in cases],'can_manage':True}],
      'policies':[{'policy_id':c['policy_id'],'version':1,'client_ids':[c['client_id']],'approved_by':'evaluation_manager','approved_at':'2026-08-01T00:00:00Z'} for c in cases],
      'contacts':[{'contact_id':'contact_'+c['client_id'],'client_id':c['client_id'],'approved_email':'approved-client@example.invalid','active':True,'approved_by':'evaluation_manager'} for c in cases],
      'communication_policies':[{'policy_id':c['policy_id'],'version':1,'approved_by':'evaluation_manager','approved_at':'2026-08-01T00:00:00Z','initial_request_enabled':True,'min_reminder_interval_hours':24,'max_reminders_per_requirement':3,'commitment_grace_hours':24,'sending_window_local':'00:00-23:59','timezone':'Asia/Singapore','escalation_owner_user_id':'evaluation_manager'} for c in cases]})
    provider=None
    if args.live:
        from closeready.provider_factory import provider_from_environment
        provider=provider_from_environment()
    results=[]
    with tempfile.TemporaryDirectory(prefix='closeready-dev-') as temp:
      app=create_app('sqlite:///'+temp+'/cases.db',config,provider=provider,mail=SandboxMailSink())
      with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+token
        def post(path,**kwargs):
            import uuid
            r=client.post('/api/v1'+path,headers={'Idempotency-Key':uuid.uuid4().hex},**kwargs)
            if r.status_code>=400:raise RuntimeError(str(r.status_code)+': '+r.json().get('error',{}).get('code','HTTP_ERROR'))
            return r.json()
        for row,body in zip(rows,cases):
          start=time.monotonic();record={'scenario_id':row['scenario_id'],'mode':'live_llm' if args.live else 'rules','status':'FAIL'}
          try:
            case=post('/cases',json=body);base='/cases/'+case['case_id'];rid=case['requirements'][0]['requirement_id']
            if row['documents']:
              findings=[];documents=[]
              for _ in range(2 if row['category']=='duplicate' else 1):
                current=client.get('/api/v1'+base).json()
                pdf=ROOT/'evaluation'/row['documents'][0]
                job=post(base+'/documents',data={'expected_state_version':str(current['state_version']),'requirement_id':rid},files={'file':(pdf.name,pdf.read_bytes(),'application/pdf')})
                kw={}
                if provider:
                  from closeready.document_ai_review import AIDocumentReviewer
                  kw['assessor']=AIDocumentReviewer(provider)
                store=app.state.document_store
                DocumentProcessor(store,**kw).execute_claimed(job['job_id'],store.claim(job['job_id']))
                finding=client.get('/api/v1'+base+'/documents/'+job['document_id']+'/finding').json()
                findings.append(finding)
                documents.append(client.get('/api/v1'+base+'/documents/'+job['document_id']).json())
              current=client.get('/api/v1'+base).json()
              actual=[] if current['requirements'][0]['status'] in ('accepted','waived') else ['bank_statement']
              passed=actual==row['expected']['outstanding'] and current['readiness_status']!='ready'
              if row['category']=='correct':passed &= current['readiness_status']=='ready_for_confirmation'
              if row['category']=='duplicate':passed &= bool(documents[-1]['duplicate_of_document_id'])
              if row['category'] in ('wrong_month','partial','wrong_document'):passed &= bool(findings[-1].get('issues') or findings[-1].get('uncertainty_reasons'))
              errors=[f.get('analysis_error') for f in findings if f.get('analysis_error')]
              record.update(status=('BLOCKED_AI' if errors else 'PASS' if passed else 'FAIL'),actual_outstanding=actual,readiness=current['readiness_status'],findings=[{k:f.get(k) for k in ('result','issues','uncertainty_reasons','analysis_source','analysis_error')} for f in findings])
            elif not provider:
              record.update(status='NOT_RUN',reason='Relative-date reply interpretation requires live model; no scripted answer substituted.')
            else:
              reply=json.loads((ROOT/'evaluation'/row['reply_file']).read_text())
              with patch('closeready.communication_store.utcnow',return_value=datetime.fromisoformat(reply['received_at'])):
                ingested=post(base+'/replies',json={'expected_state_version':1,'sender_email':reply['sender'],'body':reply['body'],'received_at':reply['received_at']})
                current=client.get('/api/v1'+base).json()
                assessed=post(base+'/replies/'+ingested['reply']['reply_id']+'/assess',json={'expected_state_version':current['state_version']})
              passed=assessed.get('commitment') is None and assessed.get('review_task_id') is not None
              record.update(status='PASS' if passed else 'FAIL',assessment=assessed,scope='Trusted sandbox reply analysis only; outbound round trip and browser not tested.')
          except Exception as exc:
            record.update(status='ERROR',error_type=type(exc).__name__)
          record['elapsed_seconds']=round(time.monotonic()-start,3);results.append(record)
          print(row['scenario_id'],record['status'],flush=True)
    output={'scope':'Development component integration checks; not full E2E or business metrics','live':args.live,'model':getattr(provider,'model',None),'results':results}
    Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    Path(args.output).write_text(json.dumps(output,indent=2)+'\n')
if __name__=='__main__':main()
