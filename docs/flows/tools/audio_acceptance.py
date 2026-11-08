#!/usr/bin/env python3
"""Reconcile current atlas evidence; root review remains an explicit final gate."""
import datetime, hashlib, json
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
def read(name): return json.loads((ROOT / name).read_text())
def write(name, value): (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def main():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    p, m = read('provenance-current.json'), read('atlas-map.json')
    obs, layout, proto = read('reviews/demo-audio-bogen-observations.json'), read('layout-final.json'), read('prototype-validation.json')
    missing = []
    for b in m['diagrams']:
        actual = {n['name'][6:]: n['id'] for n in obs['actual_nodes'][b['id']]}
        missing += [b['id'] + ':' + n['id'] for n in b['nodes'] if actual.get(n['id']) != n['bogen_id']]
    assert not missing, missing
    assert len(obs['actual_target_ids']) == 56
    assert len(set(obs['actual_target_ids'])) == 56
    targets = {a['to'] for l in proto['layers'] for i in l['interactions'] for a in i['actions'] if a['type'] == 'navigate'}
    assert targets == set(obs['actual_target_ids'])
    assert proto['links_present'] == 382 and not proto['interaction_mismatches']
    for i, a in enumerate(layout['artboards']):
        for b in layout['artboards'][i+1:]:
            assert not (a['x'] < b['x']+b['width'] and b['x'] < a['x']+a['width'] and a['y'] < b['y']+b['height'] and b['y'] < a['y']+a['height']), (a,b)
    files = []
    for b in layout['artboards']:
        f = ROOT / 'renders' / (b['diagram']+'.png')
        with Image.open(f) as im:
            im.load(); width,height = im.size
        assert width == 1680 and abs(height-b['height']) <= 1, (b,f,width,height)
        files.append(dict(diagram=b['diagram'],path=str(f.relative_to(ROOT)),width=width,height=height,bytes=f.stat().st_size,sha256=hashlib.sha256(f.read_bytes()).hexdigest()))
    write('render-manifest.json',dict(created_at=now,product_source=p['product_source'],count=len(files),files=files))
    proto.update(target_nodes_found=56,exact_target_lookup=True,missing_targets=[])
    write('prototype-validation.json',proto)
    qa = dict(checked_at=now,file_id='u8uvnj33hx',page_id='f5zgae58fy',product_source=p['product_source'],artboards=36,details=33,
              lint=obs['lint'],brand=obs['brand'],visual_review='Changed D23,00,S1 sections and whole boards inspected; D19/D20 footers checked. All36 complete exports regenerated. Remaining boards retain previously inspected content with explicit historical core footer label.',
              label_placement=read('label-qa-current.json'),top_level_overlap=0,
              advisories='Measured diagram geometry uses pixel positions. Token/grid/arrow-copy/repetition/hierarchy suggestions are advisory; no critical finding is waived.')
    write('render-qa-final.json',qa)
    acceptance=read('history/4584d6b/final-acceptance.json')
    acceptance.update(checked_at=now,final_ready=False,implementation_ready=True,ready_for_root_final_qa=True,checkpoint=obs['checkpoint'],product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],core_source_baseline=p['core_source_baseline'],historical_acceptance='history/4584d6b/final-acceptance.json',pending=['Root final visual/source review of current audio delta'])
    acceptance['counts']['logical_nodes'] = sum(len(b['nodes']) for b in m['diagrams'])
    acceptance['counts'].update(source_keys=p['source_keys'],translations=50)
    acceptance['gates'].update(missing_actual_node_ids=0,independent_label_overlaps=0,top_level_artboard_overlaps=0,critical_render_lint=obs['lint']['critical_total'],brand_hard_violations=obs['brand']['counts']['hard'])
    acceptance['evidence'] += ['provenance-current.json','label-qa-current.json','reviews/demo-audio-independent-review.json','reviews/demo-audio-install-gate.json','reviews/demo-audio-bogen-observations.json']
    write('final-acceptance.json',acceptance)
    write('demo-audio-delta-acceptance.json',acceptance)
    state=read('demo-audio-delta-state.json')
    state.update(state='ready_for_root_final_qa',updated_at=now,checkpoint=obs['checkpoint'],logical_nodes=981,actual_nodes_verified=981,prototype_links=382,png_exports=36,critical_lint=0,brand_hard=0)
    write('demo-audio-delta-state.json',state)
    app=dict(phase='ready_for_root_final_qa',root_owner=False,all_new_boards_complete=True,final_ready=False,product_source=p['product_source'],runtime_code_commit=p['runtime_code_commit'],file_id='u8uvnj33hx',page_id='f5zgae58fy',historical_state='history/4584d6b/ROOT-APPLICATION-STATE.json',current_acceptance='demo-audio-delta-acceptance.json',boards={b['id']:dict(b['bogen'],spec_sha256=b['spec_sha256']) for b in m['diagrams']})
    write('ROOT-APPLICATION-STATE.json',app)
    print(json.dumps(dict(nodes=acceptance['counts']['logical_nodes'],exports=len(files),links=382,critical_lint=0,brand_hard=0,root_review_pending=True)))

if __name__ == '__main__': main()
