#!/usr/bin/env python3
"""Real JDWP + LLDB acceptance. Uses installed JVM/Swift tools, never fake adapters."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from native_client import dispatch, atomic_json

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
parser.add_argument('--java', required=True)
parser.add_argument('--javac', required=True)
parser.add_argument('--swiftc')
parser.add_argument('--lldb-dap')
parser.add_argument('--library-path', default='')
parser.add_argument('--python-home', default='')
args = parser.parse_args()
root = Path(args.output).resolve()
root.mkdir(parents=True, exist_ok=True)
checks = []

def wait_stopped(session, after=0):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        state = dispatch('debug.state', {'session': session})
        if state['terminated']:
            raise AssertionError(state)
        if state['stopped'] and state.get('event_cursor', 0) > after:
            return state
        time.sleep(.1)
    raise AssertionError(dispatch('debug.events', {'session': session}))

java = root / 'DebugFixture.java'
java.write_text('''public class DebugFixture {
    public static void main(String[] args) throws Exception {
        int count = 40;
        String label = "Tomacco";
        count += 2;
        System.out.println(label + ": " + count);
        Thread.sleep(1000);
    }
}
''')
subprocess.run([args.javac, '-g', str(java)], check=True)
with socket.socket() as available:
    available.bind(('127.0.0.1', 0))
    port = available.getsockname()[1]
with (root / 'jvm.log').open('w') as log:
    process = subprocess.Popen([args.java, f'-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:{port}', '-cp', str(root), 'DebugFixture'], stdout=log, stderr=log)
    session = None
    try:
        for _ in range(100):
            if 'Listening for transport' in (root / 'jvm.log').read_text(): break
            time.sleep(.05)
        start = dispatch('debug.start', {'backend':'jdwp', 'port':port})
        session = start['session']
        bp = dispatch('debug.breakpoints', {'session':session, 'source':'DebugFixture', 'lines':[5]})
        assert bp['breakpoints'][0]['pending_class_load'], bp
        dispatch('debug.control', {'session':session, 'action':'continue'})
        state = wait_stopped(session, start['event_cursor'])
        stack = dispatch('debug.stack', {'session':session})
        assert stack['frames'][0]['line'] == 5, stack
        frame = str(stack['frames'][0]['id'])
        value = dispatch('debug.evaluate', {'session':session, 'frame_id':frame, 'expression':'count'})
        assert value['value'] == 40, value
        label = dispatch('debug.evaluate', {'session':session, 'frame_id':frame, 'expression':'label'})
        assert label['value'] == 'Tomacco', label
        dispatch('debug.control', {'session':session, 'action':'next'})
        state = wait_stopped(session, state['event_cursor'])
        stack = dispatch('debug.stack', {'session':session})
        value = dispatch('debug.evaluate', {'session':session, 'frame_id':str(stack['frames'][0]['id']), 'expression':'count'})
        assert value['value'] == 42, value
        events = dispatch('debug.events', {'session':session})
        checks.append({'backend':'jdwp','session':session,'events':events,'value_after_step':value,'stack':stack})
        dispatch('debug.control', {'session':session, 'action':'disconnect'})
        process.wait(timeout=10)
        assert 'Tomacco: 42' in (root / 'jvm.log').read_text()
    finally:
        if session:
            try: dispatch('debug.control', {'session':session, 'action':'disconnect'})
            except Exception: pass
        if process.poll() is None: process.terminate(); process.wait(timeout=5)

if args.swiftc and args.lldb_dap:
    swift = root / 'fixture.swift'
    swift.write_text('''import Foundation
@inline(never) func calculate() {
    var count = 40
    let label = "Tomacco"
    count += 2
    print("\\(label): \\(count)")
}
calculate()
''')
    binary = root / 'swift-fixture'
    subprocess.run([args.swiftc,'-g','-Onone',str(swift),'-o',str(binary)], check=True)
    env = {'LD_LIBRARY_PATH':args.library_path} if args.library_path else {}
    if args.python_home: env['PYTHONHOME'] = args.python_home
    start = dispatch('debug.start', {'backend':'lldb','adapter':args.lldb_dap,'program':str(binary),'cwd':str(root),'environment':env})
    session = start['session']
    try:
        state = wait_stopped(session)
        bp = dispatch('debug.breakpoints', {'session':session,'source':str(swift),'lines':[5]})
        assert bp['breakpoints'][0]['verified'], bp
        dispatch('debug.control', {'session':session,'action':'continue'})
        state = wait_stopped(session,state['event_cursor'])
        stack = dispatch('debug.stack', {'session':session})
        assert stack['frames'][0]['line'] == 5, stack
        value = dispatch('debug.evaluate', {'session':session,'frame_id':str(stack['frames'][0]['id']),'expression':'count'})
        assert value['result'] == '40', value
        dispatch('debug.control', {'session':session,'action':'next'})
        state = wait_stopped(session,state['event_cursor'])
        stack = dispatch('debug.stack', {'session':session})
        value = dispatch('debug.evaluate', {'session':session,'frame_id':str(stack['frames'][0]['id']),'expression':'count'})
        assert value['result'] == '42', value
        scopes = dispatch('debug.variables', {'session':session,'frame_id':str(stack['frames'][0]['id'])})
        assert scopes['scopes'], scopes
        variables = dispatch('debug.variables', {'session':session,'reference':str(scopes['scopes'][0]['variablesReference'])})
        assert any(v['name']=='count' and v['value']=='42' for v in variables['variables']), variables
        checks.append({'backend':'lldb-swift','session':session,'events':dispatch('debug.events',{'session':session}),'value_after_step':value,'variables':variables})
    finally:
        dispatch('debug.control',{'session':session,'action':'disconnect'})
atomic_json(root / 'results.json', {'passed':True,'checks':checks})
print(json.dumps({'passed':True,'backends':[x['backend'] for x in checks],'report':str(root / 'results.json')}))
