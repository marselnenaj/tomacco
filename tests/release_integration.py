#!/usr/bin/env python3
"""Generate an isolated signing key and sign/verify actual APK + AAB artifacts."""
import argparse,json,secrets,time
from pathlib import Path
from native_client import dispatch,jobs,atomic_json
parser=argparse.ArgumentParser();parser.add_argument('--apk',required=True);parser.add_argument('--aab',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
out=Path(args.output).resolve();out.mkdir(parents=True)
pw=out/'password';pw.write_text(secrets.token_urlsafe(36)+'\n');pw.chmod(0o600)
def finish(j):
 while j['state'] not in jobs.TERMINAL:j=jobs.wait(j['id'],5)
 assert j['state']=='succeeded',{'job':j,'logs':jobs.logs(j['id'])}
 assert pw.read_text().strip() not in jobs.logs(j['id'])['text']
 assert pw.read_text().strip() not in json.dumps(j)
 return j
key=out/'qa-upload.p12';checks=[finish(dispatch('release.keystore',{'output':str(key),'password_file':str(pw),'alias':'qa-upload'}))]
for source in map(Path,[args.apk,args.aab]):
 target=out/('signed'+source.suffix)
 j=finish(dispatch('release.sign',{'path':str(source.resolve()),'output':str(target),'keystore':str(key),'password_file':str(pw),'alias':'qa-upload'}));checks.append(j)
 assert dispatch('release.verify',{'path':str(target)})['verified']
 # A corrupt signed package must not be reported as valid.
 corrupt=out/('corrupt'+source.suffix);data=bytearray(target.read_bytes());data[len(data)//2]^=1;corrupt.write_bytes(data)
 try:dispatch('release.verify',{'path':str(corrupt)})
 except Exception:pass
 else:raise AssertionError('Corrupt signature accepted')
assert key.stat().st_mode&0o077==0
atomic_json(out/'results.json',{'passed':True,'checks':checks});print(json.dumps({'passed':True,'checks':len(checks),'report':str(out/'results.json')}))
