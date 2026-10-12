# Motionmeasure

A local video motion detector with project libraries, a recording-day calendar, original-quality playback, and a highlights editor. All processing and storage stay on this machine. Uses OpenCV MOG2 and FFmpeg. Analysis uses cached copies at up to 256 pixels wide and 2 samples/second, with GPU decoding/scaling when available and automatic CPU fallback. No API keys or GPU required.

## Start

Requires Python 3.10+, Deno 2, and FFmpeg/ffprobe with the libx264 encoder.

```bash
git clone git@github.com:jonasfrey/motionmeasure.git
cd motionmeasure
cp .env.example .env
python3 -m venv venv
venv/bin/pip install -r requirements.txt
deno task start
```

Open **http://127.0.0.1:8088**. The port comes from your local `.env` file. An alternative port/interpreter can be used with:

```bash
PYTHON=python3 PORT=8094 deno run -A server.ts
```

The app binds to loopback and is designed for local use. Run one server per data directory.

## Project → detections → highlights

1. Click **New project** and give it a name. Switch projects using the sidebar.
2. **Add videos to project** or drop multiple files into the upload area. Files belong only to that project. MP4, MOV, MKV, AVI, WebM and M4V are accepted, subject to installed decoder support. Uploads stream to disk, have unique internal names, and are limited to 8 GB each.
3. Review the **Playback order**. Each upload batch is sorted naturally by filename and appended to the project. Use arrows or **Sort by name** to change the order.
4. Click **Analyze all files**. The first pass creates small, silent analysis copies; the second finds motion in those copies. Progress identifies both stages. Copies are reused when you change detection thresholds or recording dates. Files retain a continuous project clock for edits and exports; known recording gaps reset the background model and separate events.
5. Select a day in the calendar: green indicates motion, and its time bar shows recorded periods and motion occurrences with overnight gaps preserved. Click a detection or recorded part of the bar to play the **original video and audio at original resolution**. Playback continues across source-file boundaries. **All footage** shows the continuous project timeline, including files without dates. Detection cards load in batches of 100. **JSON** downloads all events and sampled boxes with project and source timestamps.
6. Check interesting detections to add them to **Your highlights video**. Reorder clips with arrows, remove clips, or adjust start/end times in project seconds. A clip may span several source files. The selection, order, trim times and export options are automatically saved with the project.
7. **Preview selection** plays the selected ranges from the original files. Then click **Export highlights MP4**. When rendering finishes, download or play the completed MP4 below the editor. An edit JSON download records the selected ranges and export settings.

### Recording dates and playback

The calendar uses a manual recording start if set; otherwise it tries a full date/time in the filename (for example `camera_20261008_090000.mp4` or `2026-10-08_09-00-00.mp4`), then embedded `creation_time` metadata. File modification/upload times are never treated as recording dates. Metadata can represent an export date rather than capture time, so check it.

Under each file in **Playback order**, expand **Recording date** to correct its start in local time, or choose **Use auto date**. Reanalyze after changing dates; cached copies are reused. Filename/manual timestamps without a timezone use local time; metadata with an offset is displayed in the browser's local timezone. Files with unknown dates remain in **All footage**. Midnight-spanning recordings appear on both days. Overlapping recordings remain separate sources. Dates do not change playback/export order.

Original playback uses byte-range streaming and requires the browser to support the original codec/container. If it cannot play a source, **Download original** opens the unchanged file in an external player. No full-length playback transcode is required. Analysis thumbnails remain low-resolution.

### Export behavior

- Default: **original footage and audio, without motion boxes**.
- Outputs are normalized to the first source's aspect ratio, up to 1920×1080, at 30 fps, with H.264 video and AAC stereo audio. Other aspect ratios are letterboxed. Small sources are not enlarged to 1080p.
- Files without audio contribute silence. Disabling original audio makes the entire export silent.
- Motion-box exports remain available for older runs with an annotated preview. New fast analyses omit that full-length preview and disable boxed exports. Clean exports use original video frames.
- The editor's clip order is the export order. Overlapping selected ranges are included independently; trim them if you want to avoid repeating footage.
- Cuts are rounded to the 30 fps output clock. Preview selection is for review; its browser-driven jumps are less precise than the rendered export.
- Analysis and export jobs share a sequential queue. Uploads/reordering/edits for a busy project are temporarily locked, so a running job sees a consistent project snapshot.

Adding or reordering files makes the previous analysis stale. Analyze again before editing/exporting from the new timeline. A **successful reanalysis resets the selection** because detections may change; previous exports remain available. Failed jobs display an error and retain the previous successful analysis/export. Interrupted jobs can be run again.

## Repository and private data

The repository contains only application source, configuration templates, documentation,
and tests that generate synthetic footage. `.gitignore` uses an explicit allowlist:
new files are ignored until their paths are added after review.

| Location | Purpose | In Git |
| --- | --- | --- |
| `server.ts`, `index.html`, `timeline.js` | Local web server and interface | Yes |
| Root Python scripts | Detection, project processing and benchmark tooling | Yes |
| `tests/` | Reviewed synthetic unit and integration tests | Yes |
| `requirements.txt`, `deno.json`, `.env.example` | Dependencies, tasks and example settings | Yes |
| `.env` | Local settings and private paths | No |
| `projects/` | Project names, uploads, analysis, edits and exports | No |
| Root video files | Existing original footage referenced by legacy projects | No |
| `outputs/`, `benchmarks/` | Generated media, metadata and performance reports | No |
| `archive/` | Local notes and historical material | No |
| `venv/`, caches | Local dependencies and generated files | No |

Existing private files remain in place so saved references keep working. For separate
storage, set `MOTION_DATA` to a directory outside the checkout before creating projects.
This setting does not move existing data. Keep benchmark and CLI outputs outside the
source tree or in the ignored directories above.

Project JSON, request snapshots, logs, thumbnails, exports and benchmark reports may
contain sensitive information even when they are not videos. Do not force-add them.
Before committing, review `git diff --cached` and `git diff --cached --name-only`.
Git exclusion does not encrypt files or restrict local filesystem access; back up private
data separately. The app binds to loopback and has no user authentication.

## Existing footage and storage

Previously uploaded videos in the project root are automatically grouped into **Previously uploaded videos** on first launch. The migration references the original files without moving, copying or deleting them. Existing individual results in `outputs/` are preserved; run project analysis to create the continuous timeline.

New projects persist under:

```text
projects/<project-id>/
  project.json                 # library, file order, current analysis, saved edit
  uploads/<file-id>.<extension> # original uploaded footage
  proxies/                     # reusable sampled 256px analysis MP4s + cache manifests
  runs/<analysis-id>/
    request.json               # immutable analysis request
    summary.json               # timeline/events without the large sample list
    events.json                # complete downloadable motion data
    preview.mp4                # only for legacy/explicit preview-enabled worker runs
    event-N.jpg                # detection thumbnails
    status.json
    profile.json               # stage timings, CPU usage and throughput
  exports/<export-id>/
    request.json
    highlights.mp4
    edit.json
    status.json
```

Original footage is preserved. Analysis copies, runs and exports are retained on disk. The project `proxies/` directory can be deleted while no job is running to reclaim space; the next analysis recreates it. Cache keys include source path/size/modification time, sampling clock and dimensions. Interrupted/invalid copies are not reused; valid completed files survive an interrupted job. Temporary export parts are removed after rendering. Raw motion samples stream to disk during analysis rather than accumulating in RAM. Set `MOTION_DATA=/path/to/projects` to change storage; set `MOTION_IMPORT_LEGACY=0` to disable automatic import, useful for testing.

## Project JSON

Schema version 3 uses a continuous project clock. `sources` lists each file's ID, original name, source FPS/dimensions, duration, and project start/end offsets. `segments` contains stable IDs within that analysis, start/end/duration, peak motion, thumbnail and contributing source IDs. `samples` contains positive raw motion detections, including brief transients excluded from accepted events.

An abbreviated sample:

```json
{
  "timestamp": 62.4,
  "source_id": "<file-id>",
  "source_time": 2.4,
  "source_frame": 72,
  "area": 1500.0,
  "boxes": [{"x": 120, "y": 80, "width": 50, "height": 40}]
}
```

Box coordinates use the `out_size` analysis canvas, with origin at top-left. Sources with a different aspect ratio are letterboxed. To map a box back, undo that source's centered letterbox offset, then divide by `min(out_width/source_width, out_height/source_height)` (accounting for rounded resized dimensions).

By default, the sample interval is 0.5 seconds (2 samples/sec) throughout the project, even if sources use different frame rates. Setting `samples-per-second` to 0 uses `step / first_source_fps` instead. Each sample uses the source frame containing that instant. Source timestamps can differ by less than one source frame from the exact project sampling time. Analysis proxy frames retain a mapping to original source-frame indices; proxy playback timestamps are not used for original-video seeking.

Times use frame count / reported FPS. Convert variable-frame-rate footage to constant frame rate first when exact source timing is important. Scene changes, moving cameras, lights and foliage can trigger motion. A warm-up happens at project start and after known recording gaps. Its duration is five samples at the Nth-frame rate, rounded up to whole analysis samples. This detects motion regions; it does not recognize people or track object identities.

## Standalone CLI

Single-video compatibility is retained:

```bash
venv/bin/python detect_motion.py input.mp4 outputs \
  --step 5 --width 960 --min-area 800 \
  --min-duration 1 --min-frames 4 --gap 1.5 --threshold 24
```

`process_video.py` accepts the same arguments and exposes `detect()` for Python callers. It produces schema version 2 JSON, an annotated preview, thumbnails and status. CLI outputs use the input stem; use separate output directories for inputs sharing a stem.

The project worker can also be run from saved request snapshots:

```bash
venv/bin/python project_worker.py analyze PROJECT_REQUEST.json OUTPUT_DIRECTORY
venv/bin/python project_worker.py export EXPORT_REQUEST.json OUTPUT_DIRECTORY
```

## Detection settings

These are project-analysis settings. The standalone CLI retains its original preview-pixel area threshold and step/minimum-sample semantics.

| Setting | Effect |
| --- | --- |
| `step` | Sample interval relative to the first video's FPS; lower catches shorter motion. |
| `width` | Analysis canvas width cap, default and maximum 256 in the app; height follows the first source's aspect ratio. |
| `samples-per-second` | Analysis rate, default 2; 0 uses `step`. Higher rates catch shorter motion but take longer. |
| `min-area` | Minimum cleaned contour area at the reference canvas size (960px width cap); scaled by canvas area when width changes. |
| `threshold` | MOG2 variance threshold; lower is more sensitive. |
| `min-duration` | Minimum event span, in seconds. |
| `min-frames` | Minimum positive samples at the `step` rate. An explicit samples/sec rate converts this to the equivalent positive-motion time, rounded up to whole samples. |
| `gap` | Maximum quiet gap merged between active intervals. |

Quiet gaps can be included in merged events, so total motion time is the sum of accepted event spans, not the exact count of active video frames.

An explicit sampling rate also converts the five-sample warm-up to its equivalent time at the `step` rate, rounded up. This preserves the acceptance threshold in seconds within one sampling interval, but lower rates can still miss short motion. MOG2 history remains 300 samples, so its effective history in seconds varies with sampling rate. Changing analysis width scales the area threshold, not the 5×5 morphology kernel. The small canvas and lower sampling rate can miss small or brief motion; review a representative sample before processing a large archive.

## Performance measurement

For 12 days × 9 hours, the default rate analyzes 777,600 samples. Downsizing still requires decoding the original streams once; the speedup depends on source codec/resolution, hardware and disk throughput. Reanalysis with cached copies avoids that pass. NVIDIA CUDA and Linux VAAPI decoding/scaling are probed using the actual source; failure automatically retries with CPU FFmpeg. Tiny proxy encoding uses ultrafast CPU H.264. No GPU is required.

Each project analysis writes `profile.json` beside its summary. It records wall time, CPU time including child processes, realtime factor, source frames/sec, analysis samples/sec, and stage timings. CPU percentage uses 100% for one logical core. `proxy_prepare` includes cache validation and downsizing; `proxy_read` covers reading small copies. `source_frames` counts originals decoded on this run (zero when every copy is reused). For legacy decoders, `grab` includes decoder work; `retrieve` is not the entire decode cost. Preview pipe time includes backpressure, and the preview subprocess lifetime overlaps analysis rather than adding to it.

The benchmark runner runs cases sequentially in fresh worker processes on the same saved request:

```bash
venv/bin/python benchmark_analysis.py REQUEST.json BENCHMARK_DIRECTORY
# Resume only completed cases whose saved requests match:
venv/bin/python benchmark_analysis.py REQUEST.json BENCHMARK_DIRECTORY --resume
```

Use a request referencing a representative 30–60 minute section. The runner includes `proxy-256` and `proxy-256-cached` for cold/cache-reuse comparisons, alongside the original settings with preview disabled, experimental FFmpeg sampling/scaling, widths 640/480/320, rates 1/2/3/5 samples/sec, and OpenCV thread limits. Resolution, sampling and decoder comparisons disable previews to isolate analysis costs; thread comparisons retain previews. Results include temporal agreement, missed baseline events, additional events, and exact raw-sample equality. These are comparisons with the baseline, not ground-truth detection accuracy.

Project workers default to `"decoder": "proxy"`, `"preview": false`, width 256 and 2 samples/sec. `"proxy_cache": "/path/to/cache"` shares proxies between runs (the server uses the project directory); `"acceleration": "cpu"` forces the CPU path. Copies are reusable across detector threshold/date changes; changing width, rate, source content or project sampling offsets creates new cache entries.

For comparisons, explicit `"decoder": "opencv"`, `"decoder": "ffmpeg"`, and `"decoder": "ffmpeg-cuda"` retain the earlier direct-source pipelines. Set width 960, samples/sec 0 and `"preview": true` to reproduce the earlier defaults. The explicit `ffmpeg-cuda` experimental mode still requires accessible NVIDIA hardware and fails without it; the default proxy pipeline automatically falls back. `opencv_threads` or `MOTION_OPENCV_THREADS` overrides the default four-thread OpenCV limit. The benchmark preserves the old baseline defaults for comparison.

## Verification

```bash
venv/bin/python -m unittest discover -s tests -v
deno check server.ts
deno test tests/timeline_test.ts
```

Tests cover static/moving clips, fractional-FPS timing, cross-file event continuity, mixed sizes/FPS, proxy caching/corruption recovery, CPU fallback, recording-date detection, overnight separation, 12-day calendar projection, midnight splits, clean exports, reordered clips, original audio and silence across a file boundary.

Integration checks use isolated storage:

```bash
PORT=8095 MOTION_DATA=/tmp/motionmeasure-project-smoke MOTION_IMPORT_LEGACY=0 deno run -A server.ts
# In another terminal:
venv/bin/python tests/integration_projects.py http://127.0.0.1:8095
```

This tests project isolation, uploads, queue locking, persisted edits, exports, stale-analysis rejection and retained exports after reanalysis. For the browser check, launch a temporary Chrome profile with remote debugging on port 9224, then run `deno run -A tests/browser_projects.ts http://127.0.0.1:8095` after the integration test. It covers project creation, multi-upload, analysis, clip selection/trimming/reordering, original-resolution playback across files, rapid seeking, calendar date corrections, cache reuse, export downloads, reload persistence and mobile width.
