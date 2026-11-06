"use strict";
/*
 * The Aria of Sorrow tracker's page.
 *
 * The page loads the map once from /map.json. Its coordinates are in map cells: cell (x, y)
 * covers x to x + 1 across and y to y + 1 down. Every room cell and location pin is built once as
 * an SVG element, and each snapshot from the tracker (over /ws) only changes their classes and
 * visibility. The walls and doors are drawn as a few SVG paths, which each snapshot redraws.
 * The view is the SVG viewBox, so panning and zooming never rebuild anything.
 */

const SVG_NS = "http://www.w3.org/2000/svg";
const REALM = "B";      // the Chaotic Realm's area, which the game never marks as explored
const CASTLE = "castle"; // the focus that shows every castle area at once
const ZOOM_STEP = 1.2;
const DRAG_THRESHOLD = 4; // pixels a press can move and still count as a click

const $ = (id) => document.getElementById(id);

// Preferences live in localStorage, which may be unavailable, so every access is guarded.
const prefs = {
  get(key, fallback) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : JSON.parse(value);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* the page works without it */
    }
  },
};

const page = {
  map: null,           // /map.json
  snapshot: {},        // the tracker's latest snapshot
  socket: null,
  build: null,         // the build of the tracker that served this page, from /map.json
  quit: false,         // whether the player stopped the tracker from this page
  retry: null,         // the timer that opens the socket again after it closes
  reloading: false,    // set once the page has started loading a newer tracker's page
  gone: false,         // whether the page has shown that its tracker is gone
  focus: CASTLE,       // CASTLE or an area id
  selected: null,      // the selected location's id
  view: { x: 0, y: 0, w: 64, h: 47 },
  somaArea: null,      // the area Soma was last seen in, to follow him when it changes
  somaCell: null,      // the cell Soma was last seen in, to move the view when he leaves it
  askedToConnect: false,
  fittedTo: null,      // "whole", "nothing" or "explored": what the view was last fitted to
  reveal: prefs.get("reveal", false),
  revealItems: prefs.get("revealItems", false),  // also list the progression items Soma lacks
  follow: prefs.get("follow", true),
  centerMarker: prefs.get("centerMarker", false),  // draw Soma's dot in the middle of his cell
  showChecked: prefs.get("showChecked", false),
  gameLook: prefs.get("gameLook", false),  // draw the map in the game's colours and wall width
};

const els = {
  cells: new Map(),     // "x,y" -> rect
  edges: [],            // {x, y, top, door, cells, hidden}: drawn by renderEdges
  edgePaths: new Map(), // look -> the path that draws every wall or door with that look
  pins: new Map(),      // location id -> its circles, one for each of its spots
};

// The looks walls and doors can have, in drawing order, so doors sit on top of walls.
const EDGE_LOOKS = ["wall unfocused", "wall", "door unfocused", "door"];
// How thick walls and doors are drawn, in cells. style.css sets it, because "In-game look"
// changes it.
function wallWidth() {
  // Fall back to the usual width if the stylesheet hasn't loaded, rather than drawing nothing.
  return parseFloat(getComputedStyle($("map")).getPropertyValue("--wall-width")) || 0.12;
}

const locationsById = new Map();
const areasById = new Map();

const key = (x, y) => `${x},${y}`;

function svg(tag, attributes, parent) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  if (parent) parent.appendChild(element);
  return element;
}

// --- Names ----------------------------------------------------------------------------------

const UNKNOWN_AREA = "???";

const NOTHING_EXPLORED = { visited: [], remembered: [], hidden_doors: [], castle_maps: [], revealed: [] };

// What Soma has explored, or null to treat the whole castle as explored. It is null only when the
// page has no explored map and the tracker isn't tracking a slot. A tracked slot whose client
// hasn't sent the explored map yet, as before play, has nothing explored, so the page doesn't
// show every room until the map arrives.
function explored() {
  return page.snapshot.exploration || (page.snapshot.tracking ? NOTHING_EXPLORED : null);
}

// The areas Soma has been to, or null to treat them all as known. While unexplored cells are
// hidden, he has been to an area once he is in it, or has visited or remembers a cell in it.
// Checking a location that can be checked only in one area also shows that he has been there.
// The game never marks the Chaotic Realm as explored, so once he has left the Realm, a location
// checked there is the only sign that he was in it.
function knownAreas() {
  const cells = explored();
  if (page.reveal || !cells) return null;
  const known = new Set();
  for (const [x, y] of [...cells.visited, ...cells.remembered]) {
    const rect = els.cells.get(key(x, y));
    if (rect) known.add(rect.dataset.area);
  }
  if (page.snapshot.position) known.add(page.snapshot.position[0][0]);
  // A defeat location can be checked in any room where its enemy appears. So its check shows that
  // Soma was in an area only when all of those rooms are in that one area.
  for (const [id, status] of Object.entries(page.snapshot.locations || {})) {
    const location = locationsById.get(Number(id));
    if (status !== "checked" || !location) continue;
    if (new Set(location.spots.map((spot) => spot.area)).size === 1) known.add(location.area);
  }
  return known;
}

// An area's name. While unexplored cells are hidden, an area Soma hasn't been to is named "???".
function areaName(areaId, known = knownAreas()) {
  const area = areasById.get(areaId);
  if (!area) return "";
  return !known || known.has(areaId) ? area.name : UNKNOWN_AREA;
}

// While unexplored cells are hidden, an area Soma hasn't been to isn't listed, so it can't stay in
// focus either. The focus moves to his area, or to the whole castle.
function keepFocusOnAKnownArea() {
  const known = knownAreas();
  if (!known || page.focus === CASTLE || known.has(page.focus)) return;
  const somaArea = page.snapshot.position ? page.snapshot.position[0][0] : null;
  setFocus(somaArea && known.has(somaArea) ? somaArea : CASTLE);
}

// A location's title and subtitle. A name such as "Terrace [503 CASTLE MAP 3]" is a place, then
// the room and the item the game originally has there. The title is the place, and the subtitle
// gives the area, the room and that item. Any other name, such as a defeat location's, becomes the
// whole title, and the subtitle lists the areas of the location's spots.
function describeLocation(location) {
  const match = /^(.*?) \[(\S+) (.*)\]$/.exec(location.name);
  const area = [...new Set(location.spots.map((spot) => areaName(spot.area)))].join(", ");
  if (!match) return { title: location.name, subtitle: area };
  return {
    title: match[1],
    subtitle: `${area} · room ${match[2]} · originally ${match[3]}`,
  };
}

// --- Building the map -----------------------------------------------------------------------

function buildMap() {
  const map = page.map;
  const cellLayer = $("layer-cells");
  cellLayer.setAttribute("shape-rendering", "crispEdges");
  for (const cell of map.cells) {
    const rect = svg("rect", { x: cell.x, y: cell.y, width: 1, height: 1,
                               class: `cell ${cell.kind}` }, cellLayer);
    rect.dataset.area = cell.area;
    els.cells.set(key(cell.x, cell.y), rect);
  }

  const hiddenIndex = new Map(map.hidden_doors.map((door, index) => [`${door.side},${door.x},${door.y}`, index]));
  for (const edge of map.edges) {
    const { x, y } = edge;
    const top = edge.side === "top";
    const cells = top ? [key(x, y - 1), key(x, y)] : [key(x - 1, y), key(x, y)];
    const hidden = edge.kind === "hidden" ? hiddenIndex.get(`${edge.side},${x},${y}`) : null;
    els.edges.push({ x, y, top, door: edge.kind !== "wall", cells, hidden });
  }
  const edgeLayer = $("layer-edges");
  for (const look of EDGE_LOOKS) els.edgePaths.set(look, svg("path", { class: `edge ${look}` }, edgeLayer));

  const pinLayer = $("layer-pins");
  for (const location of map.locations) {
    locationsById.set(location.id, location);
    els.pins.set(location.id, location.spots.map((spot) => {
      const pin = svg("circle", { cx: spot.x, cy: spot.y, r: 0.22, class: "pin" }, pinLayer);
      pin.dataset.id = location.id;
      pin.setAttribute("aria-label", location.name);
      return pin;
    }));
  }

  for (const area of map.areas) areasById.set(area.id, area);
}

// --- The view -------------------------------------------------------------------------------

function applyView() {
  const { x, y, w, h } = page.view;
  $("map").setAttribute("viewBox", `${x} ${y} ${w} ${h}`);
}

function inFocus(area) {
  return page.focus === CASTLE ? area !== REALM : area === page.focus;
}

const MIN_VIEW = { w: 18, h: 12 }; // cells, so a few explored rooms don't fill the screen

function cellBounds(cells) {
  return [Math.min(...cells.map((c) => c.x)), Math.min(...cells.map((c) => c.y)),
          Math.max(...cells.map((c) => c.x)) + 1, Math.max(...cells.map((c) => c.y)) + 1];
}

// Fit the view to the whole map, the Chaotic Realm's space included. While unexplored cells are
// hidden, fit it to the part of the map that is shown instead, at least MIN_VIEW in size. If no
// part is shown yet, fit the whole map.
function fitView() {
  const visible = visibleCells();
  const shown = visible ? page.map.cells.filter((cell) => visible.has(key(cell.x, cell.y))) : [];
  let [x0, y0, x1, y1] = cellBounds(shown.length ? shown : page.map.cells);
  if (shown.length) {
    const grow = { w: Math.max(0, MIN_VIEW.w - (x1 - x0)) / 2, h: Math.max(0, MIN_VIEW.h - (y1 - y0)) / 2 };
    [x0, x1, y0, y1] = [x0 - grow.w, x1 + grow.w, y0 - grow.h, y1 + grow.h];
  }
  page.view = { x: x0 - 1, y: y0 - 1, w: x1 - x0 + 2, h: y1 - y0 + 2 };
  applyView();
}

// Fit the view again only when the kind of map shown changes: the whole map, nothing yet, or the
// explored part. So the view fits the first rooms the client sends and then stays where it is.
// Exploring further, or picking an area, leaves the view alone.
function fitWhenTheShownMapChanges() {
  const cells = explored();
  const anything = cells && (cells.visited.length || cells.remembered.length || cells.revealed.length);
  const shown = page.reveal || !cells ? "whole" : anything ? "explored" : "nothing";
  if (shown !== page.fittedTo) {
    page.fittedTo = shown;
    fitView();
  }
}

// Focus an area: highlight it and list what is in logic there. The view stays where it is.
function setFocus(focus) {
  page.focus = focus;
  renderMap();  // the Chaotic Realm is drawn while it is in focus
  renderFocus();
  renderAreas();
  renderLocationList();
}

// The SVG units per screen pixel. "meet" fits the whole viewBox, so the larger ratio applies.
function unitsPerPixel() {
  const box = $("map").getBoundingClientRect();
  return Math.max(page.view.w / box.width, page.view.h / box.height);
}

function svgPoint(clientX, clientY) {
  const map = $("map");
  const point = map.createSVGPoint();
  point.x = clientX;
  point.y = clientY;
  return point.matrixTransform(map.getScreenCTM().inverse());
}

const ZOOM_LIMITS = { min: 3, max: 200 };  // how many cells the view can be across

function zoom(factor, clientX, clientY) {
  const width = Math.min(ZOOM_LIMITS.max, Math.max(ZOOM_LIMITS.min, page.view.w * factor));
  factor = width / page.view.w;
  const box = $("map").getBoundingClientRect();
  const at = svgPoint(clientX ?? box.left + box.width / 2, clientY ?? box.top + box.height / 2);
  const view = page.view;
  page.view = { x: at.x - (at.x - view.x) * factor, y: at.y - (at.y - view.y) * factor,
                w: view.w * factor, h: view.h * factor };
  applyView();
}

// Whether a point is within the view, a little way in from its edges.
function inView(point, margin = 0.5) {
  const view = page.view;
  return point.x >= view.x + margin && point.x <= view.x + view.w - margin
      && point.y >= view.y + margin && point.y <= view.y + view.h - margin;
}

function centerOn(x, y) {
  page.view = { ...page.view, x: x - page.view.w / 2, y: y - page.view.h / 2 };
  applyView();
}

function setUpPanAndZoom() {
  const map = $("map");
  let press = null;

  map.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    press = { id: event.pointerId, x: event.clientX, y: event.clientY, view: { ...page.view },
              scale: unitsPerPixel(), moved: false };
    map.setPointerCapture(event.pointerId);
  });
  map.addEventListener("pointermove", (event) => {
    if (!press || event.pointerId !== press.id) return;
    const dx = event.clientX - press.x;
    const dy = event.clientY - press.y;
    if (!press.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    press.moved = true;
    map.classList.add("dragging");
    page.view = { ...press.view, x: press.view.x - dx * press.scale, y: press.view.y - dy * press.scale };
    applyView();
  });
  map.addEventListener("pointerup", (event) => {
    if (!press || event.pointerId !== press.id) return;
    map.classList.remove("dragging");
    if (!press.moved) {
      // A click on a pin selects it, and a click anywhere else clears the selection.
      const target = document.elementFromPoint(event.clientX, event.clientY);
      const pin = target && target.closest ? target.closest(".pin") : null;
      select(pin ? Number(pin.dataset.id) : null);
    }
    press = null;
  });
  map.addEventListener("pointercancel", () => {
    press = null;
    map.classList.remove("dragging");
  });
  map.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (!event.deltaY) return;  // a sideways scroll isn't a zoom
    // A mouse wheel notch is about 100 pixels, 3 lines or 1 page, while a touchpad sends many
    // small steps. So each event zooms by how far the wheel turned, up to one notch's step.
    const perNotch = { [WheelEvent.DOM_DELTA_PIXEL]: 100, [WheelEvent.DOM_DELTA_LINE]: 3,
                       [WheelEvent.DOM_DELTA_PAGE]: 1 }[event.deltaMode] || 100;
    const notches = event.deltaY / perNotch;
    zoom(ZOOM_STEP ** Math.max(-1, Math.min(1, notches)), event.clientX, event.clientY);
  }, { passive: false });

  $("zoom-in").addEventListener("click", () => zoom(1 / ZOOM_STEP));
  $("zoom-out").addEventListener("click", () => zoom(ZOOM_STEP));
  $("zoom-fit").addEventListener("click", fitView);
}

// --- What is shown --------------------------------------------------------------------------

// The cells to draw, or null for all of them. Unless the full map is revealed, a cell stays hidden
// until Soma explores it or a Castle Map he holds shows it. The game never marks the Chaotic
// Realm as explored, so it is drawn whole, but only while it is in focus or Soma is in it. With
// countRealm, the Realm's cells count as shown once Soma has been there, whatever the focus. The
// panels count locations this way, so that their numbers don't change with the focus.
function visibleCells({ countRealm = false } = {}) {
  const cells = explored();
  if (page.reveal || !cells) return null;
  const visible = new Set();
  for (const [x, y] of cells.visited) visible.add(key(x, y));
  for (const [x, y] of cells.remembered) visible.add(key(x, y));
  // The cells Soma's Castle Maps show, greyed out until he visits them, as in the game.
  for (const [x, y] of cells.revealed || []) visible.add(key(x, y));
  const soma = somaPoint();
  if (soma) visible.add(key(Math.floor(soma.x), Math.floor(soma.y)));
  const inRealm = Boolean(soma && soma.room[0] === REALM);
  const realmShown = countRealm ? inRealm || knownAreas().has(REALM) : inRealm || page.focus === REALM;
  if (realmShown) {
    for (const cell of page.map.cells) if (cell.area === REALM) visible.add(key(cell.x, cell.y));
  }
  return visible;
}

function visitedCells() {
  const cells = explored();
  return cells ? new Set(cells.visited.map(([x, y]) => key(x, y))) : null;
}

// Whether a cell is drawn in full rather than greyed out. While unexplored cells are hidden, the
// cells Soma hasn't visited are greyed out, as on the game's map. Those are the cells he only
// remembers and the cells a Castle Map shows. The Chaotic Realm is never greyed out, because the
// game never marks it as explored.
function drawnInFull(cellKey, visited) {
  const rect = els.cells.get(cellKey);
  return !visited || page.reveal || (rect && rect.dataset.area === REALM) || visited.has(cellKey);
}

// Whether a spot's pin shows on the map. It shows only if its cell is shown and drawn in full. A
// greyed-out cell is one Soma hasn't visited, so its pins stay hidden even when a Castle Map shows
// the cell.
function spotShown(spot, visible, visited) {
  const cell = key(Math.floor(spot.x), Math.floor(spot.y));
  return (!visible || visible.has(cell)) && drawnInFull(cell, visited);
}

// The spots of a location that show, among those in an area that inArea accepts. A location shows
// in the sidebar and in the counts when any of its spots does. A defeat location has a spot in each
// room where its enemy appears, and it counts once however many of them show.
function shownSpots(location, visible, visited, inArea = () => true) {
  return location.spots.filter((spot) => inArea(spot.area) && spotShown(spot, visible, visited));
}

function renderMap() {
  const visible = visibleCells();
  const visited = visitedCells();
  for (const [cellKey, rect] of els.cells) {
    rect.style.display = !visible || visible.has(cellKey) ? "" : "none";
    rect.classList.toggle("remembered", !drawnInFull(cellKey, visited));
  }
  renderEdges(visible);
  $("fog-note").hidden = Boolean(page.snapshot.exploration) || page.reveal || !page.snapshot.tracking;
  renderPins(visible);
}

// Draw the walls and doors that touch a shown cell, one path for each look. A path's stroke is
// filled as one shape, so where two segments overlap at a cell corner nothing is drawn twice.
function renderEdges(visible = visibleCells()) {
  const cells = explored();
  const visited = visitedCells();
  const passed = new Set(cells ? cells.hidden_doors : []);
  const paths = Object.fromEntries(EDGE_LOOKS.map((look) => [look, []]));
  // As on the game's map, a door fills its edge between the two corners, which stay in the wall
  // colour. So it stops half a wall's width short of each end.
  const inset = wallWidth() / 2;
  for (const edge of els.edges) {
    if (visible && !edge.cells.some((cell) => visible.has(cell))) continue;
    // A wall or door is lit where it borders a cell that is in focus and drawn in full, so the
    // walls of greyed-out rooms stay grey with them.
    const lit = edge.cells.some((cell) => els.cells.has(cell) && inFocus(els.cells.get(cell).dataset.area)
                                          && drawnInFull(cell, visited));
    const dim = lit ? "" : " unfocused";
    const { x, y } = edge;
    paths[`wall${dim}`].push(edge.top ? `M${x} ${y}h1` : `M${x} ${y}v1`);
    // A hidden door is drawn as a wall until Soma has gone through it, unless the full map is
    // revealed.
    const open = edge.hidden === null || page.reveal || !cells || passed.has(edge.hidden);
    if (edge.door && open) {
      paths[`door${dim}`].push(edge.top ? `M${x + inset} ${y}h${1 - 2 * inset}`
                                        : `M${x} ${y + inset}v${1 - 2 * inset}`);
    }
  }
  for (const look of EDGE_LOOKS) els.edgePaths.get(look).setAttribute("d", paths[look].join(""));
}

function renderPins(visible = visibleCells()) {
  const statuses = page.snapshot.locations || {};
  const hinted = new Set(page.snapshot.hinted || []);
  const visited = visitedCells();
  for (const [id, pins] of els.pins) {
    const status = statuses[id];
    const location = locationsById.get(id);
    const hint = hinted.has(id) ? " hinted" : "";
    location.spots.forEach((spot, index) => {
      const shown = status !== undefined && spotShown(spot, visible, visited)
                    && (status !== "checked" || page.showChecked);
      pins[index].style.display = shown ? "" : "none";
      const focus = inFocus(spot.area) ? "" : " unfocused";
      pins[index].setAttribute("class", `pin ${status || ""}${hint}${focus}`);
    });
  }
  renderSelectionRing();
}

// Dim the cells outside the focused area, and redraw the walls and doors to match. renderPins
// dims the pins.
function renderFocus() {
  for (const rect of els.cells.values()) rect.classList.toggle("unfocused", !inFocus(rect.dataset.area));
  renderEdges();
}

function somaPoint() {
  const position = page.snapshot.position;
  if (!position || !page.map) return null;
  const [room, x, y] = position;
  const origin = page.map.rooms[room];
  if (!origin) return null;
  return { x: origin[0] + x / page.map.room_cell[0], y: origin[1] + y / page.map.room_cell[1], room };
}

// Where to draw Soma's dot: where he stands, or the middle of his cell when "Center Soma's
// indicator" is on, as the in-game map marks him.
function somaMarker(soma) {
  if (!page.centerMarker) return { x: soma.x, y: soma.y };
  return { x: Math.floor(soma.x) + 0.5, y: Math.floor(soma.y) + 0.5 };
}

// How far the outline of Soma's cell sits inside the walls on its edges: half the outline's own
// stroke width, plus a small gap.
const SOMA_GAP = 0.08;

function renderSoma() {
  // Soma's cell is drawn under the walls and doors, so they stay visible, and his dot is drawn
  // over everything.
  const cellLayer = $("layer-soma-cell");
  const layer = $("layer-soma");
  cellLayer.replaceChildren();
  layer.replaceChildren();
  const soma = somaPoint();
  if (!soma) return;
  const [x, y] = [Math.floor(soma.x), Math.floor(soma.y)];
  svg("rect", { x, y, width: 1, height: 1, class: "soma-cell" }, cellLayer);
  const inset = wallWidth() / 2 + SOMA_GAP;
  svg("rect", { x: x + inset, y: y + inset, width: 1 - 2 * inset, height: 1 - 2 * inset,
                class: "soma-cell-outline" }, cellLayer);
  const marker = somaMarker(soma);
  svg("circle", { cx: marker.x, cy: marker.y, r: 0.22, class: "soma-dot" }, layer);
  // "Follow Soma" moves the view only when he goes into another cell and his dot is outside the
  // view. So a view the player has moved stays put while he stands still, and picking an area
  // never moves it.
  const cell = key(x, y);
  if (page.follow && page.somaCell !== null && cell !== page.somaCell && !inView(marker)) {
    centerOn(marker.x, marker.y);
  }
  page.somaCell = cell;
  const area = soma.room[0];
  if (page.follow && area !== page.somaArea && page.somaArea !== null) setFocus(area);
  if (page.somaArea === null && page.focus === CASTLE && page.follow) setFocus(area);
  page.somaArea = area;
}

function renderRoute() {
  const layer = $("layer-route");
  layer.replaceChildren();
  const route = page.snapshot.route;
  if (!route) return;
  const points = route.points.map(([x, y]) => [x, y]);
  const soma = somaPoint();
  if (route.start_room && soma) {
    const marker = somaMarker(soma);
    points.unshift([marker.x, marker.y]);  // the line starts at Soma's dot, wherever it is drawn
  }
  // A defeat location has a spot in each room where its enemy appears, and the route leads to one
  // of them. The line ends at the spot nearest the route's last point.
  const target = locationsById.get(route.target);
  const last = points[points.length - 1];
  const end = target && (last ? nearestSpot(target, last) : target.spots[0]);
  if (end) points.push([end.x, end.y]);
  const coordinates = points.map(([x, y]) => `${x},${y}`).join(" ");
  svg("polyline", { points: coordinates, class: "route-casing" }, layer);
  svg("polyline", { points: coordinates, class: "route-line" }, layer);
  if (end) svg("circle", { cx: end.x, cy: end.y, r: 0.45, class: "route-end" }, layer);
}

function nearestSpot(location, [x, y]) {
  const distance = (spot) => Math.hypot(spot.x - x, spot.y - y);
  return location.spots.reduce((best, spot) => (distance(spot) < distance(best) ? spot : best));
}

function renderSelectionRing() {
  for (const ring of document.querySelectorAll(".selection-ring")) ring.remove();
  const location = locationsById.get(page.selected);
  const pins = els.pins.get(page.selected);
  if (!location || !pins) return;
  location.spots.forEach((spot, index) => {
    if (pins[index].style.display === "none") return;  // no ring around a hidden pin
    svg("circle", { cx: spot.x, cy: spot.y, r: 0.4, class: "selection-ring" }, $("layer-pins"));
  });
}

// --- The panels -----------------------------------------------------------------------------

// Count an area's locations that are in logic, and those with tricks, among the ones that show
// (shownSpots). So while unexplored cells are hidden, the numbers don't give away what lies in
// rooms Soma hasn't visited.
function counts(area, visible, visited) {
  const statuses = page.snapshot.locations || {};
  let inLogic = 0;
  let tricks = 0;
  for (const [id, status] of Object.entries(statuses)) {
    const location = locationsById.get(Number(id));
    if (!location) continue;
    const inArea = area === CASTLE ? (spotArea) => spotArea !== REALM : (spotArea) => spotArea === area;
    if (!shownSpots(location, visible, visited, inArea).length) continue;
    if (status === "in_logic") inLogic += 1;
    if (status === "tricks") tricks += 1;
  }
  return { inLogic, tricks };
}

function renderAreas() {
  const list = $("areas");
  list.replaceChildren();
  const entries = [{ id: CASTLE, name: "Whole castle" }, ...page.map.areas];
  const somaArea = page.snapshot.position ? page.snapshot.position[0][0] : null;
  const visible = visibleCells({ countRealm: true });
  const visited = visitedCells();
  const known = knownAreas();
  for (const area of entries) {
    // While unexplored cells are hidden, an area Soma hasn't been to isn't listed.
    if (area.id !== CASTLE && known && !known.has(area.id)) continue;
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.classList.toggle("active", page.focus === area.id);
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = area.name;
    button.appendChild(name);
    if (area.id === somaArea) {
      const here = document.createElement("span");
      here.className = "here";
      here.textContent = "Soma";
      button.appendChild(here);
    }
    if (page.snapshot.tracking) {
      const { inLogic, tricks } = counts(area.id, visible, visited);
      const count = document.createElement("span");
      count.className = `count${inLogic ? "" : tricks ? " tricks" : " zero"}`;
      count.textContent = inLogic || tricks || "0";
      count.title = `${inLogic} in logic, ${tricks} with tricks`;
      button.appendChild(count);
    }
    button.addEventListener("click", () => setFocus(area.id));
    item.appendChild(button);
    list.appendChild(item);
  }
}

function renderItems() {
  const container = $("items");
  container.replaceChildren();
  const items = page.snapshot.items;
  if (!items) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "Connect to see what you have.";
    container.appendChild(note);
    return;
  }
  // Listing the items Soma doesn't have yet would give away what the seed has left to find, so
  // they are listed only with "Reveal useful items" on.
  const listed = page.revealItems ? items : items.filter((item) => item.count);
  if (!listed.length) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "Nothing collected yet.";
    container.appendChild(note);
    return;
  }
  for (const group of ["Souls", "Key items", "Weapons"]) {
    const members = listed.filter((item) => item.group === group);
    if (!members.length) continue;
    const heading = document.createElement("div");
    heading.className = "item-group";
    heading.textContent = group;
    container.appendChild(heading);
    // What Soma has comes first, so the missing items read as a list of what's left.
    members.sort((a, b) => (Boolean(b.count) - Boolean(a.count)) || a.name.localeCompare(b.name));
    for (const item of members) {
      const chip = document.createElement("span");
      chip.className = `item${item.count ? " have" : ""}`;
      chip.textContent = item.name;
      if (item.count > 1) {
        const times = document.createElement("span");
        times.className = "times";
        times.textContent = `×${item.count}`;
        chip.appendChild(times);
      }
      container.appendChild(chip);
    }
  }
}

function renderHeader() {
  const snapshot = page.snapshot;
  const connection = $("connection");
  connection.classList.toggle("tracking", Boolean(snapshot.tracking));
  // The dot is amber while the tracker connects, rebuilds the seed's logic, or waits to reconnect.
  const connecting = snapshot.following || /^(Connecting|Rebuilding)/.test(snapshot.status || "");
  connection.classList.toggle("connecting", !snapshot.tracking && Boolean(connecting));
  $("status-text").textContent = snapshot.status || "Not connected";
  $("stats").hidden = !snapshot.tracking;
  if (!snapshot.tracking) return;
  const statuses = Object.values(snapshot.locations || {});
  const checked = statuses.filter((status) => status === "checked").length;
  $("stat-checked").textContent = `${checked}/${statuses.length}`;
  // The checked count is the slot's progress, so it counts every location. The in-logic and
  // with-tricks counts include only the locations that show (shownSpots), like the area counts.
  const visible = visibleCells({ countRealm: true });
  const visited = visitedCells();
  let inLogic = 0;
  let tricks = 0;
  for (const [id, status] of Object.entries(snapshot.locations || {})) {
    const location = locationsById.get(Number(id));
    if (!location || !shownSpots(location, visible, visited).length) continue;
    if (status === "in_logic") inLogic += 1;
    if (status === "tricks") tricks += 1;
  }
  $("stat-logic").textContent = inLogic;
  $("stat-tricks").textContent = tricks;
  $("stat-goal").textContent = { yes: "in logic", tricks: "with tricks", no: "not yet" }[snapshot.goal] || "—";
}

const STATUS_TEXT = { in_logic: "In logic", tricks: "With tricks", out_of_logic: "Out of logic", checked: "Checked" };

function renderSelection() {
  const location = locationsById.get(page.selected);
  const status = location ? (page.snapshot.locations || {})[location.id] : undefined;
  $("selection").hidden = !location;
  const race = Boolean(page.snapshot.race_mode);
  $("find-route").disabled = race || !location || !page.snapshot.tracking || status === undefined || status === "checked";
  $("find-route").title = race ? "Routes are off in a race room, as in Universal Tracker" : "";
  if (location) {
    const { title, subtitle } = describeLocation(location);
    $("sel-title").textContent = title;
    $("sel-subtitle").textContent = subtitle;
    // Until the tracker tracks a slot, it has no statuses, so the card shows none.
    const chip = $("sel-status");
    chip.hidden = !page.snapshot.tracking;
    chip.className = `chip ${status || ""}`;
    chip.textContent = status ? STATUS_TEXT[status] : "Not in this seed";
  }
  renderSelectionRing();
}

function renderRouteSteps() {
  const snapshot = page.snapshot;
  const asked = snapshot.route_target !== null && snapshot.route_target !== undefined;
  $("clear-route").hidden = !asked;
  $("route").hidden = !asked;
  if (!asked) return;
  const target = locationsById.get(snapshot.route_target);
  const name = target ? describeLocation(target).title : "the location";
  $("route-heading").textContent = `Route to ${name}`;
  const list = $("route-steps");
  list.replaceChildren();
  const route = snapshot.route;
  if (!route) {
    const note = document.createElement("li");
    note.className = "route-note";
    note.textContent = "There is no way there with what you have yet.";
    list.appendChild(note);
    return;
  }
  const from = document.createElement("li");
  from.className = "route-note";
  from.textContent = (route.start_room ? `From room ${route.start_room}, where Soma is` : "From the start of the game")
    + (route.tricks ? ", using tricks" : "");
  list.appendChild(from);
  let current = null;
  const known = knownAreas();
  for (const step of route.steps) {
    const area = step.room[0];  // grouped by id, since two areas can both be "???"
    if (!current || current.area !== area) {
      const item = document.createElement("li");
      const heading = document.createElement("div");
      heading.className = "area-name";
      heading.textContent = areaName(area, known);
      const rooms = document.createElement("div");
      rooms.className = "rooms";
      item.append(heading, rooms);
      list.appendChild(item);
      current = { area, rooms };
    }
    const room = document.createElement("span");
    room.className = `room${step.needs.length ? " needs" : ""}`;
    room.textContent = step.room;
    if (step.needs.length) {
      const needs = document.createElement("span");
      needs.className = "need";
      needs.textContent = step.needs.join(", ");
      room.appendChild(needs);
    }
    current.rooms.appendChild(room);
  }
}

function renderLocationList() {
  const list = $("locations");
  list.replaceChildren();
  const statuses = page.snapshot.locations || {};
  const focusName = page.focus === CASTLE ? "Whole castle" : areaName(page.focus);
  $("list-heading").textContent = `In logic · ${focusName}`;
  const visible = visibleCells({ countRealm: true });
  const visited = visitedCells();
  const shown = page.map.locations.filter((location) => {
    const status = statuses[location.id];
    if (status !== "in_logic" && status !== "tricks") return false;
    return shownSpots(location, visible, visited, inFocus).length > 0;
  });
  shown.sort((a, b) => (statuses[a.id] === statuses[b.id] ? a.name.localeCompare(b.name) : statuses[a.id] === "in_logic" ? -1 : 1));
  if (!shown.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = !page.snapshot.tracking ? "Not tracking yet."
      : page.reveal ? "Nothing in logic here." : "Nothing in logic here that you have explored.";
    list.appendChild(item);
    return;
  }
  for (const location of shown) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.classList.toggle("selected", location.id === page.selected);
    const dot = document.createElement("span");
    dot.className = `status-dot ${statuses[location.id]}`;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = describeLocation(location).title;
    const room = document.createElement("span");
    room.className = "room-id";
    room.textContent = location.spots.length > 1 ? `${location.spots.length} rooms` : location.room;
    button.append(dot, name, room);
    const [spot] = shownSpots(location, visible, visited, inFocus);
    button.addEventListener("click", () => {
      select(location.id);
      centerOn(spot.x, spot.y);
    });
    item.appendChild(button);
    list.appendChild(item);
  }
}

function select(id) {
  page.selected = id;
  renderSelection();
  renderLocationList();
}

// --- The tracker connection -----------------------------------------------------------------

// Send a request to the tracker, and return whether it was sent. Nothing is sent while the socket
// isn't open.
function send(message) {
  if (!page.socket || page.socket.readyState !== WebSocket.OPEN) return false;
  page.socket.send(JSON.stringify(message));
  return true;
}

const NOT_RUNNING = "Tracker not running. Type /map in your client.";

// With its tracker gone, the page keeps the last map it had, but drops what needs the tracker:
// the counts, the route and "Find route". The connection button and the connect dialog say why.
function showTrackerGone() {
  const status = page.quit ? "The tracker has stopped. You can close this tab." : NOT_RUNNING;
  onSnapshot({ ...page.snapshot, status, connected: false, tracking: false,
               route: null, route_target: null });
}

function onSnapshot(snapshot) {
  page.snapshot = snapshot;
  renderHeader();
  renderMap();
  renderSoma();
  fitWhenTheShownMapChanges();
  keepFocusOnAKnownArea();
  renderRoute();
  renderAreas();
  renderItems();
  renderSelection();
  renderRouteSteps();
  renderLocationList();
  $("log").textContent = (snapshot.log || []).join("\n");
  if ($("connect-dialog").open) renderDialog();
}

function openSocket() {
  clearTimeout(page.retry);
  const socket = new WebSocket(`ws://${location.host}/ws`);
  page.socket = socket;
  socket.addEventListener("open", () => {
    page.quit = false;  // a tracker is back, whether or not this page stopped the last one
  });
  socket.addEventListener("message", (event) => {
    if (socket !== page.socket || page.reloading) return;
    const message = JSON.parse(event.data);
    if (message.type !== "snapshot") return;
    // A newer tracker that took over this address serves a newer page, so reload once to get it.
    // The snapshots that arrive before the reload are ignored.
    if (page.build && message.build !== page.build) {
      page.reloading = true;
      socket.close();
      location.reload();
      return;
    }
    // While the tracker reconnects or rebuilds the seed, its snapshots carry no explored map or
    // position. For the same slot on the same server, the page keeps the last ones it had until
    // new ones arrive. Without them, the page would show the whole castle, with every area's name
    // and hidden door. Once the tracker is tracking, a snapshot without them means the client
    // hasn't sent them yet, so nothing is kept.
    const snapshot = message.snapshot;
    const last = page.snapshot;
    if (!snapshot.exploration && !snapshot.tracking && last.exploration
        && snapshot.slot === last.slot && snapshot.server === last.server) {
      snapshot.exploration = page.snapshot.exploration;
      snapshot.position = snapshot.position ?? page.snapshot.position;
    }
    page.gone = false;
    onSnapshot(snapshot);
    // The connect dialog opens by itself only at the page's first snapshot, and only when the
    // tracker isn't connected or connecting to a room. It stays shut when a connection ends later,
    // as before a reconnect or after "Disconnect".
    if (!page.askedToConnect) {
      page.askedToConnect = true;
      if (!snapshot.connected && !snapshot.following && snapshot.status === "Not connected") {
        openConnectDialog();
      }
    }
  });
  socket.addEventListener("close", () => {
    // Ignore a socket that a newer one has replaced, or that closed for a reload.
    if (socket !== page.socket || page.reloading) return;
    if (!page.gone) {
      page.gone = true;  // shown once, not again on every retry while the tracker is away
      showTrackerGone();
    }
    page.retry = setTimeout(openSocket, 2000);
  });
}

// A browser can hold back a background tab's timers for minutes, so when the tab comes back into
// view, the page reopens a closed socket at once rather than waiting for the next retry.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !page.socket) return;
  if (page.socket.readyState === WebSocket.CLOSED && !page.reloading) openSocket();
});

function openConnectDialog() {
  if ($("connect-dialog").open) return;  // the player may be typing in it
  const form = $("connect-form");
  form.server.value = page.snapshot.server || prefs.get("server", "");
  form.slot.value = page.snapshot.slot || prefs.get("slot", "");
  renderDialog();
  $("connect-dialog").showModal();
}

// Update the connect dialog's status line and buttons from the tracker's state. The fields keep
// what the player typed.
function renderDialog() {
  const live = Boolean(page.socket && page.socket.readyState === WebSocket.OPEN);
  $("dialog-status").textContent = page.snapshot.status || "";
  // "Disconnect" also stops a tracker that is still connecting, or waiting to reconnect.
  $("disconnect").disabled = !live || !(page.snapshot.connected || page.snapshot.following);
  $("quit").disabled = !live;
}

// Switch the map between the tracker's own colours and the game's colours and wall width.
function applyLook() {
  $("map-pane").classList.toggle("game-look", page.gameLook);
}

function setUpControls() {
  const toggles = [["reveal", "reveal"], ["reveal-items", "revealItems"], ["follow", "follow"],
                   ["center-marker", "centerMarker"], ["game-look", "gameLook"],
                   ["show-checked", "showChecked"]];
  for (const [id, name] of toggles) {
    const box = $(id);
    box.checked = page[name];
    box.addEventListener("change", () => {
      page[name] = box.checked;
      prefs.set(name, box.checked);
      applyLook();  // first, because drawing the walls and Soma's cell uses the look's wall width
      renderMap();
      renderSoma();
      fitWhenTheShownMapChanges();  // "Reveal full map" can change which part is fitted
      keepFocusOnAKnownArea();
      renderRoute();
      // The counts, and which areas are named, follow what is shown.
      renderHeader();
      renderAreas();
      renderItems();
      renderSelection();
      renderRouteSteps();
      renderLocationList();
      if (name === "follow" && box.checked && page.somaArea) setFocus(page.somaArea);
    });
  }

  $("connection").addEventListener("click", openConnectDialog);
  $("cancel").addEventListener("click", () => $("connect-dialog").close());
  $("disconnect").addEventListener("click", () => {
    if (!send({ type: "disconnect" })) {
      $("dialog-status").textContent = NOT_RUNNING;
      return;
    }
    $("connect-dialog").close();
  });
  $("quit").addEventListener("click", () => {
    page.quit = send({ type: "quit" });
    if (!page.quit) {
      $("dialog-status").textContent = NOT_RUNNING;
      return;
    }
    $("connect-dialog").close();
  });
  $("show-log").addEventListener("click", () => {
    $("log").hidden = !$("log").hidden;
  });
  $("connect-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    const server = form.server.value.trim();
    const slot = form.slot.value.trim();
    prefs.set("server", server);
    prefs.set("slot", slot);
    if (!send({ type: "connect", server, slot, password: form.password.value })) {
      $("dialog-status").textContent = NOT_RUNNING;  // the dialog stays open to say so
      return;
    }
    $("connect-dialog").close();
  });

  $("find-route").addEventListener("click", () => {
    if (page.selected !== null) send({ type: "route", location: page.selected });
  });
  $("clear-route").addEventListener("click", () => send({ type: "clear_route" }));

  document.addEventListener("keydown", (event) => {
    if (event.target instanceof HTMLInputElement || $("connect-dialog").open) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;  // the browser's own shortcuts
    if (event.key === "Escape") select(null);
    if (event.key === "f" || event.key === "F") fitView();
  });
}

async function start() {
  // The page has nothing to draw without the map. If the tracker went away while the page was
  // loading, the page waits for a tracker to come back rather than staying blank.
  let failed = false;
  while (!page.map) {
    try {
      const map = await (await fetch("map.json")).json();
      // The page itself came from the tracker that went away, so it loads again from this one.
      if (failed) {
        location.reload();
        return;
      }
      page.map = map;
    } catch {
      failed = true;
      $("status-text").textContent = NOT_RUNNING;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
  page.build = page.map.build;
  applyLook();
  buildMap();
  setUpPanAndZoom();
  setUpControls();
  fitWhenTheShownMapChanges();
  renderFocus();
  renderAreas();
  openSocket();
}

start();
