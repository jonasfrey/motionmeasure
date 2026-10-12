// Calendar projection is separate from the continuous edit/export clock.
globalThis.MotionTimeline = (() => {
  const dayKey = (date) =>
    `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${
      String(date.getDate()).padStart(2, "0")
    }`;
  function sourceAt(sources, time) {
    // Binary search keeps seeking cheap even with thousands of source files.
    let lo = 0, hi = sources.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (sources[mid].end <= time) lo = mid + 1;
      else hi = mid;
    }
    return sources[Math.min(lo, sources.length - 1)] || null;
  }
  function calendar(sources, segments) {
    const days = new Map();
    const undated = [];
    for (const source of sources) {
      const recorded = source.recording_start
        ? Date.parse(source.recording_start)
        : NaN;
      if (!Number.isFinite(recorded)) {
        undated.push(source);
        continue;
      }
      let instant = recorded;
      const end = recorded + source.duration * 1000;
      while (instant < end) {
        const date = new Date(instant);
        date.setHours(0, 0, 0, 0);
        const next = new Date(date);
        next.setDate(next.getDate() + 1); // Local day length can change at DST.
        const key = dayKey(date);
        if (!days.has(key)) {
          days.set(key, {
            key,
            start: +date,
            end: +next,
            recordings: [],
            motion: [],
          });
        }
        const day = days.get(key);
        const stop = Math.min(end, +next);
        const projectStart = source.start + (instant - recorded) / 1000;
        const projectEnd = source.start + (stop - recorded) / 1000;
        day.recordings.push({
          start: instant,
          end: stop,
          projectStart,
          projectEnd,
          source,
        });
        // Events are ordered and disjoint; skip the prefix without scanning it.
        let lo = 0, hi = segments.length;
        while (lo < hi) {
          const mid = (lo + hi) >>> 1;
          if (segments[mid].end <= projectStart) lo = mid + 1;
          else hi = mid;
        }
        for (
          let i = lo;
          i < segments.length && segments[i].start < projectEnd;
          i++
        ) {
          const event = segments[i];
          const a = Math.max(projectStart, event.start),
            b = Math.min(projectEnd, event.end);
          if (b > a) {
            day.motion.push({
              start: recorded + (a - source.start) * 1000,
              end: recorded + (b - source.start) * 1000,
              projectStart: a,
              projectEnd: b,
              event,
            });
          }
        }
        instant = stop;
      }
    }
    return {
      days: [...days.values()].sort((a, b) => a.start - b.start),
      undated,
    };
  }
  function projectAt(day, instant) {
    const recording = day.recordings.find((r) =>
      r.start <= instant && instant < r.end
    );
    return recording
      ? recording.projectStart + (instant - recording.start) / 1000
      : null;
  }
  return { calendar, dayKey, sourceAt, projectAt };
})();
