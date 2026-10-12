import "../timeline.js";

const { calendar, sourceAt, projectAt } = (globalThis as any).MotionTimeline;
function assert(value: unknown, message: string) {
  if (!value) throw Error(message);
}
function source(
  id: string,
  start: number,
  duration: number,
  recording_start: string | null,
) {
  return { id, start, end: start + duration, duration, recording_start };
}
Deno.test("12 recording days retain overnight gaps and seek to original offsets", () => {
  const sources = Array.from(
    { length: 12 },
    (_, i) =>
      source(
        String(i),
        i * 32400,
        32400,
        `2026-10-${String(i + 1).padStart(2, "0")}T08:00:00`,
      ),
  );
  const events = sources.map((s: any) => ({
    id: s.id,
    start: s.start + 60,
    end: s.start + 65,
  }));
  const result = calendar(sources, events);
  assert(
    result.days.length === 12,
    "108 hours must appear as 12 recording days",
  );
  const day = result.days[7];
  assert(day.motion.length === 1, "Motion must belong to its recording day");
  assert(
    projectAt(day, Date.parse("2026-10-08T08:01:02")) === 7 * 32400 + 62,
    "Wrong original project offset",
  );
  assert(
    projectAt(day, Date.parse("2026-10-08T03:00:00")) === null,
    "Overnight gaps must not seek into footage",
  );
  assert(
    sourceAt(sources, 32400).id === "1",
    "Exact boundary must select the next source",
  );
  assert(
    sourceAt(sources, 388800).id === "11",
    "End of project must clamp to final source",
  );
});
Deno.test("midnight-spanning motion is clipped into both calendar days", () => {
  const result = calendar([source("a", 0, 20, "2026-10-08T23:59:50")], [{
    id: "e",
    start: 5,
    end: 15,
  }]);
  assert(result.days.length === 2, "Missing midnight split");
  assert(result.days[0].motion[0].projectEnd === 10, "First day not clipped");
  assert(
    result.days[1].motion[0].projectStart === 10,
    "Second day seek target not clipped",
  );
});
Deno.test("unknown dates are not assigned invented calendar days", () => {
  const result = calendar([source("a", 0, 10, null)], []);
  assert(
    result.days.length === 0 && result.undated.length === 1,
    "Unknown date was fabricated",
  );
});
Deno.test("overlapping recordings remain separate seekable spans", () => {
  const result = calendar([
    source("a", 0, 10, "2026-10-08T09:00:00"),
    source("b", 10, 10, "2026-10-08T09:00:00"),
  ], [{ id: "a", start: 2, end: 4 }, { id: "b", start: 12, end: 14 }]);
  assert(
    result.days[0].motion[1].projectStart === 12,
    "Second recording lost its original seek position",
  );
});
