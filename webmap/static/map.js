// Demo-Karte. Alle Fremdtexte (Attribute, Fehlergründe) werden maskiert eingesetzt.
"use strict";

const COLORS = { added: "#1f9d55", changed: "#e0a800", deleted: "#d64545", unchanged: "#9aa3af", replaced: "#6b7fd7" };
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = new Intl.NumberFormat("de-DE");
const fmtCoord = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 4, maximumFractionDigits: 4 });

// Ladeanzeige über der Karte, solange mindestens eine Abfrage läuft (erst nach 150 ms, damit nichts flackert).
let pending = 0;
let busyTimer = null;
function busy(delta) {
  pending = Math.max(0, pending + delta);
  clearTimeout(busyTimer);
  if (pending === 0) { $("loading").hidden = true; return; }
  busyTimer = setTimeout(() => { $("loading").hidden = pending === 0; }, 150);
}

async function api(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([k, v]) => v !== undefined && v !== null && v !== "" && url.searchParams.set(k, v));
  busy(1);
  try {
    const r = await fetch(url);
    if (r.status === 401) { window.location.assign("/login"); }  // Sitzung abgelaufen
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return await r.json();
  } finally {
    busy(-1);
  }
}

// Nur die jüngste Abfrage je Bereich darf die Karte ändern: Bei schnellem Umschalten kommen Antworten
// in beliebiger Reihenfolge an, eine ältere würde sonst die neuere Auswahl überschreiben.
const seq = {};
function latest(name) {
  const n = (seq[name] = (seq[name] || 0) + 1);
  return () => seq[name] === n;
}

function popup(props) {
  const rows = Object.entries(props)
    .filter(([, v]) => v !== null && typeof v !== "object")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  return `<dl class="popup">${rows}</dl>`;
}

const map = L.map("map", { preferCanvas: true }).setView([52.41, 12.54], 10);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18, attribution: "© OpenStreetMap-Mitwirkende",
}).addTo(map);

map.createPane("oben").style.zIndex = 450;  // Suchtreffer liegt über allen Einfärbungen

const layers = {
  features: L.geoJSON(null), changes: L.geoJSON(null), errors: L.layerGroup(), radius: L.layerGroup(),
  choro: L.geoJSON(null), search: L.layerGroup().addTo(map),
};
let info = null;
let featureData = null;   // Objekte des gewählten Datensatzes (Suche, Mittelpunkt der Umkreissuche)
let choroValues = null;   // Kennzahl je Objektschlüssel der aktuellen Einfärbung
let searchCenter = null;  // Mittelpunkt der zuletzt gefundenen Gemeinde
let clickCenter = null;   // per Klick gewählter Mittelpunkt

function setBanner(simulated) { $("sim-banner").hidden = !simulated; }

async function loadInfo() {
  info = await api("/api/info");
  $("model-hint").textContent = info.hinweis_modell || "";
  $("logout").hidden = !info.anmeldung;
  const attributions = ["© OpenStreetMap-Mitwirkende (ODbL)", ...new Set(info.quellen.map((q) => q.quellenvermerk).filter(Boolean))];
  $("attribution").textContent = "Quellenvermerke: " + attributions.join(" · ") +
    ` · Campus: ${info.campus.name} (OSM/Nominatim, abgefragt 29.10.2026)`;
  const ds = $("dataset");
  info.quellen.filter((q) => q.typ !== "csv").forEach((q) => ds.add(new Option(q.titel, q.dataset)));
}

// Eigener Schritt: Die Indikatorliste kann nach einem Import einmalig länger dauern und hält die übrige Karte nicht auf.
async function loadIndicators() {
  const ind = $("indicator");
  const csv = info.quellen.find((q) => q.typ === "csv");
  if (!csv) { ind.innerHTML = ""; ind.add(new Option("keine CSV-Quelle", "")); return; }
  let list = [];
  let failed = false;
  try { list = await api("/api/indicators", { dataset: csv.dataset }); } catch (e) { failed = true; showError(e); }
  ind.innerHTML = "";  // Platzhalter „Kennzahlen werden geladen …“ entfernen
  if (!list.length) ind.add(new Option(failed ? "Kennzahlen konnten nicht geladen werden" : "keine Kennzahlen gefunden", ""));
  list.forEach((i) => {
    const o = new Option(i.indikator, i.indikator);
    o.title = `${fmt.format(i.werte)} Werte, ${i.jahre.filter(Boolean).length} Jahre`;
    o.dataset.jahre = JSON.stringify(i.jahre);
    ind.add(o);
  });
  ind.dataset.source = csv.dataset;
  fillPeriods();
  await loadChoropleth();  // Kennzahlenkarte ist beim Start aktiv
}

function fillPeriods() {
  const opt = $("indicator").selectedOptions[0];
  const per = $("period");
  per.innerHTML = "";
  if (!opt) return;
  JSON.parse(opt.dataset.jahre || "[]").filter(Boolean).reverse().forEach((j) => per.add(new Option(j, j)));
}

// Objekte einmal je Datensatz laden: für die Anzeige und für die Suche (auch wenn die Anzeige aus ist).
const featureRequests = new Map();  // Datensatz → laufende oder fertige Abfrage
async function featuresOf(dataset) {
  if (!featureRequests.has(dataset)) {
    featureRequests.set(dataset, api("/api/features", { dataset }).catch((e) => { featureRequests.delete(dataset); throw e; }));
  }
  const fc = await featureRequests.get(dataset);
  if (dataset === $("dataset").value && featureData?.dataset !== dataset) {  // Suche nur im gewählten Datensatz
    featureData = { dataset, fc };
    $("search-list").innerHTML = "";
    fc.features.forEach((f) => $("search-list").appendChild(new Option(`${f.properties.name ?? ""} (${f.properties.key})`)));
  }
  return fc;
}

async function loadFeatures() {
  const current = latest("features");
  layers.features.clearLayers();
  if (!$("dataset").value) return;
  const fc = await featuresOf($("dataset").value);
  if (!current() || !$("show-features").checked) return;
  layers.features.clearLayers();
  layers.features = L.geoJSON(fc, {
    style: { color: "#cc0a2f", weight: 1, fillOpacity: 0.05 },
    onEachFeature: (f, l) => l.bindPopup(popup(f.properties)),
  }).addTo(map);
  if (fc.features.length) map.fitBounds(layers.features.getBounds(), { maxZoom: 12 });
}

async function loadRuns() {
  const runs = await api("/api/runs");
  const sel = $("run"); sel.innerHTML = "";
  const list = $("runs"); list.innerHTML = "";
  runs.forEach((r) => {
    const when = new Date(r.started_at).toLocaleString("de-DE");
    const label = `${when} – ${r.status}${r.simulated ? " – SIMULATION" : ""}`;
    sel.add(new Option(label, r.run_id));
    const li = document.createElement("li");
    li.textContent = `${label}: neu ${r.summary?.added ?? "–"}, geändert ${r.summary?.changed ?? "–"}, gelöscht ${r.summary?.deleted ?? "–"}`;
    if (r.simulated) li.className = "sim";
    list.appendChild(li);
  });
  setBanner(runs[0]?.simulated);
}

async function loadChanges() {
  const current = latest("changes");
  layers.changes.clearLayers();
  if (!$("show-changes").checked) { $("run-stats").textContent = ""; return; }
  const fc = await api("/api/changes", { run_id: $("run").value || undefined });
  if (!current() || !$("show-changes").checked) return;
  layers.changes.clearLayers();
  setBanner(fc.run.simulated);
  layers.changes = L.geoJSON(fc, {
    style: (f) => ({
      color: COLORS[f.properties.change] || "#333", weight: f.properties.change === "unchanged" ? 0.5 : 2,
      dashArray: f.properties.change === "deleted" ? "6 4" : null,
      fillOpacity: f.properties.change === "deleted" ? 0 : (f.properties.change === "unchanged" ? 0.1 : 0.45),
    }),
    onEachFeature: (f, l) => l.bindPopup(popup(f.properties)),
  }).addTo(map);
  const c = fc.run.counts;
  $("run-stats").textContent = `neu ${c.added || 0} · geändert ${c.changed || 0} · entfallen ${c.deleted || 0} · unverändert ${c.unchanged || 0}` +
    (c.replaced ? ` · ersetzt ohne Vergleich ${c.replaced}` : "") + (fc.run.simulated ? " · SIMULATION" : "");
}

async function loadErrors() {
  const current = latest("errors");
  layers.errors.clearLayers();
  const box = $("error-list"); box.hidden = true;
  if (!$("show-errors").checked) { $("error-stats").textContent = ""; return; }
  const fc = await api("/api/errors", { run_id: $("run").value || undefined });
  if (!current() || !$("show-errors").checked) return;
  layers.errors.clearLayers();
  fc.features.forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    L.circleMarker([lat, lon], { radius: 7, color: "#b42318", fillColor: "#d64545", fillOpacity: 0.9 })
      .bindPopup(popup({ Quelle: f.properties.source, Schlüssel: f.properties.key, Grund: f.properties.reason }))
      .addTo(layers.errors);
  });
  layers.errors.addTo(map);
  $("error-stats").textContent = `${fc.features.length} mit Lage, ${fc.ohne_geometrie.length} ohne Lage` + (fc.run.simulated ? " · SIMULATION" : "");
  const tbody = box.querySelector("tbody"); tbody.innerHTML = "";
  fc.ohne_geometrie.forEach((e) => {
    const tr = document.createElement("tr");
    [e.source, e.key ?? "–", e.reason].forEach((v) => { const td = document.createElement("td"); td.textContent = v; tr.appendChild(td); });
    tbody.appendChild(tr);
  });
  box.hidden = fc.ohne_geometrie.length === 0;
}

// ---------------------------------------------------------------- Suche
// Treffer nach Name oder AGS; ein Vorschlag aus der Liste hat die Form „Name (AGS)“.
function findFeatures(term) {
  const all = featureData?.fc.features || [];
  const t = term.trim().toLowerCase();
  if (!t) return [];
  const fromList = /\(([^()]+)\)\s*$/.exec(term);
  if (fromList) {
    const hit = all.filter((f) => String(f.properties.key) === fromList[1]);
    if (hit.length) return hit;
  }
  const name = (f) => String(f.properties.name ?? "").toLowerCase();
  const exact = all.filter((f) => name(f) === t || String(f.properties.key) === t);
  if (exact.length) return exact;
  return all.filter((f) => name(f).includes(t) || String(f.properties.key).startsWith(t));
}

function selectFeature(f) {
  layers.search.clearLayers();
  $("search-results").innerHTML = "";
  const shape = L.geoJSON(f, { pane: "oben", style: { color: "#000", weight: 3, fill: false } }).addTo(layers.search);
  const bounds = shape.getBounds();
  map.fitBounds(bounds, { maxZoom: 13, padding: [30, 30] });
  const p = f.properties;
  const value = choroValues?.get(String(p.key));
  const rows = [`<dt>Gemeinde</dt><dd>${esc(p.name)}</dd>`, `<dt>AGS</dt><dd>${esc(p.key)}</dd>`];
  if (choroValues) {
    rows.push(`<dt>${esc($("indicator").value)} ${esc($("period").value)}</dt><dd>${typeof value === "number" ? fmt.format(value) : "kein Wert"}</dd>`);
  }
  L.popup().setLatLng(bounds.getCenter()).setContent(`<dl class="popup">${rows.join("")}</dl>`).openOn(map);
  const c = bounds.getCenter();
  searchCenter = { lat: c.lat, lon: c.lng, name: String(p.name ?? p.key) };
  const opt = $("radius-center").querySelector('option[value="gemeinde"]');
  opt.disabled = false;
  opt.textContent = `Gefundene Gemeinde: ${searchCenter.name}`;
  $("search-stats").textContent = `Gefunden: ${p.name ?? ""} (AGS ${p.key}). Auch als Mittelpunkt der Umkreissuche wählbar.`;
}

async function search(ev) {
  ev?.preventDefault();
  const term = $("search").value;
  if (!term.trim()) return;
  await featuresOf($("dataset").value);
  const hits = findFeatures(term);
  const box = $("search-results");
  box.innerHTML = "";
  if (!hits.length) { $("search-stats").textContent = `Keine Gemeinde zu „${term}“ gefunden.`; return; }
  if (hits.length === 1) { selectFeature(hits[0]); return; }
  $("search-stats").textContent = `${hits.length} Treffer${hits.length > 10 ? ", die ersten 10" : ""} – bitte auswählen:`;
  hits.slice(0, 10).forEach((f) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button"; b.className = "link-btn";
    b.textContent = `${f.properties.name ?? ""} (${f.properties.key})`;
    b.addEventListener("click", () => selectFeature(f));
    li.appendChild(b); box.appendChild(li);
  });
}

// ---------------------------------------------------------------- Umkreissuche
function radiusCenter() {
  const mode = $("radius-center").value;
  if (mode === "gemeinde") return searchCenter;
  if (mode === "klick") return clickCenter;
  return { lat: info.campus.lat, lon: info.campus.lon, name: info.campus.name };
}

async function radiusSearch() {
  const c = radiusCenter();
  if (!c) {
    $("radius-stats").textContent = $("radius-center").value === "klick" ? "Bitte in die Karte klicken." : "Bitte zuerst oben unter „Gemeinde suchen“ eine Gemeinde suchen.";
    return;
  }
  const current = latest("radius");
  const btn = $("radius-btn");
  btn.disabled = true;
  try {
    const r = Number($("radius").value);
    const fc = await api("/api/radius", { dataset: $("dataset").value, lat: c.lat, lon: c.lon, radius_m: r });
    if (!current()) return;  // inzwischen neuer Klickpunkt
    layers.radius.clearLayers();
    const km = (m) => `${fmt.format(Math.round(m / 100) / 10)} km`;
    const ha = (m2) => `${fmt.format(Math.round(m2 / 10000))} ha`;
    const byKey = new Map();
    L.geoJSON(fc, {
      pane: "oben",  // über der roten Kennzahlenkarte, deshalb blau
      style: { color: "#1d4ed8", weight: 2.5, fillColor: "#1d4ed8", fillOpacity: 0.08 },
      onEachFeature: (f, l) => {
        const p = f.properties;
        l.bindPopup(`<dl class="popup"><dt>Gemeinde</dt><dd>${esc(p.name)} (${esc(p.key)})</dd>` +
          `<dt>Entfernung zum Mittelpunkt</dt><dd>${km(p.entfernung_m)}</dd><dt>Fläche im Kreis</dt><dd>${ha(p.flaeche_im_umkreis_m2)}</dd></dl>`);
        byKey.set(String(p.key), l);
      },
    }).addTo(layers.radius);
    const circle = L.circle([c.lat, c.lon], { pane: "oben", radius: r, color: "#333", weight: 2, dashArray: "6 4", fill: false }).addTo(layers.radius);
    L.marker([c.lat, c.lon]).bindPopup(`Mittelpunkt: ${esc(c.name)}`).addTo(layers.radius);
    layers.radius.addTo(map);
    map.fitBounds(circle.getBounds());  // getBounds braucht einen Kreis, der bereits auf der Karte liegt
    const n = fc.summe.anzahl;
    $("radius-stats").textContent = `${n} ${n === 1 ? "Gemeinde liegt" : "Gemeinden liegen"} ganz oder teilweise im Kreis ` +
      `(${km(r)} um ${c.name}), zusammen ${ha(fc.summe.flaeche_im_umkreis_m2)} im Kreis.` + (n ? " Zeile anklicken zum Anzeigen." : "");
    // Ergebnisliste nach Entfernung (die API liefert bereits sortiert)
    const table = $("radius-results");
    const tbody = table.querySelector("tbody");
    tbody.innerHTML = "";
    fc.features.forEach((f) => {
      const p = f.properties;
      const tr = document.createElement("tr");
      tr.tabIndex = 0;
      [`${p.name ?? p.key}`, km(p.entfernung_m), ha(p.flaeche_im_umkreis_m2)]
        .forEach((v) => { const td = document.createElement("td"); td.textContent = v; tr.appendChild(td); });
      const show = () => { const l = byKey.get(String(p.key)); if (l) { map.fitBounds(l.getBounds(), { maxZoom: 13 }); l.openPopup(); } };
      tr.addEventListener("click", show);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") show(); });
      tbody.appendChild(tr);
    });
    table.hidden = n === 0;
  } finally {
    btn.disabled = false;
  }
}

function setPickMode() {
  const pick = $("radius-center").value === "klick";
  $("map").classList.toggle("pick-mode", pick);
  if (pick && !clickCenter) $("radius-stats").textContent = "Jetzt in die Karte klicken: Der Klickpunkt wird Mittelpunkt, die Suche startet sofort.";
}

map.on("click", (e) => {
  if ($("radius-center").value !== "klick") return;
  clickCenter = { lat: e.latlng.lat, lon: e.latlng.lng, name: `Punkt ${fmtCoord.format(e.latlng.lat)}, ${fmtCoord.format(e.latlng.lng)}` };
  guard(radiusSearch)();
});

async function loadChoropleth() {
  const current = latest("choro");
  layers.choro.clearLayers();
  $("choro-legend").textContent = "";
  hideLegend();
  choroValues = null;
  if (!$("show-choropleth").checked || !$("indicator").value) return;
  const fc = await api("/api/choropleth", {
    dataset: $("indicator").dataset.source, geometry_dataset: $("dataset").value,
    indicator: $("indicator").value, period: $("period").value,
  });
  if (!current() || !$("show-choropleth").checked) return;
  layers.choro.clearLayers();
  const vals = fc.features.map((f) => f.properties.wert).filter((v) => typeof v === "number").sort((a, b) => a - b);
  const classes = quintileClasses(vals);
  const classOf = (v) => (typeof v !== "number" ? -1 : classes.findIndex((c) => v <= c.hi));
  layers.choro = L.geoJSON(fc, {
    style: (f) => {
      const i = classOf(f.properties.wert);
      return { color: "#555", weight: 0.6, fillColor: i < 0 ? NO_VALUE : classes[i].color, fillOpacity: 0.8 };
    },
    onEachFeature: (f, l) => l.bindPopup(popup(f.properties)),
  }).addTo(map);
  choroValues = new Map(fc.features.map((f) => [String(f.properties.key), f.properties.wert]));
  classes.forEach((c, i) => { c.n = vals.filter((v) => classOf(v) === i).length; });
  showLegend($("indicator").value, $("period").value, classes, fc.features.length - vals.length);
  $("choro-legend").textContent = vals.length
    ? `${fmt.format(vals.length)} Gemeinden mit Wert, Spanne ${fmt.format(vals[0])} bis ${fmt.format(vals[vals.length - 1])}`
    : "keine Werte für diese Auswahl";
}

// Klassen nach Quintilen; gleiche Grenzen (viele gleiche Werte) werden zusammengefasst.
const RAMP = ["#fde0e5", "#f5a3b2", "#e8607a", "#cc0a2f", "#7a0619"];
const NO_VALUE = "#d9d9d9";
function quintileClasses(vals) {
  if (!vals.length) return [];
  const q = (p) => vals[Math.min(vals.length - 1, Math.floor(p * vals.length))];
  const max = vals[vals.length - 1];
  const his = [...new Set([q(0.2), q(0.4), q(0.6), q(0.8)].filter((b) => b < max)), max];
  const pick = his.length === 1 ? [3] : his.map((_, i) => Math.round((i * (RAMP.length - 1)) / (his.length - 1)));
  return his.map((hi, i) => ({ lo: i === 0 ? vals[0] : his[i - 1], hi, color: RAMP[pick[i]], first: i === 0 }));
}

const legend = L.control({ position: "bottomright" });
let legendShown = false;
legend.onAdd = () => L.DomUtil.create("div", "map-legend");
function showLegend(indicator, period, classes, missing) {
  if (!legendShown) { legend.addTo(map); legendShown = true; }
  const num = (v) => fmt.format(v);
  const rows = classes.map((c) =>
    `<tr><td><span class="sw" style="background:${c.color}"></span></td>` +
    `<td>${c.first ? "" : "über "}${num(c.lo)} bis ${num(c.hi)}</td><td class="n">${num(c.n)}</td></tr>`).join("");
  const none = missing ? `<tr><td><span class="sw" style="background:${NO_VALUE}"></span></td><td>kein Wert</td><td class="n">${num(missing)}</td></tr>` : "";
  legend.getContainer().innerHTML =
    `<h3>${esc(indicator)}</h3><div class="sub">${esc(period)} · Klassen nach Quintilen · Anzahl Gemeinden</div>` +
    `<table>${rows}${none}</table>`;
}
function hideLegend() { if (legendShown) { legend.remove(); legendShown = false; } }

// Fehler sichtbar machen statt nur in der Konsole (Text, nie HTML)
function showError(e) {
  console.error(e);
  const box = $("app-error");
  box.textContent = `Abfrage fehlgeschlagen (${e?.message || e}). Bitte erneut versuchen.`;
  box.hidden = false;
}
function guard(fn) {
  return () => { $("app-error").hidden = true; return fn().catch(showError); };
}

// Abmelden per POST mit dem Token aus /api/info (CSRF-Schutz); danach immer zur Anmeldeseite
$("logout").addEventListener("click", async () => {
  try {
    await fetch("/logout", { method: "POST", headers: { "X-CSRF-Token": info?.csrf || "" }, redirect: "manual" });
  } finally {
    window.location.assign("/login");
  }
});
$("dataset").addEventListener("change", guard(loadFeatures));
$("show-features").addEventListener("change", guard(loadFeatures));
$("run").addEventListener("change", guard(async () => { await loadChanges(); await loadErrors(); }));
// Kennzahlen- und Änderungskarte färben dieselben Flächen ein: immer nur eine von beiden zeigen.
$("show-changes").addEventListener("change", guard(async () => {
  if ($("show-changes").checked && $("show-choropleth").checked) {
    $("show-choropleth").checked = false;
    await loadChoropleth();
  }
  await loadChanges();
}));
$("show-errors").addEventListener("change", guard(loadErrors));
$("radius").addEventListener("input", () => { $("radius-out").textContent = `${(Number($("radius").value) / 1000).toLocaleString("de-DE", { minimumFractionDigits: 1 })} km`; });
$("radius-btn").addEventListener("click", guard(radiusSearch));
$("radius-center").addEventListener("change", setPickMode);
$("search-form").addEventListener("submit", (ev) => guard(() => search(ev))());
async function showChoropleth() {
  $("show-choropleth").checked = true;  // neue Auswahl soll sofort sichtbar sein
  if ($("show-changes").checked) {
    $("show-changes").checked = false;
    await loadChanges();
  }
  await loadChoropleth();
}
$("show-choropleth").addEventListener("change", guard(() => ($("show-choropleth").checked ? showChoropleth() : loadChoropleth())));
$("period").addEventListener("change", guard(showChoropleth));
$("indicator").addEventListener("change", guard(async () => { fillPeriods(); await showChoropleth(); }));

(async () => {
  await loadInfo();
  await Promise.all([loadFeatures(), loadRuns(), loadIndicators()]);
})().catch(showError);
