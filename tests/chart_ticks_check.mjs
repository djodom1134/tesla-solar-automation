// Table-driven check for the tick overlap-rejection logic in static/chart.js
// (pickNonOverlappingTicks / estimateLabelWidth). Run directly with
// `node tests/chart_ticks_check.mjs`, or via tests/test_chart_ticks.py, which
// shells out to this file so the durable, table-driven cases run against the
// exact module the browser loads -- no reimplementation in Python.
//
// Only the two pure helpers are exercised here; nothing in this file touches
// the DOM, so plain Node (no jsdom, no build step) is enough.

import assert from "node:assert/strict";
import { pickNonOverlappingTicks, estimateLabelWidth } from "../static/chart.js";

const xs = (kept) => kept.map((t) => t.x);

const cases = [
  {
    name: "evenly spaced candidates that all fit -- keeps everything",
    ticks: [
      { x: 0, width: 20 },
      { x: 50, width: 20 },
      { x: 100, width: 20 },
      { x: 150, width: 20 },
    ],
    expected: [0, 50, 100, 150],
  },
  {
    name: "dense cluster bunched by real time gaps collapses; ends survive",
    // Mirrors the real bug: samples land 60s apart in one stretch and 6000s+
    // apart elsewhere, on a chart whose x position is real elapsed time, not
    // row index -- "every Nth row" bunches labels here, overlap-rejection
    // does not.
    ticks: [
      { x: 0, width: 50 },
      { x: 10, width: 50 },
      { x: 14, width: 50 },
      { x: 18, width: 50 },
      { x: 22, width: 50 },
      { x: 400, width: 50 },
      { x: 800, width: 50 },
    ],
    expected: [0, 400, 800],
  },
  {
    name: "a middle candidate that fits its neighbor but crowds the mandatory last tick is dropped",
    ticks: [
      { x: 0, width: 20 },
      { x: 50, width: 20 }, // clears x=0 (gap 40 - 10 - 10 = 20 >= padding) but not x=62
      { x: 62, width: 20 }, // last -- always kept
    ],
    expected: [0, 62],
  },
  {
    name: "single candidate -- kept as-is",
    ticks: [{ x: 42, width: 30 }],
    expected: [42],
  },
  {
    name: "two candidates are both always kept, even though they overlap",
    ticks: [
      { x: 0, width: 50 },
      { x: 5, width: 50 },
    ],
    expected: [0, 5],
  },
  {
    name: "empty input",
    ticks: [],
    expected: [],
  },
  {
    name: "larger padding drops more middle ticks than the default",
    ticks: [
      { x: 0, width: 20 },
      { x: 40, width: 20 },
      { x: 80, width: 20 },
      { x: 120, width: 20 },
    ],
    padding: 30,
    expected: [0, 120],
  },
  {
    name: "same layout at default padding keeps every tick",
    ticks: [
      { x: 0, width: 20 },
      { x: 40, width: 20 },
      { x: 80, width: 20 },
      { x: 120, width: 20 },
    ],
    expected: [0, 40, 80, 120],
  },
];

let failures = 0;
for (const c of cases) {
  const kept = pickNonOverlappingTicks(c.ticks, c.padding ?? 4);
  try {
    assert.deepEqual(xs(kept), c.expected, c.name);
    console.log(`ok - ${c.name}`);
  } catch (err) {
    failures += 1;
    console.error(`FAIL - ${c.name}`);
    console.error(err.message);
  }
}

// estimateLabelWidth is what pickNonOverlappingTicks' callers actually feed
// it: longer labels should measure wider at a fixed font size, and an empty
// label has no width.
try {
  assert.ok(
    estimateLabelWidth("11:03 AM", 11) > estimateLabelWidth("1 PM", 11),
    "estimateLabelWidth should grow with character count"
  );
  assert.equal(estimateLabelWidth("", 11), 0, "empty label has zero width");
  console.log("ok - estimateLabelWidth scales with character count");
} catch (err) {
  failures += 1;
  console.error("FAIL - estimateLabelWidth");
  console.error(err.message);
}

if (failures > 0) {
  console.error(`${failures} case(s) failed`);
  process.exit(1);
}
console.log("all chart tick cases passed");
