#!/usr/bin/env python3
"""Real Android semantic UI, JDWP and Perfetto qualification through tomaccoctl."""
import argparse,json,time
from pathlib import Path
from native_client import dispatch,jobs,atomic_json
p=argparse.ArgumentParser()
p.add_argument('--device',required=True);p.add_argument('--project',required=True);p.add_argument('--output',required=True);p.add_argument('--trace-processor',required=True)
a=p.parse_args();out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
project=dispatch('project.inspect',{'path':a.project});bundle=project['configuration']['applicationId']
assert bundle.startswith('io.github.marselnenaj.tomacco.')
checks=[]
def finish(j):
 while j['state'] not in jobs.TERMINAL:j=jobs.wait(j['id'],5)
 return j
def success(j):
 j=finish(j);assert j['state']=='succeeded',{'job':j,'logs':jobs.logs(j['id'])};return j
base={'device':a.device,'platform':'android','bundle_id':bundle}
success(dispatch('device.app',{**base,'action':'clear-data'}))
scenario={'name':'Native counter semantics','steps':[{'action':'launch','bundle_id':bundle},{'action':'assert','selector':{'text':'0'}},{'action':'click','selector':{'text':'ADD ONE'}},{'action':'assert','selector':{'text':'1'}},{'action':'screenshot'},{'action':'stop','bundle_id':bundle},{'action':'launch','bundle_id':bundle},{'action':'assert','selector':{'text':'1'}}]}
atomic_json(out/'counter.json',scenario)
j=success(dispatch('testing.run',{'path':str(out/'counter.json'),'devices':[a.device],'directory':str(out/f'pass-{time.time_ns()}')}));checks.append({'test':'semantic scenario','job':j})
scenario['steps']=[{'action':'assert','selector':{'text':'deliberate missing element'},'timeout':0}]
atomic_json(out/'failure.json',scenario)
j=finish(dispatch('testing.run',{'path':str(out/'failure.json'),'devices':[a.device],'directory':str(out/f'fail-{time.time_ns()}')}))
assert j['state']=='failed',j
artifacts=j['result']['artifacts'];assert any(v['path'].endswith('junit.xml') for v in artifacts),j
assert any(v['path'].endswith('failure.png') for v in artifacts),j
checks.append({'test':'failed scenario preserves reports and screenshot','job':j})
start=dispatch('debug.start',{'backend':'jdwp','device':a.device,'bundle_id':bundle,'launch':True});session=start['session']
try:
 source=next(Path(a.project).rglob('MainActivity.java'));line=next(i+1 for i,l in enumerate(source.read_text().splitlines()) if 'count = getPreferences' in l)
 dispatch('debug.breakpoints',{'session':session,'source':bundle+'.MainActivity','lines':[line]})
 dispatch('debug.control',{'session':session,'action':'continue'})
 deadline=time.monotonic()+20
 while time.monotonic()<deadline:
  state=dispatch('debug.state',{'session':session})
  if state['stopped'] and state['event_cursor']>start['event_cursor']:break
  time.sleep(.1)
 assert state['stopped'],state
 stack=dispatch('debug.stack',{'session':session});assert stack['frames'][0]['line']==line,stack
 variables=dispatch('debug.variables',{'session':session,'frame_id':str(stack['frames'][0]['id'])});assert any(v['name']=='this' for v in variables['variables']),variables
 fields=dispatch('debug.evaluate',{'session':session,'frame_id':str(stack['frames'][0]['id']),'expression':'this.count'});assert fields['value']==0,fields
 checks.append({'test':'Android source breakpoint and object fields','state':state,'stack':stack,'variables':variables,'fields':fields})
finally:dispatch('debug.control',{'session':session,'action':'disconnect'})
dispatch('settings.configure',{'executables':{'trace_processor':str(Path(a.trace_processor).resolve())}})
trace=out/f'counter-{time.time_ns()}.pftrace'
j=dispatch('profile.capture',{'device':a.device,'bundle_id':bundle,'path':str(trace),'seconds':8})
for _ in range(2):dispatch('testing.action',{'device':a.device,'action':'click','selector':{'text':'ADD ONE'}})
j=success(j);assert trace.stat().st_size>10000
report=dispatch('profile.analyze',{'path':str(trace),'bundle_id':bundle,'report':str(out/'profile.json')})
assert report['metrics']['cpu_ms']>0,report
comparison=dispatch('profile.compare',{'baseline':str(out/'profile.json'),'current':str(out/'profile.json'),'budgets':{'cpu_ms':{'increase_percent':0}}});assert comparison['passed'],comparison
regression=dict(report);regression['metrics']=dict(report['metrics']);regression['metrics']['cpu_ms']*=2
atomic_json(out/'regression.json',regression)
comparison=dispatch('profile.compare',{'baseline':str(out/'profile.json'),'current':str(out/'regression.json'),'budgets':{'cpu_ms':{'increase_percent':10}}});assert not comparison['passed']
checks.append({'test':'Perfetto capture, CPU attribution and regression gate','report':report,'regression':comparison})
atomic_json(out/'results.json',{'passed':True,'checks':checks});print(json.dumps({'passed':True,'checks':len(checks),'report':str(out/'results.json')}))
