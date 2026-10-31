#!/usr/bin/env python3
"""Measure native versus previous CLI, including process startup and resident memory."""
import argparse
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--native', required=True)
parser.add_argument('--baseline', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--samples', type=int, default=40)
args = parser.parse_args()
commands = {'rust': [str(Path(args.native).resolve())], 'python': [str(Path(args.baseline).resolve())]}
operations = {'schema': ['schema'], 'catalog': ['call', 'catalog.list', '{}'], 'jobs_200': ['call', 'job.list', '{"limit":200}']}
result = {'method': 'Alternating warmed process launches; JSON captured and validated; full CLI startup included. No build/compiler speed claim.', 'samples': args.samples, 'platform': platform.platform(), 'operations': {}}
with tempfile.TemporaryDirectory(prefix='tomacco-benchmark-') as temporary:
    root = Path(temporary)
    env = {**os.environ, 'TOMACCO_HOME': str(root / 'state')}
    subprocess.run(commands['rust'] + ['call', 'job.list', '{}'], env=env, check=True, capture_output=True)
    db = sqlite3.connect(root / 'state/jobs.sqlite3')
    with db:
        for i in range(200):
            db.execute("INSERT INTO jobs(id,label,state,created,updated,spec,result) VALUES(?,?,'succeeded',?,?,?,?)", (f'{i:032x}', f'Build {i}', i, i, json.dumps({'cwd': '/tmp', 'steps': [{'argv': ['gradle', 'assembleDebug']}], 'timeout': 1800, 'env': {}, 'artifacts': []}), '{"artifacts":[]}'))
    db.close()
    for name, operation in operations.items():
        samples = {key: [] for key in commands}
        expected = None
        for index in range(args.samples + 4):
            for key in (['rust', 'python'] if index % 2 == 0 else ['python', 'rust']):
                start = time.perf_counter_ns()
                output = subprocess.run(commands[key] + operation, capture_output=True, env=env, check=True)
                elapsed = (time.perf_counter_ns() - start) / 1_000_000
                value = json.loads(output.stdout)
                assert value['ok']
                if expected is None: expected = value
                assert value == expected, (key, name, 'contract mismatch')
                if index >= 4: samples[key].append(elapsed)
        values = {}
        for key, times in samples.items():
            with tempfile.TemporaryFile() as output:
                proc = subprocess.Popen(commands[key] + operation, stdout=output, stderr=output, env=env)
                _, status, usage = os.wait4(proc.pid, 0)
                proc.returncode = os.waitstatus_to_exitcode(status)
                assert proc.returncode == 0
            values[key] = {'median_ms': round(statistics.median(times), 3), 'p95_ms': round(sorted(times)[int(.95 * (len(times)-1))], 3), 'peak_rss_kib': usage.ru_maxrss}
        values['speedup'] = round(values['python']['median_ms'] / values['rust']['median_ms'], 2)
        result['operations'][name] = values
    for key, command in commands.items():
        proc = subprocess.Popen(command + ['mcp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        def call(value):
            proc.stdin.write(json.dumps(value) + '\n'); proc.stdin.flush()
            return json.loads(proc.stdout.readline())
        call({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}})
        samples = []
        for i in range(104):
            start = time.perf_counter_ns()
            value = call({'jsonrpc': '2.0', 'id': i + 2, 'method': 'tools/call', 'params': {'name': 'catalog.list'}})
            elapsed = (time.perf_counter_ns() - start) / 1_000_000
            assert not value['result']['isError']
            if i >= 4: samples.append(elapsed)
        rss = next(line.split()[1] for line in Path(f'/proc/{proc.pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
        result.setdefault('persistent_mcp_catalog', {})[key] = {'median_ms': round(statistics.median(samples), 3), 'samples': len(samples), 'resident_kib': int(rss)}
        proc.stdin.close(); proc.wait(5); proc.stdout.close()
Path(args.output).parent.mkdir(parents=True, exist_ok=True)
Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
