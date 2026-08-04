/* GRAPNEL frontend.
 *
 * Reads static JSON written by the Python pipeline. No server, no build step,
 * no framework. If you can serve docs/ you can run this.
 *
 * The dossier is the point of the whole interface. A detection is a reason to
 * start looking, not an answer, so every self-reported AIS field is shown raw
 * and copyable, and every external source that could test those fields is one
 * click away. GRAPNEL deliberately does not scrape any of them: the analyst
 * opens the door themselves, which keeps this project clear of every tracking
 * provider's terms and keeps provenance attributable to the person, not the tool.
 */

const DATA = 'data/';
const state = { detections: [], incidents: [], watchlist: [], view: 'det', kinds: new Set(), conf: new Set(), selected: null };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const KIND_LABEL = {
  anchor_drag: 'anchor drag',
  loiter: 'loiter',
  survey_pattern: 'survey pattern',
  corridor_gap: 'AIS gap',
  position_jump: 'position jump',
};

/* --------------------------------------------------------------------- map */

const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {
      basemap: {
        type: 'raster',
        tiles: ['https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png'],
        tileSize: 256,
        attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
      },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#070c12' } },
      { id: 'basemap', type: 'raster', source: 'basemap', paint: { 'raster-opacity': 0.55, 'raster-saturation': -0.3 } },
    ],
  },
  center: [25.0, 59.85],
  zoom: 7,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
map.addControl(new maplibregl.ScaleControl({ unit: 'nautical' }), 'bottom-right');

const empty = { type: 'FeatureCollection', features: [] };

map.on('load', async () => {
  for (const id of ['corridors', 'cables', 'vessels', 'detections', 'incidents', 'track']) {
    map.addSource(id, { type: 'geojson', data: empty });
  }

  map.addLayer({
    id: 'corridor-fill', type: 'fill', source: 'corridors',
    paint: { 'fill-color': '#e85aa0', 'fill-opacity': 0.07 },
  });

  // Magenta is the chart colour for submarine cables. Charted geometry gets a
  // solid line, display-only geometry gets a dash, so you can see at a glance
  // which routes can actually support a confident detection.
  map.addLayer({
    id: 'cable-line', type: 'line', source: 'cables',
    paint: {
      'line-color': ['case', ['==', ['get', 'positional_class'], 'CHARTED'], '#4fd1a5', '#e85aa0'],
      'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.8, 10, 2.2],
      'line-dasharray': ['case', ['==', ['get', 'positional_class'], 'CHARTED'], ['literal', [1]], ['literal', [3, 2]]],
      'line-opacity': 0.85,
    },
  });

  // Live traffic. Drawn beneath everything else and kept deliberately quiet:
  // it is context, not signal. Hulls currently inside a corridor get the chart
  // magenta so you can see the watchlist without reading it.
  map.addLayer({
    id: 'vessel-dot', type: 'circle', source: 'vessels',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 1.6, 9, 3.2, 12, 5],
      'circle-color': ['case',
        ['all', ['get', 'in_corridor'], ['<', ['coalesce', ['get', 'sog'], 99], 2]], '#ff6b4a',
        ['get', 'in_corridor'], '#e85aa0',
        '#4e6675'],
      'circle-opacity': ['case', ['get', 'in_corridor'], 0.95, 0.5],
    },
  });

  // Track, coloured by speed over ground: slow is the interesting end.
  map.addLayer({
    id: 'track-line', type: 'line', source: 'track',
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-width': 3,
      'line-color': ['interpolate', ['linear'], ['get', 'sog'],
        0, '#ff6b4a', 3, '#f2c744', 8, '#7c94a4', 14, '#4e6675'],
      'line-opacity': 0.95,
    },
  });

  map.addLayer({
    id: 'incident-dot', type: 'circle', source: 'incidents',
    paint: {
      'circle-radius': 6, 'circle-color': '#6aa9e8',
      'circle-stroke-width': 1.5, 'circle-stroke-color': '#0e1a24',
    },
  });

  map.addLayer({
    id: 'detection-dot', type: 'circle', source: 'detections',
    paint: {
      'circle-radius': ['case', ['==', ['get', 'selected'], true], 11, 7],
      'circle-color': ['match', ['get', 'confidence'],
        'HIGH', '#ff6b4a', 'MODERATE', '#f2c744', '#7c94a4'],
      'circle-stroke-width': 2, 'circle-stroke-color': '#070c12',
    },
  });

  map.on('click', 'vessel-dot', (e) => {
    const p = e.features[0].properties;
    new maplibregl.Popup({ closeButton: false, offset: 8 })
      .setLngLat(e.lngLat)
      .setHTML(
        `<b>${esc(p.name || 'Unnamed')}</b><br>`
        + `MMSI ${esc(p.mmsi)}${p.ship_type ? ` · ${esc(p.ship_type)}` : ''}<br>`
        + `${p.sog != null ? esc(p.sog) + ' kn' : 'speed not reported'}`
        + `${p.nav_status ? ' · ' + esc(p.nav_status) : ''}<br>`
        + (p.in_corridor === true || p.in_corridor === 'true'
          ? `<span style="color:#e85aa0">In corridor: ${esc(p.nearest_cable || '')}</span>`
          : `${esc(p.nearest_m)} m from ${esc(p.nearest_cable || 'nearest route')}`))
      .addTo(map);
  });
  map.on('mouseenter', 'vessel-dot', () => (map.getCanvas().style.cursor = 'pointer'));
  map.on('mouseleave', 'vessel-dot', () => (map.getCanvas().style.cursor = ''));

  map.on('click', 'detection-dot', (e) => select(e.features[0].properties.id));
  map.on('click', 'incident-dot', (e) => showIncident(e.features[0].properties.id));
  for (const l of ['detection-dot', 'incident-dot']) {
    map.on('mouseenter', l, () => (map.getCanvas().style.cursor = 'pointer'));
    map.on('mouseleave', l, () => (map.getCanvas().style.cursor = ''));
  }

  await load();
});

/* -------------------------------------------------------------------- data */

async function getJSON(name) {
  try {
    // GitHub Pages sits behind a CDN that purges on deploy, but a client that
    // left the tab open can still hold a stale copy for the cache lifetime.
    // A minute-resolution buster keeps a long-lived tab honest without
    // defeating caching for every asset on the page.
    const bust = Math.floor(Date.now() / 60000);
    const r = await fetch(`${DATA}${name}?v=${bust}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch (err) {
    console.warn('could not load', name, err);
    return null;
  }
}

async function load() {
  const [dets, cables, corridors, incidents, vessels, watch] = await Promise.all([
    getJSON('detections.json'), getJSON('cables.geojson'),
    getJSON('corridors.geojson'), getJSON('incidents.json'),
    getJSON('vessels.geojson'), getJSON('watchlist.json'),
  ]);

  if (cables) map.getSource('cables').setData(cables);
  if (corridors) map.getSource('corridors').setData(corridors);
  if (vessels) map.getSource('vessels').setData(vessels);
  state.watchlist = (watch && watch.vessels) || [];
  state.watchNote = (watch && watch.note) || '';

  state.incidents = (incidents && incidents.incidents) || [];
  map.getSource('incidents').setData({
    type: 'FeatureCollection',
    features: state.incidents.map((i) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [i.lon, i.lat] },
      properties: { id: i.id },
    })),
  });

  if (!dets) {
    $('list').innerHTML = '<div class="empty">No detections published yet.<br><br>'
      + 'If you are running this locally, build the data first:<br><br>'
      + '<code>python scripts/make_demo.py</code> for synthetic data that exercises every detector, '
      + 'or <code>python -m grapnel.pipeline</code> against the live feed.<br><br>'
      + 'If this is the deployed site, the monitor workflow has not completed a successful run.</div>';
    renderStats(null);
    if (state.incidents.length) {
      state.view = 'inc';
      $('tab-inc').setAttribute('aria-selected', 'true');
      $('tab-det').setAttribute('aria-selected', 'false');
    }
    return;
  }

  state.detections = dets.detections || [];
  renderStats(dets);

  // No vessels means no AIS has been ingested. Say so plainly rather than
  // letting real cable geometry imply the tool is running.
  if (!(vessels && (vessels.features || []).length)) {
    state.noAis = true;
  }

  const notes = [];
  if (dets.demo) notes.push('<b>Synthetic demo data.</b> ' + esc(dets.demo_notice || ''));
  for (const w of (dets.warnings || [])) notes.push(esc(w));
  if (notes.length) {
    const b = $('banner');
    b.hidden = false;
    b.innerHTML = notes.join('<br>');
  }

  map.getSource('detections').setData(featureCollection());
  renderFilters();
  renderList();
  if (state.noAis) {
    $('list').innerHTML = '<div class="empty">'
      + '<b>Cable geometry is loaded. No AIS ingested yet.</b><br><br>'
      + 'The routes on the map are real. There are no vessels because no AIS source has run.<br><br>'
      + '<code>python -m grapnel.pipeline</code> — live feed, vessels appear at once, '
      + 'detections after a few hours of polling<br><br>'
      + '<code>python scripts/bootstrap.py --days 1</code> — real historical AIS, '
      + 'real detections immediately'
      + '</div>';
  }

  const bbox = dets.area && dets.area.bbox;
  if (bbox) map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: 60, duration: 0 });
}

function featureCollection() {
  return {
    type: 'FeatureCollection',
    features: visible().map((d) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [d.lon, d.lat] },
      properties: { id: d.detection_id, confidence: d.confidence, selected: d.detection_id === state.selected },
    })),
  };
}

function visible() {
  return state.detections.filter((d) =>
    (state.kinds.size === 0 || state.kinds.has(d.kind))
    && (state.conf.size === 0 || state.conf.has(d.confidence)));
}

/* ------------------------------------------------------------------ chrome */

function renderStats(d) {
  if (!d) { $('stats').innerHTML = ''; return; }
  const c = d.counts || {};
  const cell = (label, value) => `<div>${esc(label)}<b>${esc(value)}</b></div>`;
  $('stats').innerHTML =
    cell('Detections', c.detections ?? 0)
    + cell('Vessels seen', c.vessels_observed ?? 0)
    + cell('In corridor', c.in_corridor_now ?? state.watchlist.length)
    + cell('Routes', `${c.routes_charted ?? 0}/${c.routes ?? 0}`)
    + cell('Updated', (d.generated_at || '').replace('T', ' ').replace('Z', 'Z'));
}

function renderFilters() {
  const kinds = [...new Set(state.detections.map((d) => d.kind))].sort();
  const confs = ['HIGH', 'MODERATE', 'LOW'].filter((c) => state.detections.some((d) => d.confidence === c));
  const chip = (label, set, value) =>
    `<button class="chip" aria-pressed="${set.has(value)}" data-set="${set === state.kinds ? 'kind' : 'conf'}" data-value="${esc(value)}">${esc(label)}</button>`;

  $('filters').innerHTML =
    confs.map((c) => chip(c.toLowerCase(), state.conf, c)).join('')
    + kinds.map((k) => chip(KIND_LABEL[k] || k, state.kinds, k)).join('');

  $('filters').querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      const set = el.dataset.set === 'kind' ? state.kinds : state.conf;
      const v = el.dataset.value;
      set.has(v) ? set.delete(v) : set.add(v);
      renderFilters();
      renderList();
      map.getSource('detections').setData(featureCollection());
    };
  });
}

function renderList() {
  const list = $('list');
  if (state.view === 'inc') return renderIncidentList();
  if (state.view === 'watch') return renderWatchlist();

  const items = visible();
  if (!items.length) {
    list.innerHTML = '<div class="empty">Nothing matches these filters.</div>';
    return;
  }

  list.innerHTML = items.map((d) => {
    const v = d.vessel || {};
    const sr = v.self_reported || {};
    const name = sr.name || `MMSI ${d.mmsi}`;
    const flag = (v.mmsi_decode && v.mmsi_decode.flag_from_mid) || 'unknown MID';
    return `<article class="card" data-id="${esc(d.detection_id)}" aria-current="${d.detection_id === state.selected}" tabindex="0">
      <div class="card-top">
        <span class="kind">${esc(KIND_LABEL[d.kind] || d.kind)}</span>
        <span class="conf ${esc(d.confidence)}">${esc(d.confidence)}</span>
      </div>
      <div class="card-name">${esc(name)} ${sr.name ? '' : '<em>— no name broadcast</em>'}</div>
      <div class="card-sub">${esc(d.mmsi)} · ${esc(flag)} · ${esc(d.start_ts.slice(0, 16).replace('T', ' '))}Z</div>
      <div class="card-summary">${esc(d.summary)}</div>
    </article>`;
  }).join('');

  list.querySelectorAll('.card').forEach((el) => {
    el.onclick = () => select(el.dataset.id);
    el.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(el.dataset.id); } };
  });
}

function renderWatchlist() {
  const list = $('list');
  if (!state.watchlist.length) {
    list.innerHTML = '<div class="empty">No vessels currently inside a cable corridor.</div>';
    return;
  }
  list.innerHTML = `<div class="empty" style="padding:11px 14px;font-size:11.5px">${esc(state.watchNote)}</div>`
    + state.watchlist.map((w) => `
    <article class="card" data-watch="${esc(w.mmsi)}" tabindex="0">
      <div class="card-top">
        <span class="kind" style="color:${(w.sog != null && w.sog < 2) ? '#ff6b4a' : '#e85aa0'}">
          ${w.sog != null ? esc(w.sog) + ' kn' : 'speed n/r'}</span>
        <span class="conf LOW">${esc(w.nearest_m)} m</span>
      </div>
      <div class="card-name">${esc(w.name || 'Unnamed')}</div>
      <div class="card-sub">${esc(w.mmsi)}${w.ship_type ? ' · ' + esc(w.ship_type) : ''} · ${esc((w.cables || []).join(', '))}</div>
      <div class="card-summary">${esc(w.nav_status || '')}${w.destination ? ' → ' + esc(w.destination) : ''}</div>
    </article>`).join('');

  list.querySelectorAll('.card').forEach((el) => {
    el.onclick = () => showWatch(el.dataset.watch);
    el.onkeydown = (e) => { if (e.key === 'Enter') showWatch(el.dataset.watch); };
  });
}

function showWatch(mmsi) {
  const w = state.watchlist.find((x) => String(x.mmsi) === String(mmsi));
  if (!w) return;
  map.getSource('track').setData(empty);
  map.flyTo({ center: [w.lon, w.lat], zoom: 11 });
  // Reuse the detection dossier: same hull, same questions, no detection yet.
  renderDossier({
    detection_id: '', kind: 'corridor_presence', mmsi: w.mmsi,
    cable_name: (w.cables || []).join(', '),
    cable_positional_class: (w.cable_positional_class || [])[0] || '',
    confidence: 'LOW', score: 0,
    start_ts: w.ts, end_ts: w.ts, lat: w.lat, lon: w.lon,
    summary: 'Most recent fix falls inside a cable corridor. Presence only — no behavioural '
      + 'detection has fired for this hull. Cable routes run through shipping lanes, anchorages '
      + 'and fishing grounds, so this is very often ordinary traffic.',
    evidence: {
      speed_over_ground_kn: w.sog, nav_status: w.nav_status,
      distance_to_nearest_route_m: w.nearest_m, nearest_route: w.nearest_cable,
      corridors_occupied: w.cables, position_time: w.ts,
    },
    vessel: w.vessel || {}, corroboration: [], track: [],
  });
  openPanel();
}

function renderIncidentList() {
  $('list').innerHTML = state.incidents.map((i) => `
    <article class="card" data-inc="${esc(i.id)}" tabindex="0">
      <div class="card-top">
        <span class="when">${esc(i.date)}</span>
        <span class="status ${esc(i.status)}">${esc(String(i.status).replace(/_/g, ' ').toLowerCase())}</span>
      </div>
      <div class="card-name">${esc(i.title)}</div>
      <div class="card-sub">${esc(i.vessel || 'vessel not established')}</div>
      <div class="card-summary">${esc(i.why_it_matters || '')}</div>
    </article>`).join('');

  $('list').querySelectorAll('.card').forEach((el) => {
    el.onclick = () => showIncident(el.dataset.inc);
    el.onkeydown = (e) => { if (e.key === 'Enter') showIncident(el.dataset.inc); };
  });
}

const TABS = [['tab-det', 'det'], ['tab-watch', 'watch'], ['tab-inc', 'inc']];
for (const [id, view] of TABS) {
  $(id).onclick = () => {
    state.view = view;
    for (const [tid, tv] of TABS) $(tid).setAttribute('aria-selected', String(tv === view));
    $('filters').style.display = view === 'det' ? '' : 'none';
    renderList();
  };
}

/* ----------------------------------------------------------------- dossier */

function openPanel() { $('dossier').classList.add('open'); $('dossier').setAttribute('aria-hidden', 'false'); }
function closePanel() {
  $('dossier').classList.remove('open');
  $('dossier').setAttribute('aria-hidden', 'true');
  state.selected = null;
  map.getSource('track').setData(empty);
  map.getSource('detections').setData(featureCollection());
  renderList();
}
$('d-close').onclick = closePanel;
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePanel(); });

function select(id) {
  const d = state.detections.find((x) => x.detection_id === id);
  if (!d) return;
  state.selected = id;

  // Draw the track one segment at a time so speed can colour the line.
  const t = d.track || [];
  map.getSource('track').setData({
    type: 'FeatureCollection',
    features: t.slice(1).map((p, i) => ({
      type: 'Feature',
      geometry: { type: 'LineString', coordinates: [[t[i][0], t[i][1]], [p[0], p[1]]] },
      properties: { sog: p[3] ?? 0 },
    })),
  });

  if (t.length > 1) {
    const b = t.reduce((acc, p) => [
      Math.min(acc[0], p[0]), Math.min(acc[1], p[1]),
      Math.max(acc[2], p[0]), Math.max(acc[3], p[1]),
    ], [180, 90, -180, -90]);
    map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 130, maxZoom: 12 });
  } else {
    map.flyTo({ center: [d.lon, d.lat], zoom: 10 });
  }

  map.getSource('detections').setData(featureCollection());
  renderDossier(d);
  renderList();
  openPanel();
}

function field(label, value, copyable) {
  const isNull = value === null || value === undefined || value === '';
  const shown = isNull ? 'not broadcast' : String(value);
  const btn = (!isNull && copyable)
    ? `<button class="copy" data-copy="${esc(shown)}" title="Copy">copy</button>` : '';
  return `<dt>${esc(label)}</dt><dd class="${isNull ? 'null' : ''}">${esc(shown)}${btn}</dd>`;
}

function renderDossier(d) {
  const v = d.vessel || {};
  const sr = v.self_reported || {};
  const mid = v.mmsi_decode || {};
  const imo = v.imo_check || {};

  $('d-title').textContent = sr.name || 'Unnamed vessel';
  state.selected = d.detection_id || null;
  $('d-mmsi').textContent = `MMSI ${d.mmsi}${sr.imo ? ` · IMO ${sr.imo}` : ''}`;

  const groups = {};
  for (const p of (v.pivots || [])) (groups[p.group] = groups[p.group] || []).push(p);

  const sections = [];

  sections.push(`<div class="sec">
    <h3>Detection</h3>
    <dl class="fields">
      ${field('Type', KIND_LABEL[d.kind] || d.kind)}
      ${field('Confidence', d.confidence)}
      ${field('Score', d.score)}
      ${field('Cable', d.cable_name || '—')}
      ${field('Geometry', d.cable_positional_class || '—')}
      ${field('First fix', d.start_ts, true)}
      ${field('Last fix', d.end_ts, true)}
      ${field('Position', `${d.lat}, ${d.lon}`, true)}
    </dl>
    <div class="claim-note">${esc(d.summary)}</div>
    ${d.cable_positional_class === 'DISPLAY' ? `<div class="claim-note">
      Cable geometry for this route is display-grade, drawn for cartography rather than surveyed.
      True position may differ by tens of kilometres, so confidence is capped at LOW regardless of
      how clean the behavioural signal looks. Load charted ENC geometry to lift this.</div>` : ''}
  </div>`);

  if ((d.corroboration || []).length) {
    sections.push(`<div class="sec"><h3>Independent corroboration</h3>
      ${d.corroboration.map((c) => `<div class="corr">
        <b>${esc(String(c.kind).replace(/_/g, ' '))}</b> — ${esc(c.asset)}<br>
        ${esc(c.detail)}<br>
        ${esc(c.start)} to ${esc(c.end)}${c.distance_km != null ? ` · ${esc(c.distance_km)} km away` : ''}<br>
        <a href="${esc(c.source_url)}" target="_blank" rel="noopener">${esc(c.source_label)}</a>
      </div>`).join('')}</div>`);
  }

  sections.push(`<div class="sec">
    <h3>Self-reported identity</h3>
    <dl class="fields">
      ${field('MMSI', d.mmsi, true)}
      ${field('Name', sr.name, true)}
      ${field('Call sign', sr.callsign, true)}
      ${field('IMO', sr.imo, true)}
      ${field('Type', sr.ship_type)}
      ${field('Length', sr.length_m ? `${sr.length_m} m` : null)}
      ${field('Beam', sr.width_m ? `${sr.width_m} m` : null)}
      ${field('Draught', sr.draught_m ? `${sr.draught_m} m` : null)}
      ${field('Destination', sr.destination, true)}
      ${field('ETA', sr.eta)}
    </dl>
    <div class="claim-note">${esc(v.disclaimer || '')}</div>
  </div>`);

  const notes = v.integrity_notes || [];
  sections.push(`<div class="sec">
    <h3>Identity integrity</h3>
    <dl class="fields">
      ${field('MID', mid.mid ? `${mid.mid} — ${mid.flag_from_mid || 'unassigned'}` : null)}
      ${field('Station', mid.station_class)}
      ${field('IMO check', imo.present ? (imo.valid ? 'checksum valid' : 'CHECKSUM FAILS') : 'none broadcast')}
    </dl>
    ${notes.length
      ? notes.map((n) => `<div class="flagrow"><span class="mark">!</span><span>${esc(n)}</span></div>`).join('')
      : '<div class="flagrow ok"><span class="mark">&check;</span><span>No identity anomalies in the observed window.</span></div>'}
  </div>`);

  sections.push(`<div class="sec">
    <h3>Verify this hull elsewhere</h3>
    ${Object.entries(groups).map(([g, items]) => `<div class="pivot-group">
      <span>${esc(g)}</span>
      <div class="pivots">${items.map((p) =>
        `<a class="pivot" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.label)}</a>`).join('')}</div>
      ${items.filter((p) => p.note).map((p) => `<div class="pivot-note">${esc(p.label)}: ${esc(p.note)}</div>`).join('')}
    </div>`).join('')}
  </div>`);

  sections.push(`<div class="sec"><h3>Evidence</h3>
    <pre class="evidence">${esc(JSON.stringify(d.evidence, null, 2))}</pre></div>`);

  $('d-body').innerHTML = sections.join('');
  $('d-body').querySelectorAll('.copy').forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(b.dataset.copy);
      b.textContent = 'copied';
      setTimeout(() => (b.textContent = 'copy'), 1200);
    };
  });
}

function showIncident(id) {
  const i = state.incidents.find((x) => x.id === id);
  if (!i) return;
  state.selected = null;
  map.getSource('track').setData(empty);
  map.flyTo({ center: [i.lon, i.lat], zoom: 8 });

  $('d-title').textContent = i.title;
  $('d-mmsi').textContent = `${i.date} · ${i.sea_area || ''}`;
  $('d-body').innerHTML = `
    <div class="sec">
      <h3>Case</h3>
      <dl class="fields">
        ${field('Date', i.date)}
        ${field('Vessel', i.vessel)}
        ${field('Flag', i.vessel_flag)}
        ${field('Mechanism', i.mechanism)}
        ${field('Status', String(i.status || '').replace(/_/g, ' ').toLowerCase())}
        ${field('Position', `${i.lat}, ${i.lon} (${i.coord_quality || 'approximate'})`)}
      </dl>
    </div>
    <div class="sec"><h3>Assets affected</h3>
      <ul class="sources">${(i.assets || []).map((a) => `<li>${esc(a)}</li>`).join('')}</ul></div>
    <div class="sec"><h3>Outcome</h3><div class="claim-note">${esc(i.outcome || '')}</div></div>
    <div class="sec"><h3>Why it matters here</h3><div class="claim-note">${esc(i.why_it_matters || '')}</div></div>
    <div class="sec"><h3>Reporting</h3>
      <ul class="sources">${(i.sources || []).map((s) =>
        `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a></li>`).join('')}</ul>
      <div class="claim-note">Coordinates are derived from press reporting, not from official
      incident positions. They are good enough to place a marker and not good enough for anything else.</div>
    </div>`;
  openPanel();
}
