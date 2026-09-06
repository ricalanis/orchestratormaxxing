"""Offline boundary regression using only owned synthetic adapter processes."""
import json, os, shlex, subprocess, sys, tempfile
from pathlib import Path
root=Path(sys.argv[1]).resolve()
tool=root/'bin/worker-path-bench'
assert tool.is_file(), 'benchmark CLI missing'
with tempfile.TemporaryDirectory() as tmp:
 d=Path(tmp);adapter=d/'adapter.py';cases=d/'cases.jsonl';out=d/'out.jsonl';summary=d/'summary.json'
 adapter.write_text("import json,sys\nc=json.load(sys.stdin)\nassert not any(k in c for k in ['answer','expected','contract','verifier_canary'])\nmode=c.get('fixture_mode','pass')\nif mode=='flood': print('X'*2097152)\nelif mode=='metric': print(json.dumps({'output':'PONG','events':[{'cache_read_tokens':'not-a-number'}]}))\nelif mode=='wrong': print(json.dumps({'output':'WRONG'}))\nelse: print(json.dumps({'output':'PONG','usage':{'prompt_tokens':1},'events':[]}))\n")
 rows=[{'id':m,'prompt':'answer','fixture_mode':m,'answer':'DO_NOT_SEND','expected':'DO_NOT_SEND','verifier_canary':'DO_NOT_SEND','timeout_seconds':2,'contract':{'type':'exact','expected':'PONG'}} for m in ['pass','wrong','metric','flood']]
 cases.write_text(''.join(json.dumps(r)+'\n' for r in rows))
 command=[sys.executable,str(tool),'--cases',str(cases),'--adapter','local='+shlex.join([sys.executable,str(adapter)]),'--output',str(out),'--summary',str(summary)]
 p=subprocess.run(command,capture_output=True,text=True,timeout=15)
 assert p.returncode==0,(p.returncode,p.stderr[-1000:])
 result={r['case_id']:r for r in map(json.loads,out.read_text().splitlines())}
 assert result['pass']['contract_pass'] and not result['pass']['infrastructure_failure']
 assert not result['wrong']['contract_pass'] and not result['wrong']['infrastructure_failure']
 assert result['metric']['infrastructure_failure'],result['metric']
 assert result['flood']['infrastructure_failure'] and result['flood']['output_limit_exceeded'] is True and out.stat().st_size<1200000
 stats=json.loads(summary.read_text())['adapters']['local']
 assert stats['passed']==1 and stats['contract_failures']==1 and stats['infrastructure_failures']==2,stats
 for timeout in [0,-1,'nan','infinity','not-a-number']:
  cases.write_text(json.dumps({'id':'bad','prompt':'x','timeout_seconds':timeout,'contract':{'type':'exact','expected':'PONG'}})+'\n')
  p=subprocess.run(command,capture_output=True,text=True,timeout=5)
  assert p.returncode==2 and 'worker-path-bench:' in p.stderr and 'Traceback' not in p.stderr,(timeout,p.returncode,p.stderr[-200:])
print('PASS: withheld verifier data; pass/fail/infra classification; malformed metrics; bounded flood; timeout validation')

# Telemetry uses adapter observations; zero/string event counts remain valid,
# and malformed metrics preserve useful stderr diagnostics.
import runpy
module = runpy.run_path(str(tool))
with tempfile.TemporaryDirectory() as temporary:
 d = Path(temporary); a = d / "telemetry.py"
 a.write_text("import json,sys;json.load(sys.stdin);sys.stderr.write('adapter-note');print(json.dumps({'output':'PONG','usage':{'prompt_tokens':0},'events':[{'cache_read_tokens':'7'},{'cache_read_tokens':0}]}))")
 row = module['run_case']('telemetry',[sys.executable,str(a)],{'id':'t','contract':{'type':'exact','expected':'PONG'}})
 assert row['contract_pass'] and row['cache_read_tokens']==7 and row['input_tokens_actual']==0, row
 assert row['stderr']=='adapter-note', row
 a.write_text("import json,sys;json.load(sys.stdin);sys.stderr.write('adapter-note');print(json.dumps({'output':'PONG','events':[{'cache_read_tokens':-1}]}))")
 row = module['run_case']('bad',[sys.executable,str(a)],{'id':'t','contract':{'type':'exact','expected':'PONG'}})
 assert row['infrastructure_failure'] and 'adapter-note' in row['stderr'] and 'event-metrics-error' in row['stderr'], row
print('PASS: observed zero/string metrics, default timeout, retained diagnostic context')
