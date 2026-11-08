#!/usr/bin/env python3
"""Offline, labelled OCR quality/latency benchmark through Tomacco's JSON CLI.

    python3 tools/benchmark_ocr.py build --media-root /path/to/Launch-Thread
    python3 tools/benchmark_ocr.py run --cli /path/to/tomaccoctl --trials 3 --output qa/ocr-fast/result.json

Only local PNGs are read. Every trial launches a fresh CLI process, so timings
include CLI startup/guard, PNG I/O/decode, OCR, merging and JSON serialization;
they exclude device capture/transfer, MCP transport, tapping and agent reasoning.
This is not an FPS, end-to-end device latency or competitor benchmark.
"""
import argparse
from collections import Counter
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import random
import queue
import shutil
import statistics
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE_CORPUS = ROOT / 'tests/fixtures/ocr-corpus'
CORPUS = SOURCE_CORPUS if SOURCE_CORPUS.is_dir() else ROOT / 'benchmarks/ocr-corpus'
TRAPS = ('adb','fastboot','ssh','scp','uxplay','bluetoothctl','ideviceinfo','idevicepair','iproxy','xtool','xcrun','pymobiledevice3','tidevice','go-ios','hyprctl','pactl','wpctl','tomacco')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_corpus(media_root):
    # Deterministic pixel fixtures are test data, not product screenshots or
    # generated marketing imagery. Pillow is required only for regeneration.
    import PIL
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
    CORPUS.mkdir(parents=True, exist_ok=True)
    cases=[]
    def save(name, image, targets, primary=None, **extra):
        path=CORPUS/(name+'.png');image.convert('RGB').save(path)
        cases.append({**dict(id=name,image=path.name,width=image.width,height=image.height,sha256=sha(path),targets=targets,
                             primary_target=primary or (targets[0] if targets else None),source_type='synthetic'),**extra})
    def real(name, video, timestamp, box, targets, primary, primary_rect):
        path=media_root/video/'VIDEO.mp4'
        data=subprocess.check_output(['/usr/bin/ffmpeg','-nostdin','-v','error','-ss',str(timestamp),'-i',str(path),'-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'])
        image=Image.open(io.BytesIO(data)).convert('RGB').crop(box)
        save(name,image,targets,primary,source_type='real_app_frame_crop',primary_rect=primary_rect,
             provenance=dict(archive_relative=video+'/VIDEO.mp4',video_sha256=sha(path),timestamp_seconds=timestamp,
                             crop=[box[0],box[1],box[2]-box[0],box[3]-box[1]],transform='crop only, no resizing or retouching'))
        return image
    portrait_targets=['tomatoes','Make room','for today','Today','Progress','Settings','Task title','Save task','Ship the demo']
    portrait=real('real-portrait','08-screen-understanding',3,(1016,220,1327,918),portrait_targets,'Save task',[18,364,275,41])
    real('real-landscape','07-emulator-tools',4,(810,408,1535,729),['tomatoes','Make room','for today','Today','Progress','Settings'],'Settings',[185,201,80,37])
    real('real-project-header','14-native-workspace',2,(790,158,1864,324),['Project','Toolchain','Open your app'],'Toolchain',[954,20,100,33])
    real('real-build-controls','14-native-workspace',2,(800,458,1845,681),['Build','Android','Debug','APK','Tests','Check code','Show build steps','Run on device','Manage devices','Build & run'],'Check code',[465,65,89,32])
    save('derived-dark-portrait',ImageOps.invert(portrait),portrait_targets,'Save task',source_type='derived_stress',primary_rect=[18,364,275,41],
         provenance=dict(derived_from='real-portrait.png',transform='RGB inversion; synthetic dark-stress variant, not a fresh app capture'))
    def font(size,weight=400):
        f=ImageFont.truetype(str(ROOT/'assets/fonts/archivo-var.ttf'),size);f.set_variation_by_axes([weight,100]);return f
    def panel(bg,fg,size=26):
        image=Image.new('RGB',(900,440),bg);draw=ImageDraw.Draw(image)
        for y,text in [(35,'Account settings'),(130,'Email address'),(205,'hello@example.test'),(330,'Continue')]:draw.text((40,y),text,font=font(size),fill=fg)
        return image
    panel_targets=['Account settings','Email address','hello@example.test','Continue']
    for name,bg,fg in [('light-panel','#f4f2ec','#121211'),('dark-panel','#121211','#f4f2ec')]:
        save(name,panel(bg,fg),panel_targets,'Continue',primary_rect=[35,320,210,60])
    for name,base,ink,card,cardink in [('minority-dark','#fafafa','#171717','#131313','#fafafa'),('minority-light','#101010','#ededed','#fafafa','#191919')]:
        image=Image.new('RGB',(1000,700),base);draw=ImageDraw.Draw(image)
        for y,text in [(30,'Device settings'),(110,'Choose a connection'),(190,'Nearby devices'),(270,'No recent errors')]:draw.text((40,y),text,font=font(25),fill=ink)
        draw.rectangle((730,570,965,645),fill=card);draw.text((750,590),'Reconnect',font=font(18),fill=cardink)
        save(name,image,['Device settings','Choose a connection','Nearby devices','No recent errors','Reconnect'],'Reconnect',primary_rect=[730,570,236,76])
    save('small-low-contrast',panel('#eeeeee','#777777',15),panel_targets,'Continue',primary_rect=[35,320,160,45])
    save('small-text',panel('#f7f7f7','#1b1b1b',12),panel_targets,'Continue',primary_rect=[35,320,130,40])
    save('blurred-text',panel('#f7f7f7','#1b1b1b',22).filter(ImageFilter.GaussianBlur(1.25)),panel_targets,'Continue',primary_rect=[35,320,200,55])
    image=Image.new('RGB',(1000,500),'#f4f2ec');draw=ImageDraw.Draw(image)
    latin=['Überblick','Änderungen speichern','Fermer la fenêtre','Español: Configuración','Português: Ações']
    for i,text in enumerate(latin):draw.text((36,28+i*85),text,font=font(26),fill='#121211')
    save('localized-latin-eng-model',image,latin,'Änderungen speichern',primary_rect=[30,105,430,55],qualification='Latin-script labels with eng only, not qualification of German/French/Spanish/Portuguese language models')
    image=Image.new('RGB',(650,400),'white');draw=ImageDraw.Draw(image)
    for y in [60,260]:draw.rectangle((40,y-10,330,y+50),outline='#444444');draw.text((60,y),'Continue',font=font(24),fill='black')
    save('duplicate-targets',image,['Continue','Continue'],'Continue',expected_primary_matches=2,primary_rects=[[40,50,291,61],[40,250,291,61]])
    save('empty',Image.new('RGB',(640,480),'white'),[],negative_target='Continue')
    rng=random.Random(72431);image=Image.new('RGB',(320,240));image.putdata([(v,v,v) for v in (rng.randrange(0,256) for _ in range(320*240))])
    save('noise-no-text',image,[],negative_target='Continue',seed=72431)
    # Region variants reuse exactly the full image's pixels and global coords.
    for full in [c for c in cases if c['id'] in ('minority-dark','minority-light')]:
        cases.append({**full,'id':full['id']+'-region','targets':['Reconnect'],'evaluation_role':'region_variant','region':dict(x=720,y=560,width=260,height=100)})
    # These already-labelled images exposed a real normal-pass miss. Pin the
    # missed target for repeatable fallback tests; do not count duplicate focus
    # cases as additional independent images in aggregate quality statistics.
    for source_id,target,rect in [('real-landscape','Progress',[90,201,90,37]),('small-text','Account settings',[35,25,200,40])]:
        original=next(c for c in cases if c['id']==source_id)
        cases.append({**original,'id':source_id+'-inversion-target','primary_target':target,'primary_rect':rect,
                      'evaluation_role':'regression_focus','selection_note':'Same pre-labelled image; primary target pinned after baseline exposed a normal-pass miss recovered by auto. Not another independent sample.'})
    manifest=dict(schema='tomacco-ocr-corpus/1',label_method='Ground truth authored from visually inspected app UI and synthetic source strings before OCR; no OCR output used to set labels. Targets are a declared subset, not exhaustive transcription.',
                  font=dict(path='assets/fonts/archivo-var.ttf',sha256=sha(ROOT/'assets/fonts/archivo-var.ttf')),
                  regeneration=dict(pillow=PIL.__version__,command='python3 tools/benchmark_ocr.py build --media-root /path/to/Launch-Thread'),cases=cases,
                  limits=['Only installed eng+osd data; benchmark uses eng','Real frames are crops from existing public demo recordings, not new device captures','Derived/synthetic variants explicitly labelled','No physical device, transfer, agent-loop, PhoneUse or FPS comparison'])
    (CORPUS/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(cases=len(cases),images=len({c['image'] for c in cases}),manifest=str(CORPUS/'manifest.json'))))


def token(text):
    text=text.lower()
    while text and not text[0].isalnum():text=text[1:]
    while text and not text[-1].isalnum():text=text[:-1]
    return text


def tokens(text):
    return [v for v in map(token,text.split()) if v]


def occurrences(lines, target):
    wanted=tokens(target);joined=''.join(wanted);count=0
    for line in lines:
        words=tokens(line['text']);at=0
        while at<len(words):
            size=next((n for n in range(1,min(len(wanted)+1,len(words)-at)+1) if ''.join(words[at:at+n])==joined),None)
            if size is None:at+=1
            else:count+=1;at+=size
    return count


def quality(case, result):
    target_counts=Counter(case['targets']);hits={text:min(count,occurrences(result.get('lines',[]),text)) for text,count in target_counts.items()}
    expected_words=Counter(word for target in case['targets'] for word in tokens(target));read_words=Counter(token(w['text']) for w in result.get('words',[]))
    matched=sum((expected_words&read_words).values());expected=sum(expected_words.values())
    target=case.get('primary_target') or case.get('negative_target');matches=result.get('matches',[])
    rects=case.get('primary_rects') or ([case['primary_rect']] if case.get('primary_rect') else [])
    centres=[]
    for match in matches:
        rect=match.get('rect',{});x,y=rect.get('x',-1),rect.get('y',-1)
        centres.append(any(rx<=x<rx+rw and ry<=y<ry+rh for rx,ry,rw,rh in rects))
    return dict(target_hits=sum(hits.values()),target_total=len(case['targets']),target_hits_by_label=hits,
                labelled_word_hits=matched,labelled_word_total=expected,primary_target=target,
                primary_api_matches=len(matches),expected_primary_matches=case.get('expected_primary_matches',1 if case['targets'] else 0),
                primary_centres_in_labelled_region=centres,negative_control=not bool(case['targets']),
                false_positive_words=len(result.get('words',[])) if not case['targets'] else None)


class McpSession:
    """Bounded newline JSON-RPC client; never discovers or opens a device."""
    def __init__(self,cli,env):
        self.process=subprocess.Popen([str(cli),'mcp'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.replies=queue.Queue();self.ident=0
        def read():
            for line in self.process.stdout:
                try:self.replies.put(json.loads(line))
                except ValueError:self.replies.put({'invalid_json':line.decode(errors='replace')})
        threading.Thread(target=read,daemon=True).start()
        threading.Thread(target=lambda:self.process.stderr.read(),daemon=True).start()
        self.request('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='offline-ocr-corpus',version='1')))
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n');self.process.stdin.flush()
    def request(self,method,params):
        self.ident+=1
        self.process.stdin.write((json.dumps(dict(jsonrpc='2.0',id=self.ident,method=method,params=params))+'\n').encode());self.process.stdin.flush()
        while True:
            reply=self.replies.get(timeout=120)
            if reply.get('id')==self.ident:return reply
    def call(self,args):
        start=time.perf_counter_ns();reply=self.request('tools/call',dict(name='screen.ocr',arguments=args));wall=(time.perf_counter_ns()-start)/1e6
        result=reply.get('result',{});text=next((c['text'] for c in result.get('content',[]) if c.get('type')=='text'),'{}');value=json.loads(text)
        if result.get('isError') or 'error' in reply:raise RuntimeError(reply)
        return value,wall,len(text.encode()),len(json.dumps(reply,ensure_ascii=False).encode())
    def close(self):
        try:self.process.stdin.close();self.process.wait(timeout=10)
        except (OSError,subprocess.TimeoutExpired):self.process.kill();self.process.wait()


def run_mcp(args):
    """One persistent server: verify cache reuse, fallback and payload savings."""
    cli=Path(args.cli).resolve();cli_sha=sha(cli);rows=[];failures=[]
    with tempfile.TemporaryDirectory(prefix='tomacco-ocr-mcp-') as tmp:
        home=Path(tmp);(home/'bin').mkdir();(home/'run').mkdir(mode=0o700);traplog=home/'forbidden-tools'
        for name in TRAPS:
            p=home/'bin'/name;p.write_text('#!/bin/sh\nprintf "%s\\n" '+name+' >> '+str(traplog)+'\nexit 97\n');p.chmod(0o700)
        env={**os.environ,'HOME':str(home),'TOMACCO_HOME':str(home/'tomacco'),'XDG_RUNTIME_DIR':str(home/'run'),'XDG_CONFIG_HOME':str(home/'.config'),'XDG_DATA_HOME':str(home/'.local/share'),'XDG_STATE_HOME':str(home/'.local/state'),'XDG_CACHE_HOME':str(home/'.cache'),'PATH':str(home/'bin')+':/usr/bin:/bin','DBUS_SYSTEM_BUS_ADDRESS':'unix:path='+str(home/'no-system-bus'),'DBUS_SESSION_BUS_ADDRESS':'unix:path='+str(home/'no-session-bus'),'TOMACCO_TESSERACT':shutil.which('tesseract') or '/usr/bin/tesseract','TESSDATA_PREFIX':str(args.tessdata.resolve()),'LANG':'C.UTF-8'}
        for key in ('TOMACCO_PARENT_JOB','TOMACCO_TEST_ROOT','TOMACCO_POLICY','TOMACCO_USBMUXD_SOCKET_ADDRESS'):env.pop(key,None)
        session=McpSession(cli,env)
        def read(label,image,target,mode='adaptive',detail='matches'):
            value,wall,text_bytes,envelope_bytes=session.call(dict(image=str(CORPUS/image),language='eng',min_confidence=40,invert=mode,text_contains=target,detail=detail))
            row=dict(label=label,wall_ms=wall,text_payload_utf8_bytes=text_bytes,envelope_utf8_bytes=envelope_bytes,passes=value.get('passes'),engine_runs=value.get('engine_runs'),cached=value.get('cached'),fallback_reason=value.get('fallback_reason'),read_ms=value.get('read_ms'),total_ms=value.get('total_ms'),matches=value.get('matches',[]),word_count=value.get('word_count'),has_full_words='words'in value,has_full_lines='lines'in value)
            rows.append(row);return row
        def expect(condition,why):
            if not condition:failures.append(why)
        try:
            first=read('light-first','light-panel.png','Continue',detail='full')
            compact=read('light-compact-cached','light-panel.png','Continue')
            changed_query=read('light-other-query-cached','light-panel.png','Account settings')
            promoted=read('light-auto-adds-one-inverted-pass','light-panel.png','Continue',mode='auto',detail='full')
            full_cached=read('light-auto-both-cached','light-panel.png','Continue',mode='auto')
            landscape=read('landscape-normal-seed','real-landscape.png','Progress',mode='never',detail='full')
            recovered=read('landscape-target-miss-fallback','real-landscape.png','Progress')
            replay=read('landscape-target-cached','real-landscape.png','Progress')
            duplicate=read('duplicate-targets-remain-ambiguous','duplicate-targets.png','Continue')
            low=read('small-text-exhausted-fallback','small-text.png','Continue')
            expect(first['engine_runs']==1 and first['passes']==1,'Confident light first read should use one engine pass')
            expect(compact['engine_runs']==0 and compact['cached'] is True,'Same compact target should reuse its existing reading')
            expect(compact['matches']==first['matches'],'Compaction must retain identical matches')
            expect(not compact['has_full_words'] and not compact['has_full_lines'],'matches detail should omit full words/lines')
            expect(compact['text_payload_utf8_bytes']<first['text_payload_utf8_bytes'],'Compact result should be smaller')
            expect(changed_query['engine_runs']==0 and len(changed_query['matches'])==1,'Changing a found target should reuse the normal polarity')
            expect(promoted['engine_runs']==1 and promoted['passes']==2,'Explicit auto after normal cache should run only missing inversion')
            expect(full_cached['engine_runs']==0 and full_cached['cached'] is True,'Repeat auto should reuse both polarities')
            expect(not landscape['matches'],'Pinned normal-pass Progress miss should remain represented')
            expect(len(recovered['matches'])==1 and recovered['engine_runs']==1 and recovered['passes']==2,'Adaptive target fallback should recover Progress using only the missing inverted pass')
            expect(recovered['fallback_reason'] is not None,'Fallback should state its reason')
            expect(replay['engine_runs']==0 and replay['cached'] is True,'Recovered target should reuse both existing polarities')
            expect(len(duplicate['matches'])==2,'Adaptive OCR must not silently choose one of two labels')
            expect(low['passes']==2 and not low['matches'],'Fallback must report an unresolved difficult target honestly')
        finally:session.close()
        forbidden=traplog.read_text().splitlines() if traplog.exists() else []
        expect(not forbidden,'Device/host trap was invoked')
    if sha(cli)!=cli_sha:raise SystemExit('CLI changed during MCP probe')
    report=dict(schema='tomacco-ocr-mcp-cache-probe/1',created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),cli=dict(path=str(cli),sha256=cli_sha),rows=rows,failures=failures,passed=not failures,forbidden_tool_calls=len(forbidden),
                compact_payload_reduction=1-compact['text_payload_utf8_bytes']/first['text_payload_utf8_bytes'],
                boundaries='One persistent local MCP server, image-only OCR; times include MCP transport+OCR/cache+JSON, exclude device capture, input and agent reasoning. One sequence is a functional/cache observation, not a3-run performance distribution.')
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(dict(output=str(args.output),passed=not failures,failures=failures,compact_payload_reduction=report['compact_payload_reduction'])))
    if failures:raise SystemExit(1)


def run(args):
    cli=Path(args.cli or shutil.which('tomaccoctl') or ROOT/'build/tomaccoctl').resolve()
    if not cli.is_file():raise SystemExit('Compiled tomaccoctl missing: '+str(cli))
    cli_sha=sha(cli)
    manifest=json.loads((CORPUS/'manifest.json').read_text());cases=manifest['cases']
    if args.cases:cases=[c for c in cases if c['id'] in args.cases.split(',')]
    if not cases:raise SystemExit('No selected cases')
    modes=args.modes.split(',');records=[]
    with tempfile.TemporaryDirectory(prefix='tomacco-ocr-benchmark-') as tmp:
        home=Path(tmp);(home/'bin').mkdir();(home/'run').mkdir(mode=0o700);traplog=home/'forbidden-tools'
        for name in TRAPS:
            p=home/'bin'/name;p.write_text('#!/bin/sh\nprintf "%s\\n" '+name+' >> '+str(traplog)+'\nexit 97\n');p.chmod(0o700)
        env={**os.environ,'HOME':str(home),'TOMACCO_HOME':str(home/'tomacco'),'XDG_RUNTIME_DIR':str(home/'run'),'XDG_CONFIG_HOME':str(home/'.config'),'XDG_DATA_HOME':str(home/'.local/share'),'XDG_STATE_HOME':str(home/'.local/state'),'XDG_CACHE_HOME':str(home/'.cache'),'PATH':str(home/'bin')+':/usr/bin:/bin','DBUS_SYSTEM_BUS_ADDRESS':'unix:path='+str(home/'no-system-bus'),'DBUS_SESSION_BUS_ADDRESS':'unix:path='+str(home/'no-session-bus'),'TOMACCO_TESSERACT':shutil.which('tesseract') or '/usr/bin/tesseract','TESSDATA_PREFIX':str(args.tessdata.resolve()),'LANG':'C.UTF-8'}
        # Each OCR call is image-only. No capability/device discovery is needed.
        for key in ('TOMACCO_PARENT_JOB','TOMACCO_TEST_ROOT','TOMACCO_POLICY','TOMACCO_USBMUXD_SOCKET_ADDRESS'):env.pop(key,None)
        for case in cases:
            image=CORPUS/case['image']
            if sha(image)!=case['sha256']:raise SystemExit('Fixture hash changed: '+str(image))
            for trial in range(args.trials):
                # Alternate order to reduce drift bias between modes.
                for mode in modes if trial%2==0 else list(reversed(modes)):
                    arguments=dict(image=str(image),language='eng',invert=mode,min_confidence=args.min_confidence,max_words=5000)
                    primary=case.get('primary_target') or case.get('negative_target')
                    if primary:arguments['text_contains']=primary
                    if case.get('region'):arguments['region']=case['region']
                    if args.detail!='full':arguments['detail']=args.detail
                    start=time.perf_counter_ns()
                    try:
                        done=subprocess.run([str(cli),'call','screen.ocr',json.dumps(arguments)],env=env,capture_output=True,text=True,timeout=120)
                        elapsed=(time.perf_counter_ns()-start)/1e6;response=json.loads(done.stdout)
                    except (subprocess.TimeoutExpired,json.JSONDecodeError) as error:
                        records.append(dict(case=case['id'],mode=mode,trial=trial,wall_ms=(time.perf_counter_ns()-start)/1e6,error=str(error)));continue
                    record=dict(case=case['id'],mode=mode,trial=trial,wall_ms=elapsed,exit_code=done.returncode)
                    if not response.get('ok'):record['error']=response.get('error',response)
                    else:
                        result=response['result'];record.update(quality=quality(case,result),passes=result.get('passes'),cached=result.get('cached'),engine_runs=result.get('engine_runs'),fallback_reason=result.get('fallback_reason'),reported_seconds=result.get('seconds'),read_ms=result.get('read_ms'),capture_ms=result.get('capture_ms'),total_ms=result.get('total_ms'),words=result.get('words',[]),lines=result.get('lines',[]),matches=result.get('matches',[]))
                    records.append(record)
            print(case['id'],flush=True)
        forbidden=traplog.read_text().splitlines() if traplog.exists() else []
        if forbidden:raise SystemExit('Forbidden tool called: '+str(forbidden))
    summaries=[]
    for case in cases:
        for mode in modes:
            rows=[r for r in records if r['case']==case['id'] and r['mode']==mode];good=[r for r in rows if 'error' not in r]
            summary=dict(case=case['id'],mode=mode,trials=len(rows),errors=len(rows)-len(good),wall_median_ms=round(statistics.median(r['wall_ms'] for r in rows),3))
            if good:
                summary.update(target_recall=statistics.mean(r['quality']['target_hits']/r['quality']['target_total'] for r in good) if case['targets'] else None,
                               labelled_word_recall=statistics.mean(r['quality']['labelled_word_hits']/r['quality']['labelled_word_total'] for r in good) if case['targets'] else None,
                               primary_hits=[r['quality']['primary_api_matches'] for r in good],passes=[r['passes'] for r in good],
                               false_positive_words=[r['quality']['false_positive_words'] for r in good] if not case['targets'] else None)
            summaries.append(summary)
    if sha(cli)!=cli_sha:raise SystemExit('CLI changed during benchmark; rerun against a stable binary')
    report=dict(schema='tomacco-ocr-benchmark/1',created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                cli=dict(path=str(cli),sha256=cli_sha),tesseract=subprocess.check_output(['tesseract','--version'],text=True,env=env).splitlines()[0],
                installed_languages=subprocess.check_output(['tesseract','--list-langs'],text=True,env=env).splitlines()[1:],
                tessdata=dict(path=str(args.tessdata.resolve()),eng_sha256=sha(args.tessdata/'eng.traineddata')),
                host=dict(platform=platform.platform(),machine=platform.machine(),cpu_count=os.cpu_count()),
                trials_per_case_mode=args.trials,modes=modes,min_confidence=args.min_confidence,detail=args.detail,
                corpus_manifest_sha256=sha(CORPUS/'manifest.json'),corpus_cases=len(cases),forbidden_tool_calls=0,
                boundaries=dict(measured='Process-cold one-shot JSON CLI image OCR wall time: startup/guard+PNG I/O/decode+Tesseract+merge+JSON; OS page caches are not flushed',not_measured=['Device screenshot/transfer','MCP/persistent cache','Tap/action latency','Agent reasoning','FPS','PhoneUse or any competitor']),
                label_scoring='Case-insensitive edge-punctuation-normalized exact phrase/space-folded target recall; labelled-word multiset recall. Labels are a subset of visible text, not a precision/CER claim.',summaries=summaries,trials=records)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    errors=sum('error'in r for r in records)
    print(json.dumps(dict(output=str(args.output),calls=len(records),errors=errors,forbidden_tools=0)))
    if errors:raise SystemExit(1)


def main():
    global CORPUS
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    build=sub.add_parser('build');build.add_argument('--media-root',type=Path,required=True)
    bench=sub.add_parser('run');bench.add_argument('--cli');bench.add_argument('--trials',type=int,default=3);bench.add_argument('--modes',default='auto,never');bench.add_argument('--cases');bench.add_argument('--detail',choices=['full','matches'],default='full');bench.add_argument('--min-confidence',type=float,default=40);bench.add_argument('--tessdata',type=Path,default=Path('/usr/share/tessdata'));bench.add_argument('--corpus',type=Path,help='Use an explicit labelled corpus directory');bench.add_argument('--output',type=Path,default=ROOT/'qa/ocr-fast/result.json')
    mcp=sub.add_parser('mcp');mcp.add_argument('--cli',required=True);mcp.add_argument('--tessdata',type=Path,default=Path('/usr/share/tessdata'));mcp.add_argument('--corpus',type=Path,help='Use an explicit labelled corpus directory');mcp.add_argument('--output',type=Path,default=ROOT/'qa/ocr-fast/mcp-cache.json')
    args=parser.parse_args()
    if getattr(args,'corpus',None) is not None:CORPUS=args.corpus.resolve()
    if args.command=='build':build_corpus(args.media_root)
    elif args.command=='mcp':run_mcp(args)
    else:
        if args.trials<1:parser.error('--trials must be positive')
        run(args)

if __name__=='__main__':main()
