#!/usr/bin/env python3
"""Inspect the actual Qt workspace, including agent-driven source navigation."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from native_client import dispatch, preview, atomic_json, StudioError

parser = argparse.ArgumentParser()
parser.add_argument('--binary', default='build-product/tomacco')
parser.add_argument('--project', required=True)
parser.add_argument('--file', default='pubspec.yaml')
parser.add_argument('--output', default='qa/workspace')
args = parser.parse_args()
output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('TOMACCO_HOME', str(output / 'state'))
session = 'workspace-' + str(os.getpid())
checks = []
with (output / 'client.log').open('w') as log:
    process = subprocess.Popen([str(Path(args.binary).resolve()), '--preview-session', session],
        env={**os.environ, 'QT_QPA_PLATFORMTHEME':'generic', 'QT_QUICK_CONTROLS_STYLE':'Basic',
             'XDG_CONFIG_HOME':str(output/'xdg/config'), 'XDG_DATA_HOME':str(output/'xdg/data'), 'XDG_CACHE_HOME':str(output/'xdg/cache')},
        stdout=log, stderr=log)
    try:
        deadline = time.monotonic()+30
        while True:
            try: preview.call('state', session=session); break
            except StudioError:
                if process.poll() is not None or time.monotonic()>deadline: raise AssertionError((output/'client.log').read_text())
                time.sleep(.1)
        preview.ready(session)
        time.sleep(1)
        dispatch('desktop.open', {'session':session, 'tab':'files', 'path':str(Path(args.project).resolve()), 'file':args.file, 'line':3})
        deadline = time.monotonic()+15
        while True:
            state = preview.call('state', session=session)['workspace']
            if state['file']==args.file and state['entries']>0 and state['visible']: break
            assert time.monotonic()<deadline, state
            time.sleep(.1)
        assert not state['notice'], state
        assert state['visible'] and state['tab']=='files', state
        time.sleep(.3)
        capture = preview.invoke('capture', session=session, path=str(output/'files.png'), window=True, scale=1)
        checks.append({'check':'source-file-reveal', 'passed':True, 'state':state, 'capture':capture})
        for tab in ['project','devices','jobs','tools','debug','testing','profiling','release']:
            dispatch('desktop.open', {'session':session,'tab':tab})
            time.sleep(.3)
            state = preview.call('state', session=session)['workspace']
            assert state['tab']==tab and state['file']==args.file, state
            capture=preview.invoke('capture', session=session, path=str(output/(tab+'.png')), window=True, scale=1)
            checks.append({'check':tab,'passed':True,'capture':capture})
        # Opening a second file and switching back must preserve the document tabs.
        dispatch('desktop.open', {'session':session,'tab':'files','file':'README.md'})
        deadline=time.monotonic()+10
        while preview.call('state',session=session)['workspace']['file']!='README.md':
            assert time.monotonic()<deadline
            time.sleep(.1)
        dispatch('desktop.open',{'session':session,'tab':'files','file':args.file})
        state=preview.call('state',session=session)['workspace']
        assert set(state['files'])=={args.file,'README.md'},state
        checks.append({'check':'document-tabs-preserved','passed':True})
    finally:
        process.terminate()
        try: process.wait(10)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
errors=[line for line in (output/'client.log').read_text().splitlines() if any(word in line for word in ['TypeError:','ReferenceError:','Cannot assign','Binding loop','is not a function','failed to load component'])]
assert not errors,errors
atomic_json(output/'results.json',{'passed':True,'checks':checks})
print(json.dumps({'passed':True,'checks':len(checks),'output':str(output)}))
