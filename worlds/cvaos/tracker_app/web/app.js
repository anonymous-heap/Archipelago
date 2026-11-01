"use strict";
/*
 * The Aria of Sorrow tracker's page.
 *
 * The map comes once from /map.json, in map cells: cell (x, y) covers x to x + 1 across and
 * y to y + 1 down. Every room cell, wall, door and location pin is built once as an SVG element,
 * and each snapshot from the tracker (over /ws) then only changes their classes and visibility.
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
  build: null,         // the version of the tracker that served this page, from its map
  quit: false,         // whether the player stopped the tracker from this page
  retry: null,         // the timer that opens the socket again after it closes
  reloading: false,    // set once the page has started loading a newer tracker's page
  gone: false,         // whether the page has shown that its tracker is gone
  focus: CASTLE,       // CASTLE or an area id
  selected: null,      // the selected location's id
  view: { x: 0, y: 0, w: 64, h: 47 },
  somaArea: null,      // the area Soma was last seen in, to follow him when it changes
  somaCell: null,      // and the cell, to follow him with the view when he leaves it
  askedToConnect: false,
  fittedTo: null,      // "whole" or "explored": the map the view was last fitted to
  reveal: prefs.get("reveal", false),
  revealItems: prefs.get("revealItems", false),  // list the progression items Soma doesn't have
  follow: prefs.get("follow", true),
  centerMarker: prefs.get("centerMarker", false),  // draw Soma's dot in the middle of his cell
  showChecked: prefs.get("showChecked", false),
  gameLook: prefs.get("gameLook", false),  // draw the map in the game's colours and wall width
};

const els = {
  cells: new Map(),     // "x,y" -> rect
  edges: [],            // {x, y, top, door, cells, hidden}: drawn by renderEdges
  edgePaths: new Map(), // look -> the path that draws every wall or door with that look
  pins: new Map(),      // location id -> circle
};

// The looks walls and doors can have, in drawing order, so doors sit on top of walls.
const EDGE_LOOKS = ["wall unfocused", "wall", "door unfocused", "door"];
// How thick walls and doors are drawn, in cells. It depends on the look, so style.css sets it.
function wallWidth() {
  // The usual width if the stylesheet hasn't loaded, rather than drawing nothing.
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

// The areas Soma has been to, or null to treat them all as known. While unexplored cells are
// hidden, he has been to an area once he has visited or remembers a cell in it, is in it, or has
// checked a location in it. The last is the only sign of the Chaotic Realm, which the game never
// marks as explored.
function knownAreas() {
  const explored = page.snapshot.exploration;
  if (page.reveal || !explored) return null;
  const known = new Set();
  for (const [x, y] of [...explored.visited, ...explored.remembered]) {
    const rect = els.cells.get(key(x, y));
    if (rect) known.add(rect.dataset.area);
  }
  if (page.snapshot.position) known.add(page.snapshot.position[0][0]);
  for (const [id, status] of Object.entries(page.snapshot.locations || {})) {
    const location = locationsById.get(Number(id));
    if (status === "checked" && location) known.add(location.area);
  }
  return known;
}

// An area's name, or "???" for one Soma hasn't been to while unexplored areas are hidden.
function areaName(areaId, known = knownAreas()) {
  const area = areasById.get(areaId);
  if (!area) return "";
  return !known || known.has(areaId) ? area.name : UNKNOWN_AREA;
}

// An area Soma hasn't been to isn't listed while unexplored areas are hidden, so it can't stay in
// focus either. The focus moves to his area, or to the whole castle.
function keepFocusOnAKnownArea() {
  const known = knownAreas();
  if (!known || page.focus === CASTLE || known.has(page.focus)) return;
  const somaArea = page.snapshot.position ? page.snapshot.position[0][0] : null;
  setFocus(somaArea && known.has(somaArea) ? somaArea : CASTLE);
}

// "Terrace [503 CASTLE MAP 3]" is a place, then the room and the item the game put there.
function describeLocation(location) {
  const match = /^(.*?) \[(\S+) (.*)\]$/.exec(location.name);
  const area = areaName(location.area);
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
    const pin = svg("circle", { cx: location.x, cy: location.y, r: 0.22, class: "pin" }, pinLayer);
    pin.dataset.id = location.id;
    pin.setAttribute("aria-label", location.name);
    els.pins.set(location.id, pin);
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

// Fit the whole map, the Chaotic Realm's space included. While unexplored cells are hidden, fit the
// part of the map that is shown instead, at least MIN_VIEW across.
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

// Fit the view again only when what kind of map is shown changes: the whole map, or the explored
// part once the client has sent it. Exploring further, or picking an area, leaves the view alone.
function fitWhenTheShownMapChanges() {
  const shown = page.reveal ? "whole" : page.snapshot.exploration ? "explored" : "whole";
  if (shown !== page.fittedTo) {
    page.fittedTo = shown;
    fitView();
  }
}

// Focus an area: highlight it and list what is in logic there. The view stays where it is.
function setFocus(focus) {
  page.focus = focus;
  renderMap();  // the Chaotic Realm shows only while it is in focus
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
      // A click: on a pin selects it, anywhere else clears the selection.
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
    // A mouse wheel notch is about 100 pixels, 3 lines or 1 page, and a touchpad sends many small
    // steps, so the zoom follows how far the wheel turned, up to one notch's step for each event.
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

// The cells to draw, or null for all of them. Cells Soma hasn't explored, and that no Castle Map
// he holds shows, stay hidden unless the full map is revealed. The game never marks the Chaotic
// Realm as explored, so it is drawn whole, but only while it is in focus or Soma is in it. The
// panels count the Realm's checks once Soma has been there (countRealm), so that their numbers
// don't change with the focus.
function visibleCells({ countRealm = false } = {}) {
  const explored = page.snapshot.exploration;
  if (page.reveal || !explored) return null;
  const visible = new Set();
  for (const [x, y] of explored.visited) visible.add(key(x, y));
  for (const [x, y] of explored.remembered) visible.add(key(x, y));
  // The areas a Castle Map shows. Not visited yet, they are drawn dimmed, as the game does.
  for (const [x, y] of explored.revealed || []) visible.add(key(x, y));
  const soma = somaPoint();
  if (soma) visible.add(key(Math.floor(soma.x), Math.floor(soma.y)));
  const known = countRealm ? knownAreas() : null;
  if (page.focus === REALM || (soma && soma.room[0] === REALM) || (known && known.has(REALM))) {
    for (const cell of page.map.cells) if (cell.area === REALM) visible.add(key(cell.x, cell.y));
  }
  return visible;
}

function visitedCells() {
  const explored = page.snapshot.exploration;
  return explored ? new Set(explored.visited.map(([x, y]) => key(x, y))) : null;
}

// Whether a cell is drawn in full rather than greyed out. While unexplored cells are hidden, the
// cells Soma hasn't visited are greyed out, walls too, as on the game's map: those he only
// remembers, and those a Castle Map shows. The Chaotic Realm, which the game never marks, is not.
function drawnInFull(cellKey, visited) {
  const rect = els.cells.get(cellKey);
  return !visited || page.reveal || (rect && rect.dataset.area === REALM) || visited.has(cellKey);
}

// Whether a location's check shows, on the map, in the sidebar and in the counts: only in a shown
// room drawn in full. A greyed-out room is one Soma hasn't visited, so its checks stay hidden even
// when a Castle Map shows the room.
function checkShown(location, visible, visited) {
  const cell = key(Math.floor(location.x), Math.floor(location.y));
  return (!visible || visible.has(cell)) && drawnInFull(cell, visited);
}

function renderMap() {
  const visible = visibleCells();
  const explored = page.snapshot.exploration;
  const visited = visitedCells();
  for (const [cellKey, rect] of els.cells) {
    rect.style.display = !visible || visible.has(cellKey) ? "" : "none";
    rect.classList.toggle("remembered", !drawnInFull(cellKey, visited));
  }
  renderEdges(visible);
  $("fog-note").hidden = Boolean(explored) || page.reveal || !page.snapshot.tracking;
  renderPins(visible);
}

// Draw the walls and doors that touch a shown cell, one path for each look. A path's stroke is
// filled as one shape, so where two segments overlap at a cell corner nothing is drawn twice.
function renderEdges(visible = visibleCells()) {
  const explored = page.snapshot.exploration;
  const visited = visitedCells();
  const passed = new Set(explored ? explored.hidden_doors : []);
  const paths = Object.fromEntries(EDGE_LOOKS.map((look) => [look, []]));
  // As on the game's map, a door fills its edge between the two corners, which stay in the wall
  // colour. So it stops half a wall's width short of each end.
  const inset = wallWidth() / 2;
  for (const edge of els.edges) {
    if (visible && !edge.cells.some((cell) => visible.has(cell))) continue;
    // A wall is lit where it borders a room of the highlighted area that is drawn in full, so the
    // walls of greyed-out rooms stay grey with them.
    const lit = edge.cells.some((cell) => els.cells.has(cell) && inFocus(els.cells.get(cell).dataset.area)
                                          && drawnInFull(cell, visited));
    const dim = lit ? "" : " unfocused";
    const { x, y } = edge;
    paths[`wall${dim}`].push(edge.top ? `M${x} ${y}h1` : `M${x} ${y}v1`);
    // A door is its edge in the door colour, all but the wall at each corner, as on the game's own
    // map. A hidden door looks like a wall until Soma has gone through it.
    const open = edge.hidden === null || page.reveal || !explored || passed.has(edge.hidden);
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
  for (const [id, pin] of els.pins) {
    const status = statuses[id];
    const location = locationsById.get(id);
    const shown = status !== undefined && checkShown(location, visible, visited)
                  && (status !== "checked" || page.showChecked);
    pin.style.display = shown ? "" : "none";
    const focus = inFocus(location.area) ? "" : " unfocused";
    pin.setAttribute("class", `pin ${status || ""}${hinted.has(id) ? " hinted" : ""}${focus}`);
  }
  renderSelectionRing();
}

// Dim everything outside the focused area: its rooms here, and its walls, doors and pins in
// renderEdges and renderPins.
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

// Where Soma's dot goes: where he stands, or the middle of his cell, as the in-game map marks him.
function somaMarker(soma) {
  if (!page.centerMarker) return { x: soma.x, y: soma.y };
  return { x: Math.floor(soma.x) + 0.5, y: Math.floor(soma.y) + 0.5 };
}

// How far the outline of Soma's cell sits inside it, past the walls on its edges and then its own
// stroke's half width and a small gap.
const SOMA_GAP = 0.08;

function renderSoma() {
  // The cell he is in sits under the walls and doors, so they stay visible. The dot marking where
  // he stands is drawn over everything.
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
  // Following Soma moves the view only once he has gone into another cell outside it, so a view
  // the player has moved stays put while he stands still, and picking an area never moves it.
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
    points.unshift([marker.x, marker.y]);  // the line starts at his dot, wherever that is drawn
  }
  const target = locationsById.get(route.target);
  if (target) points.push([target.x, target.y]);
  const coordinates = points.map(([x, y]) => `${x},${y}`).join(" ");
  svg("polyline", { points: coordinates, class: "route-casing" }, layer);
  svg("polyline", { points: coordinates, class: "route-line" }, layer);
  if (target) svg("circle", { cx: target.x, cy: target.y, r: 0.45, class: "route-end" }, layer);
}

function renderSelectionRing() {
  for (const ring of document.querySelectorAll(".selection-ring")) ring.remove();
  const location = locationsById.get(page.selected);
  const pin = els.pins.get(page.selected);
  if (!location || !pin || pin.style.display === "none") return;  // no ring around a hidden pin
  svg("circle", { cx: location.x, cy: location.y, r: 0.4, class: "selection-ring" }, $("layer-pins"));
}

// --- The panels -----------------------------------------------------------------------------

// Count an area's locations in logic and with tricks: the ones whose checks show (checkShown). So
// while unexplored cells are hidden, the numbers don't give away what lies in rooms Soma hasn't
// visited.
function counts(area, visible, visited) {
  const statuses = page.snapshot.locations || {};
  let inLogic = 0;
  let tricks = 0;
  for (const [id, status] of Object.entries(statuses)) {
    const location = locationsById.get(Number(id));
    if (!location) continue;
    const inArea = area === CASTLE ? location.area !== REALM : location.area === area;
    if (!inArea || !checkShown(location, visible, visited)) continue;
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
    // An area Soma hasn't been to isn't listed while unexplored areas are hidden.
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
  // The items Soma doesn't have yet are the seed's own list of what is left to find, so they show
  // only with "Reveal useful items" on.
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
  connection.classList.toggle("connecting", !snapshot.tracking && /^(Connecting|Rebuilding)/.test(snapshot.status || ""));
  $("status-text").textContent = snapshot.status || "Not connected";
  $("stats").hidden = !snapshot.tracking;
  if (!snapshot.tracking) return;
  const statuses = Object.values(snapshot.locations || {});
  const checked = statuses.filter((status) => status === "checked").length;
  $("stat-checked").textContent = `${checked}/${statuses.length}`;
  // The checked count is the slot's progress, so it counts every location. In logic and with
  // tricks count only the checks the page lists, like the area counts.
  const visible = visibleCells({ countRealm: true });
  const visited = visitedCells();
  let inLogic = 0;
  let tricks = 0;
  for (const [id, status] of Object.entries(snapshot.locations || {})) {
    const location = locationsById.get(Number(id));
    if (!location || !checkShown(location, visible, visited)) continue;
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
    const chip = $("sel-status");
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
    return inFocus(location.area) && checkShown(location, visible, visited);
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
    room.textContent = location.room;
    button.append(dot, name, room);
    button.addEventListener("click", () => {
      select(location.id);
      centerOn(location.x, location.y);
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

// Send a request to the tracker, and say whether it went. It can't while the tracker is away.
function send(message) {
  if (!page.socket || page.socket.readyState !== WebSocket.OPEN) return false;
  page.socket.send(JSON.stringify(message));
  return true;
}

const NOT_RUNNING = "Tracker not running. Type /map in your client.";

// With its tracker gone, the page keeps the last map it had, but drops what needs the tracker:
// the counts, the route and Find Route. The pill and the connection dialog say why.
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
    // A newer tracker that took over this address serves a newer page, so load that instead,
    // once, ignoring the snapshots that come before the new page does.
    if (page.build && message.build !== page.build) {
      page.reloading = true;
      socket.close();
      location.reload();
      return;
    }
    // While the tracker reconnects or rebuilds the seed, its snapshots carry no explored map or
    // position. The page keeps the last ones it had for the same slot in the same room, rather
    // than show the whole castle, with every area's name and secret door, until the next arrive.
    // Once the tracker is tracking, a snapshot without them is a seed the client hasn't sent them
    // for yet, so nothing is kept.
    const snapshot = message.snapshot;
    const last = page.snapshot;
    if (!snapshot.exploration && !snapshot.tracking && last.exploration
        && snapshot.slot === last.slot && snapshot.server === last.server) {
      snapshot.exploration = page.snapshot.exploration;
      snapshot.position = snapshot.position ?? page.snapshot.position;
    }
    page.gone = false;
    onSnapshot(snapshot);
    // The dialog opens by itself only for a tracker that has no room when the page first hears
    // from it, not when a connection later ends, as before a reconnect or after Disconnect.
    if (!page.askedToConnect) {
      page.askedToConnect = true;
      if (!snapshot.connected && !snapshot.following && snapshot.status === "Not connected") {
        openConnectDialog();
      }
    }
  });
  socket.addEventListener("close", () => {
    // A socket already replaced by a newer one, or closed for a reload, has nothing to report.
    if (socket !== page.socket || page.reloading) return;
    if (!page.gone) {
      page.gone = true;  // drawn once, rather than again on every retry while it is away
      showTrackerGone();
    }
    page.retry = setTimeout(openSocket, 2000);
  });
}

// A browser can hold back a background tab's timers for minutes, so a tab brought back into view
// tries again at once rather than waiting for its next retry.
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

// The dialog's status line and buttons follow the tracker, while the fields are left to the player.
function renderDialog() {
  const live = Boolean(page.socket && page.socket.readyState === WebSocket.OPEN);
  $("dialog-status").textContent = page.snapshot.status || "";
  // Disconnect also stops a tracker that is reconnecting, or retrying a room that has closed.
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
      applyLook();  // first, since the walls and Soma's cell are drawn to fit it
      renderMap();
      renderSoma();
      fitWhenTheShownMapChanges();  // "Reveal full map" switches between the two fits
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
  // loading, it waits for one to come back rather than staying blank.
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
