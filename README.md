# Motionmeasure

A local video motion detector with project libraries, a continuous timeline, and a highlights editor. All processing and storage stay on this machine. Uses OpenCV MOG2 and FFmpeg; no API keys or GPU required.

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
4. Click **Analyze all files**. The files are analyzed back-to-back on one project clock, beginning at zero. The background model stays alive across file boundaries, and motion crossing a boundary can be one detection. This does not infer gaps or overlaps from recording dates in filenames.
5. Click a detection thumbnail or timeline marker to play it. Dotted timeline lines mark file boundaries. Green boxes show raw motion; event duration/persistence filters decide which detections appear in the list. **JSON** downloads all events and sampled bounding boxes with both project and source timestamps.
6. Check interesting detections to add them to **Your highlights video**. Reorder clips with arrows, remove clips, or adjust start/end times in project seconds. A clip may span several source files. The selection, order, trim times and export options are automatically saved with the project.
7. **Preview selection** plays the selected ranges using the analysis preview. Then click **Export highlights MP4**. When rendering finishes, download or play the completed MP4 below the editor. An edit JSON download records the selected ranges and export settings.

### Export behavior

- Default: **original footage and audio, without motion boxes**.
- Outputs are normalized to the first source's aspect ratio, up to 1920×1080, at 30 fps, with H.264 video and AAC stereo audio. Other aspect ratios are letterboxed. Small sources are not enlarged to 1080p.
- Files without audio contribute silence. Disabling original audio makes the entire export silent.
- Optional motion boxes use the downscaled, sampled analysis preview as video while keeping original source audio. The default clean export uses original video frames.
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
| `server.ts`, `index.html` | Local web server and interface | Yes |
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
  runs/<analysis-id>/
    request.json               # immutable analysis request
    summary.json               # timeline/events without the large sample list
    events.json                # complete downloadable motion data
    preview.mp4                # continuous annotated review video
    event-N.jpg                # detection thumbnails
    status.json
    profile.json               # stage timings, CPU usage and throughput
  exports/<export-id>/
    request.json
    highlights.mp4
    edit.json
    status.json
```

Original footage is preserved. Analysis runs and exports are retained on disk. Temporary export parts are removed after rendering. Raw motion samples stream to disk during analysis rather than accumulating in RAM. Set `MOTION_DATA=/path/to/projects` to change storage; set `MOTION_IMPORT_LEGACY=0` to disable automatic import, useful for testing.

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

Box coordinates use the `out_size` preview canvas, with origin at top-left. Sources with a different aspect ratio are letterboxed. To map a box back, undo that source's centered letterbox offset, then divide by `min(out_width/source_width, out_height/source_height)` (accounting for rounded resized dimensions).

By default, the sample interval is `step / first_source_fps` seconds throughout the project, even if later sources use different frame rates. An explicit `samples-per-second` setting uses its reciprocal instead. Each sample uses the source frame containing that instant. Source timestamps can differ by less than one source frame from the exact project sampling time. Preview length may extend by less than one sample interval due to its final frame.

Times use frame count / reported FPS. Convert variable-frame-rate footage to constant frame rate first when exact source timing is important. Scene changes, moving cameras, lights and foliage can trigger motion. A single warm-up happens at project start (five samples at the default sampling settings). This detects motion regions; it does not recognize people or track object identities.

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
| `width` | Analysis canvas width cap, default 960; height follows the first source's aspect ratio. |
| `samples-per-second` | Explicit analysis rate; 0 (default) uses `step`. |
| `min-area` | Minimum cleaned contour area at the default canvas size (960px width cap); scaled by canvas area when width changes. |
| `threshold` | MOG2 variance threshold; lower is more sensitive. |
| `min-duration` | Minimum event span, in seconds. |
| `min-frames` | Minimum positive samples at the `step` rate. An explicit samples/sec rate converts this to the equivalent positive-motion time, rounded up to whole samples. |
| `gap` | Maximum quiet gap merged between active intervals. |

Quiet gaps can be included in merged events, so total motion time is the sum of accepted event spans, not the exact count of active video frames.

An explicit sampling rate also converts the five-sample warm-up to its equivalent time at the `step` rate, rounded up. This preserves the acceptance threshold in seconds within one sampling interval, but lower rates can still miss short motion. MOG2 history remains 300 samples, so its effective history in seconds varies with sampling rate. Changing analysis width scales the area threshold, not the 5×5 morphology kernel. Defaults preserve the original detection settings.

## Performance measurement

Each project analysis writes `profile.json` beside its summary. It records wall time, CPU time including child processes, realtime factor, source frames/sec, analysis samples/sec, and stage timings. CPU percentage uses 100% for one logical core. `grab` includes decoder work; `retrieve` is not the entire decode cost. Preview pipe time includes backpressure, and the preview subprocess lifetime overlaps analysis rather than adding to it.

The benchmark runner runs cases sequentially in fresh worker processes on the same saved request:

```bash
venv/bin/python benchmark_analysis.py REQUEST.json BENCHMARK_DIRECTORY
# Resume only completed cases whose saved requests match:
venv/bin/python benchmark_analysis.py REQUEST.json BENCHMARK_DIRECTORY --resume
```

Use a request referencing a representative 30–60 minute section. The runner compares the original settings with preview disabled, experimental FFmpeg sampling/scaling, widths 640/480/320, rates 1/2/3/5 samples/sec, and OpenCV thread limits. Resolution, sampling and decoder comparisons disable previews to isolate analysis costs; thread comparisons retain previews. Results include temporal agreement, missed baseline events, additional events, and exact raw-sample equality. These are comparisons with the baseline, not ground-truth detection accuracy.

Advanced saved-request options are `"preview": false`, `"decoder": "ffmpeg"`, and `"opencv_threads": 1`. Preview suppression and the experimental decoder are benchmark/worker options; the web app continues to generate the preview required by its player. Clean exports work without a preview; boxed exports require one. The FFmpeg sampler preserves the existing source-frame-index clock and scales selected frames before transferring them to Python. It still decodes the source stream, and its scaling can change pixel values and detections.

On a host with usable NVIDIA device access, `"decoder": "ffmpeg-cuda"` enables the experimental NVDEC path for 8-bit footage. It selects frames before downloading them from the GPU; scaling and MOG2 remain on the CPU. This mode fails if hardware decoding is unavailable rather than silently falling back. Run matching host comparisons with `--cases host-opencv-4 host-ffmpeg-4 host-nvdec-4`; these GPU-dependent cases are excluded from the default benchmark suite.

Project analysis caps OpenCV at four threads by default (or retains a lower existing limit). `MOTION_OPENCV_THREADS` overrides this limit; an explicit request value takes precedence. No GPU decoder is enabled automatically. Benchmark results depend on the host and footage; generate reports locally with the benchmark runner.

## Verification

```bash
venv/bin/python -m unittest discover -s tests -v
deno check server.ts
```

Tests cover static/moving clips, fractional-FPS timing, cross-file event continuity, mixed source sizes/FPS, clean exports, reordered clips, original audio and silence across a file boundary.

Integration checks use isolated storage:

```bash
PORT=8095 MOTION_DATA=/tmp/motionmeasure-project-smoke MOTION_IMPORT_LEGACY=0 deno run -A server.ts
# In another terminal:
venv/bin/python tests/integration_projects.py http://127.0.0.1:8095
```

This tests project isolation, uploads, queue locking, persisted edits, exports, stale-analysis rejection and retained exports after reanalysis. For the browser check, launch a temporary Chrome profile with remote debugging on port 9224, then run `deno run -A tests/browser_projects.ts http://127.0.0.1:8095` after the integration test. It covers project creation, multi-upload, analysis, clip selection/trimming/reordering, playback, export downloads, reload persistence and mobile width.
