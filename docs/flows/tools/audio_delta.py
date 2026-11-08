#!/usr/bin/env python3
"""Prepare/record the source-audio atlas delta. This script never calls Bogen.

Live canvas tools must first run `gate RECEIPT`; the receipt belongs to the
qualification task and must explicitly confirm a successful desktop install.
"""
from pathlib import Path
import datetime,hashlib,json,sys
sys.path.insert(0,str(Path(__file__).parent))
import atlas, metadata
ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]
EXPECTED='1713806db7ef00eda9754bb5bea23cd803179aa0'
RUNTIME='578819be350cc430196fef59a8d07ea91b3c34e4'
def load(p):return json.loads(p.read_text())
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def gate(path):
    receipt=load(Path(path))
    if receipt.get('desktop_installed') is not True or receipt.get('product_commit')!=EXPECTED:
        raise SystemExit('Live Bogen write gate closed: qualified product install has not been confirmed')
    return receipt

def payload():
    data={};boards=atlas.load_boards()
    for bid in ['D19','D20','D23']:
        board=next(b for b in boards if b['id']==bid)
        problems=atlas.check(board)
        if problems:raise SystemExit(str(problems))
        if bid=='D23':
            canvas,record=atlas.build(board)
            data[bid]={'header':atlas.header_html(board),'canvas':canvas,'footer':atlas.footer_html(board),'record':record}
        else:data[bid]={'footer':atlas.footer_html(board)}
    data['metadata']=metadata.payloads()
    data['spec_sha256']={b['id']:hashlib.sha256((REPO/b['spec_file']).read_bytes()).hexdigest() for b in boards if b['id'] in data}
    (ROOT/'build').mkdir(exist_ok=True);save(ROOT/'build/audio-delta-payload.json',data)
    print(json.dumps({'payload':'docs/flows/build/audio-delta-payload.json','D23_nodes':len(data['D23']['record']['nodes']),'D23_edges':len(data['D23']['record']['edges'])}))

if __name__=='__main__':
    cmd=sys.argv[1]
    if cmd=='gate':
        x=gate(sys.argv[2]);print(json.dumps({'desktop_installed':True,'product_commit':x['product_commit']}))
    elif cmd=='installed':
        receipt=gate(sys.argv[2]);save(ROOT/'reviews/demo-audio-install-gate.json',receipt);p=load(ROOT/'provenance-current.json');p.update(desktop_installed=True,product_source=receipt['product_commit'],qualification_receipt_sha256=hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest(),installed_gate_checked_at=now());save(ROOT/'provenance-current.json',p)
        spec=ROOT/'specs/74_d23_demo.py';s=spec.read_text().replace('Demo-Audio · Installationsnachweis folgt','Audio '+RUNTIME[:7]+' · Produkt '+EXPECTED[:7]);spec.write_text(s)
        state=load(ROOT/'demo-audio-delta-state.json');state.update(state='qualified_install_confirmed_ready_for_canvas',desktop_installed=True,product_commit=EXPECTED,updated_at=now());save(ROOT/'demo-audio-delta-state.json',state);payload()
    elif cmd=='payload':payload()
    elif cmd=='record':
        receipt=gate(sys.argv[2]);update=json.loads(sys.argv[3]);state=load(ROOT/'demo-audio-delta-state.json');state.update(live_writes_started=True,state='applying_canvas',updated_at=now());state.setdefault('mutations',[]).append(update);save(ROOT/'demo-audio-delta-state.json',state)
        if update.get('board') and update.get('part') and update.get('new_id'):
            ids=load(ROOT/'bogen-ids.json');ids['boards'][update['board']][update['part']]=update['new_id'];save(ROOT/'bogen-ids.json',ids)
        print(json.dumps(update,ensure_ascii=False))
    else:raise SystemExit('Expected gate / installed / payload / record')
