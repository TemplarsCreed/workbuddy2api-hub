/* The rendered heatmap advances across local midnight with the board snapshot. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const start = html.indexOf('function buildHeatGrid(heat)');
const end = html.indexOf('const dimDefs', start);
assert.ok(start > 0 && end > start);

const els = new Map();
const element = id => {
  if (!els.has(id)) els.set(id, {style: {}});
  return els.get(id);
};
const RealDate = Date;
let now = new RealDate(2026, 8, 21, 23, 58).getTime();
class FakeDate extends RealDate {
  constructor(value) { super(value === undefined ? now : value); }
}
const fmtDate = epoch => {
  const d = new RealDate(epoch * 1000);
  return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'),
    String(d.getDate()).padStart(2, '0')].join('-');
};
const factory = new Function('$', 'fmtDate', 'fmtCompact', 'Date',
  html.slice(start, end) + '\nreturn buildHeatGrid;');
const build = factory(element, fmtDate, String, FakeDate);
const series = [
  {at: new RealDate(2026, 8, 21).getTime() / 1000, requests: 4, total_tokens: 40},
  {at: new RealDate(2026, 8, 22).getTime() / 1000, requests: 7, total_tokens: 70},
];
build({series: series.slice(0, 1)});
assert.match(element('hmGrid').innerHTML, /data-d="2026-09-21" data-r="4"/);
assert.doesNotMatch(element('hmGrid').innerHTML, /data-d="2026-09-22"/);
now = new RealDate(2026, 8, 22, 0, 0, 5).getTime();
build({series});
assert.match(element('hmGrid').innerHTML, /data-d="2026-09-22" data-r="7"/);
assert.match(element('hmMeta').textContent, /11/);
console.log('dashboard heat snapshot assertions passed');
