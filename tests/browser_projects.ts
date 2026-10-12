// Run after integration_projects.py, with Chrome remote debugging on port 9224.
const origin = Deno.args[0] || "http://127.0.0.1:8095";
const fixture = JSON.parse(
  await Deno.readTextFile("/tmp/motionmeasure-project-browser.json"),
);
const tabs = await (await fetch("http://127.0.0.1:9224/json")).json();
const ws = new WebSocket(tabs[0].webSocketDebuggerUrl);
await new Promise((r) => ws.onopen = r);
let id = 0;
let onLoad: (() => void) | null = null;
const pending = new Map();
const errors: string[] = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Page.loadEventFired") onLoad?.();
  if (m.id) {
    pending.get(m.id)?.(m);
    pending.delete(m.id);
  }
  if (m.method === "Runtime.exceptionThrown") {
    errors.push(JSON.stringify(m.params));
  }
};
function send(method: string, params = {}) {
  return new Promise<any>((resolve, reject) => {
    const n = ++id;
    pending.set(
      n,
      (m: any) =>
        m.error ? reject(Error(JSON.stringify(m.error))) : resolve(m.result),
    );
    ws.send(JSON.stringify({ id: n, method, params }));
  });
}
async function evaluate(expression: string) {
  const r = await send("Runtime.evaluate", {
    expression,
    userGesture: true,
    awaitPromise: true,
    returnByValue: true,
  });
  if (r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
async function until(expression: string) {
  for (let i = 0; i < 200; i++) {
    if (await evaluate(expression)) return;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw Error("Timed out: " + expression);
}
await send("Runtime.enable");
await send("Page.enable");
await send("DOM.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1500,
  height: 1200,
  deviceScaleFactor: 1,
  mobile: false,
});
const loaded = new Promise<void>((r) => onLoad = r);
await send("Page.navigate", { url: origin });
await loaded;
onLoad = null;
await until("typeof project !== 'undefined' && !!project");
errors.length = 0; // Ignore exceptions from the page that was open before navigation.
const name = `Garden camera · Browser test ${Date.now()}`;
await evaluate(
  `$('new-project').click(); $('project-name').value=${
    JSON.stringify(name)
  }; $('project-form').requestSubmit()`,
);
await until(`project?.name===${JSON.stringify(name)}`);
const document = await send("DOM.getDocument");
const input = await send("DOM.querySelector", {
  nodeId: document.root.nodeId,
  selector: "#upload",
});
await send("DOM.setFileInputFiles", {
  nodeId: input.nodeId,
  files: fixture.paths,
});
await until("project.files.length===2 && !uploading");
await evaluate(
  "for(const [key,value] of Object.entries({'step':1,'min-area':80,'min-duration':.3,'min-frames':2,'gap':.2}))document.querySelector(`[data-key='${key}']`).value=value; $('analyze').click()",
);
await until(
  "project.result?.segment_count===2 && !project.busy && video.readyState>=2",
);
if (!await evaluate("project.result.segments[0].source_ids.length===2")) {
  throw Error("Missing continuous seam event");
}
await evaluate("document.querySelector('.event input').click()");
await until("draft.clips.length===1 && $('save-state').textContent==='Saved'");
await evaluate(
  "const start=document.querySelector('.clip .start'); start.value=5; start.dispatchEvent(new Event('change')); const end=document.querySelector('.clip .end'); end.value=7; end.dispatchEvent(new Event('change'))",
);
await evaluate(
  "document.querySelectorAll('.event input')[1].click(); document.querySelector('.clip .down').click()",
);
await until(
  "draft.clips.length===2 && draft.clips[0].segment_id==='event-2' && $('save-state').textContent==='Saved'",
);
await evaluate("$('preview-edit').click()");
await until("projectTime()>9 && !video.paused");
if (
  !await evaluate(
    "video.videoWidth===320 && video.src.includes('/sources/') && project.result.out_size[0]===256",
  )
) {
  throw Error(
    "Playback must use original resolution, separate from analysis copies",
  );
}
await evaluate("stopEditPreview(); startEditPreview([{start:5,end:7}])");
await until(
  "playbackSource.id===project.result.sources[1].id && projectTime()>6 && !video.paused",
);
await until(
  "!editPlayback && video.paused && projectTime()>=6.9 && projectTime()<7.3",
);
await evaluate("playAt(10); playAt(1); playAt(10.4)");
await until(
  "playbackSource.id===project.result.sources[1].id && projectTime()>=10.4 && !video.paused",
);
await evaluate("stopEditPreview(); $('export').click()");
await until(
  "project.exports.length===1 && !project.busy && !!$('export-result').querySelector('a')",
);
if (!await evaluate("draft.audio && !draft.boxes")) {
  throw Error("Wrong export defaults");
}
const exported = await evaluate(
  "$('export-result').querySelector('video').src",
);
const response = await fetch(exported, { headers: { Range: "bytes=0-127" } });
if (
  response.status !== 206 || (await response.arrayBuffer()).byteLength !== 128
) throw Error("Export download failed");
const reloaded = new Promise<void>((r) => onLoad = r);
await send("Page.reload");
await reloaded;
onLoad = null;
await until("project?.exports.length===1 && draft?.clips.length===2");
if (
  !await evaluate(
    "draft.clips[0].segment_id==='event-2' && draft.clips[1].start===5 && draft.clips[1].end===7",
  )
) throw Error("Saved edit was not restored");
// Correct dates through the file controls, then verify day filtering and original seeks.
for (let i = 0; i < 2; i++) {
  await evaluate(`(() => {
    const row = document.querySelectorAll('.recording-date')[${i}];
    row.querySelector('input').value='2026-10-${
    String(8 + i).padStart(2, "0")
  }T09:00:00';
    row.querySelector('.save-date').click();
  })()`);
  await until(
    `project.files[${i}].recording_start?.startsWith('2026-10-${
      String(8 + i).padStart(2, "0")
    }T09:00')`,
  );
}
await evaluate(
  "for(const [key,value] of Object.entries({'step':1,'min-area':80,'min-duration':.3,'min-frames':2,'gap':.2}))document.querySelector(`[data-key='${key}']`).value=value; $('analyze').click()",
);
await until("!project.busy && !project.stale && calendarData.days.length===2");
if (
  !await evaluate(
    "project.result.proxies.every(p => p.cached) && document.querySelectorAll('#calendar button:not(:disabled)').length===2",
  )
) {
  throw Error(
    "Date corrections should reuse analysis copies and produce two calendar days",
  );
}
await evaluate(
  "document.querySelector('[data-day=\"2026-10-09\"]').click(); document.querySelector('#timeline button').click()",
);
await until(
  "selectedDay==='2026-10-09' && playbackSource.id===project.result.sources[1].id && !video.paused",
);
if (
  !await evaluate(
    "video.videoWidth===320 && $('duration').textContent==='24:00:00' && $('day-label').textContent.includes('2026')",
  )
) {
  throw Error("Day timeline must retain full-quality original playback");
}
await evaluate("stopEditPreview()");
await evaluate("$('notice').hidden=true");
const shot = await send("Page.captureScreenshot", {
  format: "png",
  captureBeyondViewport: true,
});
await Deno.writeFile(
  "/tmp/motionmeasure-project-desktop.png",
  Uint8Array.from(atob(shot.data), (c) => c.charCodeAt(0)),
);
await send("Emulation.setDeviceMetricsOverride", {
  width: 390,
  height: 844,
  deviceScaleFactor: 1,
  mobile: true,
});
if (!await evaluate("document.documentElement.scrollWidth<=innerWidth")) {
  throw Error("Mobile overflow");
}
if (errors.length) throw Error(errors.join("\n"));
console.log(
  "PASS: browser project creation, multi-upload, continuous analysis, selection, trim, reorder, preview, MP4 export, reload persistence, original-resolution cross-file playback, calendar correction/cache reuse and mobile layout",
);
ws.close();
