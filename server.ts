// Local project library, continuous analysis queue and highlight export jobs.
const ROOT = decodeURIComponent(new URL(".", import.meta.url).pathname);
const DATA =
  (Deno.env.get("MOTION_DATA") || ROOT + "projects").replace(/\/$/, "") + "/";
const PY = Deno.env.get("PYTHON") || ROOT + "venv/bin/python";
const PORT = Number(Deno.env.get("PORT") || 8088);
const EXT = /\.(mp4|mov|mkv|avi|webm|m4v)$/i;
const MAX_UPLOAD = 8 * 1024 ** 3;
const ID =
  /^(?:legacy|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;
const json = (obj: unknown, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
const safe = (name: string) =>
  !!name && !/[\/\\\x00-\x1f]/.test(name) && !name.startsWith(".") &&
  name.length < 240;
interface Source {
  id: string;
  name: string;
  path: string;
  size: number;
  recording_start?: string | null;
}
interface Clip {
  segment_id: string;
  start: number;
  end: number;
}
interface Draft {
  analysis_id: string;
  clips: Clip[];
  audio: boolean;
  boxes: boolean;
}
interface Project {
  id: string;
  name: string;
  created: string;
  revision: number;
  files: Source[];
  analysis?: { id: string; revision: number };
  analysisJob?: string;
  exportJob?: string;
  draft?: Draft;
  exports: { id: string; created: string; clip_count: number }[];
}
interface Job {
  projectId: string;
  id: string;
  action: "analyze" | "export";
  out: string;
  revision: number;
  clipCount: number;
}
const queue: Job[] = [];
let active: Job | null = null;
const locks = new Map<string, Promise<unknown>>();
const busy = (id: string) =>
  active?.projectId === id || queue.some((j) => j.projectId === id);
const dir = (id: string) => {
  if (!ID.test(id)) throw Error("Invalid project ID");
  return DATA + id + "/";
};
async function readJson(path: string) {
  try {
    return JSON.parse(await Deno.readTextFile(path));
  } catch (e) {
    if (e instanceof Deno.errors.NotFound) return null;
    throw e;
  }
}
async function writeJson(path: string, obj: unknown) {
  const tmp = path + "." + crypto.randomUUID() + ".tmp";
  await Deno.writeTextFile(tmp, JSON.stringify(obj));
  await Deno.rename(tmp, path);
}
async function load(id: string): Promise<Project> {
  const p = await readJson(dir(id) + "project.json");
  if (!p) throw Error("Project not found");
  return p;
}
async function save(p: Project) {
  await writeJson(dir(p.id) + "project.json", p);
}
function locked<T>(id: string, fn: () => Promise<T>): Promise<T> {
  const next = (locks.get(id) || Promise.resolve()).catch(() => {}).then(fn);
  locks.set(id, next);
  void next.finally(() => {
    if (locks.get(id) === next) locks.delete(id);
  }).catch(() => {});
  return next;
}
function editable(id: string) {
  if (busy(id)) throw Error("Wait for this project’s current job to finish.");
}
function settings(body: Record<string, unknown>) {
  const result: Record<string, number> = {};
  for (
    const [key, min, max, integer] of [
      ["step", 1, 120, true],
      ["width", 32, 256, true],
      ["samples-per-second", 0, 120, false],
      ["min-area", 1, 1000000, false],
      ["min-duration", 0, 3600, false],
      ["gap", 0, 60, false],
      ["threshold", 1, 255, false],
      ["min-frames", 1, 10000, true],
    ] as const
  ) {
    if (body[key] === undefined) continue;
    const n = Number(body[key]);
    if (
      !Number.isFinite(n) || n < min || n > max ||
      (integer && !Number.isInteger(n))
    ) throw Error("Invalid setting: " + key);
    result[key] = n;
  }
  return result;
}
async function jobStatus(p: Project, action: "analyze" | "export") {
  const id = action === "analyze" ? p.analysisJob : p.exportJob;
  if (!id) return null;
  const folder = action === "analyze" ? "runs" : "exports";
  if (queue.some((j) => j.id === id)) {
    return { state: "queued", pct: 0, message: "Waiting in queue", id };
  }
  const state = await readJson(dir(p.id) + `${folder}/${id}/status.json`);
  if (active?.id === id) {
    return { ...(state || { state: "processing", pct: 0 }), id };
  }
  if (!state || ["processing", "queued"].includes(state.state)) {
    return {
      state: "error",
      error: "Job was interrupted. Please run it again.",
      id,
    };
  }
  return { ...state, id };
}
async function publicProject(p: Project) {
  const result = p.analysis
    ? await readJson(dir(p.id) + `runs/${p.analysis.id}/summary.json`)
    : null;
  return {
    ...p,
    files: p.files.map(({ path: _path, ...f }) => f),
    busy: busy(p.id),
    stale: !!p.analysis && p.analysis.revision !== p.revision,
    result,
    analysisStatus: await jobStatus(p, "analyze"),
    exportStatus: await jobStatus(p, "export"),
  };
}
async function validateDraft(p: Project, body: Draft): Promise<Draft> {
  if (
    !p.analysis || p.analysis.revision !== p.revision ||
    body.analysis_id !== p.analysis.id
  ) throw Error("Analyze all files before selecting or exporting clips.");
  const summary = await readJson(
    dir(p.id) + `runs/${p.analysis.id}/summary.json`,
  );
  const ids = new Set(summary.segments.map((s: { id: string }) => s.id));
  if (body.boxes === true && summary.preview_available === false) {
    throw Error(
      "Motion boxes are unavailable for this analysis. Export original footage instead.",
    );
  }
  if (!Array.isArray(body.clips) || body.clips.length > 1000) {
    throw Error("Select at most 1000 clips.");
  }
  const seen = new Set();
  const clips = body.clips.map((c) => {
    if (!ids.has(c.segment_id) || seen.has(c.segment_id)) {
      throw Error("Invalid or duplicate detection.");
    }
    seen.add(c.segment_id);
    if (
      typeof c.start !== "number" || typeof c.end !== "number" ||
      !Number.isFinite(c.start + c.end) || c.start < 0 || c.end <= c.start ||
      c.end > summary.duration + 1e-6
    ) throw Error("Clip start and end must lie within the project timeline.");
    return { segment_id: c.segment_id, start: c.start, end: c.end };
  });
  return {
    analysis_id: p.analysis.id,
    clips,
    audio: body.audio !== false,
    boxes: body.boxes === true,
  };
}
async function pump() {
  if (active || !queue.length) return;
  const job = queue.shift()!;
  active = job;
  try {
    await writeJson(job.out + "/status.json", {
      state: "processing",
      pct: 0,
      message: "Starting",
    });
    const child = new Deno.Command(PY, {
      args: [
        ROOT + "project_worker.py",
        job.action,
        job.out + "/request.json",
        job.out,
      ],
      stdout: "null",
      stderr: "piped",
    }).spawn();
    const result = await child.output();
    if (!result.success) {
      throw Error(
        new TextDecoder().decode(result.stderr).slice(-1500) || "Worker failed",
      );
    }
    await locked(job.projectId, async () => {
      const p = await load(job.projectId);
      if (job.action === "analyze") {
        p.analysis = { id: job.id, revision: job.revision };
        p.draft = { analysis_id: job.id, clips: [], audio: true, boxes: false };
      } else {p.exports.push({
          id: job.id,
          created: new Date().toISOString(),
          clip_count: job.clipCount,
        });}
      await save(p);
    });
  } catch (error) {
    await writeJson(job.out + "/status.json", {
      state: "error",
      error: String(error),
    });
  } finally {
    active = null;
    void pump();
  }
}
await Deno.mkdir(DATA, { recursive: true });
// Keep previously uploaded footage available without moving or duplicating large files.
if (
  Deno.env.get("MOTION_IMPORT_LEGACY") !== "0" &&
  !await readJson(DATA + "legacy/project.json")
) {
  const files: Source[] = [];
  for await (const f of Deno.readDir(ROOT)) {
    if (f.isFile && EXT.test(f.name)) {
      files.push({
        id: crypto.randomUUID(),
        name: f.name,
        path: ROOT + f.name,
        size: (await Deno.stat(ROOT + f.name)).size,
      });
    }
  }
  if (files.length) {
    files.sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { numeric: true })
    );
    await Deno.mkdir(DATA + "legacy", { recursive: true });
    await save({
      id: "legacy",
      name: "Previously uploaded videos",
      created: new Date().toISOString(),
      revision: 1,
      files,
      exports: [],
    });
  }
}
async function serveFile(
  path: string,
  req: Request,
  type: string,
  download = false,
) {
  let stat;
  try {
    stat = await Deno.stat(path);
  } catch {
    return new Response("Not found", { status: 404 });
  }
  if (!stat.isFile) return new Response("Not found", { status: 404 });
  const headers = new Headers({
    "content-type": type,
    "accept-ranges": "bytes",
    "cache-control": "no-cache",
  });
  if (download) headers.set("content-disposition", "attachment");
  let start = 0, end = stat.size - 1, status = 200;
  const range = req.headers.get("range");
  if (range) {
    const m = /^bytes=(\d*)-(\d*)$/.exec(range);
    if (!m || (!m[1] && !m[2])) {
      return new Response(null, {
        status: 416,
        headers: { "content-range": `bytes */${stat.size}` },
      });
    }
    start = m[1] ? Number(m[1]) : Math.max(0, stat.size - Number(m[2]));
    end = m[1] && m[2] ? Math.min(Number(m[2]), end) : end;
    if (start > end || start >= stat.size) {
      return new Response(null, {
        status: 416,
        headers: { "content-range": `bytes */${stat.size}` },
      });
    }
    status = 206;
    headers.set("content-range", `bytes ${start}-${end}/${stat.size}`);
  }
  headers.set("content-length", String(Math.max(0, end - start + 1)));
  if (req.method === "HEAD") return new Response(null, { status, headers });
  const file = await Deno.open(path);
  await file.seek(start, Deno.SeekMode.Start);
  let remaining = end - start + 1, closed = false;
  const close = () => {
    if (!closed) {
      closed = true;
      file.close();
    }
  };
  const stream = new ReadableStream({
    async pull(controller) {
      try {
        if (remaining <= 0) {
          close();
          controller.close();
          return;
        }
        const buf = new Uint8Array(Math.min(65536, remaining));
        const n = await file.read(buf);
        if (n === null) {
          close();
          controller.close();
          return;
        }
        remaining -= n;
        controller.enqueue(buf.subarray(0, n));
      } catch (e) {
        close();
        controller.error(e);
      }
    },
    cancel() {
      close();
    },
  });
  return new Response(stream, { status, headers });
}
async function handler(req: Request): Promise<Response> {
  const url = new URL(req.url);
  if (
    req.method === "POST" && req.headers.has("origin") &&
    req.headers.get("origin") !== url.origin
  ) return json({ error: "Origin not allowed" }, 403);
  try {
    if (url.pathname === "/" && req.method === "GET") {
      return serveFile(ROOT + "index.html", req, "text/html; charset=utf-8");
    }
    if (url.pathname === "/api/projects") {
      if (req.method === "POST") {
        const body = await req.json();
        const name = typeof body.name === "string" ? body.name.trim() : "";
        if (!name || name.length > 100) {
          throw Error("Enter a project name (1–100 characters).");
        }
        const p: Project = {
          id: crypto.randomUUID(),
          name,
          created: new Date().toISOString(),
          revision: 1,
          files: [],
          exports: [],
        };
        await Deno.mkdir(dir(p.id));
        await save(p);
        return json(await publicProject(p), 201);
      }
      const projects = [];
      for await (const d of Deno.readDir(DATA)) {
        if (d.isDirectory && ID.test(d.name)) {
          const p = await load(d.name);
          projects.push({
            id: p.id,
            name: p.name,
            created: p.created,
            file_count: p.files.length,
            busy: busy(p.id),
          });
        }
      }
      projects.sort((a, b) => b.created.localeCompare(a.created));
      return json({ projects });
    }
    const match =
      /^\/api\/projects\/([^/]+)(?:\/(upload|order|recording|analyze|draft|export))?$/
        .exec(url.pathname);
    if (match) {
      const [, id, action] = match;
      if (!action && req.method === "GET") {
        return json(await publicProject(await load(id)));
      }
      if (req.method !== "POST") {
        return json({ error: "Method not allowed" }, 405);
      }
      return await locked(id, async () => {
        const p = await load(id);
        editable(id);
        if (action === "upload") {
          const name = url.searchParams.get("name") || "";
          if (!safe(name) || !EXT.test(name) || !req.body) {
            throw Error(
              "Choose a nonempty MP4, MOV, MKV, AVI, WebM or M4V video.",
            );
          }
          const fileId = crypto.randomUUID();
          const saved = fileId +
            name.slice(name.lastIndexOf(".")).toLowerCase();
          await Deno.mkdir(dir(id) + "uploads", { recursive: true });
          const path = dir(id) + "uploads/" + saved;
          let size = 0;
          try {
            const file = await Deno.open(path + ".partial", {
              createNew: true,
              write: true,
            });
            await req.body.pipeThrough(
              new TransformStream({
                transform(chunk, ctrl) {
                  size += chunk.length;
                  if (size > MAX_UPLOAD) {
                    throw Error("Upload exceeds 8 GB limit.");
                  }
                  ctrl.enqueue(chunk);
                },
              }),
            ).pipeTo(file.writable);
            if (!size) throw Error("Empty upload");
            await Deno.rename(path + ".partial", path);
            p.files.push({ id: fileId, name, path, size });
            p.revision++;
            await save(p);
          } catch (e) {
            await Deno.remove(path + ".partial").catch(() => {});
            await Deno.remove(path).catch(() => {});
            throw e;
          }
          return json({ id: fileId, name });
        }
        if (action === "order") {
          const body = await req.json();
          if (
            !Array.isArray(body.ids) || body.ids.length !== p.files.length ||
            new Set(body.ids).size !== p.files.length ||
            body.ids.some((x: string) => !p.files.some((f) => f.id === x))
          ) {
            throw Error(
              "File order must contain every project file exactly once.",
            );
          }
          if (body.ids.some((x: string, i: number) => p.files[i].id !== x)) {
            p.files = body.ids.map((x: string) =>
              p.files.find((f) => f.id === x)!
            );
            p.revision++;
            await save(p);
          }
          return json({ ok: true });
        }
        if (action === "recording") {
          const body = await req.json();
          const file = p.files.find((f) => f.id === body.id);
          if (!file) throw Error("Source not found");
          const value = body.recording_start;
          if (
            value !== null && (typeof value !== "string" ||
              !/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)?$/
                .test(value) ||
              !Number.isFinite(Date.parse(value)))
          ) {
            throw Error("Choose a valid recording date and time.");
          }
          if (value !== null) {
            const [year, month, day, hour, minute, second = 0] = value.slice(
              0,
              19,
            ).split(/\D/).map(Number);
            const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
            if (
              year < 1970 || month < 1 || month > 12 || day < 1 ||
              day > daysInMonth ||
              hour > 23 || minute > 59 || second > 59
            ) {
              throw Error("Choose a valid recording date and time.");
            }
          }
          file.recording_start = value;
          p.revision++;
          await save(p);
          return json({ ok: true });
        }
        if (action === "draft") {
          p.draft = await validateDraft(p, await req.json());
          await save(p);
          return json({ ok: true });
        }
        if (action === "analyze" || action === "export") {
          if (!p.files.length) throw Error("Upload videos first.");
          const body = await req.json();
          const jobId = crypto.randomUUID();
          const out = dir(id) + (action === "analyze" ? "runs/" : "exports/") +
            jobId;
          let request;
          if (action === "analyze") {
            request = {
              id: p.id,
              name: p.name,
              revision: p.revision,
              run_id: jobId,
              files: p.files,
              settings: settings(body),
              decoder: "proxy",
              preview: false,
              proxy_cache: dir(id) + "proxies",
            };
            p.analysisJob = jobId;
          } else {
            p.draft = await validateDraft(p, body);
            if (!p.draft.clips.length) {
              throw Error("Add at least one detection to your highlights.");
            }
            request = {
              ...p.draft,
              project_id: id,
              files: p.files,
              analysis_dir: dir(id) + `runs/${p.analysis!.id}`,
            };
            p.exportJob = jobId;
          }
          await Deno.mkdir(out, { recursive: true });
          await writeJson(out + "/request.json", request);
          await writeJson(out + "/status.json", {
            state: "queued",
            pct: 0,
            message: "Waiting in queue",
          });
          await save(p);
          queue.push({
            projectId: id,
            id: jobId,
            action,
            out,
            revision: p.revision,
            clipCount: p.draft?.clips.length || 0,
          });
          void pump();
          return json({ ok: true, job_id: jobId }, 202);
        }
        return json({ error: "Not found" }, 404);
      });
    }
    if (url.pathname === "/timeline.js" && req.method === "GET") {
      return serveFile(ROOT + "timeline.js", req, "text/javascript");
    }
    const original = /^\/media\/([^/]+)\/sources\/([^/]+)$/.exec(url.pathname);
    if (original && ["GET", "HEAD"].includes(req.method)) {
      const [, projectId, sourceId] = original;
      const p = await load(projectId);
      const source = p.files.find((f) => f.id === sourceId);
      if (!source) return json({ error: "Not found" }, 404);
      const ext = source.name.split(".").pop()!.toLowerCase();
      const types: Record<string, string> = {
        mp4: "video/mp4",
        m4v: "video/mp4",
        mov: "video/quicktime",
        webm: "video/webm",
        mkv: "video/x-matroska",
        avi: "video/x-msvideo",
      };
      return serveFile(
        source.path,
        req,
        types[ext] || "application/octet-stream",
        url.searchParams.has("download"),
      );
    }
    const media = /^\/media\/([^/]+)\/(runs|exports)\/([^/]+)\/([^/]+)$/.exec(
      url.pathname,
    );
    if (media) {
      const [, projectId, kind, runId, name] = media;
      if (
        !ID.test(runId) ||
        !/^(preview\.mp4|events\.json|event-\d+\.jpg|highlights\.mp4|edit\.json)$/
          .test(name)
      ) return json({ error: "Not found" }, 404);
      const path = dir(projectId) + `${kind}/${runId}/${name}`;
      return serveFile(
        path,
        req,
        name.endsWith(".mp4")
          ? "video/mp4"
          : name.endsWith(".jpg")
          ? "image/jpeg"
          : "application/json",
        url.searchParams.has("download"),
      );
    }
    return json({ error: "Not found" }, 404);
  } catch (error) {
    return json({ error: String(error) }, 400);
  }
}
Deno.serve({ hostname: "127.0.0.1", port: PORT }, handler);
