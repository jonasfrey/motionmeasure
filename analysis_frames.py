"""Experimental FFmpeg sampling/scaling with the project's frame-index clock."""
import math
import resource
import subprocess
import tempfile

import numpy as np


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
