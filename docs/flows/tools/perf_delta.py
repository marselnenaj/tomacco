#!/usr/bin/env python3
"""Prepare/record the performance atlas delta; no Bogen, install or device calls."""
from pathlib import Path
import datetime, hashlib, json, shutil, subprocess, sys
sys.path.insert(0, str(Path(__file__).parent))
import atlas, metadata
ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]
PLAN=ROOT/'perf-delta-plan.json'
SOURCE_FILES=['native/main.rs','native/screen.rs','native/screen_ocr.rs','native/screen_ocr_engine.rs','native/screen_ocr_worker.rs','native/toolchain.rs','native/visual.rs','native/runtime/iphone.rs','native/runtime/android.rs','native/runtime/fixture.rs','native/devices.rs','native/gpx.rs','assets/operations.json','assets/errors.json','CMakeLists.txt','Cargo.toml','Cargo.lock','assets/io.github.marselnenaj.tomacco.metainfo.xml']
def read(p): return json.loads(p.read_text())
def write(p,d): p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def gate(path):
    p=read(PLAN)
    if Path(path).resolve()!=Path(p['qualification_receipt']).resolve():
        raise SystemExit('Release live-write gate closed: use only the pinned new1.5.0 qualification receipt')
    try: r=read(Path(path))
    except FileNotFoundError: raise SystemExit('Performance live-write gate closed: qualification receipt absent')
    if r.get('desktop_installed') is not True or r.get('product_commit')!=p['expected_product']:
        raise SystemExit('Performance live-write gate closed: exact pinned product is not confirmed installed')
    if r.get('runtime_code_commit')!=p['expected_runtime']:
        raise SystemExit('Performance live-write gate closed: runtime pin mismatch')
    return p,r
def payload():
    boards={b['id']:b for b in atlas.load_boards()}
    out={'prepared':read(PLAN),'metadata':metadata.payloads(),
         'spec_sha256':{b['id']:sha(REPO/b['spec_file']) for b in boards.values()},
         'source_sha256':{f:sha(REPO/f) for f in SOURCE_FILES},'schema_sha256':sha(REPO/'assets/operations.json')}
    for bid in ['D09','D12']:
        b=boards[bid]
        errors=atlas.check(b)
        if errors: raise SystemExit(str(errors))
        canvas,record=atlas.build(b)
        out[bid]={'canvas':canvas,'footer':atlas.footer_html(b),'record':record}
    (ROOT/'build').mkdir(exist_ok=True)
    write(ROOT/'build/perf-delta-payload.json',out)
    print(json.dumps({'D12_nodes':len(out['D12']['record']['nodes']),'D12_edges':len(out['D12']['record']['edges']),'D12_height':out['D12']['record']['height'],'atlas_nodes':sum(len(b['nodes']) for b in boards.values()),'live_writes_started':out['prepared']['live_writes_started']}))
def archive(plan):
    hist=ROOT/'history/1caa172'
    if hist.exists(): return
    hist.mkdir(parents=True)
    for f in ['README.md','atlas-map.json','bogen-ids.json','bogen-node-ids.json','metadata-ids-overlay.json','final-acceptance.json','ROOT-APPLICATION-STATE.json','ROOT-QA-NOTES.json','layout-final.json','render-manifest.json','render-qa-final.json','prototype-validation.json','provenance-current.json','root-final-review.json','ocr-delta-acceptance.json','label-qa-current.json']:
        (hist/f).write_bytes(subprocess.check_output(['git','show',plan['prepared_from']+':docs/flows/'+f],cwd=REPO))
    (hist/'renders').mkdir()
    for bid in ['00','D09','D12','S1']: shutil.copyfile(ROOT/'renders'/f'{bid}.png',hist/'renders'/f'{bid}.png')
    (hist/'specs').mkdir()
    for f in ['04_devices.py','05_agent_loop.py']:
        data=subprocess.check_output(['git','show',plan['prepared_from']+':docs/flows/specs/'+f],cwd=REPO)
        (hist/'specs'/f).write_bytes(data)
    write(hist/'HISTORY.json',{'epoch':'qualified adaptive OCR desktop1caa172/runtimee552e6f','historical':True,'captured_before':'performance worker/WDA delta','previous_product':plan['previous_installed_product'],'previous_runtime':plan['previous_runtime'],'source_snapshot_commit':plan['prepared_from']})
def installed(path):
    plan,receipt=gate(path)
    for f in SOURCE_FILES:
        expected=subprocess.check_output(['git','show',plan['expected_product']+':'+f],cwd=REPO)
        if (REPO/f).read_bytes()!=expected: raise SystemExit('Installed source snapshot mismatch: '+f)
    archive(plan)
    (ROOT/'reviews').mkdir(exist_ok=True)
    write(ROOT/'reviews/perf-install-gate.json',receipt)
    p=atlas.provenance()
    p.update(product_source=plan['expected_product'],runtime_code_commit=plan['expected_runtime'],verification_tree_commit=receipt.get('verification_tree_commit'),candidate_version=plan['candidate_version'],candidate_visibility=plan['candidate_visibility'],public_release_version=plan['public_release_version'],public_release_tag=plan['public_release_tag'],public_release_commit=plan.get('public_release_commit'),publication_receipt=plan.get('publication_receipt'),desktop_installed=True,perf_delta='ocr-worker-wda-20261108',perf_arm_status='new_1_5_candidate_package_not_yet_qualified',desktop_test_summary=receipt.get('desktop_test_summary','Lokale Steuerungs-, OCR- und UI-Qualifikation bestanden;'),qualification_receipt_sha256=sha(Path(path)),installed_gate_checked_at=now(),perf_source_sha256={f:sha(REPO/f) for f in SOURCE_FILES})
    write(ROOT/'provenance-current.json',p)
    for f,old in [('05_agent_loop.py','Kern 4584d6b · Performance-Delta noch nicht installiert'),('04_devices.py','Kern 4584d6b · Kontext-Delta noch nicht installiert')]:
        s=ROOT/'specs'/f
        s.write_text(s.read_text().replace(old,'Kern 4584d6b · Performance '+plan['expected_runtime'][:7]))
    plan.update(state='qualified_install_confirmed_ready_for_canvas',installed_gate_checked_at=now())
    write(PLAN,plan); payload()
if __name__=='__main__':
    cmd=sys.argv[1]
    if cmd=='payload': payload()
    elif cmd=='pin':
        p=read(PLAN)
        for h in sys.argv[2:4]:
            if len(h)!=40 or any(c not in '0123456789abcdef' for c in h): raise SystemExit('Supply root-pinned full product/runtime hashes')
        if sys.argv[2]==p['previous_installed_product']: raise SystemExit('Cannot reuse previous installed source')
        p.update(expected_product=sys.argv[2],expected_runtime=sys.argv[3],state='source_pinned_waiting_for_install');write(PLAN,p)
    elif cmd=='gate':
        p,r=gate(sys.argv[2]);print(json.dumps({'product_commit':r['product_commit'],'desktop_installed':True}))
    elif cmd=='installed': installed(sys.argv[2])
    elif cmd=='record':
        p,r=gate(sys.argv[2]);u=json.loads(sys.argv[3]);p.update(live_writes_started=True,state='applying_canvas',updated_at=now());p.setdefault('mutations',[]).append(u);write(PLAN,p)
        if u.get('board') and u.get('part') and u.get('new_id'):
            ids=read(ROOT/'bogen-ids.json');ids['boards'][u['board']][u['part']]=u['new_id'];write(ROOT/'bogen-ids.json',ids)
    else: raise SystemExit('Expected payload / pin PRODUCT RUNTIME / gate RECEIPT / installed RECEIPT / record RECEIPT JSON')
