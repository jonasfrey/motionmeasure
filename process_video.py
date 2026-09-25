#!/usr/bin/env python3
"""OpenCV motion detector and JSON exporter; also used by the local web app."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

import cv2


def write_json(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)


def detect(input_path, output_dir, *, step=5, width=960, min_area=800,
           gap=1.5, min_duration=1.0, min_frames=4, threshold=24):
    if step < 1 or width < 32 or min_area <= 0 or min_frames < 1 or threshold <= 0:
        raise ValueError('Step, area, frames and threshold must be positive; width must be >=32.')
    if not all(math.isfinite(v) for v in (gap, min_duration, min_area, threshold)) or gap < 0 or min_duration < 0:
        raise ValueError('Duration and gap must be finite, non-negative numbers.')
    source, out = Path(input_path), Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = source.stem
    status = out / f'{base}.status.json'
    write_json(status, {'state': 'processing', 'pct': 0})
    cap = cv2.VideoCapture(str(source))
    ff = None
    temp_video = out / f'{base}.partial.mp4'
    try:
        if not cap.isOpened():
            raise ValueError('Cannot open video. The file may be damaged or unsupported.')
        fps = cap.get(cv2.CAP_PROP_FPS)
        sw, sh = (int(cap.get(p)) for p in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not math.isfinite(fps) or fps <= 0 or min(sw, sh) < 2:
            raise ValueError('Video has invalid frame rate or dimensions.')
        ow = max(2, min(width, sw) // 2 * 2)
        oh = max(2, round(sh * ow / sw) // 2 * 2)
        # Exact rate is essential: rounding fps/step causes cumulative timestamp drift.
        output_fps = fps / step
        backsub = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=threshold, detectShadows=True)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        segments, samples, current = [], [], None
        processed, idx = 0, 0
        warmup = 5
        params = dict(step=step, width=width, min_area=min_area, gap=gap,
                      min_duration=min_duration, min_frames=min_frames, threshold=threshold)

        def commit():
            nonlocal current
            if current is None:
                return
            img = current.pop('_thumb')
            current['duration'] = round(current['end'] - current['start'], 6)
            if current['duration'] >= min_duration and current['frames'] >= min_frames:
                current['index'] = len(segments)
                current['thumb'] = f'{base}.seg{len(segments):04d}.jpg'
                if not cv2.imwrite(str(out / current['thumb']), img):
                    raise RuntimeError('Could not write thumbnail.')
                segments.append(current)
            current = None

        with tempfile.TemporaryFile() as errors:
            ff = subprocess.Popen([
                'ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                '-s', f'{ow}x{oh}', '-r', str(output_fps), '-i', '-', '-an',
                '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', str(temp_video),
            ], stdin=subprocess.PIPE, stderr=errors)
            while cap.grab():
                frame_idx = idx
                idx += 1
                if frame_idx % step:
                    continue
                ok, frame = cap.retrieve()
                if not ok:
                    raise ValueError(f'Cannot decode video frame {frame_idx}.')
                t = frame_idx / fps
                small = cv2.resize(frame, (ow, oh), interpolation=cv2.INTER_AREA)
                mask = cv2.threshold(backsub.apply(small), 200, 255, cv2.THRESH_BINARY)[1]
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
                mask = cv2.dilate(mask, kernel, iterations=2)
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                boxes, area = [], 0
                if processed >= warmup:
                    for contour in contours:
                        a = cv2.contourArea(contour)
                        if a >= min_area:
                            x, y, w, h = cv2.boundingRect(contour)
                            boxes.append({'x': x, 'y': y, 'width': w, 'height': h})
                            area += a
                            cv2.rectangle(small, (x, y), (x+w, y+h), (60, 225, 160), 2)
                # Store raw positive samples even when a short event is later filtered out.
                if boxes:
                    samples.append({'timestamp': round(t, 6), 'frame': frame_idx,
                                    'area': area, 'boxes': boxes})
                if current and t - current['_last_t'] > gap + step / fps + 1e-6:
                    commit()
                if boxes:
                    if current is None:
                        current = {'start': t, 'end': t + step/fps, 'frames': 0,
                                   'peak_area': -1, 'peak_t': t, '_last_t': t}
                    current['end'] = t + step/fps
                    current['_last_t'] = t
                    current['frames'] += 1
                    if area > current['peak_area']:
                        current.update(peak_area=area, peak_t=t, _thumb=small.copy())
                ff.stdin.write(small.tobytes())
                processed += 1
                if processed % 30 == 0:
                    write_json(status, {'state': 'processing', 'pct': min(99, round(idx/max(total, 1)*100, 1)),
                                        'frame': idx, 'total': total, 'segments': len(segments)})
            if not processed:
                raise ValueError('Video contains no decodable frames.')
            if total > 0 and idx < total - max(step, 2):
                raise ValueError(f'Video ended early: decoded {idx} of {total} frames.')
            duration = idx / fps
            if current:
                current['end'] = min(current['end'], duration)
            commit()
            ff.stdin.close()
            if ff.wait() != 0:
                errors.seek(0)
                raise RuntimeError('Video encoding failed: ' + errors.read().decode(errors='replace')[-1000:])
        for s in segments:
            s.pop('_last_t', None)
            for key in ('start', 'end', 'peak_t'):
                s[key] = round(s[key], 6)
        annotated = f'{base}.annotated.mp4'
        os.replace(temp_video, out / annotated)
        result = {'schema_version': 2, 'base': base, 'source': source.name,
                  'model': 'OpenCV MOG2', 'params': params,
                  'src_fps': fps, 'src_size': [sw, sh], 'out_size': [ow, oh],
                  'out_fps': output_fps, 'duration': duration, 'step': step,
                  'warmup_seconds': warmup * step / fps,
                  'coordinate_system': 'pixels in out_size; origin top-left',
                  'timestamp_basis': 'source frame index / source FPS (constant frame rate)',
                  'sample_interval': step / fps, 'samples': samples,
                  'annotated': annotated, 'segments': segments, 'segment_count': len(segments),
                  'total_motion_s': round(sum(s['duration'] for s in segments), 6)}
        write_json(out / f'{base}.events.json', result)
        write_json(status, {'state': 'done', 'pct': 100, 'segments': len(segments)})
        return result
    except Exception as exc:
        write_json(status, {'state': 'error', 'error': str(exc)})
        raise
    finally:
        cap.release()
        if ff and ff.poll() is None:
            ff.kill()
            ff.wait()
        if ff and ff.stdin and not ff.stdin.closed:
            ff.stdin.close()
        temp_video.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('output_dir')
    for name, typ, default in [('step', int, 5), ('width', int, 960), ('min-area', float, 800),
                               ('gap', float, 1.5), ('min-duration', float, 1),
                               ('min-frames', int, 4), ('threshold', float, 24)]:
        parser.add_argument('--' + name, type=typ, default=default)
    args = vars(parser.parse_args())
    source, out = args.pop('input'), args.pop('output_dir')
    result = detect(source, out, **args)
    print(json.dumps({'done': True, 'segments': result['segment_count']}))


if __name__ == '__main__':
    main()
