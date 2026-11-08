"""Demo Studio public CLI/job regressions. Only local generated media and mocks.

Run with TOMACCOCTL pointing at the compiled native CLI. Each test uses a
private HOME, TOMACCO_HOME and sibling renderer; no device tool is executed.
"""
import concurrent.futures
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]

RENDERER = r'''
import json, math, pathlib, subprocess, sys, time
args=dict(zip(sys.argv[1::2],sys.argv[2::2])); scene=json.loads(pathlib.Path(args['--scene']).read_text()); output=pathlib.Path(args['--output'])
root=pathlib.Path(__file__).parent.parent; mode=(root/'mode').read_text().strip()
(root/'renderer-started').write_text('yes')
if mode=='sleep': time.sleep(30)
if mode=='gate':
 deadline=time.monotonic()+20
 while not (root/'renderer-release').exists():
  if time.monotonic()>deadline:sys.exit('render gate timed out')
  time.sleep(.02)
if mode=='published-wait':
 request=json.loads((output.parent/'request.json').read_text()); entries=[]
 for source,target in [(pathlib.Path(str(output)+'.poster.png'),pathlib.Path(request['poster'])),(output.parent/'checked-manifest.json',pathlib.Path(request['manifest']))]:
  source.write_bytes(b'owned incomplete publication');stat=source.stat();entries.append(dict(source=str(source),destination=str(target),device=stat.st_dev,inode=stat.st_ino))
 (output.parent/'publication.json').write_text(json.dumps(entries))
 for entry in entries: pathlib.Path(entry['destination']).hardlink_to(entry['source'])
 (root/'publication-started').write_text('yes');time.sleep(30)
if mode=='fail': print('intentional renderer failure',file=sys.stderr);sys.exit(31)
if mode=='corrupt': output.write_bytes(b'not rendered media');sys.exit(0)
if mode=='change-source': (root/'source.mp4').write_bytes(b'changed after private source preparation')
w=scene['output']['width']+(2 if mode=='wrong-dimensions' else 0);h=scene['output']['height'];fps=scene['output']['fps'];duration=scene['output']['duration'];frames=math.ceil(fps*duration)
audio=scene.get('audio',{}).get('mode','muted')=='source'
if mode=='unexpected-audio':audio=True
if mode=='missing-audio':audio=False
def run(tail):
 subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i',f'color=c=blue:s={w}x{h}:r={fps}',*tail],check=True)
if '--time' in args:
 run(['-frames:v','1','-threads','1',str(output)]);sys.exit(0)
if audio:
 args=['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i',f'color=c=blue:s={w}x{h}:r={fps}','-f','lavfi','-i',f'sine=frequency=733:sample_rate=48000:duration={frames/fps}','-map','0:v:0','-map','1:a:0']
 if mode=='multiple-audio':args+=['-map','1:a:0']
 args+=['-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','libmp3lame' if mode=='wrong-audio-codec' else 'aac','-ac','2','-ar','48000','-t',str(frames/fps),str(output)]
 subprocess.run(args,check=True)
 if mode=='corrupt-audio':
  packets=json.loads(subprocess.check_output(['/usr/bin/ffprobe','-v','error','-select_streams','a:0','-show_packets','-of','json',str(output)]))['packets']
  packet=packets[len(packets)//2];data=bytearray(output.read_bytes());offset=int(packet['pos']);size=int(packet['size']);data[offset:offset+size]=b'\xff'*size;output.write_bytes(data)
else:run(['-frames:v',str(frames),'-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(output)])
poster=pathlib.Path(str(output)+'.poster.png');run(['-frames:v','1','-threads','1',str(poster)])
manifest=pathlib.Path(str(output)+'.manifest.json');record=dict(format='tomacco.demo.render',version=1,frames=frames,width=w,height=h,fps=fps,duration=frames/fps,requested_duration=duration,silent=not audio)
if audio:
 record['audio']=dict(mode='source',codec='aac',sample_rate=48000,channels=2)
 record['source_audio_origin']=scene['clip']['start']
if mode=='manifest-lies':record['silent']=True
manifest.write_text(json.dumps(record))
print(json.dumps(dict(output=str(output),poster=str(poster),manifest=str(manifest),frames=frames)))
'''


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'local ffmpeg/ffprobe required')
class DemoBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='tomacco-demo-test-')
        self.root=Path(self.temp.name); (self.root/'bin').mkdir(); (self.root/'home').mkdir()
        cli=Path(os.environ.get('TOMACCOCTL',ROOT/'tomaccoctl')).resolve()
        self.binary=self.root/'bin/tomaccoctl'
        try: os.link(cli,self.binary)
        except OSError: shutil.copy2(cli,self.binary)
        helper=self.root/'bin/tomacco-demo-render';helper.write_text('#!'+sys.executable+'\n'+RENDERER);helper.chmod(0o700)
        for name in ('adb','fastboot','ssh','ideviceinfo','xtool','uxplay'):
            p=self.root/'bin'/name;p.write_text('#!'+sys.executable+'\nimport pathlib,sys\npathlib.Path('+repr(str(self.root/'FORBIDDEN-TOOL'))+').write_text(sys.argv[0]);sys.exit(99)\n');p.chmod(0o700)
        self.env={**os.environ,'HOME':str(self.root/'home'),'XDG_CONFIG_HOME':str(self.root/'home/.config'),'XDG_DATA_HOME':str(self.root/'home/.local/share'),'TOMACCO_HOME':str(self.root/'state'),'PATH':str(self.root/'bin')+':/usr/bin:/bin'}
        for key in ('TOMACCO_PARENT_JOB','TOMACCO_TEST_ROOT','TOMACCO_POLICY','TOMACCO_USBMUXD_SOCKET_ADDRESS'):
            self.env.pop(key,None)
        (self.root/'mode').write_text('success')
        self.source=self.root/'source.mp4';self.scene=self.root/'scene.json'
        subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=s=320x640:r=24:d=1','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(self.source)],env=self.env,check=True,capture_output=True)
        self.jobs=[]

    def tearDown(self):
        for job in self.jobs:
            current=self.call('job.get',{'job_id':job['id']})
            if current['state'] not in ('succeeded','failed','cancelled','timed_out'):
                self.call('job.cancel',{'job_id':job['id']});self.wait(job)
        self.assertFalse((self.root/'FORBIDDEN-TOOL').exists(),'device tool must never run')
        self.temp.cleanup()

    def raw(self,op,args=None):
        p=subprocess.run([str(self.binary),'call',op,json.dumps(args or {})],env=self.env,capture_output=True,text=True,timeout=45)
        self.assertTrue(p.stdout,(p.returncode,p.stderr));return json.loads(p.stdout)

    def call(self,op,args=None):
        value=self.raw(op,args);self.assertTrue(value['ok'],value);return value['result']

    def error(self,op,args,code):
        value=self.raw(op,args);self.assertFalse(value['ok'],value);self.assertEqual(value['error']['code'],code,value)

    def create(self):
        result=self.call('demo.scene.create',{'path':str(self.scene),'source':str(self.source),'preset':'portrait-orbit'})
        return self.call('demo.scene.update',{'path':str(self.scene),'if_revision':result['revision'],'changes':{'output':{'width':320,'height':320,'duration':.2},'keyframes':[{'time':0,'yaw':0,'pitch':0,'roll':0,'scale':.72,'x':.5,'y':.5,'easing':'linear'}]}})

    def create_with_audio(self):
        subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=s=320x640:r=24:d=1','-f','lavfi','-i','sine=frequency=557:sample_rate=48000:duration=1','-map','0:v:0','-map','1:a:0','-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac','-y',str(self.source)],env=self.env,check=True,capture_output=True)
        created=self.create()
        self.assertTrue(created['scene']['source']['has_audio'])
        return self.call('demo.scene.update',{'path':str(self.scene),'if_revision':created['revision'],'changes':{'audio':{'mode':'source'}}})

    def render(self,preview=False,**extra):
        args={'path':str(self.scene),'if_revision':2,'output':str(self.root/('preview.png' if preview else 'video.mp4')),**extra}
        if preview:args.setdefault('time',.1)
        job=self.call('demo.preview' if preview else 'demo.export',args);self.jobs.append(job);return job

    def wait(self,job):return self.call('job.wait',{'job_id':job['id'],'seconds':30})

    def test_local_discovery_inspection_and_optimistic_atomic_updates(self):
        caps=self.call('demo.capabilities');self.assertTrue(caps['ready']);self.assertEqual(len(caps['presets']),4)
        info=self.call('demo.media.inspect',{'path':str(self.source)});self.assertEqual((info['width'],info['height']),(320,640));self.assertEqual(len(info['sha256']),64)
        scene=self.create();self.assertEqual(scene['revision'],2)
        before=self.scene.read_bytes();self.error('demo.scene.update',{'path':str(self.scene),'if_revision':1,'changes':{'title':'stale'}},'demo_conflict');self.assertEqual(self.scene.read_bytes(),before)
        self.error('demo.scene.create',{'path':str(self.scene),'source':str(self.source)},'demo_conflict')
        self.error('demo.scene.update',{'path':str(self.scene),'if_revision':2,'changes':{'source':{'path':'/other'}}},'invalid_argument')
        loaded=self.call('demo.scene.get',{'path':str(self.scene)});self.assertEqual(loaded,scene)
        self.assertEqual(self.scene.stat().st_mode&0o777,0o600)

    def test_readiness_refuses_ffmpeg_without_h264_encoder(self):
        fake=self.root/'bin/ffmpeg';fake.write_text('#!'+sys.executable+'\nprint("Encoders: no H.264 encoder")\n');fake.chmod(0o700)
        caps=self.call('demo.capabilities');self.assertFalse(caps['ready']);self.assertFalse(caps['h264_encoder']['available']);self.assertIn('ffmpeg encoder libx264',caps['missing'])

    def test_missing_aac_refuses_source_audio_but_keeps_silent_exports_ready(self):
        scene=self.create_with_audio()
        fake=self.root/'bin/ffmpeg';fake.write_text('#!'+sys.executable+'\nimport os,sys\nif "-encoders" in sys.argv:\n print(" V..... libx264 H.264 encoder");sys.exit(0)\nos.execv("/usr/bin/ffmpeg",["ffmpeg",*sys.argv[1:]])\n');fake.chmod(0o700)
        caps=self.call('demo.capabilities');self.assertTrue(caps['ready']);self.assertFalse(caps['audio']['source_available'])
        self.error('demo.export',{'path':str(self.scene),'if_revision':scene['revision'],'output':str(self.root/'video.mp4')},'demo_tool_missing')
        muted=self.call('demo.scene.update',{'path':str(self.scene),'if_revision':scene['revision'],'changes':{'audio':{'mode':'muted'}}})
        job=self.render(if_revision=muted['revision']);self.assertEqual(self.wait(job)['state'],'succeeded')
        self.assertFalse(self.call('demo.export.inspect',{'path':str(self.root/'video.mp4')})['has_audio'])

    def test_concurrent_revision_writers_have_one_winner(self):
        self.create()
        def write(title):return self.raw('demo.scene.update',{'path':str(self.scene),'if_revision':2,'changes':{'title':title}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,['first','second']))
        self.assertEqual(sum(x['ok'] for x in results),1);self.assertEqual(next(x for x in results if not x['ok'])['error']['code'],'demo_conflict')
        self.assertEqual(self.call('demo.scene.get',{'path':str(self.scene)})['revision'],3)

    def test_preview_and_export_require_actual_decodable_matching_artifacts(self):
        self.create();preview=self.render(True);self.assertEqual(self.wait(preview)['state'],'succeeded')
        self.assertTrue((self.root/'preview.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        job=self.render(request_id='one');final=self.wait(job);self.assertEqual(final['state'],'succeeded',self.call('job.logs',{'job_id':job['id']}))
        inspected=self.call('demo.export.inspect',{'path':str(self.root/'video.mp4')});self.assertEqual(inspected['frames'],6);self.assertEqual(inspected['width'],320);self.assertTrue(inspected['decoded']);self.assertFalse(inspected['has_audio'])
        self.assertTrue((self.root/'video.mp4.poster.png').exists());self.assertTrue((self.root/'video.mp4.manifest.json').exists());self.assertGreaterEqual(len(final['result']['artifacts']),4)
        same=self.render(request_id='one');self.assertEqual(same['id'],job['id'])
        self.error('demo.export',{'path':str(self.scene),'if_revision':2,'output':str(self.root/'another.mp4'),'request_id':'one'},'demo_conflict')
        self.error('demo.export',{'path':str(self.scene),'if_revision':2,'output':str(self.root/'video.mp4'),'request_id':'new'},'demo_conflict')

    def test_fractional_duration_uses_ceil_and_preview_endpoint_is_last_frame(self):
        self.create()
        result=self.call('demo.scene.update',{'path':str(self.scene),'if_revision':2,'changes':{'output':{'duration':.21}}})
        job=self.render(if_revision=result['revision']);self.assertEqual(self.wait(job)['state'],'succeeded')
        inspected=self.call('demo.export.inspect',{'path':str(self.root/'video.mp4')})
        self.assertEqual(inspected['frames'],7);self.assertAlmostEqual(inspected['duration'],7/30,places=5)
        preview=self.render(True,if_revision=result['revision'],time=.21);final=self.wait(preview);self.assertEqual(final['state'],'succeeded')
        report=next(a for a in final['result']['artifacts'] if a['path'].endswith('validation.json'))
        validation=json.loads(Path(report['path']).read_text());self.assertEqual(validation['frame_index'],6)

    def test_renderer_failure_is_durable_failed_job_without_final_output(self):
        self.create();(self.root/'mode').write_text('fail');job=self.render();final=self.wait(job)
        self.assertEqual(final['state'],'failed');self.assertFalse((self.root/'video.mp4').exists());self.assertIn('intentional renderer failure',self.call('job.logs',{'job_id':job['id']})['text'])

    def test_zero_exit_corrupt_renderer_never_becomes_success(self):
        self.create();(self.root/'mode').write_text('corrupt');job=self.render(True)
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'preview.png').exists())

    def test_wrong_encoded_dimensions_are_rejected_after_zero_exit(self):
        self.create();(self.root/'mode').write_text('wrong-dimensions');job=self.render()
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'video.mp4').exists())

    def test_source_audio_is_validated_independently_of_renderer_claims(self):
        scene=self.create_with_audio();job=self.render(if_revision=scene['revision']);final=self.wait(job)
        self.assertEqual(final['state'],'succeeded',self.call('job.logs',{'job_id':job['id']}))
        inspected=self.call('demo.export.inspect',{'path':str(self.root/'video.mp4')})
        self.assertTrue(inspected['has_audio']);self.assertTrue(inspected['decoded'])
        manifest=json.loads((self.root/'video.mp4.manifest.json').read_text())
        self.assertFalse(manifest['validation']['silent']);self.assertEqual(manifest['scene']['audio']['mode'],'source')

    def test_missing_source_audio_is_rejected_after_zero_exit(self):
        scene=self.create_with_audio();(self.root/'mode').write_text('missing-audio');job=self.render(if_revision=scene['revision'])
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'video.mp4').exists())

    def test_unexpected_audio_in_muted_export_is_rejected(self):
        self.create();(self.root/'mode').write_text('unexpected-audio');job=self.render()
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'video.mp4').exists())

    def test_multiple_audio_streams_are_rejected(self):
        scene=self.create_with_audio();(self.root/'mode').write_text('multiple-audio');job=self.render(if_revision=scene['revision'])
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'video.mp4').exists())

    def test_wrong_audio_codec_is_rejected(self):
        scene=self.create_with_audio();(self.root/'mode').write_text('wrong-audio-codec');job=self.render(if_revision=scene['revision'])
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'video.mp4').exists())

    def test_corrupt_audio_packet_cannot_pass_video_only_decode(self):
        scene=self.create_with_audio();(self.root/'mode').write_text('corrupt-audio');job=self.render(if_revision=scene['revision'])
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'video.mp4').exists())

    def test_renderer_manifest_silent_claim_must_match_decoded_audio(self):
        scene=self.create_with_audio();(self.root/'mode').write_text('manifest-lies');job=self.render(if_revision=scene['revision'])
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'video.mp4').exists())

    def test_audio_mode_is_frozen_with_the_submitted_scene_revision(self):
        scene=self.create_with_audio();(self.root/'mode').write_text('gate');job=self.render(if_revision=scene['revision']);deadline=time.monotonic()+10
        while not (self.root/'renderer-started').exists():
            self.assertLess(time.monotonic(),deadline);time.sleep(.03)
        updated=self.call('demo.scene.update',{'path':str(self.scene),'if_revision':scene['revision'],'changes':{'audio':{'mode':'muted'}}})
        (self.root/'renderer-release').write_text('continue')
        self.assertEqual(self.wait(job)['state'],'succeeded',self.call('job.logs',{'job_id':job['id']}))
        manifest=json.loads((self.root/'video.mp4.manifest.json').read_text())
        self.assertEqual(manifest['scene']['revision'],scene['revision']);self.assertEqual(manifest['scene']['audio']['mode'],'source')
        self.assertTrue(self.call('demo.export.inspect',{'path':str(self.root/'video.mp4')})['has_audio'])
        later=self.render(if_revision=updated['revision'],output=str(self.root/'muted.mp4'))
        self.assertEqual(self.wait(later)['state'],'succeeded')
        self.assertFalse(self.call('demo.export.inspect',{'path':str(self.root/'muted.mp4')})['has_audio'])

    def test_export_inspection_refuses_sidecar_audio_contradictions(self):
        scene=self.create_with_audio();job=self.render(if_revision=scene['revision'])
        self.assertEqual(self.wait(job)['state'],'succeeded')
        sidecar=self.root/'video.mp4.manifest.json';original=sidecar.read_bytes();value=json.loads(original)
        for field in ('media','renderer','audio','scene'):
            with self.subTest(field=field):
                changed=json.loads(original)
                if field=='media':changed['validation']['media']['has_audio']=False
                elif field=='renderer':changed['validation']['renderer']['silent']=True
                elif field=='audio':changed['validation']['audio']={'mode':'muted'}
                else:changed['scene']['audio']={'mode':'muted'}
                sidecar.write_text(json.dumps(changed))
                self.error('demo.export.inspect',{'path':str(self.root/'video.mp4')},'demo_output_invalid')
        sidecar.write_bytes(original)
        self.assertTrue(self.call('demo.export.inspect',{'path':str(self.root/'video.mp4')})['has_audio'])

    def test_changed_source_is_rejected_before_and_during_render(self):
        self.create();(self.root/'mode').write_text('change-source');job=self.render()
        self.assertEqual(self.wait(job)['state'],'failed');self.assertFalse((self.root/'video.mp4').exists())
        self.error('demo.scene.get',{'path':str(self.scene)},'demo_source_changed')

    def test_existing_sidecar_and_symlink_are_preserved(self):
        self.create();existing=self.root/'video.mp4.poster.png';existing.write_bytes(b'owned old poster')
        self.error('demo.export',{'path':str(self.scene),'if_revision':2,'output':str(self.root/'video.mp4')},'demo_conflict');self.assertEqual(existing.read_bytes(),b'owned old poster')
        (self.root/'preview.png').symlink_to(existing)
        self.error('demo.preview',{'path':str(self.scene),'if_revision':2,'time':0,'output':str(self.root/'preview.png')},'invalid_path');self.assertEqual(existing.read_bytes(),b'owned old poster')

    def test_cancel_cleans_owned_partial_publication_before_terminal_state(self):
        self.create();(self.root/'mode').write_text('published-wait');job=self.render();deadline=time.monotonic()+10
        while not (self.root/'publication-started').exists():
            self.assertLess(time.monotonic(),deadline);time.sleep(.03)
        self.assertTrue((self.root/'video.mp4.poster.png').exists())
        self.call('job.cancel',{'job_id':job['id']});self.assertEqual(self.wait(job)['state'],'cancelled')
        for name in ('video.mp4','video.mp4.poster.png','video.mp4.manifest.json'):self.assertFalse((self.root/name).exists(),name)

    def test_cancel_terminates_own_job_without_publishing_output(self):
        self.create();(self.root/'mode').write_text('sleep');job=self.render();deadline=time.monotonic()+10
        while not (self.root/'renderer-started').exists():
            self.assertLess(time.monotonic(),deadline);time.sleep(.03)
        self.call('job.cancel',{'job_id':job['id']});self.assertEqual(self.wait(job)['state'],'cancelled');self.assertFalse((self.root/'video.mp4').exists())


if __name__=='__main__':unittest.main()
