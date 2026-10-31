#!/usr/bin/env python3
"""Qualify output playback on an existing Android session without restarting it.

Requires the real scrcpy client, PipeWire and an already-connected runtime.
Only this test's audio jobs are stopped. Successful execution leaves sound on.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import struct
import subprocess
import time

from native_client import atomic_json, dispatch, jobs


def clients(device, job=None):
    result = []
    for file in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = file.read_bytes().split(b'\0')
            if not args or not args[0].endswith(b'/scrcpy') or b'--no-video' not in args:
                continue
            if b'--serial' not in args or args[args.index(b'--serial') + 1] != device.encode():
                continue
            if job and b'TOMACCO_PARENT_JOB=' + job.encode() not in (file.parent / 'environ').read_bytes().split(b'\0'):
                continue
            result.append(int(file.parent.name))
        except (OSError, IndexError):
            pass
    return result


def stream_for(pid):
    graph = json.loads(subprocess.check_output(['pw-dump'], timeout=5))
    owned = {row['id'] for row in graph if row.get('type', '').endswith(':Client')
             and str(row.get('info', {}).get('props', {}).get('application.process.id')) == str(pid)}
    return [row for row in json.loads(subprocess.check_output(['pactl', '-f', 'json', 'list', 'sink-inputs'], timeout=5))
            if int(row.get('properties', {}).get('client.id', -1)) in owned]


def wait_playback(device, job):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = jobs.get(job)
        assert state['state'] not in jobs.TERMINAL, state
        owners = clients(device, job)
        if len(owners) == 1:
            streams = stream_for(owners[0])
            if len(streams) == 1:
                stream = streams[0]
                assert not stream['mute'] and not stream['corked'], stream
                return dict(pid=owners[0], index=stream['index'], sink=stream['sink'],
                            format=stream['sample_specification'], volume=stream['volume'])
        time.sleep(.1)
    raise AssertionError(f'No playback stream for {job}: {jobs.logs(job)}')


def video_packet(state):
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(5)
        stream.connect(state['video_socket'])
        def read(size):
            data = b''
            while len(data) < size:
                part = stream.recv(size - len(data))
                assert part, 'Video stream closed during audio change'
                data += part
            return data
        packet = read(struct.unpack('>I', read(4))[0])
        assert packet and len(packet) < 16 * 1024 * 1024
        return len(packet)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--output', default='qa/android-audio/results.json')
    args = parser.parse_args()
    initial = dispatch('runtime.state', dict(session=args.session))
    assert initial['platform'] == 'android' and initial['status'] == 'connected', initial
    assert not initial.get('audio', {}).get('enabled'), 'Preserved existing audio; stop it explicitly before qualification'
    assert not clients(initial['device']), 'Preserved an external audio client on this device'
    before_bytes = video_packet(initial)
    request = dict(session=args.session, action='start')
    with ThreadPoolExecutor(max_workers=6) as pool:
        started = list(pool.map(lambda _: dispatch('runtime.audio', request), range(6)))
    job = started[0]['job_id']
    assert len({r['job_id'] for r in started}) == 1, started
    first = wait_playback(initial['device'], job)
    assert len(clients(initial['device'], job)) == 1
    dispatch('runtime.audio', dict(session=args.session, action='stop'))
    assert jobs.wait(job, 5)['state'] == 'cancelled'
    assert not clients(initial['device'], job)
    assert not stream_for(first['pid'])
    resumed = dispatch('runtime.audio', request)
    assert resumed['job_id'] != job
    playback = wait_playback(initial['device'], resumed['job_id'])
    current = dispatch('runtime.state', dict(session=args.session))
    for key in ['session', 'pid', 'pid_token', 'device', 'video_socket', 'control_socket', 'adb_port', 'scrcpy_id']:
        assert current[key] == initial[key], (key, initial, current)
    assert current['status'] == 'connected' and current['audio']['enabled'], current
    report = dict(passed=True, session=args.session, device=initial['device'], reused_calls=sum(r['reused'] for r in started),
                  cancelled_job=job, active_job=resumed['job_id'], playback=playback,
                  video_worker_preserved=dict(pid=current['pid'], token=current['pid_token']),
                  video_packet_bytes_before=before_bytes, video_packet_bytes_after=video_packet(current),
                  scope='Real playback stream and lifecycle; human audibility is not asserted')
    atomic_json(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
