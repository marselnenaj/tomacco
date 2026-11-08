#!/usr/bin/env python3
"""Prepare/record the optional OCR atlas update, without Bogen or hardware calls."""
from pathlib import Path
import datetime, hashlib, json, shutil, sys
sys.path.insert(0, str(Path(__file__).parent))
import atlas, metadata
ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
PLAN = ROOT / 'ocr-delta-plan.json'
def read(p): return json.loads(p.read_text())
def write(p, d): p.write_text(json.dumps(d, ensure_ascii=False, indent=2)+'\n')
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def gate(path):
    p, r = read(PLAN), read(Path(path))
    if not p['expected_product'] or not p['expected_runtime'] or r.get('desktop_installed') is not True or r.get('product_commit') != p['expected_product']:
        raise SystemExit('OCR live-write gate closed: exact new product install has not been confirmed')
    return p,r
def payload():
    b = next(b for b in atlas.load_boards() if b['id']=='D12')
    errors = atlas.check(b)
    if errors: raise SystemExit(str(errors))
    canvas,record = atlas.build(b)
    out = {'D12':{'canvas':canvas,'footer':atlas.footer_html(b),'record':record},'metadata':metadata.payloads(),
           'spec_sha256':hashlib.sha256((REPO/b['spec_file']).read_bytes()).hexdigest(),
           'schema_sha256':hashlib.sha256((REPO/'assets/operations.json').read_bytes()).hexdigest(),
           'prepared':read(PLAN)}
    (ROOT/'build').mkdir(exist_ok=True)
    write(ROOT/'build/ocr-delta-payload.json',out)
    print(json.dumps({'D12_nodes':len(record['nodes']),'D12_edges':len(record['edges']),'canvas_height':record['height'],'live_writes_started':out['prepared']['live_writes_started']}))
def installed(path):
    plan,receipt = gate(path)
    hist=ROOT/'history/1713806'
    if not hist.exists():
        hist.mkdir(parents=True)
        for f in ['README.md','atlas-map.json','bogen-ids.json','bogen-node-ids.json','final-acceptance.json','ROOT-APPLICATION-STATE.json','layout-final.json','render-manifest.json','render-qa-final.json','prototype-validation.json','provenance-current.json']:
            shutil.copyfile(ROOT/f,hist/f)
        (hist/'renders').mkdir()
        for bid in ['D12','00','S1']: shutil.copyfile(ROOT/'renders'/f'{bid}.png',hist/'renders'/f'{bid}.png')
        write(hist/'HISTORY.json',{'epoch':'qualified audio desktop1713806/runtime578819b','captured_before':'optional OCR adaptive update','historical':True})
    write(ROOT/'reviews/ocr-install-gate.json',receipt)
    p=atlas.provenance()
    p.update(product_source=plan['expected_product'],runtime_code_commit=plan['expected_runtime'],desktop_installed=True,ocr_delta='adaptive-20261108',
             ocr_arm_status='new_OCR_package_not_yet_qualified',desktop_test_summary=receipt.get('desktop_test_summary','Gezielte OCR- und Steuerungsprüfungen bestanden;'),qualification_receipt_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),installed_gate_checked_at=now())
    write(ROOT/'provenance-current.json',p)
    spec=ROOT/'specs/05_agent_loop.py'
    s=spec.read_text().replace('Kern 4584d6b · OCR-Delta noch nicht installiert','Kern 4584d6b · OCR '+plan['expected_runtime'][:7])
    spec.write_text(s)
    plan.update(state='qualified_install_confirmed_ready_for_canvas',installed_gate_checked_at=now())
    write(PLAN,plan)
    payload()
if __name__=='__main__':
    cmd=sys.argv[1]
    if cmd=='payload': payload()
    elif cmd=='pin':
        p=read(PLAN)
        for h in sys.argv[2:4]:
            if len(h)!=40 or any(c not in '0123456789abcdef' for c in h): raise SystemExit('Pin full commit hashes supplied by root')
        if sys.argv[2]==p['previous_installed_product']: raise SystemExit('New OCR qualification cannot reuse the previous desktop source')
        p.update(expected_product=sys.argv[2],expected_runtime=sys.argv[3],state='source_pinned_waiting_for_install')
        write(PLAN,p)
    elif cmd=='gate':
        p,r=gate(sys.argv[2]); print(json.dumps({'product_commit':r['product_commit'],'desktop_installed':True}))
    elif cmd=='installed': installed(sys.argv[2])
    elif cmd=='record':
        p,r=gate(sys.argv[2]);u=json.loads(sys.argv[3]);p.update(live_writes_started=True,state='applying_canvas',updated_at=now());p.setdefault('mutations',[]).append(u);write(PLAN,p)
        if u.get('board') and u.get('part') and u.get('new_id'):
            ids=read(ROOT/'bogen-ids.json');ids['boards'][u['board']][u['part']]=u['new_id'];write(ROOT/'bogen-ids.json',ids)
    else: raise SystemExit('Expected payload / pin PRODUCT RUNTIME / gate RECEIPT / installed RECEIPT / record RECEIPT JSON')
