#!/usr/bin/env python3
"""Remeasure final edge labels with the shipped font and check their polygons.

This consumes the generated record; it does not reuse atlas.check's collision
functions. No app, device, browser or network access is performed.
"""
from pathlib import Path
from PIL import ImageFont
import json, math, sys
sys.path.insert(0,str(Path(__file__).parent))
import atlas
ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]
font=ImageFont.truetype(str(REPO/'assets/fonts/archivo-var.ttf'),16)
font.set_variation_by_axes([500,100])
def rect(x,y,w,h): return [(x,y),(x+w,y),(x+w,y+h),(x,y+h)]
def node_poly(n):
    x,y,w,h=n['box']
    return [(x+w/2,y),(x+w,y+h/2),(x+w/2,y+h),(x,y+h/2)] if n['kind']=='decision' else rect(x,y,w,h)
def intersects(a,b):
    for poly in (a,b):
        for p,q in zip(poly,poly[1:]+poly[:1]):
            nx,ny=q[1]-p[1],p[0]-q[0]
            aa=[nx*x+ny*y for x,y in a];bb=[nx*x+ny*y for x,y in b]
            if max(aa)<=min(bb) or max(bb)<=min(aa):return False
    return True
issues=[];count=0;labels=0
for board in atlas.load_boards():
    record=atlas.build(board)[1];count+=1;placed=[]
    for edge in record['edges']:
        if not edge.get('label'):continue
        labels+=1;x,y,w,h=edge['label_box'];measured=math.ceil(font.getlength(edge['label']))+14
        if measured>w:issues.append([board['id'],edge['from'],edge['to'],'text exceeds label box'])
        shape=rect(x,y,measured,h)
        for n in record['nodes']:
            if intersects(shape,node_poly(n)):issues.append([board['id'],edge['from'],edge['to'],'overlaps node '+n['id']])
        for old,poly in placed:
            if intersects(shape,poly):issues.append([board['id'],edge['label'],'overlaps label '+old])
        placed.append((edge['label'],shape))
out={'boards':count,'labels':labels,'issues':issues,'overlaps':len(issues),'method':'Independent Archivo500@16 measurement and separating-axis polygon checks'}
(ROOT/'label-qa-current.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(out,ensure_ascii=False));raise SystemExit(bool(issues))
