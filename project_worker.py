#!/usr/bin/env python3
"""Continuous project analysis and source-quality highlight compilation."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
import time
import resource
import re
from datetime import datetime
from collections import defaultdict
from contextlib import contextmanager

import cv2
import numpy as np
from process_video import write_json
from analysis_frames import ffmpeg_frames, prepare_proxy, proxy_frames


class AnalysisProfile:
    """Wall-time attribution plus process-tree CPU usage (100% = one core)."""
    def __init__(self):
        self.started = time.perf_counter()
        self.cpu_started = self.cpu()
        self.seconds = defaultdict(float)
        self.source_frames = 0
        self.samples = 0
        self.preview_started = None
        self.preview_elapsed = 0.0
        self.preview_cpu_seconds = 0.0
        self.decoder_cpu_seconds = 0.0

    @staticmethod
    def cpu():
        usages = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
        return sum(u.ru_utime + u.ru_stime for u in usages)

    @contextmanager
    def stage(self, name):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[name] += time.perf_counter() - start

    def result(self, duration):
        elapsed = time.perf_counter() - self.started
        cpu = self.cpu() - self.cpu_started
        return dict(wall_seconds=elapsed, cpu_seconds=cpu, cpu_percent=100*cpu/elapsed,
                    opencv_threads=cv2.getNumThreads(),
                    realtime_factor=duration/elapsed, source_frames=self.source_frames,
                    analysis_samples=self.samples, source_frames_per_second=self.source_frames/elapsed,
                    analysis_samples_per_second=self.samples/elapsed, stages_seconds=dict(self.seconds),
                    unattributed_seconds=elapsed-sum(self.seconds.values()),
                    preview_process_elapsed_seconds=self.preview_elapsed,
                    preview_cpu_seconds=self.preview_cpu_seconds,
                    ffmpeg_decoder_cpu_seconds=self.decoder_cpu_seconds,
                    note='Stage wall times are exclusive. Preview process lifetime overlaps analysis; '
                         'pipe writes include backpressure. CPU includes reaped child processes; 100% is one core.')


def recording_date(file):
    if file.get('recording_start'):
        return file['recording_start'], 'manual'
    # Only full dates AND times: a date alone does not establish a start time.
    match = re.search(r'(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)[T _-]?'
                      r'([0-2]\d)[:_-]?([0-5]\d)[:_-]?([0-5]\d)(?!\d)', file['name'])
    if match:
        try:
            return datetime(*map(int, match.groups())).isoformat(), 'filename'
        except ValueError:
            pass
    metadata = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                               'format_tags=creation_time:stream_tags=creation_time',
                               '-of', 'json', file['path']], capture_output=True, text=True)
    if metadata.returncode == 0:
        data = json.loads(metadata.stdout)
        for item in [data.get('format', {}), *data.get('streams', [])]:
            value = item.get('tags', {}).get('creation_time')
            if value:
                try:
                    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    if parsed.year >= 1970:
                        return parsed.isoformat(), 'metadata'
                except ValueError:
                    pass
    return None, 'unknown'


def probe_sources(files):
    sources, offset = [], 0.0
    if not files:
        raise ValueError('Add at least one video to the project.')
    for file in files:
        cap = cv2.VideoCapture(file['path'])
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            width, height = (int(cap.get(p)) for p in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT))
            if not cap.isOpened() or not math.isfinite(fps) or fps <= 0 or frames <= 0 or min(width, height) < 2:
                raise ValueError(f"Cannot read video metadata: {file['name']}")
            duration = frames / fps
            recorded, basis = recording_date(file)
            sources.append(dict(file, fps=fps, frames=frames, width=width, height=height,
                                start=offset, end=offset + duration, duration=duration,
                                recording_start=recorded, recording_date_basis=basis))
            offset += duration
        finally:
            cap.release()
    return sources


def fit_frame(frame, width, height):
    h, w = frame.shape[:2]
    ratio = min(width/w, height/h)
    rw, rh = max(1, round(w*ratio)), max(1, round(h*ratio))
    result = np.zeros((height, width, 3), np.uint8)
    x, y = (width-rw)//2, (height-rh)//2
    result[y:y+rh, x:x+rw] = cv2.resize(frame, (rw, rh), interpolation=cv2.INTER_AREA)
    return result


def opencv_frames(source, interval, sample_index, profile):
    cap = cv2.VideoCapture(source['path'])
    frame_idx = 0
    try:
        while True:
            with profile.stage('grab'):
                grabbed = cap.grab()
            if not grabbed:
                break
            profile.source_frames += 1
            frame_end = source['start'] + (frame_idx+1)/source['fps']
            if sample_index*interval < min(source['end'], frame_end) - 1e-9:
                with profile.stage('retrieve'):
                    ok, frame = cap.retrieve()
                if not ok:
                    raise ValueError(f"Cannot decode {source['name']} at frame {frame_idx}.")
                yield frame_idx, frame
                while sample_index*interval < min(source['end'], frame_end) - 1e-9:
                    sample_index += 1
            frame_idx += 1
        if frame_idx < source['frames'] - 2:
            raise ValueError(f"Video ended early: {source['name']} ({frame_idx}/{source['frames']} frames).")
    finally:
        cap.release()


def analyze(request, out):
    previous_threads = cv2.getNumThreads()
    threads = request.get('opencv_threads', os.environ.get('MOTION_OPENCV_THREADS', min(4, previous_threads)))
    if threads is not None:
        if isinstance(threads, bool) or str(threads) != str(int(threads)) or not 1 <= int(threads) <= 256:
            raise ValueError('OpenCV threads must be an integer from 1 to 256.')
        cv2.setNumThreads(int(threads))
    try:
        return _analyze(request, out)
    finally:
        if threads is not None:
            cv2.setNumThreads(previous_threads)


def _analyze(request, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    profile = AnalysisProfile()
    with profile.stage('metadata'):
        sources = probe_sources(request['files'])
    p = dict(step=5, width=256, samples_per_second=2, min_area=800, min_duration=1.0, min_frames=4, gap=1.5, threshold=24)
    p.update({k.replace('-', '_'): v for k, v in request.get('settings', {}).items()})
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in p.values()):
        raise ValueError('Detection settings must be finite numbers.')
    if p['step'] < 1 or int(p['step']) != p['step'] or p['min_area'] <= 0 or p['min_frames'] < 1 or p['threshold'] <= 0 or min(p['gap'], p['min_duration']) < 0:
        raise ValueError('Invalid detection settings.')
    if p['width'] < 32 or p['width'] > 3840 or int(p['width']) != p['width'] or not 0 <= p['samples_per_second'] <= 120:
        raise ValueError('Width must be an integer from 32 to 3840; sample rate must be from 0 to 120.')
    decoder = request.get('decoder', 'proxy')
    if decoder not in ('proxy', 'opencv', 'ffmpeg', 'ffmpeg-cuda'):
        raise ValueError('Decoder must be proxy, opencv, ffmpeg or ffmpeg-cuda.')
    if decoder == 'proxy':
        p['width'] = min(256, p['width'])
    preview = request.get('preview', False)
    if not isinstance(preview, bool):
        raise ValueError('Preview must be a boolean.')
    legacy_interval = p['step'] / sources[0]['fps']
    interval = 1/p['samples_per_second'] if p['samples_per_second'] else legacy_interval
    reference_width = max(2, min(960, sources[0]['width'])//2*2)
    reference_height = max(2, round(sources[0]['height']*reference_width/sources[0]['width'])//2*2)
    width = int(max(2, min(p['width'], sources[0]['width'])//2*2))
    height = max(2, round(sources[0]['height'] * width / sources[0]['width'])//2*2)
    effective_min_area = p['min_area'] * (width*height)/(reference_width*reference_height)
    # Preserve the required positive-motion time when an explicit rate is chosen.
    positive_seconds = p['min_frames'] * legacy_interval
    effective_min_frames = max(1, math.ceil(positive_seconds/interval - 1e-9))
    warmup_samples = max(1, math.ceil(5*legacy_interval/interval - 1e-9))
    duration = sources[-1]['end']
    proxies = []
    if decoder == 'proxy':
        acceleration = request.get('acceleration', 'auto')
        if acceleration not in ('auto', 'cpu'):
            raise ValueError('Acceleration must be auto or cpu.')
        cache = request.get('proxy_cache', out/'proxies')
        for i, source in enumerate(sources):
            def status(fraction, message):
                write_json(out/'status.json', dict(state='processing', phase='downsize',
                           pct=80*(source['start']+fraction*source['duration'])/duration,
                           message=f"{message} · {i+1}/{len(sources)} · {source['name']}"))
            with profile.stage('proxy_prepare'):
                proxies.append(prepare_proxy(source, interval, width, height, cache, status, acceleration))
        profile.source_frames = sum(s['frames'] for s, proxy in zip(sources, proxies) if not proxy['cached'])
    model = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=p['threshold'], detectShadows=True)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    segments, current = [], None
    sample_index = 0
    warmup_until = warmup_samples
    last_status = 0
    run_id = request.get('run_id', str(uuid.uuid4()))

    def commit():
        nonlocal current
        if current:
            thumb = current.pop('_thumb')
            current.pop('_last')
            current['duration'] = current['end'] - current['start']
            if current['duration'] + 1e-8 >= p['min_duration'] and current['frames'] >= effective_min_frames:
                current['id'] = f"event-{len(segments)+1}"
                current['index'] = len(segments)
                current['thumb'] = f"event-{len(segments)+1}.jpg"
                current['source_ids'] = [s['id'] for s in sources if s['start'] < current['end'] and s['end'] > current['start']]
                with profile.stage('output_io'):
                    if not cv2.imwrite(str(out/current['thumb']), thumb):
                        raise RuntimeError('Could not write thumbnail.')
                segments.append(current)
        current = None

    # Stream raw samples to disk; long projects do not keep all bounding boxes in memory.
    sample_path = out / 'samples.ndjson'
    with tempfile.TemporaryFile() as errors, sample_path.open('w') as samples:
        profile.preview_started = time.perf_counter() if preview else None
        ff = subprocess.Popen(['ffmpeg', '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                               '-s', f'{width}x{height}', '-r', str(1/interval), '-i', '-', '-an',
                               '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                               '-movflags', '+faststart', str(out/'preview.mp4')], stdin=subprocess.PIPE, stderr=errors) if preview else None
        try:
            for source_index, source in enumerate(sources):
                # Do not merge events or preserve a stale background over overnight gaps.
                previous = sources[source_index-1] if source_index else None
                if previous and previous.get('recording_start') and source.get('recording_start'):
                    a = datetime.fromisoformat(previous['recording_start'].replace('Z', '+00:00')).timestamp()
                    b = datetime.fromisoformat(source['recording_start'].replace('Z', '+00:00')).timestamp()
                    if abs(b-a-previous['duration']) > 1:
                        if current:
                            current['end'] = min(current['end'], source['start'])
                        commit()
                        model = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=p['threshold'], detectShadows=True)
                        warmup_until = sample_index+warmup_samples
                frames = (proxy_frames(source, interval, proxies[source_index], profile) if decoder == 'proxy'
                          else opencv_frames(source, interval, sample_index, profile) if decoder == 'opencv'
                          else ffmpeg_frames(source, interval, width, height, profile, hardware=decoder == 'ffmpeg-cuda'))
                try:
                    for frame_idx, frame in frames:
                        frame_end = source['start'] + (frame_idx+1)/source['fps']
                        while sample_index*interval < min(frame_end, source['end']) - 1e-9:
                            t = sample_index * interval
                            with profile.stage('resize_letterbox'):
                                small = fit_frame(frame, width, height) if decoder == 'opencv' else frame.copy()
                            with profile.stage('mog2'):
                                foreground = model.apply(small)
                            with profile.stage('mask_contours'):
                                mask = cv2.threshold(foreground, 200, 255, cv2.THRESH_BINARY)[1]
                                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
                                mask = cv2.dilate(mask, kernel, iterations=2)
                                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                            boxes, area = [], 0.0
                            if sample_index >= warmup_until:
                                for c in contours:
                                    a = cv2.contourArea(c)
                                    if a >= effective_min_area:
                                        x, y, w, h = cv2.boundingRect(c)
                                        boxes.append(dict(x=x, y=y, width=w, height=h))
                                        area += a
                                        cv2.rectangle(small, (x, y), (x+w, y+h), (60, 225, 160), 2)
                            if current and t - current['_last'] > p['gap'] + interval + 1e-8:
                                commit()
                            if boxes:
                                with profile.stage('output_io'):
                                    samples.write(json.dumps(dict(timestamp=t, source_id=source['id'], source_time=t-source['start'], source_frame=frame_idx, area=area, boxes=boxes))+'\n')
                                if current is None:
                                    current = dict(start=t, end=t, frames=0, peak_area=-1)
                                current.update(end=min(t+interval, duration), _last=t)
                                current['frames'] += 1
                                if area > current['peak_area']:
                                    current.update(peak_area=area, peak_t=t, _thumb=small.copy())
                            if ff:
                                with profile.stage('preview_pipe'):
                                    ff.stdin.write(small.tobytes())
                            sample_index += 1
                            profile.samples += 1
                            if time.monotonic()-last_status >= 1:
                                with profile.stage('output_io'):
                                    pct = 80+20*t/duration if decoder == 'proxy' else t/duration*100
                                    write_json(out/'status.json', dict(state='processing', phase='analyze', pct=min(99, pct), message=f"Finding motion · {source['name']}", segments=len(segments)))
                                last_status = time.monotonic()
                finally:
                    frames.close()
            commit()
            if ff:
                with profile.stage('preview_completion'):
                    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
                    ff.stdin.close()
                    returncode = ff.wait()
                    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                    profile.preview_cpu_seconds = (child_after.ru_utime + child_after.ru_stime
                                                   - child_before.ru_utime - child_before.ru_stime)
                profile.preview_elapsed = time.perf_counter() - profile.preview_started
                if returncode != 0:
                    errors.seek(0)
                    raise RuntimeError('Preview encoding failed: '+errors.read().decode(errors='replace')[-1000:])
        finally:
            if ff and ff.poll() is None:
                ff.kill()
                ff.wait()
            if ff and not ff.stdin.closed:
                ff.stdin.close()
    public_sources = [{k: v for k, v in s.items() if k != 'path'} for s in sources]
    result = dict(schema_version=3, analysis_id=run_id, project_id=request['id'], project_name=request['name'],
                  revision=request['revision'], model='OpenCV MOG2', params=p, sources=public_sources,
                  duration=duration, out_size=[width, height], out_fps=1/interval, sample_interval=interval,
                  warmup_seconds=warmup_samples*interval, preview_available=preview, decoder=decoder,
                  proxies=[{k: v for k, v in proxy.items() if k != 'path'} for proxy in proxies],
                  effective_min_area=effective_min_area, effective_min_frames=effective_min_frames,
                  minimum_positive_seconds=positive_seconds, coordinate_system='pixels in out_size; origin top-left; sources letterboxed',
                  timestamp_basis='continuous project time; source frame count / FPS',
                  segments=segments, segment_count=len(segments), total_motion_s=sum(s['duration'] for s in segments))
    with profile.stage('output_io'):
        write_json(out/'summary.json', result)
        # Assemble a valid JSON download without loading the potentially large sample list.
        with (out/'events.json.tmp').open('w') as dest, sample_path.open() as samples:
            dest.write(json.dumps(result)[:-1]+', "samples": [')
            first = True
            for line in samples:
                if not first:
                    dest.write(',')
                dest.write(line.strip())
                first = False
            dest.write(']}')
        os.replace(out/'events.json.tmp', out/'events.json')
        sample_path.unlink()
        write_json(out/'status.json', dict(state='done', pct=100, message='Analysis complete'))
    write_json(out/'profile.json', profile.result(duration))
    return result


def clip_parts(clip, sources):
    start, end = float(clip['start']), float(clip['end'])
    if not math.isfinite(start+end) or start < 0 or end <= start or end > sources[-1]['end'] + 1e-6:
        raise ValueError('Clip times must be inside the project timeline, with end after start.')
    return [dict(source=s, start=max(start, s['start']), end=min(end, s['end']))
            for s in sources if s['start'] < end and s['end'] > start]


def run_ffmpeg(args):
    result = subprocess.run(['ffmpeg', '-y', '-v', 'error', *args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-1500:])


def export_video(request, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    analysis_dir = Path(request['analysis_dir'])
    result = json.loads((analysis_dir/'summary.json').read_text())
    source_paths = {s['id']: s['path'] for s in request['files']}
    sources = [dict(s, path=source_paths[s['id']]) for s in result['sources']]
    events = {s['id']: s for s in result['segments']}
    clips = request['clips']
    if not clips or len(clips) > 1000:
        raise ValueError('Select between 1 and 1000 clips.')
    parts = []
    for clip in clips:
        if clip['segment_id'] not in events:
            raise ValueError('A selected detection is no longer available. Analyze and select again.')
        parts.extend(clip_parts(clip, sources))
    boxes = bool(request.get('boxes', False))
    if boxes and not result.get('preview_available', True):
        raise ValueError('Motion-box export requires an analysis preview. Reanalyze with preview enabled.')
    audio = bool(request.get('audio', True))
    first = sources[0]
    ratio = min(1, 1920/first['width'], 1080/first['height'])
    width, height = max(2, int(first['width']*ratio)//2*2), max(2, int(first['height']*ratio)//2*2)
    audio_cache = {}
    total_seconds = 0
    with tempfile.TemporaryDirectory(prefix='render-', dir=out) as tmp:
        tmp = Path(tmp)
        for i, part in enumerate(parts):
            source = part['source']
            local_start = part['start'] - source['start']
            # Quantize each cut once to the export's 30 fps clock.
            seconds = max(1, round((part['end']-part['start'])*30))/30
            total_seconds += seconds
            write_json(out/'status.json', dict(state='processing', pct=i/len(parts)*95, message=f'Rendering part {i+1} of {len(parts)}'))
            video_path = str(analysis_dir/'preview.mp4') if boxes else source['path']
            video_start = part['start'] if boxes else local_start
            args = ['-ss', str(video_start), '-i', video_path]
            if source['id'] not in audio_cache:
                metadata = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_streams', '-of', 'json', source['path']]))
                audio_cache[source['id']] = bool(metadata['streams'])
            if audio and audio_cache[source['id']]:
                if boxes:
                    args += ['-ss', str(local_start), '-i', source['path']]
                    audio_map = '1:a:0'
                else:
                    audio_map = '0:a:0'
            else:
                args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
                audio_map = '1:a:0'
            vf = f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,setpts=PTS-STARTPTS,fps=30,tpad=stop_mode=clone:stop_duration=1'
            args += ['-map', '0:v:0', '-map', audio_map, '-vf', vf,
                     '-af', f'aresample=48000,apad,atrim=duration={seconds},asetpts=PTS-STARTPTS',
                     '-t', str(seconds), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                     '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '2', str(tmp/f'part-{i:05d}.mkv')]
            run_ffmpeg(args)
        listing = tmp/'concat.txt'
        listing.write_text(''.join(f"file 'part-{i:05d}.mkv'\n" for i in range(len(parts))))
        write_json(out/'status.json', dict(state='processing', pct=96, message='Joining selected clips'))
        run_ffmpeg(['-f', 'concat', '-safe', '1', '-i', str(listing), '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(out/'highlights.partial.mp4')])
        os.replace(out/'highlights.partial.mp4', out/'highlights.mp4')
    manifest = dict(project_id=request['project_id'], analysis_id=result['analysis_id'], clips=clips,
                    boxes=boxes, audio=audio, duration=total_seconds, size=[width, height], fps=30)
    write_json(out/'edit.json', manifest)
    write_json(out/'status.json', dict(state='done', pct=100, message='Export ready', duration=total_seconds))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['analyze', 'export'])
    parser.add_argument('request')
    parser.add_argument('output_dir')
    args = parser.parse_args()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        request = json.loads(Path(args.request).read_text())
        write_json(out/'status.json', dict(state='processing', pct=0, message='Starting'))
        (analyze if args.action == 'analyze' else export_video)(request, out)
    except Exception as error:
        write_json(out/'status.json', dict(state='error', error=str(error)))
        raise


if __name__ == '__main__':
    main()
