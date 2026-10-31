#!/usr/bin/env python3
"""Real Unicode UI input and measured CPU regression through the public API."""
import argparse,json,time
from pathlib import Path
from native_client import dispatch,jobs,atomic_json
p=argparse.ArgumentParser();p.add_argument('--device',required=True);p.add_argument('--output',required=True);p.add_argument('--trace-processor',required=True);a=p.parse_args()
out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
project=out/'UnicodeFixture';bundle='io.github.marselnenaj.tomacco.unicodefixture';checks=[]
def finish(job):
 while job['state'] not in jobs.TERMINAL:job=jobs.wait(job['id'],5)
 assert job['state']=='succeeded',{'job':job,'logs':jobs.logs(job['id'],limit=20000)}
 return job
if not project.exists():dispatch('project.create',{'path':str(project),'name':'UnicodeFixture','organization':'io.github.marselnenaj.tomacco','kind':'android'})
file='app/src/main/java/io/github/marselnenaj/tomacco/unicodefixture/MainActivity.java'
old=dispatch('files.read',{'path':str(project),'file':file})
dispatch('files.write',{'path':str(project),'file':file,'if_match':old['sha256'],'text':(Path(__file__).parent/'fixtures/NativeInputActivity.java').read_text()})
finish(dispatch('project.execute',{'path':str(project),'platform':'android','action':'build'}))
finish(dispatch('device.install',{'platform':'android','device':a.device,'paths':[str(project/'app/build/outputs/apk/debug/app-debug.apk')]}))
finish(dispatch('device.app',{'platform':'android','device':a.device,'bundle_id':bundle,'action':'launch'}))
dispatch('testing.instrumentation',{'device':a.device})
selector={'resource_id':'android:id/edit'}
for text in ['Grüße aus Österreich 🎵 日本語 %s', 'Neu: "äöü" & $HOME', '']:
 result=dispatch('testing.action',{'device':a.device,'action':'fill','selector':selector,'text':text})
 match=dispatch('testing.find',{'device':a.device,'selector':{**selector,**({'text':text} if text else {})}})
 assert len(match['matches'])==1,match
 checks.append({'test':'lossless text replacement','text':text,'result':result})
dispatch('device.capture',{'platform':'android','device':a.device,'path':str(out/'input.png')})
dispatch('settings.configure',{'executables':{'trace_processor':str(Path(a.trace_processor).resolve())}})
def capture(name,load):
 trace=out/(name+'.pftrace')
 job=dispatch('profile.capture',{'device':a.device,'bundle_id':bundle,'path':str(trace),'seconds':6})
 deadline=time.monotonic()+10
 while '--txt' not in jobs.logs(job['id'])['text']:
  assert time.monotonic()<deadline
  time.sleep(.05)
 time.sleep(.5)
 if load:dispatch('testing.action',{'device':a.device,'action':'click','selector':{'text':'CPU WORK'}})
 else:dispatch('testing.action',{'device':a.device,'action':'fill','selector':selector,'text':'baseline'})
 finish(job)
 report=dispatch('profile.analyze',{'path':str(trace),'bundle_id':bundle,'report':str(out/(name+'.json'))})
 assert report['metrics']['cpu_ms'] is not None,report
 return report
baseline=capture('baseline',False);current=capture('regressed',True)
comparison=dispatch('profile.compare',{'baseline':str(out/'baseline.json'),'current':str(out/'regressed.json'),'budgets':{'cpu_ms':{'increase_percent':50,'maximum':baseline['metrics']['cpu_ms']+500}}})
assert not comparison['passed'],comparison
assert current['metrics']['cpu_ms']>baseline['metrics']['cpu_ms']+500,(baseline,current)
checks.append({'test':'measured CPU regression','baseline':baseline,'current':current,'comparison':comparison})
atomic_json(out/'results.json',{'passed':True,'checks':checks});print(json.dumps({'passed':True,'checks':len(checks),'report':str(out/'results.json')}))
