#!/usr/bin/env python3
"""Reconcile the applied OCR atlas delta. Final root review is a separate gate."""
import datetime, hashlib, json
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]
def read(f): return json.loads((ROOT/f).read_text())
def write(f,d): (ROOT/f).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def sha(f): return hashlib.sha256(f.read_bytes()).hexdigest()
def main():
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    p,m,obs=read('provenance-current.json'),read('atlas-map.json'),read('reviews/ocr-bogen-observations.json')
    assert p['desktop_installed'] and p['product_source']==obs['product_source']
    gate=read('reviews/ocr-install-gate.json')
    assert gate['desktop_installed'] and gate['product_commit']==p['product_source']
    assert m['registry_sha256']==sha(REPO/'assets/operations.json')
    for k,v in p['ocr_source_sha256'].items(): assert sha(REPO/k)==v,k
    review=read('reviews/ocr-adaptive-independent-review.json')
    assert review['open_findings']==0
    for k,v in review['source_sha256'].items(): assert sha(REPO/k)==v,k
    for b in m['diagrams']:
        assert b['spec_sha256']==sha(REPO/b['spec_file']),b['id']
        actual=obs['actual_nodes'][b['id']]
        assert len(actual)==len(b['nodes']),b['id']
        for n in b['nodes']: assert actual[n['id']]==n['bogen_id'],(b['id'],n['id'])
    nodes=sum(len(b['nodes']) for b in m['diagrams'])
    assert nodes==1000 and len(m['diagrams'])==33 and len(m['operations'])==296
    assert all(o['bogen_node'] in obs['actual_nodes'][o['primary_flow']].values() for o in m['operations'])
    old_ids=read('history/1713806/bogen-ids.json')['boards']
    ids=read('bogen-ids.json')['boards']
    assert all(ids[k]['artboard']==v['artboard'] for k,v in old_ids.items())
    proto=read('prototype-validation.json')
    targets={a['to'] for l in proto['layers'] for i in l['interactions'] for a in i['actions'] if a['type']=='navigate'}
    assert len(proto['layers'])==382 and len(targets)==56 and targets==set(obs['actual_target_ids'])
    assert proto['original_start']=='eivfth6g13' and not proto['missing_targets'] and not proto['interaction_mismatches']
    layout=read('layout-final.json')
    for i,a in enumerate(layout['artboards']):
        for b in layout['artboards'][i+1:]:
            assert not (a['x']<b['x']+b['width'] and b['x']<a['x']+a['width'] and a['y']<b['y']+b['height'] and b['y']<a['y']+a['height']),(a,b)
    old_pngs={r['diagram']:r for r in read('history/1713806/render-manifest.json')['files']}
    files=[]
    for b in layout['artboards']:
        f=ROOT/'renders'/f"{b['diagram']}.png"
        with Image.open(f) as im: im.load();w,h=im.size
        assert w==1680 and abs(h-b['height'])<=1,(b,w,h)
        digest=sha(f)
        if b['diagram'] not in obs['regenerated_pngs']: assert digest==old_pngs[b['diagram']]['sha256'],b['diagram']
        files.append(dict(diagram=b['diagram'],path=str(f.relative_to(ROOT)),width=w,height=h,bytes=f.stat().st_size,sha256=digest))
    write('render-manifest.json',dict(created_at=now,product_source=p['product_source'],count=len(files),regenerated=obs['regenerated_pngs'],unchanged_exports_from='history/1713806/render-manifest.json',files=files))
    for key in ['contrast','overflow','alt','small-text','target']: assert obs['lint']['counts'][key]==0
    assert obs['brand']['counts']['hard']==0 and obs['brand']['counts']['soft']==0
    labels=read('label-qa-current.json');assert labels['overlaps']==0 and labels['boards']==33
    write('render-qa-final.json',dict(checked_at=now,file_id=obs['file_id'],page_id=obs['page_id'],product_source=p['product_source'],artboards=36,details=33,lint=obs['lint'],brand=obs['brand'],visual_review=obs['visual_review'],label_placement=labels,top_level_overlap=0,unchanged_board_content='Previous qualified audio-era content retained exceptD12/00/S1; all36 actual bounds and1000 nodes rechecked.',advisories='Measured diagram pixel geometry, token/grid/repetition/hierarchy/arrow-copy suggestions remain advisory. No critical issue was waived.'))
    acceptance=read('history/1713806/final-acceptance.json')
    acceptance.update(checked_at=now,final_ready=False,implementation_ready=True,ready_for_root_final_qa=True,checkpoint=obs['checkpoint'],product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],historical_acceptance='history/1713806/final-acceptance.json',pending=['Root final review of current OCR atlas delta'])
    acceptance.pop('root_review',None)
    acceptance['counts']['logical_nodes']=nodes
    acceptance['evidence']=[e for e in acceptance['evidence'] if e!='root-final-review.json']+['ocr-delta-plan.json','reviews/ocr-bogen-observations.json','reviews/ocr-install-gate.json','reviews/ocr-desktop-update-receipt.json','reviews/ocr-adaptive-independent-review.json','../ocr-adaptive-qualification.json']
    acceptance['qualification_limits']+=['Adaptive OCR can miss additional words/duplicates; no absence/uniqueness proof','Current OCR ARM package not built/qualified;578819b ARM proof remains separate']
    write('final-acceptance.json',acceptance);write('ocr-delta-acceptance.json',acceptance)
    m['validation'].update(root_final_acceptance_pending=True,final_ready=False,ocr_observations='reviews/ocr-bogen-observations.json')
    write('atlas-map.json',m)
    state=read('ocr-delta-plan.json');state.update(state='ready_for_root_final_qa',updated_at=now,checkpoint=obs['checkpoint'],nodes_verified=1000,prototype_links=382,png_exports=36,regenerated_pngs=obs['regenerated_pngs'],critical_lint=0,brand_hard=0);write('ocr-delta-plan.json',state)
    write('ROOT-APPLICATION-STATE.json',dict(phase='ready_for_root_final_qa',root_owner=False,all_new_boards_complete=True,final_ready=False,product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],file_id=obs['file_id'],page_id=obs['page_id'],historical_state='history/1713806/ROOT-APPLICATION-STATE.json',current_acceptance='ocr-delta-acceptance.json',boards={b['id']:dict(b['bogen'],spec_sha256=b['spec_sha256']) for b in m['diagrams']}))
    write('ROOT-QA-NOTES.json',dict(source_epoch=p['product_source'],historical_root_review='history/1713806/root-final-review.json',current_evidence='ocr-delta-acceptance.json',root_final_acceptance_pending=True))
    print(json.dumps(dict(spec_hashes_verified=33,nodes=nodes,operations=296,links=382,targets=56,pngs=len(files),regenerated=obs['regenerated_pngs'],critical_lint=0,root_review_pending=True)))
if __name__=='__main__': main()
