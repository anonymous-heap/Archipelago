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
  focus: CASTLE,       // CASTLE or an area id
  selected: null,      // the selected location's id
  view: { x: 0, y: 0, w: 64, h: 47 },
  somaArea: null,      // the area Soma was last seen in, to follow him when it changes
  askedToConnect: false,
  fittedTo: null,      // "whole" or "explored": the map the view was last fitted to
  reveal: prefs.get("reveal", false),
  revealItems: prefs.get("revealItems", false),  // list the progression items Soma doesn't have
  follow: prefs.get("follow", true),
  centerMarker: prefs.get("centerMarker", false),  // draw Soma's dot in the middle of his cell
  showChecked: prefs.get("showChecked", false),
};

const els = {
  cells: new Map(),     // "x,y" -> rect
  edges: [],            // {x, y, top, door, cells, hidden}: drawn by renderEdges
  edgePaths: new Map(), // look -> the path that draws every wall or door with that look
  pins: new Map(),      // location id -> circle
};

// The looks walls and doors can have, in drawing order, so doors sit on top of walls.
const EDGE_LOOKS = ["wall unfocused", "wall", "door unfocused", "door"];
// As on the game's map, a door fills its edge between the two corners, which stay in the wall
// colour. So it stops half a wall's thickness (the stroke width in style.css) short of each end.
const DOOR_INSET = 0.06;

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

function zoom(factor, clientX, clientY) {
  const box = $("map").getBoundingClientRect();
  const at = svgPoint(clientX ?? box.left + box.width / 2, clientY ?? box.top + box.height / 2);
  const view = page.view;
  page.view = { x: at.x - (at.x - view.x) * factor, y: at.y - (at.y - view.y) * factor,
                w: view.w * factor, h: view.h * factor };
  applyView();
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
    zoom(event.deltaY < 0 ? 1 / ZOOM_STEP : ZOOM_STEP, event.clientX, event.clientY);
  }, { passive: false });

  $("zoom-in").addEventListener("click", () => zoom(1 / ZOOM_STEP));
  $("zoom-out").addEventListener("click", () => zoom(ZOOM_STEP));
  $("zoom-fit").addEventListener("click", fitView);
}

// --- What is shown --------------------------------------------------------------------------

// The cells to draw, or null for all of them. Cells Soma hasn't explored, and that no Castle Map
// he holds shows, stay hidden unless the full map is revealed. The game never marks the Chaotic
// Realm as explored, so it is drawn whole, but only while it is in focus or Soma is in it.
function visibleCells() {
  const explored = page.snapshot.exploration;
  if (page.reveal || !explored) return null;
  const visible = new Set();
  for (const [x, y] of explored.visited) visible.add(key(x, y));
  for (const [x, y] of explored.remembered) visible.add(key(x, y));
  // The areas a Castle Map shows. Not visited yet, they are drawn dimmed, as the game does.
  for (const [x, y] of explored.revealed || []) visible.add(key(x, y));
  const soma = somaPoint();
  if (soma) visible.add(key(Math.floor(soma.x), Math.floor(soma.y)));
  if (page.focus === REALM || (soma && soma.room[0] === REALM)) {
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
      paths[`door${dim}`].push(edge.top ? `M${x + DOOR_INSET} ${y}h${1 - 2 * DOOR_INSET}`
                                        : `M${x} ${y + DOOR_INSET}v${1 - 2 * DOOR_INSET}`);
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

const SOMA_INSET = 0.14; // keeps the cell's outline inside it, clear of the walls on its edges

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
  svg("rect", { x: x + SOMA_INSET, y: y + SOMA_INSET, width: 1 - 2 * SOMA_INSET,
                height: 1 - 2 * SOMA_INSET, class: "soma-cell-outline" }, cellLayer);
  const marker = somaMarker(soma);
  svg("circle", { cx: marker.x, cy: marker.y, r: 0.22, class: "soma-dot" }, layer);
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
  if (!location) return;
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
  const visible = visibleCells();
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
  const visible = visibleCells();
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
  $("find-route").disabled = !location || !page.snapshot.tracking || status === undefined || status === "checked";
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
  const visible = visibleCells();
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

function send(message) {
  if (page.socket && page.socket.readyState === WebSocket.OPEN) page.socket.send(JSON.stringify(message));
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
  if (!snapshot.connected && snapshot.status === "Not connected" && !page.askedToConnect) {
    page.askedToConnect = true;
    openConnectDialog();
  }
}

function openSocket() {
  const socket = new WebSocket(`ws://${location.host}/ws`);
  page.socket = socket;
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "snapshot") onSnapshot(message.snapshot);
  });
  socket.addEventListener("close", () => {
    $("status-text").textContent = "The tracker isn't running";
    $("connection").classList.remove("tracking", "connecting");
    setTimeout(openSocket, 2000);
  });
}

function openConnectDialog() {
  const form = $("connect-form");
  form.server.value = page.snapshot.server || prefs.get("server", "");
  form.slot.value = page.snapshot.slot || prefs.get("slot", "");
  $("dialog-status").textContent = page.snapshot.status || "";
  $("disconnect").disabled = !page.snapshot.connected;
  $("connect-dialog").showModal();
}

function setUpControls() {
  const toggles = [["reveal", "reveal"], ["reveal-items", "revealItems"], ["follow", "follow"],
                   ["center-marker", "centerMarker"], ["show-checked", "showChecked"]];
  for (const [id, name] of toggles) {
    const box = $(id);
    box.checked = page[name];
    box.addEventListener("change", () => {
      page[name] = box.checked;
      prefs.set(name, box.checked);
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
    send({ type: "disconnect" });
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
    send({ type: "connect", server, slot, password: form.password.value });
    $("connect-dialog").close();
  });

  $("find-route").addEventListener("click", () => {
    if (page.selected !== null) send({ type: "route", location: page.selected });
  });
  $("clear-route").addEventListener("click", () => send({ type: "clear_route" }));

  document.addEventListener("keydown", (event) => {
    if (event.target instanceof HTMLInputElement || $("connect-dialog").open) return;
    if (event.key === "Escape") select(null);
    if (event.key === "f" || event.key === "F") fitView();
  });
}

async function start() {
  page.map = await (await fetch("map.json")).json();
  buildMap();
  setUpPanAndZoom();
  setUpControls();
  fitWhenTheShownMapChanges();
  renderFocus();
  renderAreas();
  openSocket();
}

start();
