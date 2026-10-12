import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_worker import analyze, export_video, clip_parts, AnalysisProfile, opencv_frames, probe_sources, recording_date
from analysis_frames import ffmpeg_frames, prepare_proxy, sample_indices, proxy_frames


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.files = []
        for part in range(2):
            raw = self.root/f'raw{part}.avi'
            writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*'MJPG'), 15, (320, 180))
            for local in range(90):
                i = part*90+local
                frame = np.full((180, 320, 3), 35, np.uint8)
                if 65 <= i < 115 or 145 <= i < 175:
                    x = 20 + (i*4)%230
                    cv2.rectangle(frame, (x, 65), (x+40, 115), (230, 230, 230), -1)
                writer.write(frame)
            writer.release()
            target = self.root/f'source{part}.mp4'
            command = ['ffmpeg','-y','-v','error','-i',str(raw)]
            if part == 0:
                command += ['-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=6','-c:a','aac']
            command += ['-c:v','libx264','-pix_fmt','yuv420p',str(target)]
            subprocess.run(command,check=True)
            self.files.append(dict(id=f'source-{part}',name=target.name,path=str(target)))
        self.request = dict(id='project',name='Test project',revision=1,run_id='test-analysis',files=self.files, decoder='opencv', preview=True,
                            settings={'width':960,'samples-per-second':0,'step':1,'min-area':80,'min-duration':.3,'min-frames':2,'gap':.2})

    def tearDown(self):
        self.tmp.cleanup()

    def test_continuous_motion_across_file_seam_and_export_audio(self):
        result = analyze(self.request, self.root/'run')
        self.assertEqual(result['duration'], 12)
        self.assertEqual(result['sources'][1]['start'], 6)
        self.assertEqual(result['segment_count'], 2)
        first, second = result['segments']
        self.assertLess(first['start'], 6)
        self.assertGreater(first['end'], 6)
        self.assertEqual(first['source_ids'], ['source-0', 'source-1'])
        data = json.loads((self.root/'run/events.json').read_text())
        self.assertTrue(any(s['source_id']=='source-1' and 6 <= s['timestamp'] < 6.2 for s in data['samples']))
        self.assertFalse(any('path' in s for s in data['sources']))
        # Deliberately reverse the selected event order. Second source has no audio.
        clips = [dict(segment_id=second['id'],start=10,end=11),dict(segment_id=first['id'],start=5,end=7)]
        export = export_video(dict(project_id='project',analysis_dir=str(self.root/'run'),files=self.files,clips=clips,audio=True,boxes=False),self.root/'export')
        output = self.root/'export/highlights.mp4'
        info = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(output)]))
        self.assertLess(abs(float(info['format']['duration'])-3),.1)
        self.assertEqual([s['codec_type'] for s in info['streams']],['video','audio'])
        self.assertEqual(export['clips'],clips)
        audio = subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-vn','-ac','1','-ar','48000','-f','f32le','-'])
        waveform = np.frombuffer(audio,np.float32)
        rms = lambda start,end: np.sqrt(np.mean(waveform[int(start*48000):int(end*48000)]**2))
        self.assertLess(rms(.2,.8),.001) # selected second-source clip comes first, silent
        self.assertGreater(rms(1.2,1.8),.02) # original first-source audio retained
        self.assertLess(rms(2.2,2.8),.001) # cross-file tail uses silence, no repeated audio
        # Clean output must not contain the green motion-box pixels from the preview.
        cap = cv2.VideoCapture(str(output))
        cap.set(cv2.CAP_PROP_POS_MSEC,1500)
        ok,frame = cap.read()
        cap.release()
        self.assertTrue(ok)
        self.assertLess(np.max(frame[:,:,1].astype(int)-frame[:,:,0].astype(int)),25)

    def test_mixed_rates_and_sizes_share_one_clock(self):
        # Replace the second source with a different resolution/FPS of equal duration.
        replacement = self.root/'mixed.mp4'
        subprocess.run(['ffmpeg','-y','-v','error','-i',self.files[1]['path'],'-vf','scale=480:270,fps=24',str(replacement)],check=True)
        self.files[1]['path']=str(replacement)
        result=analyze(self.request,self.root/'mixed-run')
        self.assertAlmostEqual(result['duration'],12)
        self.assertEqual(result['sources'][1]['fps'],24)
        cap=cv2.VideoCapture(str(self.root/'mixed-run/preview.mp4'))
        preview_duration=cap.get(cv2.CAP_PROP_FRAME_COUNT)/cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        self.assertLess(abs(preview_duration-12),result['sample_interval']+.001)
        with self.assertRaises(ValueError):
            clip_parts({'start':-1,'end':2},result['sources'])
        with self.assertRaises(ValueError):
            clip_parts({'start':2,'end':13},result['sources'])

    def test_no_preview_preserves_detections_and_profiles_work(self):
        baseline = analyze(self.request, self.root/'baseline')
        self.request['preview'] = False
        candidate = analyze(self.request, self.root/'no-preview')
        self.assertEqual(baseline['segments'], candidate['segments'])
        original = json.loads((self.root/'baseline/events.json').read_text())
        changed = json.loads((self.root/'no-preview/events.json').read_text())
        self.assertEqual(original['samples'], changed['samples'])
        self.assertFalse((self.root/'no-preview/preview.mp4').exists())
        profile = json.loads((self.root/'no-preview/profile.json').read_text())
        self.assertEqual(profile['source_frames'], 180)
        self.assertEqual(profile['analysis_samples'], 180)
        self.assertGreater(profile['stages_seconds']['mog2'], 0)
        self.assertNotIn('preview_pipe', profile['stages_seconds'])
        event = candidate['segments'][0]
        with self.assertRaisesRegex(ValueError, 'requires an analysis preview'):
            export_video(dict(project_id='project', analysis_dir=str(self.root/'no-preview'),
                              files=self.files, boxes=True,
                              clips=[dict(segment_id=event['id'], start=event['start'], end=event['end'])]),
                         self.root/'export-no-preview')

    def test_resolution_and_rate_preserve_area_and_positive_time(self):
        self.request['preview'] = False
        self.request['settings'].update({'width':160, 'step':5, 'samples-per-second':2,
                                         'min-frames':4})
        result = analyze(self.request, self.root/'smaller')
        self.assertEqual(result['out_size'], [160, 90])
        self.assertEqual(result['effective_min_area'], 20)
        self.assertEqual(result['effective_min_frames'], 3)
        self.assertAlmostEqual(result['minimum_positive_seconds'], 4/3)
        self.assertEqual(result['warmup_seconds'], 2)
        self.assertTrue(result['segments'])

    def test_ffmpeg_sampler_matches_clock_across_mixed_source_boundary(self):
        replacement = self.root/'mixed.mp4'
        subprocess.run(['ffmpeg','-y','-v','error','-i',self.files[1]['path'],
                        '-vf','scale=480:270,fps=24',str(replacement)],check=True)
        self.files[1]['path'] = str(replacement)
        self.request['preview'] = False
        self.request['settings'].update({'step':5, 'samples-per-second':3})
        reference = analyze(self.request, self.root/'opencv')
        self.request['decoder'] = 'ffmpeg'
        result = analyze(self.request, self.root/'ffmpeg')
        self.assertEqual(reference['segment_count'], result['segment_count'])
        ref = json.loads((self.root/'opencv/events.json').read_text())
        test = json.loads((self.root/'ffmpeg/events.json').read_text())
        clock = lambda d: [(s['timestamp'], s['source_id'], s['source_frame']) for s in d['samples']]
        self.assertEqual(clock(ref), clock(test))
        self.assertEqual(json.loads((self.root/'ffmpeg/profile.json').read_text())['analysis_samples'], 36)

    def test_ffmpeg_frame_selection_fractional_phase_and_oversampling(self):
        source = probe_sources(self.files[:1])[0]
        source.update(start=6.1, end=12.1)
        for rate in (2.7, 20):
            interval = 1/rate
            import math
            first = math.ceil(source['start']/interval - 1e-9)
            ref = [idx for idx, frame in opencv_frames(source, interval, first, AnalysisProfile())]
            result = list(ffmpeg_frames(source, interval, 160, 90, AnalysisProfile()))
            self.assertEqual(ref, [idx for idx, frame in result])
            self.assertTrue(all(frame.shape == (90,160,3) for idx, frame in result))

    def test_thread_setting_restored_and_invalid_options_rejected(self):
        before = cv2.getNumThreads()
        reference = analyze(dict(self.request, preview=False, opencv_threads=32), self.root/'threads-reference')
        self.request.update(preview=False, opencv_threads=1)
        candidate = analyze(self.request, self.root/'threads')
        self.assertEqual(reference['segments'], candidate['segments'])
        self.assertEqual(json.loads((self.root/'threads-reference/events.json').read_text())['samples'],
                         json.loads((self.root/'threads/events.json').read_text())['samples'])
        self.assertEqual(cv2.getNumThreads(), before)
        self.assertEqual(json.loads((self.root/'threads/profile.json').read_text())['opencv_threads'], 1)
        for setting in ({'width':0}, {'samples-per-second':-1}, {'width':640.5}):
            request = dict(self.request, settings=setting)
            with self.assertRaises(ValueError):
                analyze(request, self.root/'invalid')
            self.assertEqual(cv2.getNumThreads(), before)

    def test_fast_defaults_cache_and_original_clock(self):
        request = {k:v for k,v in self.request.items() if k not in ('decoder', 'preview', 'settings')}
        request.update(proxy_cache=str(self.root/'cache'), acceleration='cpu')
        first = analyze(request, self.root/'fast')
        self.assertEqual(first['out_size'], [256, 144])
        self.assertEqual(first['sample_interval'], .5)
        self.assertFalse(first['preview_available'])
        self.assertFalse((self.root/'fast/preview.mp4').exists())
        self.assertEqual(first['sources'][0]['width'], 320)
        self.assertEqual(len(first['proxies']), 2)
        self.assertTrue(all(not p['cached'] for p in first['proxies']))
        self.assertTrue(first['segments'])
        for sample in json.loads((self.root/'fast/events.json').read_text())['samples']:
            self.assertEqual(sample['source_frame'], int(sample['source_time']*15))
        again = analyze(request, self.root/'cached')
        self.assertTrue(all(p['cached'] for p in again['proxies']))
        self.assertEqual(first['segments'], again['segments'])
        profile = json.loads((self.root/'cached/profile.json').read_text())
        self.assertEqual(profile['source_frames'], 0)
        self.assertEqual(profile['analysis_samples'], 24)
        request['settings'] = {'min-area':200}
        tuned = analyze(request, self.root/'tuned')
        self.assertTrue(all(p['cached'] for p in tuned['proxies']))
        # Corrupt cache entries must be regenerated, not mistaken for a completed pass.
        cached = next((self.root/'cache').glob('*.mp4'))
        cached.write_bytes(b'broken')
        repaired = analyze(request, self.root/'repaired')
        self.assertEqual(sum(p['cached'] for p in repaired['proxies']), 1)

    def test_proxy_mixed_clock_and_gpu_fallback(self):
        source = probe_sources(self.files[:1])[0]
        source.update(start=6.1, end=12.1)
        # A missing hardware backend exercises the real probe/fallback path.
        with patch('analysis_frames.hardware_backends', return_value=['vaapi:/dev/motionmeasure-missing']):
            for rate in (2.7, 20):
                proxy = prepare_proxy(source, 1/rate, 256, 144, self.root/'proxy-clock', lambda *args:None)
                self.assertEqual(proxy['backend'], 'cpu')
                frames = list(proxy_frames(source, 1/rate, proxy, AnalysisProfile()))
                self.assertEqual([i for i,f in frames], list(sample_indices(source, 1/rate)))
                self.assertTrue(all(f.shape == (144,256,3) for i,f in frames))
        # A source shorter than the interval can contain no sample at this phase.
        source.update(start=6.1, end=6.2)
        proxy = prepare_proxy(source, 1, 256, 144, self.root/'empty', lambda *args:None)
        self.assertEqual(list(proxy_frames(source, 1, proxy, AnalysisProfile())), [])

    def test_recording_dates_and_overnight_motion_separation(self):
        file = dict(self.files[0], name='camera_20261008_123456.mp4')
        self.assertEqual(recording_date(file), ('2026-10-08T12:34:56','filename'))
        file['recording_start'] = '2026-10-09T10:00:00'
        self.assertEqual(recording_date(file), (file['recording_start'],'manual'))
        self.files[0]['recording_start'] = '2026-10-08T09:00:00'
        self.files[1]['recording_start'] = '2026-10-09T09:00:00'
        result = analyze(self.request, self.root/'overnight')
        self.assertTrue(all(len(s['source_ids']) == 1 for s in result['segments']))
        self.assertEqual(result['sources'][1]['recording_start'], '2026-10-09T09:00:00')
        self.files[0].pop('recording_start')
        self.files[1].pop('recording_start')
        self.assertEqual(recording_date(self.files[0]), (None, 'unknown'))


if __name__ == '__main__':
    unittest.main()
