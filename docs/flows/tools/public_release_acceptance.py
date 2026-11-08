#!/usr/bin/env python3
"""Reconcile a publication-only atlas epoch; never grants root approval or writes Bogen."""
import datetime, hashlib, json
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]; REPO=ROOT.parents[1]
def read(name): return json.loads((ROOT/name).read_text())
def write(name,data): (ROOT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    p=read('provenance-current.json');o=read('reviews/public-1.5-bogen-observations.json');m=read('atlas-map.json')
    previous=read('history/private-1.5-c2e2326/final-acceptance.json')
    current=read('final-acceptance.json')
    if current.get('product_source')==p['product_source'] and current.get('final_ready'):
        raise SystemExit('Already root-accepted: preserve approval; no overwrite')
    pub=read('reviews/public-1.5-publication-receipt.json');arm=read('reviews/public-1.5-arm-public-verification.json');gui=read('reviews/public-1.5-arm-native-gui-strict.json')
    assert pub['state']=='published' and pub['latest'] and len(pub['assets'])==43
    assert pub['source_commit']==p['product_source']==o['product_source']==m['product_source']
    assert pub['runtime_code_commit']==p['runtime_code_commit']==o['runtime_code_commit']==m['runtime_code_commit']
    assert arm['source_commit']==p['product_source'] and arm['all_elf_bytes_and_modes_identical'] and arm['elf_count']==171
    assert gui['public_source_commit']==p['product_source'] and gui['checks_all_true'] and gui['control_plane_mocked'] and not gui['physical_device_access']
    for f,v in p['perf_source_sha256'].items(): assert sha(REPO/f)==v,f
    assert m['registry_sha256']==sha(REPO/'assets/operations.json')
    actual={}
    ids=read('bogen-ids.json')['boards']; prior_ids=read('history/1caa172/bogen-ids.json')['boards']
    assert all(ids[k]['artboard']==v['artboard'] for k,v in prior_ids.items())
    for b in m['diagrams']:
        assert b['spec_sha256']==sha(REPO/b['spec_file']),b['id']
        found=o['actual_nodes'][b['id']]['layers']; actual[b['id']]={n['name'].split(' · ',1)[1]:n['id'] for n in found}
        assert len(found)==len(b['nodes']) and all(n['parent']==ids[b['id']]['canvas'] for n in found)
        assert actual[b['id']]=={n['id']:n['bogen_id'] for n in b['nodes']},b['id']
    assert sum(map(len,actual.values()))==1023 and len(m['operations'])==296 and len(m['diagrams'])==33
    assert all(x['bogen_node']==actual[x['primary_flow']][x['logical_node']] for x in m['operations'])
    assert actual==read('bogen-node-ids.json')
    proto=read('prototype-validation.json');live=o['prototype']
    assert {x['id']:x['interactions'] for x in proto['layers']}=={x['id']:x['interactions'] for x in live['layers']}
    assert live['start']==proto['original_start']=='eivfth6g13' and len(live['layers'])==382
    targets={a['to'] for l in live['layers'] for i in l['interactions'] for a in i['actions'] if a['type']=='navigate'}
    assert len(targets)==56 and targets==set(o['targetproof']) and all(k==v['id'] for k,v in o['targetproof'].items())
    proto.update(product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],layers=live['layers'],actual_target_ids=sorted(targets),missing_targets=[],interaction_mismatches=[],verified_at=o['observed_at'])
    write('prototype-validation.json',proto)
    layout=read('layout-final.json');layout['product_source']=p['product_source'];layout['runtime_code_commit']=p['runtime_code_commit']
    for b in layout['artboards']:
        c=o['computed'][b['id']];b.update(x=float(c['style']['left'].removesuffix('px')),y=float(c['style']['top'].removesuffix('px')),width=c['box']['width'],height=c['box']['height'])
    for i,a in enumerate(layout['artboards']):
        for b in layout['artboards'][i+1:]:assert not(a['x']<b['x']+b['width'] and b['x']<a['x']+a['width'] and a['y']<b['y']+b['height'] and b['y']<a['y']+a['height']),(a,b)
    write('layout-final.json',layout)
    pngs=[]
    assert len(o['exports'])==36
    for b in layout['artboards']:
        f=ROOT/'renders'/f"{b['diagram']}.png"
        with Image.open(f) as im:im.load();w,h=im.size
        assert w==1680 and abs(h-b['height'])<=1,(b,w,h)
        pngs.append(dict(diagram=b['diagram'],path=str(f.relative_to(ROOT)),width=w,height=h,bytes=f.stat().st_size,sha256=sha(f)))
    write('render-manifest.json',dict(created_at=o['observed_at'],product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],count=36,regenerated=[r['diagram'] for r in pngs],files=pngs))
    for k in ['contrast','overflow','alt','small-text','target']:assert o['lint']['counts'][k]==0
    assert o['brand']['counts']['hard']==o['brand']['counts']['soft']==0
    labels=read('label-qa-current.json');assert labels['boards']==33 and labels['labels']==491 and not labels['issues'] and labels['overlaps']==0
    for bid in ['00','S1','D09','D12']:
        v=read('reviews/public-1.5-design-verify-'+bid+'.json');assert v['automated']['state']=='passed' and v['visual']['state']=='self-assessed'
    write('render-qa-final.json',dict(checked_at=o['observed_at'],file_id=o['file_id'],page_id=o['page_id'],product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],artboards=36,details=33,lint=o['lint'],brand=o['brand'],top_level_overlap=0,label_placement=labels,visual_review=dict(state='agent-self-assessed',boards=o['screenshots_observed'],root_final_review_pending=True),changed_content='Nine text layers in00/S1/D09/D12 updated in place; all36 PNGs regenerated. All artboard, logical-node and prototype IDs preserved. Fixed-canvas token/grid/copy and narrow responsive suggestions remain advisory. No critical finding waived.'))
    a=dict(previous);a.update(checked_at=o['observed_at'],final_ready=False,implementation_ready=True,ready_for_root_final_qa=True,checkpoint=o['checkpoint'],product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],public_release='1.5.0',public_release_version='1.5.0',public_release_tag='v1.5.0',public_release_commit=p['product_source'],candidate_visibility='published',publication_receipt=p['publication_receipt'],documentation_source_commit=p['product_source'],historical_acceptance='history/private-1.5-c2e2326/final-acceptance.json',pending=['Root final review of publication-only atlas metadata'])
    a.pop('root_review',None);a['gates']=dict(a['gates'],root_final_review_passed=False)
    a['evidence']=[x for x in a['evidence'] if x!='root-final-review.json']+['reviews/public-1.5-bogen-observations.json','reviews/public-1.5-publication-receipt.json','reviews/public-1.5-desktop-update-receipt.json','reviews/public-1.5-source-identity-bridge.json','reviews/public-1.5-arm-public-verification.json','reviews/public-1.5-arm-native-gui-strict.json']
    a['qualification_limits']=[x for x in a.get('qualification_limits',[]) if 'not yet finally qualified' not in x and 'not built' not in x]+['Experimental ARM: native GUI13 checks use mock control plane; actual CLI separately offline; no three-native-distro or incomplete-QEMU success claim','No physical Pixel installation or physical iPhone/AirPlay qualification']
    write('final-acceptance.json',a);write('public-1.5-delta-acceptance.json',a)
    m['validation'].update(root_final_acceptance_pending=True,final_ready=False,publication_observations='reviews/public-1.5-bogen-observations.json');write('atlas-map.json',m)
    write('root-final-review.json',dict(state='pending',product_source=p['product_source'],historical_review='history/private-1.5-c2e2326/root-final-review.json'))
    write('ROOT-QA-NOTES.json',dict(source_epoch=p['product_source'],historical_root_review='history/private-1.5-c2e2326/root-final-review.json',current_evidence='public-1.5-delta-acceptance.json',root_final_acceptance_pending=True))
    write('ROOT-APPLICATION-STATE.json',dict(phase='ready_for_root_final_qa',root_owner=False,all_new_boards_complete=True,final_ready=False,product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],file_id=o['file_id'],page_id=o['page_id'],historical_state='history/private-1.5-c2e2326/ROOT-APPLICATION-STATE.json',current_acceptance='public-1.5-delta-acceptance.json',boards={b['id']:dict(b['bogen'],spec_sha256=b['spec_sha256']) for b in m['diagrams']}))
    print(json.dumps(dict(specs=33,actual_nodes=1023,operations=296,links=382,targets=56,pngs=36,critical_lint=0,brand_hard=0,root_review_pending=True)))
if __name__=='__main__': main()
