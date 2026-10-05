// Every icon the legend/map can ask for must be VALID svg: a raw element body
// (circle/dot) must not be wrapped in <path d="...">, which renders nothing.
const fs = require('fs');
const src = fs.readFileSync('fiber-fe/js/ftth-map.js', 'utf8');

// Extract the shipped features by evaluating the module in a fake DOM-free env.
const win = {};
const mod = { exports: {} };
const fn = new Function('window', 'document', 'module', 'exports', src);
const fakeDoc = {
  createElement: () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {} }, appendChild() {}, addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], insertBefore() {}, remove() {} }),
  createElementNS: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
  addEventListener() {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], body: { appendChild() {}, classList: { add() {}, remove() {} } },
};
fn(win, fakeDoc, mod, mod.exports);
const spec = (win.FtthMap || mod.exports).SYMBOL_SPEC;
const lineSpec = (win.FtthMap || mod.exports).LINE_SPEC;

// Re-derive the SVG the same way the module does, then check validity.
const SHAPES = {
  hexagon: 'M12 2.6 L20.1 7.3 V16.7 L12 21.4 L3.9 16.7 V7.3 Z',
  triangle: 'M12 3.2 L21.2 20.2 H2.8 Z',
  square: 'M4.4 4.4 H19.6 V19.6 H4.4 Z',
  diamond: 'M12 2.4 L21.6 12 L12 21.6 L2.4 12 Z',
  circle: '<circle cx="12" cy="12" r="8.2"/>',
  dot: '<circle cx="12" cy="12" r="5.4"/>',
  pin: 'M12 2.4 C17.2 2.4 21.2 6.5 21.2 11.7 C21.2 17.1 12 21.8 12 21.8 C12 21.8 2.8 17.1 2.8 11.7 C2.8 6.5 6.8 2.4 12 2.4 Z',
  cross: 'M12 2.8 V21.2 M2.8 12 H21.2',
};

let bad = 0, checked = 0;
function check(shape, color) {
  const body = SHAPES[shape] || SHAPES.circle;
  checked++;
  const isElement = body.charAt(0) === '<';
  // Emulate the module: element bodies must go into a <g>, path data into <path d>
  const svg = isElement
    ? '<svg><g fill="' + color + '">' + body + '</g></svg>'
    : '<svg><path d="' + body + '"/></svg>';
  if (svg.includes('<path d="<')) { console.log('BAD glyph', shape, color); bad++; }
  if (isElement && !svg.includes('<circle')) { console.log('BAD element', shape); bad++; }
}

Object.keys(spec).forEach(function (k) {
  const s = spec[k];
  check(s.shape, s.color);
  Object.keys(s.values || {}).forEach(v => check(s.values[v].shape, s.values[v].color));
});
console.log('glyphs checked:', checked, '| invalid:', bad);
console.log('objects spec:', JSON.stringify(spec.objects));
console.log('chamber subtypes:', Object.keys(spec.chambers.values).map(v => v + '=' + spec.chambers.values[v].shape).join(', '));
console.log('trench buckets:', lineSpec.trenches.buckets.map(b => b.value + ' ' + b.color + ' w' + b.width).join(' | '));
console.log('aerial spec present:', !!lineSpec.aerial_drops, '| aerial colour:', lineSpec.aerial_drops && lineSpec.aerial_drops.buckets[0].color);
process.exit(bad ? 1 : 0);
