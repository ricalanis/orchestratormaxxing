"""Offline boundary regression using only owned synthetic adapter processes."""
import json, os, signal, subprocess, sys, tempfile, time
from pathlib import Path
root=Path(sys.argv[1]).resolve(); tool=root/'bin/worker-path-bench'
failures=[]
with tempfile.TemporaryDirectory() as tmp:
 d=Path(tmp)
 script=d/'adapter.py'
 script.write_text("import os,sys,time,json,signal\nmode=sys.argv[1]\nopen(sys.argv[2],'w').write(str(os.getpid()))\nif mode=='stdin': time.sleep(20)\nelif mode=='slowstdin': os.read(0,8192); time.sleep(20)\nelif mode=='closed': print(json.dumps({'output':'PONG'}),flush=True); os.close(0); os.close(1); os.close(2); time.sleep(20)\nelif mode=='partial': os.write(1,b'x'); time.sleep(20)\nelif mode=='child':\n p=os.fork()\n if p==0: signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20); sys.exit(0)\n open(sys.argv[3],'w').write(str(p)); print(json.dumps({'output':'PONG'}),flush=True); sys.exit(0)\nelse: json.load(sys.stdin); print(json.dumps({'output':'PONG','usage':{'prompt_tokens':'bad'}}))\n")
 for mode in ['stdin','slowstdin','partial','child','closed','metric']:
  pidfile=d/(mode+'.pid');childfile=d/(mode+'.child');cases=d/'cases';out=d/'out';summary=d/'summary'
  case={'id':mode,'prompt':'X'*2097152 if mode in ('stdin','slowstdin') else 'x','timeout_seconds':0.2,'contract':{'type':'exact','expected':'PONG'}}
  cases.write_text(json.dumps(case)+'\n')
  import shlex
  cmd=[sys.executable,str(tool),'--cases',str(cases),'--adapter','test='+shlex.join([sys.executable,str(script),mode,str(pidfile),str(childfile)]),'--output',str(out),'--summary',str(summary)]
  proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
  try:
   try:stdout,stderr=proc.communicate(timeout=3)
   except subprocess.TimeoutExpired:
    failures.append(mode+': deadline exceeded');os.killpg(proc.pid,signal.SIGKILL);proc.communicate();continue
   if proc.returncode!=0:failures.append(mode+': runner exit '+str(proc.returncode));continue
   row=json.loads(out.read_text())
   if mode != 'metric' and row['timed_out'] is not True:failures.append(mode+': timeout flag missing')
   if not row['infrastructure_failure']:failures.append(mode+': malformed/stalled adapter not infra')
   if mode=='child' and childfile.exists():
    pid=int(childfile.read_text());time.sleep(0.1)
    check=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
    if check.stdout.strip() and not check.stdout.strip().startswith('Z'):failures.append('child: live descendant after result')
  finally:
   if proc.poll() is None:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
   if pidfile.exists():
    try:os.killpg(int(pidfile.read_text()),signal.SIGKILL)
    except ProcessLookupError:pass
print('FAIL: '+ '; '.join(failures) if failures else 'PASS: blocked stdin, partial stdout, resistant descendant, invalid usage metrics')
if failures:
 raise SystemExit(1)
# Terminating the runner must also terminate its adapter group.
with tempfile.TemporaryDirectory() as temporary:
 d=Path(temporary); script=d/'cancel.py'; pidfile=d/'pid'; cases=d/'cases'
 script.write_text("import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(20)")
 cases.write_text(json.dumps({'id':'cancel','prompt':'x','contract':{'type':'exact','expected':'PONG'}})+'\n')
 import shlex
 runner=subprocess.Popen([sys.executable,str(tool),'--cases',str(cases),'--adapter','cancel='+shlex.join([sys.executable,str(script),str(pidfile)]),'--output',str(d/'out'),'--summary',str(d/'summary')],start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 try:
  deadline=time.monotonic()+3
  while not pidfile.exists() and time.monotonic()<deadline:time.sleep(0.01)
  assert pidfile.exists(), 'adapter never started'
  runner.terminate();stdout,stderr=runner.communicate(timeout=3)
  assert runner.returncode==130,(runner.returncode,stderr)
  pid=int(pidfile.read_text());check=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
  assert not check.stdout.strip() or check.stdout.strip().startswith('Z'),'adapter survived cancellation'
 finally:
  if runner.poll() is None:os.killpg(runner.pid,signal.SIGKILL);runner.communicate()
  if pidfile.exists():
   try:os.killpg(int(pidfile.read_text()),signal.SIGKILL)
   except ProcessLookupError:pass
print('PASS: cancellation cleans the adapter group')

# Cancellation arriving inside the cleanup grace period must not skip SIGKILL.
with tempfile.TemporaryDirectory() as temporary:
 d=Path(temporary); script=d/'cleanup.py'; pidfile=d/'pid'; marker=d/'term'; cases=d/'cases'
 script.write_text("import os,sys,signal,time,json\np=os.fork()\nif p==0:\n signal.signal(signal.SIGTERM,lambda s,f:open(sys.argv[2],'w').write('term'))\n open(sys.argv[1],'w').write(str(os.getpid()))\n os.close(0);os.close(1);os.close(2)\n time.sleep(20);os._exit(0)\nwhile not os.path.exists(sys.argv[1]):time.sleep(0.001)\nprint(json.dumps({'output':'PONG'}),flush=True)\n")
 cases.write_text(json.dumps({'id':'cleanup','prompt':'x','timeout_seconds':2,'contract':{'type':'exact','expected':'PONG'}})+'\n')
 runner=subprocess.Popen([sys.executable,str(tool),'--cases',str(cases),'--adapter','cleanup='+shlex.join([sys.executable,str(script),str(pidfile),str(marker)]),'--output',str(d/'out'),'--summary',str(d/'summary')],start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 child=None
 try:
  deadline=time.monotonic()+3
  while not marker.exists() and time.monotonic()<deadline:time.sleep(0.001)
  assert marker.exists(),'cleanup never began'
  child=int(pidfile.read_text());runner.terminate();stdout,stderr=runner.communicate(timeout=3)
  assert runner.returncode==130,(runner.returncode,stderr)
  check=subprocess.run(['ps','-o','stat=','-p',str(child)],capture_output=True,text=True)
  assert not check.stdout.strip() or check.stdout.strip().startswith('Z'),'child survived cancellation during cleanup'
 finally:
  if runner.poll() is None:os.killpg(runner.pid,signal.SIGKILL);runner.communicate()
  if child:
   try:os.kill(child,signal.SIGKILL)
   except ProcessLookupError:pass
print('PASS: cancellation during cleanup preserves descendant termination')
