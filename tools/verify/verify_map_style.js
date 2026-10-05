/**
 * Verify the map's styling decision for each layer, using the map module's OWN
 * spec tables and resolver (extracted from fiber-fe/js/ftth-map.js), against the
 * real API payload the results page fetches.
 *
 *   node tmp/verify_map_style.js <project_id>
 *
 * Prints, per layer: the spec it resolves to, the bucket its features match and
 * the exact paint (colour, width, dash) MapLibre is handed.
 */
const fs = require('fs');
const path = require('path');

const MAP_JS = 'fiber-fe/js/ftth-map.js';
const src = fs.readFileSync(MAP_JS, 'utf8');

/** Slice a `const NAME = { ... };` literal out of the source by brace depth. */
function objectLiteral(name) {
  const start = src.indexOf('const ' + name + ' = {');
  if (start < 0) throw new Error('not found: ' + name);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(open, i + 1);
    }
  }
  throw new Error('unbalanced: ' + name);
}

/** Slice `function name(...) { ... }` out of the source by brace depth. */
function functionBody(header) {
  const start = src.indexOf(header);
  if (start < 0) throw new Error('not found: ' + header);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced: ' + header);
}

const LINE_SPEC = eval('(' + objectLiteral('LINE_SPEC') + ')');
const SYMBOL_SPEC = eval('(' + objectLiteral('SYMBOL_SPEC') + ')');
const _layerKey = eval('(' + functionBody('function _layerKey(id)') + ')');
const _specFor = eval('(' + functionBody('function _specFor(spec, key)') + ')');

const pid = process.argv[2];
if (!pid) {
  console.error('usage: node tmp/verify_map_style.js <project_id>');
  process.exit(2);
}
const cacheDir = path.join('tmp', 'apicache', pid);

/** The exact bucketing the map does, copied from addLayer's line branch. */
function resolveLine(layerId, geojson, opts) {
  opts = opts || {};
  const lineKey = _layerKey(layerId);
  const lineSpec = _specFor(LINE_SPEC, lineKey);
  if (!lineSpec || !lineSpec.field) return { key: lineKey, spec: null };
  const found = {};
  (geojson.features || []).forEach(function (feat) {
    const v = feat.properties && feat.properties[lineSpec.field];
    if (v === null || v === undefined || String(v).trim() === '') return;
    found[String(v).trim()] = true;
  });
  const derived = Object.keys(found).length === 0;
  if (derived) {
    let hint = null;
    lineSpec.buckets.forEach(function (b) {
      if (hint) return;
      const aliases = b.aliases || [];
      for (let ai = 0; ai < aliases.length; ai++) {
        if (lineKey.indexOf(aliases[ai]) !== -1) { hint = b.value; return; }
      }
    });
    if (hint) found[hint] = true;
  }
  const built = [];
  lineSpec.buckets.forEach(function (b, bi) {
    let match = null;
    Object.keys(found).forEach(function (v) {
      if (!match && v.toLowerCase() === String(b.value).toLowerCase()) match = v;
    });
    if (!match) return;
    built.push({ value: match, color: b.color, width: b.width, dash: b.dash || null });
  });
  return { key: lineKey, spec: lineSpec, field: lineSpec.field,
           values: Object.keys(found), derived: derived,
           specUseBucketColor: !!lineSpec.useBucketColor, buckets: built };
}

const layers = fs.readdirSync(cacheDir).filter((f) => f.endsWith('.json'));
const wanted = ['trenches', 'aerial_drop_trenches', 'aerial_cable', 'aerial_drops',
                'ducts', 'cables', 'feeder_ducts', 'distribution_ducts', 'drop_ducts',
                'feeder_cable', 'distribution_cable', 'drop_cable'];
for (const file of layers) {
  const name = file.replace(/\.json$/, '');
  if (wanted.indexOf(name) === -1) continue;
  let gj;
  try {
    gj = JSON.parse(fs.readFileSync(path.join(cacheDir, file), 'utf8'));
  } catch (e) { continue; }
  const r = resolveLine(name, gj, {});
  if (!r.spec) {
    // not styled by LINE_SPEC: check the symbol table
    const symKey = _layerKey(name);
    const sym = _specFor(SYMBOL_SPEC, symKey);
    console.log((name + ' ').padEnd(22, '.') +
      ' LINE_SPEC: (none)' + (sym ? '   SYMBOL: shape=' + sym.shape +
      ' color=' + sym.color + ' size=' + sym.size : ''));
    continue;
  }
  const paint = r.buckets.map(function (b) {
    const colour = false ? b.color : b.color;   // bucket colour as declared
    return b.value + ' -> ' + colour + ' w=' + b.width +
           ' dash=' + (b.dash ? JSON.stringify(b.dash) : 'solid');
  });
  console.log((name + ' ').padEnd(22, '.') +
    ' field=' + r.field +
    (r.derived ? ' (field absent -> derived from layer name)' : '') +
    '  useBucketColor=' + r.specUseBucketColor);
  if (!paint.length) console.log('   !! NO BUCKET MATCHED — falls back to a flat stroke');
  paint.forEach(function (p) { console.log('      ' + p); });
}
