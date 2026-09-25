#!/usr/bin/env python3
"""Run isolated, sequential project-analysis comparisons on the same footage."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys

import cv2

from process_video import write_json


CASES = {
    'baseline': {},
    'no-preview': {'preview': False},
    'ffmpeg': {'preview': False, 'decoder': 'ffmpeg'},
    **{f'width-{w}': {'preview': False, 'settings': {'width': w}} for w in (640, 480, 320)},
    **{f'rate-{r}': {'preview': False, 'settings': {'samples-per-second': r}} for r in (1, 2, 3, 5)},
    'threads-1': {'opencv_threads': 1},
    'threads-4': {'opencv_threads': 4},
    'baseline-repeat': {},
    'no-preview-repeat': {'preview': False},
    'threads-1-repeat': {'opencv_threads': 1},
    'threads-4-repeat': {'opencv_threads': 4},
    'host-opencv-4': {'preview': False, 'opencv_threads': 4},
    'host-ffmpeg-4': {'preview': False, 'decoder': 'ffmpeg', 'opencv_threads': 4},
    'host-nvdec-4': {'preview': False, 'decoder': 'ffmpeg-cuda', 'opencv_threads': 4},
}

DEFAULT_CASES = [name for name in CASES if not name.startswith('host-')]


def overlaps(a, b):
    return max(0, min(a['end'], b['end']) - max(a['start'], b['start']))


def compare_events(baseline, candidate):
    """Temporal agreement, not ground-truth precision/recall or object identity."""
    ref, test = baseline['segments'], candidate['segments']
    missed = [a for a in ref if not any(overlaps(a, b) > 0 for b in test)]
    extra = [b for b in test if not any(overlaps(a, b) > 0 for a in ref)]
    intersection = sum(overlaps(a, b) for a in ref for b in test)
    ref_time = sum(a['end']-a['start'] for a in ref)
    test_time = sum(b['end']-b['start'] for b in test)
    union = ref_time+test_time-intersection
    return dict(baseline_events=len(ref), events=len(test), missed_baseline_events=len(missed),
                unmatched_candidate_events=len(extra), missed_intervals=[ [a['start'], a['end']] for a in missed],
                unmatched_intervals=[[b['start'], b['end']] for b in extra],
                temporal_iou=intersection/union if union else 1.0,
                baseline_time_covered=intersection/ref_time if ref_time else None,
                event_seconds=test_time, baseline_event_seconds=ref_time,
                split_baseline_events=sum(sum(overlaps(a,b)>0 for b in test)>1 for a in ref),
                merged_candidate_events=sum(sum(overlaps(a,b)>0 for a in ref)>1 for b in test))


def report(root):
    baseline_path = root/'baseline/summary.json'
    if not baseline_path.exists():
        return
    baseline = json.loads(baseline_path.read_text())
    rows = []
    for name in CASES:
        folder = root/name
        if not (folder/'profile.json').exists():
            continue
        summary = json.loads((folder/'summary.json').read_text())
        profile = json.loads((folder/'profile.json').read_text())
        comparison = compare_events(baseline, summary)
        reference_samples = json.loads((root/'baseline/events.json').read_text())['samples']
        candidate_samples = json.loads((folder/'events.json').read_text())['samples']
        comparison['raw_samples_identical'] = reference_samples == candidate_samples
        comparison['segments_identical'] = baseline['segments'] == summary['segments']
        rows.append(dict(configuration=name, profile=profile, comparison=comparison))
    write_json(root/'results.json', rows)
    lines = ['# Analysis benchmark results', '',
             'CPU includes the worker and child processes; 100% means one logical core. '
             'Event differences use temporal overlap with the reference baseline, not manually labelled truth.', '',
             '| Configuration | Wall seconds | × realtime | CPU % | Source frames/s | Analysis samples/s | Events | Missed baseline | Unmatched candidate | Temporal IoU | Identical raw samples |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |']
    for row in rows:
        p, c = row['profile'], row['comparison']
        lines.append(f"| {row['configuration']} | {p['wall_seconds']:.2f} | {p['realtime_factor']:.2f} | {p['cpu_percent']:.1f} | {p['source_frames_per_second']:.2f} | {p['analysis_samples_per_second']:.2f} | {c['events']} | {c['missed_baseline_events']} | {c['unmatched_candidate_events']} | {c['temporal_iou']:.3f} | {'yes' if c['raw_samples_identical'] else 'no'} |")
    lines += ['', '## Baseline stage attribution', '', '| Stage | Wall seconds |', '| --- | ---: |']
    for key, value in rows[0]['profile']['stages_seconds'].items():
        lines.append(f'| {key} | {value:.3f} |')
    lines += ['', 'Preview-process lifetime overlaps analysis and must not be added to stage times. '
              'FFmpeg decoder pipe time combines decoding, scaling, transfer, and waiting; it is not pure decoder compute. '
              'Full per-case timing and interval comparisons are in `results.json`.', '']
    (root/'results.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request', type=Path, help='Saved project request pointing to a representative 30–60 minute section')
    parser.add_argument('output', type=Path)
    parser.add_argument('--cases', nargs='+', choices=list(CASES), default=DEFAULT_CASES)
    parser.add_argument('--resume', action='store_true', help='Reuse completed cases only when their request matches')
    args = parser.parse_args()
    base = json.loads(args.request.read_text())
    # Compare against the library's original thread default, even when the app
    # uses a tuned default. A request can explicitly pin another reference.
    base.setdefault('opencv_threads', cv2.getNumThreads())
    args.output.mkdir(parents=True, exist_ok=True)
    for name in args.cases:
        req = copy.deepcopy(base)
        changes = copy.deepcopy(CASES[name])
        req.setdefault('settings', {}).update(changes.pop('settings', {}))
        req.update(changes)
        req['run_id'] = name
        folder = args.output/name
        snapshot = folder/'request.json'
        if folder.exists() and (folder/'profile.json').exists():
            if args.resume and snapshot.exists() and json.loads(snapshot.read_text()) == req:
                print(f'Reusing {name}', flush=True)
                report(args.output)
                continue
            raise ValueError(f'Completed output already exists: {folder}. Use a new directory or --resume with the identical request.')
        folder.mkdir(parents=True, exist_ok=True)
        write_json(snapshot, req)
        print(f'Running {name}', flush=True)
        subprocess.run([sys.executable, str(Path(__file__).with_name('project_worker.py')), 'analyze', str(snapshot), str(folder)], check=True)
        report(args.output)
        print((folder/'profile.json').read_text(), flush=True)


if __name__ == '__main__':
    main()
