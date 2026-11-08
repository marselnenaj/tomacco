#!/usr/bin/env python3
"""Generate the inspectable operation/flow map from registry + diagram specs."""
import json, pathlib, sys, re, hashlib, collections
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import atlas
from metadata import ROUTES
ROOT=pathlib.Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]

def generate():
    bs=atlas.load_boards(); records=[atlas.build(b)[1] for b in bs]
    ops=json.loads((REPO/'assets/operations.json').read_text())
    ids=json.loads((ROOT/'bogen-ids.json').read_text())
    if (ROOT/'metadata-ids-overlay.json').exists():
        for bid,v in json.loads((ROOT/'metadata-ids-overlay.json').read_text())['boards'].items():
            ids['boards'].setdefault(bid,{}).update(v)
    known={o['name'] for o in ops}
    invalid=[(b['id'],o) for b in bs for o in b.get('ops',[]) if o not in known]
    if invalid: raise ValueError(('non-registry ops',invalid))
    curated=json.loads((ROOT/'curated-node-map.json').read_text())['map']
    actual_nodes=json.loads((ROOT/'bogen-node-ids.json').read_text()) if (ROOT/'bogen-node-ids.json').exists() else {}
    rows=[]
    for op in ops:
        name=op['name']; candidates=[]
        for b in bs:
            nodes={n['id']:n for n in b['nodes']}
            # Extra mapping is explicit authored coverage for grouped support-tool flows.
            groups=dict(b.get('ops_by_node',{}))
            groups.update(curated.get(b['id'],{}))
            for nid,members in groups.items():
                if name in members:
                    assert nid in nodes,(b['id'],nid)
                    candidates.append((100,b,nodes[nid],'explicit_node_group'))
            if name not in b.get('ops',[]): continue
            for n in b['nodes']:
                text=n.get('op','')+' '+n['title']
                score=70 if re.search(r'(?<![\w.])'+re.escape(name)+r'(?![\w.])',text) else 0
                if not score:
                    prefix=name.rsplit('.',1)[0]+'.'
                    if prefix+'*' in text: score=40
                if score: candidates.append((score,b,n,'named_operation' if score==70 else 'explicit_prefix_group'))
        if not candidates:
            # This is deliberately an error: domain-level fallbacks do not prove flow coverage.
            raise ValueError('Operation needs an explicit logical node: '+name)
        candidates.sort(key=lambda c:(-c[0],c[1]['id'],c[2]['id']))
        score,b,n,kind=candidates[0]
        related=sorted({c[1]['id'] for c in candidates})
        rows.append({'operation':name,'title':op.get('title',name),'domain':name.split('.')[0],
            'primary_flow':b['id'],'logical_node':n['id'],'mapping_kind':kind,
            'related_flows':related,'description':op.get('description',''),
            'source_contract':'assets/operations.json#/'+str(next(i for i,o in enumerate(ops) if o['name']==name)),
            'source_anchors':b.get('sources',[]),'status':'implemented_source_contract; hardware qualifications see S1',
            'destructive':op.get('annotations',{}).get('destructiveHint',False),
            'bogen_artboard':ids['boards'].get(b['id'],{}).get('artboard'),
            'bogen_node':actual_nodes.get(b['id'],{}).get(n['id'])})
    for rec in records:
        rec['bogen']=ids['boards'].get(rec['id'],{})
        rec['spec_sha256']=hashlib.sha256((REPO/rec['spec_file']).read_bytes()).hexdigest()
        for node in rec['nodes']: node['bogen_id']=actual_nodes.get(rec['id'],{}).get(node['id'])
    d={'schema':'tomacco-flow-atlas/1','product_source':'4584d6bca3a07840e81683d99cd0adb386aeff35',
       'date':'2026-11-08','registry_sha256':hashlib.sha256((REPO/'assets/operations.json').read_bytes()).hexdigest(),
       'bogen':{'file_id':ids['file_id'],'page_id':ids['page_id']},
       'counts':{'diagrams':len(bs),'operations':len(ops),'domains':len({r['domain'] for r in rows}),'cli_routes':len(ROUTES)},
       'diagrams':records,'operations':rows,'cli_routes':ROUTES,
       'extra_entries':[{'entry':'tomaccoctl setup agents [status|remove]','flow':'D29','kind':'CLI bootstrap outside registry'},
                        {'entry':'Studio.serveFolder','flow':'D28','kind':'GUI helper outside registry'},
                        {'entry':'setup status','flow':'D05','kind':'CLI setup outside registry'}],
       'route_notes':['13 CLI enums; 11 desktop rail entries','Phone four primary entries plus More','preview is CLI; web-preview is internal phone extra ID','runtime and preview are Main.showWorkspace special routes; ios-research opens devices panel'],
       'qualification_limits':['No Pixel installation or physical qualification','No physical iPhone AirPlay audio/swipe/verified-fill qualification','Virtual iPhone experimental research runtime','Mobile parity source prose is advisory and does not override actual routes'],
       'validation':{'invalid_operations':invalid,'unmapped_operations':[],
          'all_logical_nodes_exist':True,'all_operation_bogen_nodes_resolved':all(r['bogen_node'] for r in rows),
          'all_diagram_bogen_nodes_resolved':all(n['bogen_id'] for b in records for n in b['nodes']),
          'prototype_evidence':'prototype-validation.json','layout_evidence':'layout-final.json',
          'review_resolutions':'review-resolutions.json','root_final_acceptance_pending':True,'final_ready':False}}
    (ROOT/'atlas-map.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
    (ROOT/'build'/'operation-rows.json').write_text(json.dumps(rows,ensure_ascii=False))
    compact=[{k:r[k] for k in ['operation','title','domain','primary_flow','logical_node','mapping_kind','source_contract','status','destructive','bogen_artboard','bogen_node']} for r in rows]
    (ROOT/'build'/'bogen-operation-rows.json').write_text(json.dumps(compact,ensure_ascii=False,separators=(',',':')))
    print(json.dumps(d['counts']))
    return d

if __name__=='__main__': generate()
