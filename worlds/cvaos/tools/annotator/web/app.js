"use strict";
// The annotator's page. The server sends snapshots of the enemy tab; the page sends actions back.
// A placement is one enemy in one room (by enemy number), a row gives one set of its doors a
// requirement, and a pair is one door and one placement.

const SVG = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);
const page = {
  map: null, snap: null, socket: null,
  room: null, enemy: null, rowKey: null,   // rowKey: the selected row's doors cell
  draft: null,                             // the editor's copy of the selected row
  pick: null, picks: new Set(),            // pick mode: "enemies" or "entrances"
  pending: null,                           // the last action, kept to resend after a confirm
  conflict: null,                          // enemies whose rows changed in the sheet under a save
  lastSnapshot: 0,
  testDoor: null, loadout: new Set(),      // the Test panel's door and the souls to start with
};
const STALE_AFTER = 30000;   // refresh on returning to the tab after this many milliseconds

// --- connection --------------------------------------------------------------------------------

function connect() {
  const socket = new WebSocket(`ws://${location.host}/ws`);
  page.socket = socket;
  socket.onmessage = (event) => receive(JSON.parse(event.data));
  socket.onclose = () => setTimeout(connect, 1000);
}

function send(message) {
  page.socket.send(JSON.stringify(message));
}

function act(message) {
  page.pending = { ...message, version: page.snap.version };
  send(page.pending);
}

function receive(message) {
  if (message.type === "snapshot") {
    page.snap = message;
    page.lastSnapshot = Date.now();
    keepSelection();
    render();
  } else if (message.type === "run") {
    if (page.snap) { page.snap.run = message.run; renderRun(); }
  } else if (message.type === "result") {
    toast(message.message, message.ok);
    if (message.action === "launch_emulator" || message.action === "connect_emulator") renderTest();
    if (message.conflict) {
      page.conflict = message.conflict;
      render();
      return;
    }
    if (message.ok && message.action === "new_row") {
      const blank = rowsOf(page.enemy).find((row) => !row.doors_cell && !row.ticks.length
                                                     && !row.combos.length);
      if (blank) selectRow(blank.doors_cell);
    }
    if (message.ok) page.conflict = null;
    if (!message.ok && /confirm to overwrite/.test(message.message) && page.pending
        && confirm(`${message.message}.\n\nOverwrite?`)) {
      act({ ...page.pending, overwrite: true });
      return;
    }
    if (message.ok && page.pick) { endPick(); render(); }
  }
}

function toast(text, ok = true) {
  const box = $("toast");
  box.textContent = text;
  box.className = ok ? "" : "bad";
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, ok ? 2500 : 6000);
}

// --- data helpers ------------------------------------------------------------------------------

function rowsOf(enemy) {
  return (page.snap.rows || []).filter((row) => row.enemy === enemy);
}

function selectedRow() {
  return rowsOf(page.enemy).find((row) => row.doors_cell === page.rowKey) || null;
}

function placementsIn(room) {
  const seen = new Map();
  for (const row of page.snap.rows || []) {
    if (row.room === room && !seen.has(row.enemy)) seen.set(row.enemy, row);
  }
  return [...seen.values()];
}

// done / total pairs for a placement, counting annotated pairs and pairs implied free
function placementProgress(enemy) {
  const pairs = (page.snap.pairs || {})[enemy] || [];
  const done = pairs.filter((p) => p.state === "annotated"
                                   || (p.state === "implied" && p.masks.includes(0))).length;
  return [done, pairs.length];
}

function colour(done, total) {
  if (!total) return "#bdbdb6";
  if (done === total) return "var(--done)";
  return done ? "var(--warn)" : "var(--open)";
}

function maskText(mask) {
  if (!mask) return "free";
  const names = [];
  for (const [bit, name] of Object.entries(page.snap.flags || {})) {
    if (mask & Number(bit)) names.push(name);
  }
  return names.join("+");
}

function labelToken(label) {
  const match = label.match(/\(([^)]+)\)/);
  return match ? match[1] : (label.trim() === "No ability needed" ? "free" : `"${label.trim()}"`);
}

function requirementText(row) {
  const ways = row.ticks.map((t) => t.replace(/\.$/, ""));
  for (const combo of row.combos) {
    ways.push(combo.split(",").map(labelToken).join("+"));
  }
  return ways.length ? ways.join(" | ") : "—";
}

// --- selection ---------------------------------------------------------------------------------

function selectRoom(room) {
  page.room = room;
  page.enemy = null;
  page.rowKey = null;
  page.draft = null;
  endPick();
  render();
}

function selectPlacement(room, enemy) {
  page.room = room;
  page.enemy = enemy;
  const rows = rowsOf(enemy);
  const blank = rows.find((row) => !row.doors_cell && !row.ticks.length && !row.combos.length);
  selectRow((blank || rows[0]).doors_cell);
}

function selectRow(doorsCell) {
  page.rowKey = doorsCell;
  page.conflict = null;
  const row = selectedRow();
  page.draft = row && {
    doors: row.doors_cell, ticks: new Set(row.ticks), status: row.status, notes: row.notes,
    combos: row.combos.map((c) => c.split(",").map((l) => l.trim()).filter(Boolean)),
  };
  endPick();
  render();
}

function keepSelection() {
  if (page.enemy === null) return;
  const rows = rowsOf(page.enemy);
  if (!rows.length) { page.enemy = null; return; }
  if (!rows.some((row) => row.doors_cell === page.rowKey)) {
    const blank = rows.find((row) => !row.doors_cell);
    page.rowKey = (blank || rows[0]).doors_cell;
  }
  if (!page.draft || page.draft.doors !== page.rowKey) {
    const row = selectedRow();
    page.draft = row && {
      doors: row.doors_cell, ticks: new Set(row.ticks), status: row.status, notes: row.notes,
      combos: row.combos.map((c) => c.split(",").map((l) => l.trim()).filter(Boolean)),
    };
  }
}

function startPick(mode) {
  page.pick = mode;
  page.picks = new Set();
  render();
}

function endPick() {
  page.pick = null;
  page.picks = new Set();
}

function nextItem() {
  const items = visibleItems();
  if (!items.length) return;
  const index = items.findIndex((item) => item.enemy === page.enemy);
  const item = items[(index + 1) % items.length];
  selectPlacement(item.room, item.enemy);
}

// --- rendering ---------------------------------------------------------------------------------

function render() {
  if (!page.snap) return;
  renderHeader();
  renderWorklist();
  renderMap();
  renderPanel();
}

function renderHeader() {
  const { state } = page.snap;
  $("status").textContent = state.message || (state.access ? "Connected to the sheet." : "");
  $("sign-in").hidden = state.signed_in;
  $("grant").hidden = !state.signed_in || state.access;
  if (document.activeElement !== $("annotator")) {
    $("annotator").value = page.snap.settings.annotator || "";
  }
  const progress = Object.values(page.snap.progress || {});
  const done = progress.reduce((sum, [d]) => sum + d, 0);
  const total = progress.reduce((sum, [, t]) => sum + t, 0);
  $("progress").textContent = total ? `${done} / ${total} pairs finished` : "";
}

// The items the worklist shows: all of them, or each type's first (easiest) placement, narrowed
// by the filter box's text, which matches an enemy's name or room.
function visibleItems() {
  const text = $("filter").value.trim().toLowerCase();
  const onePerType = $("one-per-type").checked;
  const seen = new Set();
  return (page.snap.worklist || []).filter((item) => {
    if (text && !item.name.toLowerCase().includes(text) && !item.room.toLowerCase().includes(text)) {
      return false;
    }
    if (onePerType) {
      if (seen.has(item.name)) return false;
      seen.add(item.name);
    }
    return true;
  });
}

function renderWorklist() {
  const items = visibleItems();
  const total = (page.snap.worklist || []).length;
  document.querySelector("#worklist-pane h2").firstChild.textContent =
    items.length === total ? `Worklist: ${total} enemies ` : `Worklist: ${items.length} of ${total} `;
  const list = $("worklist");
  list.replaceChildren();
  let group = null;
  const names = { "unrouted type": "Types with no route yet", "logic type": "Soul sources",
                  other: "Everything else" };
  for (const item of items) {
    if (item.group !== group) {
      group = item.group;
      const header = document.createElement("li");
      header.className = "group";
      header.textContent = names[group];
      list.append(header);
    }
    const li = document.createElement("li");
    li.textContent = `${item.name} #${item.enemy} (${item.room}) `;
    element("span", `${item.open_doors.length} door${item.open_doors.length === 1 ? "" : "s"}`,
            li, "doors");
    if (item.enemy === page.enemy) li.className = "current";
    li.onclick = () => selectPlacement(item.room, item.enemy);
    list.append(li);
  }
}

function svg(tag, attributes, parent) {
  const element = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  parent.append(element);
  return element;
}

function renderMap() {
  const map = $("map");
  map.replaceChildren();
  $("castle").hidden = !page.room;
  $("background-toggle").hidden = !page.room;
  $("room-title").textContent = page.room ? `Room ${page.room}` : "Castle map: click a room";
  $("pick-hint").textContent = page.pick === "enemies"
    ? "Click the enemies that share this row's requirement, then Apply."
    : page.pick === "entrances" ? "Click the doors that share this row's requirement, then Apply."
    : "";
  if (!page.map) return;
  if (!page.room) {
    map.setAttribute("viewBox", `0 0 ${page.map.columns} ${page.map.rows}`);
    for (const cell of page.map.cells) {
      const [done, total] = (page.snap.progress || {})[cell.room] || [0, 0];
      const rect = svg("rect", { x: cell.x, y: cell.y, width: 1, height: 1, class: "cell",
                                 fill: colour(done, total) }, map);
      const title = svg("title", {}, rect);
      title.textContent = `${cell.room}: ${done}/${total}`;
      rect.onclick = () => selectRoom(cell.room);
    }
    return;
  }
  const geometry = page.map.geometry[page.room];
  if (!geometry) return;
  const xs = geometry.cells.map((c) => c[0]);
  const ys = geometry.cells.map((c) => c[1]);
  // At least five cells across and tall, so that a one-screen room isn't drawn huge.
  const width = Math.max(Math.max(...xs) - Math.min(...xs) + 2.4, 5);
  const height = Math.max(Math.max(...ys) - Math.min(...ys) + 2.4, 5);
  const x0 = (Math.min(...xs) + Math.max(...xs) + 1) / 2 - width / 2;
  const y0 = (Math.min(...ys) + Math.max(...ys) + 1) / 2 - height / 2;
  map.setAttribute("viewBox", `${x0} ${y0} ${width} ${height}`);
  // The room's tiles, drawn from the ROM by the server, sit under the cell outlines. A cell is
  // one 256-pixel screen, so the image spans the room's box of cells from its origin.
  const showBackground = $("background").checked;
  if (showBackground) {
    // Dark cells behind the image, as in the game, where a room's layers leave gaps.
    for (const [x, y] of geometry.cells) {
      svg("rect", { x, y, width: 1, height: 1, class: "room-shade" }, map);
    }
    const [bx, by] = geometry.origin;
    svg("image", { href: `/room/${page.room}.png`, x: bx, y: by,
                   width: Math.max(...xs) - bx + 1, height: Math.max(...ys) - by + 1,
                   preserveAspectRatio: "none", class: "background" }, map);
  }
  for (const [x, y] of geometry.cells) {
    svg("rect", { x, y, width: 1, height: 1,
                  class: showBackground ? "room-cell outline" : "room-cell" }, map);
  }
  // With a placement selected, each door shows its state for that placement.
  const pairState = Object.fromEntries(((page.snap.pairs || {})[page.enemy] || [])
    .map((pair) => [pair.door, pair]));
  for (const door of geometry.doors) {
    const cx = (door.inside[0] + door.outside[0]) / 2 + 0.5;
    const cy = (door.inside[1] + door.outside[1]) / 2 + 0.5;
    const picked = page.pick === "entrances" && page.picks.has(door.label);
    const pair = pairState[door.node];
    const state = !pair ? "" : pair.state === "annotated" ? " annotated"
      : pair.state === "implied" && pair.masks.includes(0) ? " annotated"
      : pair.state === "implied" ? " implied" : " open";
    const rect = svg("rect", { x: cx - 0.06, y: cy - 0.06, width: 0.12, height: 0.12,
                               class: `door${state}${picked ? " picked" : ""}` }, map);
    const doorTitle = svg("title", {}, rect);
    doorTitle.textContent = pair ? `${door.label}: ${pair.state}`
      + (pair.masks.length ? ` (${pair.masks.map(maskText).join(" | ")})` : "") : door.label;
    rect.onclick = () => clickDoor(door);
    const label = svg("text", { x: cx + 0.08, y: cy - 0.08, class: "door-label" }, map);
    label.textContent = door.label;
  }
  const [ox, oy] = geometry.origin;
  const placed = [];   // placements whose markers would overlap are spread out sideways
  for (const row of placementsIn(page.room)) {
    const [done, total] = placementProgress(row.enemy);
    const cy = oy + row.y / 256;
    let cx = ox + row.x / 256;
    while (placed.some(([px, py]) => Math.hypot(px - cx, py - cy) < 0.12)) cx += 0.16;
    placed.push([cx, cy]);
    const classes = ["enemy"];
    if (row.enemy === page.enemy) classes.push("selected");
    if (page.pick === "enemies" && page.picks.has(row.enemy)) classes.push("picked");
    const dot = svg("circle", { cx, cy, r: 0.05, fill: colour(done, total),
                                class: classes.join(" ") }, map);
    dot.onclick = () => clickEnemy(row);
    const title = svg("title", {}, dot);
    title.textContent = `${row.name} #${row.enemy}: ${done}/${total} doors done`;
    const label = svg("text", { x: cx + 0.07, y: cy + 0.03, class: "enemy-label" }, map);
    label.textContent = `${row.enemy}`;
  }
}

function clickEnemy(row) {
  if (page.pick === "enemies") {
    if (row.enemy === page.enemy) return;
    page.picks.has(row.enemy) ? page.picks.delete(row.enemy) : page.picks.add(row.enemy);
    render();
  } else {
    selectPlacement(page.room, row.enemy);
  }
}

function clickDoor(door) {
  if (page.pick === "entrances") {
    page.picks.has(door.label) ? page.picks.delete(door.label) : page.picks.add(door.label);
    render();
  } else if (page.draft) {
    toggleDoor(door.label);
  }
}

function toggleDoor(label) {
  const entries = page.draft.doors.split(",").map((d) => d.trim()).filter(Boolean)
    .filter((d) => d !== "Any");
  const index = entries.indexOf(label);
  if (label === "Any") {
    page.draft.doors = page.draft.doors.trim() === "Any" ? "" : "Any";
  } else {
    index >= 0 ? entries.splice(index, 1) : entries.push(label);
    page.draft.doors = entries.join(",");
  }
  render();
}

function element(tag, text, parent, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  if (parent) parent.append(node);
  return node;
}

function renderPanel() {
  const panel = $("panel");
  panel.replaceChildren();
  if (!page.snap.state.access) {
    element("p", "Sign in and pick the sheet to start.", panel, "muted");
    return;
  }
  if (!page.room) {
    element("p", "Pick a room on the map, or a placement from the worklist. Rooms are grey "
            + "before any annotation, yellow partway, and green when every pair is done.", panel,
            "muted");
    return;
  }
  const [done, total] = page.snap.progress[page.room] || [0, 0];
  element("h2", `Room ${page.room}: ${done}/${total} pairs done`, panel);
  for (const row of placementsIn(page.room)) {
    const [d, t] = placementProgress(row.enemy);
    const div = element("div", null, panel,
                        `placement${row.enemy === page.enemy ? " selected" : ""}`);
    const dot = element("span", null, div, "dot");
    dot.style.background = colour(d, t);
    element("span", `#${row.enemy} ${row.name}${row.specifier} (${d}/${t})`, div);
    div.onclick = () => selectPlacement(page.room, row.enemy);
  }
  if (page.enemy === null) return;
  renderPairs(panel);
  element("div", null, panel, "test").id = "test-box";
  renderTest();
  renderRows(panel);
  if (page.draft) renderEditor(panel);
}

function renderPairs(panel) {
  element("h3", "Doors", panel);
  const table = element("table", null, panel);
  const labels = Object.fromEntries((page.map.geometry[page.room]?.doors || [])
    .map((d) => [d.node, d.label]));
  for (const pair of (page.snap.pairs[page.enemy] || [])) {
    const tr = element("tr", null, table);
    element("td", labels[pair.door] || pair.door, tr);
    element("td", pair.state, tr, pair.state === "open" ? "muted" : "");
    element("td", pair.masks.map(maskText).join(" | "), tr);
  }
}

function renderRows(panel) {
  element("h3", "Rows", panel);
  const table = element("table", null, panel);
  const header = element("tr", null, table);
  for (const name of ["Doors", "Requirement", "Status", "By"]) element("th", name, header);
  for (const row of rowsOf(page.enemy)) {
    const tr = element("tr", null, table,
                       `row${row.doors_cell === page.rowKey ? " selected" : ""}`);
    element("td", row.doors_cell || "(none)", tr);
    element("td", requirementText(row), tr);
    element("td", row.status, tr);
    element("td", row.annotated_by, tr);
    tr.onclick = () => selectRow(row.doors_cell);
    if (row.shared_enemies || row.shared_entrances) {
      const shared = element("tr", null, table);
      const td = element("td", `shared with ${[row.shared_enemies, row.shared_entrances]
        .filter(Boolean).join("; ")}`, shared, "muted");
      td.colSpan = 4;
    }
  }
  const buttons = element("div", null, panel, "buttons");
  const add = element("button", "New row", buttons);
  add.title = "Add a blank row for another set of doors";
  add.onclick = () => act({ type: "new_row", enemy: page.enemy });
}

// --- testing in EmuHawk ------------------------------------------------------------------------
// The Test box is drawn on its own, because a run updates it twice a second and redrawing the
// whole panel that often would take the focus from the editor's fields.

function doorLabel(node) {
  const door = (page.map.geometry[page.room]?.doors || []).find((d) => d.node === node);
  return door ? door.label : node;
}

function tokensText(tokens) {
  return tokens.length ? tokens.join("+") : "no souls";
}

function renderTest() {
  const box = $("test-box");
  if (!box || !page.snap) return;
  box.replaceChildren();
  element("h3", "Test in EmuHawk", box);
  const emu = page.snap.emulator || {};
  if (!emu.connected) {
    renderLaunch(box, emu);
    return;
  }
  const doors = page.map.geometry[page.room]?.doors || [];
  if (!doors.length) {
    element("div", "This room has no doors to start from.", box, "muted");
    return;
  }
  if (!doors.some((d) => d.node === page.testDoor)) {
    const open = ((page.snap.pairs || {})[page.enemy] || []).find((p) => p.state === "open");
    page.testDoor = open ? open.door : doors[0].node;
  }

  const start = element("div", null, box);
  element("span", "From ", start);
  const select = element("select", null, start);
  for (const door of doors) element("option", door.label, select).value = door.node;
  select.value = page.testDoor;
  select.onchange = () => { page.testDoor = select.value; renderTest(); };
  element("span", " with:", start);
  const souls = element("div", null, box, "souls");
  for (const token of page.snap.tokens || []) {
    const on = page.loadout.has(token);
    const chip = element("button", token, souls, `soul${on ? " on" : ""}`);
    chip.onclick = () => { on ? page.loadout.delete(token) : page.loadout.add(token); renderTest(); };
  }
  const buttons = element("div", null, box, "buttons");
  const test = element("button", "Test", buttons);
  test.title = "Put Soma at this door with these souls on, and watch for the enemy";
  test.onclick = () => send({ type: "test", enemy: page.enemy, door: page.testDoor,
                              loadout: [...page.loadout] });
  const clear = element("button", "No souls", buttons);
  clear.onclick = () => { page.loadout.clear(); renderTest(); };

  element("div", null, box).id = "run-line";
  renderRun();
  renderResults(box);
}

function renderLaunch(box, emu) {
  if (emu.message) element("div", emu.message, box, "muted");
  const settings = page.snap.settings;
  for (const [key, label] of [["emuhawk", "EmuHawk.exe"], ["rom_path", "US ROM"]]) {
    const line = element("label", null, box, "path");
    line.append(`${label} `);
    const input = element("input", null, line);
    input.value = settings[key] || "";
    input.onchange = () => send({ type: "settings", [key]: input.value });
  }
  const buttons = element("div", null, box, "buttons");
  const launch = element("button", "Launch EmuHawk", buttons);
  launch.title = "Start EmuHawk on a test copy of the ROM, with the annotator's script running";
  launch.onclick = () => {
    launch.disabled = true;
    launch.textContent = "Launching…";
    send({ type: "launch_emulator" });
  };
  const connect = element("button", "Connect", buttons);
  connect.title = "Connect to an EmuHawk that is already running the annotator's script";
  connect.onclick = () => send({ type: "connect_emulator" });
}

// The run line is redrawn on its own while a run goes on, so the door list and souls above it
// stay usable.
function renderRun() {
  const holder = $("run-line");
  const run = page.snap.run;
  if (!holder) return;
  holder.replaceChildren();
  if (!run || (run.enemy !== page.enemy && run.outcome)) return;
  const line = element("div", null, holder, `run${run.outcome ? " " + run.outcome : ""}`);
  if (run.enemy !== page.enemy) {
    element("span", `Testing #${run.enemy} ${run.name} in room ${run.room}. `, line);
    const show = element("button", "Show", line);
    show.onclick = () => selectPlacement(run.room, run.enemy);
    return;
  }
  const from = doorLabel(run.door);
  if (!run.outcome) {
    const distance = run.distance === null ? "" : `, ${run.distance} px away`;
    element("div", `Running from ${from}: ${tokensText(run.used)} used so far${distance}.`, line);
    element("div", "In EmuHawk, L+R+Select retries and L+R+Start gives up.", line, "muted");
    if (!run.defeat_counts) {
      element("div", "Another placement of this type is in the room, so only reaching this one "
              + "counts, not a defeat.", line, "muted");
    }
  } else {
    const verb = { defeated: "Defeated", reached: "Reached", failed: "Couldn't reach" }[run.outcome];
    element("div", `${verb} from ${from} with ${tokensText(run.used)}.`, line);
  }
  const buttons = element("div", null, line, "buttons");
  const retry = element("button", "Retry", buttons);
  retry.title = "Back to the door with the same souls";
  retry.onclick = () => send({ type: "retry" });
  if (!run.outcome) {
    const reached = element("button", "Reached it", buttons);
    reached.title = "Record a success the watcher missed";
    reached.onclick = () => send({ type: "end_run", outcome: "reached" });
    const failed = element("button", "Couldn't reach", buttons);
    failed.onclick = () => send({ type: "end_run", outcome: "failed" });
  }
}

function renderResults(box) {
  const summary = (page.snap.attempts || {})[`${page.testDoor}|${page.enemy}`];
  if (!summary) {
    element("div", `No runs from ${doorLabel(page.testDoor)} yet.`, box, "muted");
    return;
  }
  const results = element("div", null, box, "results");
  const ways = summary.ways.map(tokensText).join(" | ") || "none yet";
  element("div", `From ${doorLabel(page.testDoor)}: ${summary.successes} reached, `
          + `${summary.failures} failed. Ways through: ${ways}.`, results);
  if (summary.contradictions.length) {
    element("div", `These ways also failed in some run, so test them again: `
            + `${summary.contradictions.map(tokensText).join(" | ")}.`, results, "warning");
  }
  if (summary.next.length) {
    const next = element("div", "Not settled yet: ", results);
    for (const tokens of summary.next) {
      const chip = element("button", tokensText(tokens), next, "suggestion");
      chip.title = "Use these souls for the next test";
      chip.onclick = () => { page.loadout = new Set(tokens); renderTest(); };
    }
  }
  if (summary.ways.length) {
    const use = element("button", "Use these ways in the row", results);
    use.title = "Fill the row editor with these ways and mark it Tested";
    use.onclick = () => useResults(summary.ways, page.testDoor);
  }
}

function abilityColumn(token) {
  return page.snap.abilities.find((name) => name.replace(/\.$/, "") === token) || null;
}

function comboLabel(token) {
  return page.snap.combo_options.find((label) => labelToken(label) === token) || null;
}

// Each way becomes a tick when one column names it, or a combo of dropdown labels otherwise.
function useResults(ways, door) {
  const draft = page.draft;
  if (!draft) {
    toast("Select a row, or add one, first.", false);
    return;
  }
  const ticks = new Set();
  const combos = [];
  for (const way of ways) {
    const column = way.length === 0 ? abilityColumn("None") : (way.length === 1 ? abilityColumn(way[0]) : null);
    if (column) {
      ticks.add(column);
      continue;
    }
    const labels = way.map(comboLabel);
    const missing = way.filter((token, index) => !labels[index]);
    if (missing.length) {
      toast(`The sheet has no column or dropdown label for ${missing.join(", ")}.`, false);
      return;
    }
    combos.push(labels);
  }
  const label = doorLabel(door);
  const entries = draft.doors.split(",").map((d) => d.trim()).filter(Boolean);
  if (!entries.includes(label) && !entries.includes("Any")) {
    draft.doors = [...entries, label].join(",");
  }
  draft.ticks = ticks;
  draft.combos = combos;
  draft.status = "Tested";
  render();
  toast(`Filled the row from the runs at ${label}. Check it, then save.`);
}

// A requirement with no Status is held back by the importer, so the first tick or label on a
// row that has none sets it to Judged (decided from the map). Tests in the emulator use Tested.
function defaultStatus() {
  if (!page.draft.status) {
    page.draft.status = "Judged";
    render();
  }
}

function renderConflict(box) {
  const row = selectedRow();
  const banner = element("div", null, box, "conflict");
  element("div", "This row changed in the sheet while you were editing. The sheet now has: "
          + (row ? `${requirementText(row)} (${row.status || "no status"})` : "no such row")
          + ".", banner);
  const keep = element("button", "Keep mine", banner);
  keep.title = "Save your version over the sheet's";
  keep.onclick = () => { page.conflict = null; render(); };
  const theirs = element("button", "Use the sheet's", banner);
  theirs.onclick = () => { page.conflict = null; page.draft = null; selectRow(page.rowKey); };
}

function renderEditor(panel) {
  const draft = page.draft;
  const box = element("div", null, panel, "editor");
  if (page.conflict && page.conflict.includes(page.enemy)) renderConflict(box);
  element("strong", `Row: ${page.rowKey || "(no doors yet)"}`, box);
  if (!page.snap.settings.annotator) {
    element("div", "Set your name in \"Annotated by\" above, so rows record who annotated them.",
            box, "warning");
  }

  const doors = element("div", null, box);
  element("span", "Doors: ", doors);
  const doorInput = element("input", null, doors);
  doorInput.value = draft.doors;
  doorInput.size = 18;
  doorInput.onchange = () => { draft.doors = doorInput.value.trim(); };
  const doorButtons = element("div", null, box);
  for (const label of ["Any", ...(page.map.geometry[page.room]?.doors || []).map((d) => d.label)]) {
    const entries = draft.doors.split(",").map((d) => d.trim());
    const button = element("button", entries.includes(label) ? `✓ ${label}` : label, doorButtons);
    button.onclick = () => toggleDoor(label);
  }

  const ticks = element("div", null, box);
  element("div", "Ways through (each tick is one way on its own):", ticks, "muted");
  for (const name of page.snap.abilities) {
    const label = element("label", null, ticks);
    const box2 = element("input", null, label);
    box2.type = "checkbox";
    box2.checked = draft.ticks.has(name);
    box2.onchange = () => {
      box2.checked ? draft.ticks.add(name) : draft.ticks.delete(name);
      if (box2.checked) defaultStatus();
    };
    label.append(` ${name.replace(/\.$/, "")}`);
  }

  element("div", "Combos (all labels in one combo together make one way):", box, "muted");
  draft.combos.forEach((labels, index) => {
    const combo = element("div", null, box, "combo");
    for (const text of labels) {
      const chip = element("span", text, combo, "chip");
      const remove = element("button", "×", chip);
      remove.onclick = () => { labels.splice(labels.indexOf(text), 1); render(); };
    }
    const select = element("select", null, combo);
    element("option", "add a label…", select).value = "";
    for (const option of page.snap.combo_options) element("option", option, select).value = option;
    select.onchange = () => {
      if (select.value) { labels.push(select.value); defaultStatus(); render(); }
    };
    const drop = element("button", "remove combo", combo);
    drop.onclick = () => { draft.combos.splice(index, 1); render(); };
  });
  if (draft.combos.length < 5) {
    const addCombo = element("button", "Add combo", box);
    addCombo.onclick = () => { draft.combos.push([]); render(); };
  }

  const meta = element("div", null, box);
  element("span", "Status: ", meta);
  const status = element("select", null, meta);
  for (const option of page.snap.statuses) element("option", option || "(not done)", status).value = option;
  status.value = draft.status;
  status.onchange = () => { draft.status = status.value; };
  element("span", " Notes: ", meta);
  const notes = element("input", null, meta);
  notes.value = draft.notes;
  notes.size = 22;
  notes.onchange = () => { draft.notes = notes.value; };

  const buttons = element("div", null, box, "buttons");
  const save = element("button", "Save row", buttons);
  save.onclick = () => {
    act({ type: "record", enemy: page.enemy, doors_cell: page.rowKey,
          new_doors_cell: draft.doors, requirement: {
            ticks: [...draft.ticks], combos: draft.combos.filter((c) => c.length).map((c) => c.join(", ")),
            status: draft.status, notes: draft.notes } });
    page.rowKey = draft.doors;
  };
  const row = selectedRow();
  const saved = row && row.doors_cell && (row.ticks.length || row.combos.length);
  if (page.pick) {
    const apply = element("button", `Apply to ${page.picks.size}`, buttons);
    apply.disabled = !page.picks.size;
    apply.onclick = () => {
      if (page.pick === "enemies") {
        act({ type: "same_for_enemies", enemy: page.enemy, doors_cell: page.rowKey,
              targets: [...page.picks] });
      } else {
        act({ type: "same_for_entrances", enemy: page.enemy, doors_cell: page.rowKey,
              doors: [...page.picks] });
      }
    };
    const cancel = element("button", "Cancel", buttons);
    cancel.onclick = () => { endPick(); render(); };
  } else {
    const enemies = element("button", "Same for enemy…", buttons);
    enemies.disabled = !saved;
    enemies.title = "Give other enemies in this room a row with these doors and this requirement";
    enemies.onclick = () => startPick("enemies");
    const entrances = element("button", "Same for entrance…", buttons);
    entrances.disabled = !saved;
    entrances.title = "Add doors that need the same thing to this row";
    entrances.onclick = () => startPick("entrances");
  }
  if (!saved) element("div", "Save the row before sharing it.", box, "muted");
}

// --- Google's file picker ----------------------------------------------------------------------

async function openPicker() {
  const config = await (await fetch("/picker.json")).json();
  if (!config.apiKey) { toast("Sign in first.", false); return; }
  if (!window.gapi) {
    await new Promise((resolve) => {
      const script = document.createElement("script");
      script.src = "https://apis.google.com/js/api.js";
      script.onload = resolve;
      document.head.append(script);
    });
  }
  await new Promise((resolve) => gapi.load("picker", resolve));
  const view = new google.picker.DocsView(google.picker.ViewId.SPREADSHEETS);
  if (typeof view.setFileIds === "function") view.setFileIds(config.fileId);
  new google.picker.PickerBuilder().addView(view).setOAuthToken(config.token)
    .setDeveloperKey(config.apiKey).setAppId(config.appId).setTitle("Pick the logic sheet")
    .setCallback((data) => {
      if (data.action === google.picker.Action.PICKED) send({ type: "picked" });
    }).build().setVisible(true);
}

// --- wiring ------------------------------------------------------------------------------------

$("sign-in").onclick = () => send({ type: "sign_in" });
$("grant").onclick = openPicker;
$("refresh").onclick = () => send({ type: "refresh" });
$("castle").onclick = () => selectRoom(null);
$("filter").oninput = renderWorklist;
$("background").onchange = renderMap;
$("one-per-type").onchange = renderWorklist;
$("annotator").onchange = () => send({ type: "settings", annotator: $("annotator").value });
window.addEventListener("focus", () => {
  if (page.socket && Date.now() - page.lastSnapshot > STALE_AFTER) send({ type: "refresh" });
});
document.addEventListener("keydown", (event) => {
  const active = document.activeElement;
  const typing = active.tagName === "SELECT" || active.tagName === "TEXTAREA"
    || (active.tagName === "INPUT" && !["checkbox", "button"].includes(active.type));
  if (typing) return;
  if (event.key === "n" || event.key === "N") nextItem();
  if (event.key === "Escape" && page.pick) { endPick(); render(); }
});

fetch("/map.json").then((r) => r.json()).then((map) => { page.map = map; connect(); });
