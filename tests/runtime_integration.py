#!/usr/bin/env python3
"""Actual Android video/control and Qt display acceptance; does not qualify iOS."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import time
from native_client import dispatch, preview, atomic_json, StudioError

parser = argparse.ArgumentParser()
parser.add_argument('--device', default='emulator-5554')
parser.add_argument('--binary', default='build-product/tomacco')
parser.add_argument('--project', required=True)
parser.add_argument('--bundle-id', default='dev.example.app')
parser.add_argument('--output', default='qa/runtime')
args = parser.parse_args()
out = Path(args.output).resolve(); out.mkdir(parents=True, exist_ok=True)
checks = []

def check(name, **evidence):
    checks.append({'check':name, 'passed':True, **evidence})
    print(name, flush=True)

def wait_job(job):
    deadline = time.monotonic()+60
    while time.monotonic()<deadline:
        result = dispatch('job.wait', {'job_id':job['id'], 'seconds':5})
        if result['state'] not in ['queued','running']:
            assert result['state']=='succeeded',result
            return result
    raise AssertionError('Device job timed out')

def read_exact(stream, size):
    data=b''
    while len(data)<size:
        chunk=stream.recv(size-len(data))
        assert chunk, 'Video socket closed'
        data+=chunk
    return data

session='runtime-'+str(os.getpid())
live = None; process = None
with (out/'desktop.log').open('w') as log:
    try:
        wait_job(dispatch('device.app', {'platform':'android','device':args.device,'bundle_id':args.bundle_id,'action':'launch'}))
        live=dispatch('runtime.start', {'platform':'android','device':args.device})
        if live.get('reused'):
            live=None
            raise SystemExit('This lifecycle/crash test needs a dedicated device with no existing native session; preserved the active session.')
        assert live['native_runtime'] and live['width']>0 and live['height']>0,live
        check('real-runtime-start',state=live)
        with socket.socket(socket.AF_UNIX) as stream:
            stream.settimeout(10); stream.connect(live['video_socket'])
            data=b''.join(read_exact(stream,struct.unpack('>I',read_exact(stream,4))[0]) for _ in range(2))
            assert b'\x00\x00\x00\x01' in data
            (out/'first-video.h264').write_bytes(data)
        check('actual-annex-b-stream',bytes=len(data))
        process=subprocess.Popen([str(Path(args.binary).resolve()),'--preview-session',session,'--project',str(Path(args.project).resolve())],
            env={**os.environ,'QT_QPA_PLATFORMTHEME':'generic','QT_QUICK_CONTROLS_STYLE':'Basic'},stdout=log,stderr=log)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            assert process.poll() is None,(out/'desktop.log').read_text()
            try:
                state=preview.call('state',session=session)
                if state.get('native',{}).get('frames',0)>0:break
            except StudioError:pass
            time.sleep(.2)
        else:raise AssertionError('Native frames did not reach Qt')
        assert state['nativeRuntime'] and state['mode']=='runtime',state
        assert (state['native']['width'],state['native']['height'])==(live['width'],live['height']),state
        preview.invoke('capture',session=session,path=str(out/'native-home.png'),window=True,scale=1)
        check('qt-native-display',state=state['native'])
        target=dispatch('testing.find',{'device':args.device,'selector':{'description':'Einstellungen','clickable':True}})['matches']
        assert len(target)==1,target
        rect=target[0]['rect']
        dispatch('runtime.input',{'session':live['session'],'action':'tap','x':rect['x']/live['width'],'y':rect['y']/live['height']})
        dispatch('testing.find',{'device':args.device,'selector':{'description':'Ein Ton mehr.'},'state':'absent'})
        dispatch('testing.find',{'device':args.device,'selector':{'description':'Einstellungen'}})
        capture=dispatch('runtime.capture',{'session':live['session'],'path':str(out/'native-settings.png')})
        hierarchy=dispatch('runtime.hierarchy',{'session':live['session']})
        assert 'Einstellungen' in hierarchy['xml']
        check('live-touch-and-accessibility',capture=capture)
        dispatch('runtime.input',{'session':live['session'],'action':'key','key':'back'})
        dispatch('testing.find',{'device':args.device,'selector':{'description':'Ein Ton mehr.'}})
        check('live-key-back')
        # Two subscribers can attach while the app is idle: cached configuration
        # plus a complete keyframe sequence lets both decode immediately.
        with socket.socket(socket.AF_UNIX) as stream:
            stream.settimeout(5);stream.connect(live['video_socket'])
            packet=read_exact(stream,struct.unpack('>I',read_exact(stream,4))[0]);assert packet
        check('concurrent-late-subscriber')
        dispatch('runtime.stop',{'session':live['session']})
        deadline=time.monotonic()+10
        while dispatch('runtime.state',{'session':live['session']})['status']=='connected':
            assert time.monotonic()<deadline;time.sleep(.1)
        assert not Path(live['video_socket']).exists(),live
        dispatch('runtime.stop',{'session':live['session']})
        check('idempotent-stop-and-socket-cleanup')
        recovered=dispatch('runtime.start',{'platform':'android','device':args.device})
        os.kill(recovered['pid'],signal.SIGKILL);time.sleep(.5)
        state=dispatch('runtime.state',{'session':recovered['session']})
        assert state['status']=='disconnected' and not Path(recovered['video_socket']).exists(),state
        settings=dispatch('settings.get');adb=str(Path(settings['android_sdk'])/'platform-tools/adb')
        forwards=subprocess.run([adb,'forward','--list'],capture_output=True,text=True,check=True).stdout
        assert f"tcp:{recovered['adb_port']} " not in forwards,forwards
        check('worker-crash-recovers-owned-forward',state=state)
    finally:
        if process:
            process.terminate()
            try:process.wait(10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        if live:
            try:dispatch('runtime.stop',{'session':live['session']})
            except StudioError:pass
        time.sleep(1)
errors=[line for line in (out/'desktop.log').read_text().splitlines() if any(word in line for word in ['TypeError:','ReferenceError:','Cannot assign','Binding loop','AddressSanitizer','corrupted','failed to load component'])]
assert not errors,errors
atomic_json(out/'results.json',{'passed':True,'platform':'android','checks':checks})
print(json.dumps({'passed':True,'checks':len(checks),'output':str(out)}))
