#!/usr/bin/env python3
"""Reconcile the applied performance atlas delta. Final root review is a separate gate."""
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
    p,m,obs=read('provenance-current.json'),read('atlas-map.json'),read('reviews/perf-bogen-observations.json')
    assert p['desktop_installed'] and p['product_source']==obs['product_source']
    gate=read('reviews/perf-install-gate.json')
    assert gate['desktop_installed'] and gate['product_commit']==p['product_source']
    assert m['registry_sha256']==sha(REPO/'assets/operations.json')
    for k,v in p['perf_source_sha256'].items(): assert sha(REPO/k)==v,k
    review=read('reviews/perf-independent-review.json')
    assert not review['open_findings']
    delta=read('reviews/perf-reviewed-source-delta.json')
    assert delta['candidate_runtime']==p['runtime_code_commit'] and delta['worker_semantic_changes'] is False
    assert sha(ROOT/'reviews'/delta['patch'])==delta['patch_sha256']
    for k,v in review['source_sha256'].items():
        assert delta['source_pairs'][k]['reviewed_sha256']==v,k
        assert sha(REPO/k)==delta['source_pairs'][k]['candidate_sha256'],k
    benchmark=read('reviews/perf-final-qualification.json')
    assert all(benchmark['gates'].values())
    assert benchmark['final_runtime_commit']==p['runtime_code_commit']
    for v in obs['design_verify'].values(): assert v['observed_screenshot'] and v['discipline']=='documentation' and v['passed']
    for b in m['diagrams']:
        assert b['spec_sha256']==sha(REPO/b['spec_file']),b['id']
        actual=obs['actual_nodes'][b['id']]
        assert len(actual)==len(b['nodes']),b['id']
        for n in b['nodes']: assert actual[n['id']]==n['bogen_id'],(b['id'],n['id'])
    nodes=sum(len(b['nodes']) for b in m['diagrams'])
    assert nodes==1023 and len(m['diagrams'])==33 and len(m['operations'])==296
    assert all(o['bogen_node'] in obs['actual_nodes'][o['primary_flow']].values() for o in m['operations'])
    old_ids=read('history/1caa172/bogen-ids.json')['boards']
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
    old_pngs={r['diagram']:r for r in read('history/1caa172/render-manifest.json')['files']}
    files=[]
    for b in layout['artboards']:
        f=ROOT/'renders'/f"{b['diagram']}.png"
        with Image.open(f) as im: im.load();w,h=im.size
        assert w==1680 and abs(h-b['height'])<=1,(b,w,h)
        digest=sha(f)
        if b['diagram'] not in obs['regenerated_pngs']: assert digest==old_pngs[b['diagram']]['sha256'],b['diagram']
        files.append(dict(diagram=b['diagram'],path=str(f.relative_to(ROOT)),width=w,height=h,bytes=f.stat().st_size,sha256=digest))
    write('render-manifest.json',dict(created_at=now,product_source=p['product_source'],count=len(files),regenerated=obs['regenerated_pngs'],unchanged_exports_from='history/1caa172/render-manifest.json',files=files))
    for key in ['contrast','overflow','alt','small-text','target']: assert obs['lint']['counts'][key]==0
    assert obs['brand']['counts']['hard']==0 and obs['brand']['counts']['soft']==0
    labels=read('label-qa-current.json');assert labels['overlaps']==0 and labels['boards']==33 and labels['labels']==491
    write('render-qa-final.json',dict(checked_at=now,file_id=obs['file_id'],page_id=obs['page_id'],product_source=p['product_source'],artboards=36,details=33,lint=obs['lint'],brand=obs['brand'],visual_review=obs['visual_review'],label_placement=labels,top_level_overlap=0,unchanged_board_content='Previously accepted1caa172 content retained exceptD09 title/footer,D12 canvas/footer,00 product span andS1 body; all36 actual bounds and1023 nodes rechecked.',advisories='Measured diagram pixel geometry, token/grid/repetition/hierarchy/arrow-copy suggestions remain advisory. No critical issue was waived.'))
    acceptance=read('history/1caa172/final-acceptance.json')
    acceptance.update(checked_at=now,final_ready=False,implementation_ready=True,ready_for_root_final_qa=True,checkpoint=obs['checkpoint'],product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],historical_acceptance='history/1caa172/final-acceptance.json',pending=['Root final review of current performance atlas delta'])
    acceptance.pop('root_review',None)
    acceptance.update(candidate_version=p['candidate_version'],candidate_visibility=p['candidate_visibility'],public_release_version=p['public_release_version'],public_release_tag=p['public_release_tag'],public_release_commit=p.get('public_release_commit'),publication_receipt=p.get('publication_receipt'),documentation_source_commit=p.get('documentation_source_commit'))
    acceptance['counts']['logical_nodes']=nodes
    acceptance['evidence']=[e for e in acceptance['evidence'] if e!='root-final-review.json']+['perf-delta-plan.json','reviews/perf-bogen-observations.json','reviews/perf-install-gate.json','reviews/perf-desktop-update-receipt.json','reviews/perf-independent-review.json','reviews/perf-final-qualification.json','reviews/perf-installed-smoke.json','reviews/perf-reviewed-source-delta.json','reviews/perf-design-verify-D12.json','reviews/perf-design-verify-D09.json','reviews/perf-design-verify-00.json','reviews/perf-design-verify-S1.json']
    acceptance['qualification_limits']=[x for x in acceptance['qualification_limits'] if not x.startswith('Current OCR ARM package not built')]
    acceptance['qualification_limits']+=['Warm persistent-MCP speedup is offline corpus-specific; cold/custom CLI routes differ','Model retention raises resident memory until bounded cleanup; no general memory saving','Parallel WDA context is loopback-qualified, not physical iPhone latency','Private1.5 candidate ARM packages built, not yet finally qualified; prior578819b proof stays separate; no Pixel installation']
    write('final-acceptance.json',acceptance);write('perf-delta-acceptance.json',acceptance)
    m['validation'].update(root_final_acceptance_pending=True,final_ready=False,perf_observations='reviews/perf-bogen-observations.json')
    write('atlas-map.json',m)
    state=read('perf-delta-plan.json');state.update(state='ready_for_root_final_qa',updated_at=now,checkpoint=obs['checkpoint'],nodes_verified=1023,prototype_links=382,png_exports=36,regenerated_pngs=obs['regenerated_pngs'],critical_lint=0,brand_hard=0);write('perf-delta-plan.json',state)
    write('ROOT-APPLICATION-STATE.json',dict(phase='ready_for_root_final_qa',root_owner=False,all_new_boards_complete=True,final_ready=False,product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],file_id=obs['file_id'],page_id=obs['page_id'],historical_state='history/1caa172/ROOT-APPLICATION-STATE.json',current_acceptance='perf-delta-acceptance.json',boards={b['id']:dict(b['bogen'],spec_sha256=b['spec_sha256']) for b in m['diagrams']}))
    write('ROOT-QA-NOTES.json',dict(source_epoch=p['product_source'],historical_root_review='history/1caa172/root-final-review.json',current_evidence='perf-delta-acceptance.json',root_final_acceptance_pending=True))
    print(json.dumps(dict(spec_hashes_verified=33,nodes=nodes,operations=296,links=382,targets=56,pngs=len(files),regenerated=obs['regenerated_pngs'],critical_lint=0,root_review_pending=True)))
if __name__=='__main__': main()
