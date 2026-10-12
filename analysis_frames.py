"""FFmpeg sampling and reusable small analysis videos, on a frame-index clock."""
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time

import cv2
import numpy as np

from process_video import write_json


def sample_indices(source, interval):
    sample = math.ceil(source['start']/interval - 1e-9)
    previous = -1
    while sample*interval < source['end'] - 1e-9:
        idx = max(0, math.floor((sample*interval-source['start'])*source['fps'] + 1e-8))
        if idx != previous:
            yield idx
            previous = idx
        sample += 1


def selection_filter(source, interval):
    phase = source['start'] / interval
    stride = 1 / (source['fps'] * interval)
    return (f"select='gt(ceil((n+1)*{stride:.17g}+{phase:.17g}-1e-9),"
            f"ceil(n*{stride:.17g}+{phase:.17g}-1e-9))'")


def proxy_command(source, interval, width, height, backend):
    ratio = min(width/source['width'], height/source['height'])
    # All encoder/GPU dimensions are even; padding preserves the source aspect.
    rw = max(2, round(source['width']*ratio)//2*2)
    rh = max(2, round(source['height']*ratio)//2*2)
    options = []
    if backend == 'cuda':
        options = ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
        scale = f'scale_cuda={rw}:{rh}:format=nv12,hwdownload,format=nv12'
    elif backend.startswith('vaapi:'):
        options = ['-hwaccel', 'vaapi', '-hwaccel_device', backend.split(':', 1)[1],
                   '-hwaccel_output_format', 'vaapi']
        scale = f'scale_vaapi=w={rw}:h={rh}:format=nv12,hwdownload,format=nv12'
    else:
        scale = f'scale={rw}:{rh}:flags=fast_bilinear'
    vf = (f'{selection_filter(source, interval)},{scale},'
          f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,'
          f'settb=AVTB,setpts=N*{interval:.17g}/TB')
    return ['ffmpeg', '-y', '-v', 'error', '-nostdin', '-filter_threads', '2', '-threads', '4', *options,
            '-i', source['path'], '-map', '0:v:0', '-an', '-sn', '-dn',
            '-vf', vf, '-fps_mode', 'passthrough', '-enc_time_base', '1:1000000',
            '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', '-crf', '18',
            '-pix_fmt', 'yuv420p']


def hardware_backends():
    # Compiled-in FFmpeg support alone does not imply accessible hardware.
    backends = []
    if Path('/dev/nvidia0').exists():
        backends.append('cuda')
    backends.extend('vaapi:'+str(p) for p in sorted(Path('/dev/dri').glob('renderD*')))
    return backends


def prepare_proxy(source, interval, width, height, cache, status, acceleration='auto'):
    """Atomically cache sampled media; only publish after validating every frame count.

    Detector thresholds are deliberately absent from the key, so tuning detection
    reuses the expensive decode/scale pass. Phase is included for mixed-rate seams.
    """
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    stat = Path(source['path']).stat()
    identity = dict(version=1, path=str(Path(source['path']).resolve()), size=stat.st_size,
                    mtime=stat.st_mtime_ns, width=width, height=height, interval=interval,
                    start=source['start'], frames=source['frames'], fps=source['fps'],
                    acceleration=acceleration)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path, manifest = cache/(key+'.mp4'), cache/(key+'.json')
    expected = sum(1 for _ in sample_indices(source, interval))
    if not expected:
        return dict(path=None, frames=0, backend='none', cached=True)

    def valid(candidate):
        cap = cv2.VideoCapture(str(candidate))
        try:
            return (cap.isOpened() and int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == expected
                    and int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) == width
                    and int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) == height)
        finally:
            cap.release()

    if path.exists() and manifest.exists():
        try:
            saved = json.loads(manifest.read_text())
            if saved['identity'] == identity and saved['bytes'] == path.stat().st_size and valid(path):
                status(1, 'Reusing analysis copy')
                return dict(path=str(path), frames=expected, backend=saved['backend'], cached=True)
        except (ValueError, KeyError):
            pass
    tmp = cache/(key+'.partial.mp4')
    candidates = hardware_backends() if acceleration == 'auto' else []
    # Exercise the actual codec/filter/driver combination before processing hours.
    backend = 'cpu'
    for candidate in candidates:
        status(0, 'Checking GPU acceleration')
        try:
            probe = subprocess.run(proxy_command(source, interval, width, height, candidate)
                                   + ['-frames:v', '1', '-f', 'null', '-'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            if probe.returncode == 0:
                backend = candidate
                break
        except subprocess.TimeoutExpired:
            pass
    attempts = [backend, 'cpu'] if backend != 'cpu' else ['cpu']
    try:
        for backend in attempts:
            label = 'GPU' if backend != 'cpu' else 'CPU'
            status(0, f'Downsizing · {label}')
            with tempfile.TemporaryFile() as errors:
                proc = subprocess.Popen(proxy_command(source, interval, width, height, backend)
                                        + ['-progress', 'pipe:1', '-stats_period', '1', str(tmp)],
                                        stdout=subprocess.PIPE, stderr=errors, text=True)
                try:
                    last = 0
                    for line in proc.stdout:
                        if line.startswith('frame='):
                            now = time.monotonic()
                            if now-last >= .5:
                                status(min(.99, int(line.split('=')[1])/expected), f'Downsizing · {label}')
                                last = now
                    code = proc.wait()
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    proc.stdout.close()
                if code == 0 and valid(tmp):
                    os.replace(tmp, path)
                    write_json(manifest, dict(identity=identity, backend=backend, bytes=path.stat().st_size))
                    status(1, f'Analysis copy ready · {label}')
                    return dict(path=str(path), frames=expected, backend=backend, cached=False)
                errors.seek(0)
                detail = errors.read().decode(errors='replace')[-1500:]
                if backend == 'cpu':
                    raise RuntimeError('Analysis copy failed or has missing frames: '+detail)
                status(0, 'GPU unavailable for this video; retrying on CPU')
    finally:
        tmp.unlink(missing_ok=True)


def proxy_frames(source, interval, proxy, profile):
    if proxy['path'] is None:
        return
    cap = cv2.VideoCapture(proxy['path'])
    try:
        for idx in sample_indices(source, interval):
            with profile.stage('proxy_read'):
                ok, frame = cap.read()
            if not ok:
                raise ValueError('Analysis copy ended early. Remove the project proxy cache and retry.')
            yield idx, frame
        if cap.grab():
            raise ValueError('Analysis copy has extra frames; sampling clock mismatch.')
    finally:
        cap.release()


def ffmpeg_frames(source, interval, width, height, profile, hardware=False):
    """Yield (source frame index, BGR canvas), once per required source frame.

    Selection uses frame indices rather than container timestamps, matching the
    existing constant-frame-rate interpretation even on imperfect camera files.
    Multiple analysis instants within one source frame are handled by the caller.
    """
    fps = source['fps']
    phase = source['start'] / interval
    stride = 1 / (fps * interval)
    # ceil differences identify frames containing at least one sampling instant.
    select = (f'gt(ceil((n+1)*{stride:.17g}+{phase:.17g}-1e-9),'
              f'ceil(n*{stride:.17g}+{phase:.17g}-1e-9))')
    ratio = min(width/source['width'], height/source['height'])
    rw, rh = max(1, round(source['width']*ratio)), max(1, round(source['height']*ratio))
    x, y = (width-rw)//2, (height-rh)//2
    # Convert to BGR before resizing so padding and odd dimensions are exact.
    download = 'hwdownload,format=nv12,' if hardware else ''
    vf = f"select='{select}',{download}format=bgr24,scale={rw}:{rh}:flags=area,pad={width}:{height}:{x}:{y}:black"
    # Experimental NVDEC path for 8-bit footage: decode on the GPU, select
    # there, then download only selected frames. MOG2 and scaling stay on CPU.
    input_options = ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'] if hardware else []
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(['ffmpeg', '-v', 'error', *input_options, '-i', source['path'],
                                 '-an', '-sn', '-dn', '-vf', vf, '-fps_mode', 'passthrough',
                                 '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'],
                                stdout=subprocess.PIPE, stderr=errors)
        try:
            sample = math.ceil(source['start']/interval - 1e-9)
            previous = -1
            while sample*interval < source['end'] - 1e-9:
                idx = max(0, math.floor((sample*interval-source['start'])*fps + 1e-8))
                if idx != previous:
                    size = width*height*3
                    with profile.stage('ffmpeg_decode_scale_read'):
                        data = proc.stdout.read(size)
                    if len(data) != size:
                        errors.seek(0)
                        raise ValueError('FFmpeg frame stream ended early: '+errors.read().decode(errors='replace')[-1500:])
                    yield idx, np.frombuffer(data, np.uint8).reshape(height, width, 3).copy()
                    previous = idx
                sample += 1
            with profile.stage('ffmpeg_decode_completion'):
                extra = proc.stdout.read(1)
                if extra:
                    raise ValueError('FFmpeg produced extra analysis frames; sampling clock mismatch.')
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                code = proc.wait()
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                profile.decoder_cpu_seconds += after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
            if code:
                errors.seek(0)
                raise RuntimeError('FFmpeg decoding failed: '+errors.read().decode(errors='replace')[-1500:])
            profile.source_frames += source['frames']
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
