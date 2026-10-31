#!/usr/bin/env python3
"""Mac-free onboarding and verified upstream artifact acceptance. No iPhone claim.

Requires an active desktop, curl, unzip, and network for the pinned WDA download.
Uses an isolated control-plane home and never installs anything on a device.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from native_client import dispatch, preview, atomic_json, StudioError

parser=argparse.ArgumentParser()
parser.add_argument('--binary',default='build-product/tomacco')
parser.add_argument('--project',required=True)
parser.add_argument('--android-adb',help='Optional adb path for the Android-to-Apple target regression')
parser.add_argument('--output',default='qa/iphone-linux')
args=parser.parse_args()
out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
os.environ['TOMACCO_HOME']=str(out/'state')
if args.android_adb:
    dispatch('settings.configure',{'executables':{'adb':str(Path(args.android_adb).resolve())}})
checks=[]
def check(name,**details):
    checks.append(dict(check=name,passed=True,**details));print(name,flush=True)

doctor=dispatch('ios.doctor',{'path':str(Path(args.project).resolve())})
assert doctor['mac_required'] is False and doctor['local_modern_ios_emulator'] is False
assert any(c['name']=='apple_sdk' for c in doctor['checks'])
check('honest-local-prerequisites',report=doctor)
job=dispatch('ios.wda_prepare')
deadline=time.monotonic()+180
while time.monotonic()<deadline:
    done=dispatch('job.wait',{'job_id':job['id'],'seconds':5})
    if done['state'] not in ('queued','running'):break
else:raise AssertionError('Runner download timed out')
assert done['state']=='succeeded',dispatch('job.logs',{'job_id':job['id']})
report=json.loads(Path(done['result']['artifacts'][0]['path']).read_text())
assert report['passed'] and not report['result']['installed']
assert (Path(report['result']['app'])/'Info.plist').is_file()
check('official-runner-downloaded-and-verified',report=report)

session='iphone-linux-'+str(os.getpid())
env={**os.environ,'XDG_CONFIG_HOME':str(out/'xdg/config'),'XDG_CACHE_HOME':str(out/'xdg/cache'),
     'QT_QPA_PLATFORMTHEME':'generic','QT_QUICK_CONTROLS_STYLE':'Basic'}
process=None
with (out/'desktop.log').open('w') as log:
    try:
        process=subprocess.Popen([str(Path(args.binary).resolve()),'--preview-session',session,'--project',str(Path(args.project).resolve())],env=env,stdout=log,stderr=log)
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            assert process.poll() is None,(out/'desktop.log').read_text()
            try:
                state=preview.call('state',session=session)
                break
            except StudioError:time.sleep(.2)
        else:raise AssertionError('Desktop did not become ready')
        if args.android_adb:
            android=dispatch('device.list',{'platform':'android'})['devices']
            ids=[d['id'] for d in android];assert ids,'A real Android device is required for this regression'
            deadline=time.monotonic()+10
            while state['native']['device'] not in ids and time.monotonic()<deadline:
                time.sleep(.2);state=preview.call('state',session=session)
            assert state['native']['device'] in ids,state
        state=preview.call('configure',{'native':{'platform':'ios','apple_target':'device','setup':True}},session=session)
        deadline=time.monotonic()+45
        while not state['native']['setupChecks'] and time.monotonic()<deadline:
            time.sleep(.2);state=preview.call('state',session=session)
        assert state['native']['platform']=='ios' and state['native']['appleTarget']=='device',state
        assert state['native']['setupVisible'] and state['native']['setupChecks'],state
        assert not state['nativeRuntime'] and state['native']['frames']==0,state
        if args.android_adb:
            assert state['native']['device'] not in ids,state
            check('platform-switch-clears-android-target',selected=state['native']['device'])
        preview.invoke('capture',session=session,path=str(out/'iphone-setup.png'),window=True,scale=1)
        check('mac-free-setup-displayed',state=state['native'])
        preview.call('configure',{'native':{'setup':False}},session=session)
        preview.invoke('capture',session=session,path=str(out/'iphone-workspace.png'),window=True,scale=1)
        check('physical-iphone-default-workspace')
    finally:
        if process:
            process.terminate()
            try:process.wait(10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
atomic_json(out/'results.json',{'passed':True,'physical_iphone_qualified':False,'checks':checks})
print(json.dumps({'passed':True,'checks':len(checks),'physical_iphone_qualified':False}))
