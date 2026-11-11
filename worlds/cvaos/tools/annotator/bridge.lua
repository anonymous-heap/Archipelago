-- Bridge between the CVAoS routing annotator and BizHawk, loaded by the boot script that
-- emulator.py writes. It listens on 127.0.0.1:43090 and speaks JSON lines: each request is an
-- object with an "id" and a "cmd", and each reply carries the same "id". Addresses are full GBA
-- bus addresses (0x02xxxxxx). Several clients can be connected at once, and each gets the replies
-- to its own requests.
--
--   ping                                   -> {frame}
--   read    {reads = [[addr, len], ...]}   -> {data = [[byte, ...], ...]}
--   write   {writes = [[addr, [bytes]], ...]}   all written within one frame callback
--   freeze  {key, addr, bytes}             rewritten at the start and end of every frame
--   unfreeze {key} (or no key for all)
--   watch   {reads = [[addr, len], ...], every = frames}   pushes {event = "tick", ...} to
--                                          the client that asked
--   state_save / state_load {slot}         memory savestates, kept by name
--   state_drop {slot}                      frees a memory savestate
--   speed   {percent, throttle, sound, frameskip}   each is optional: the speed in percent,
--                                          whether the frame rate is limited at all, whether
--                                          sound plays, and how many frames go undrawn per frame
--                                          drawn
--   overlay {text}                         drawn at the bottom left of the game screen, one
--                                          line per "\n" ("" clears it)
--   screenshot {path}                      saves the current frame as a PNG
--   entities {type}                        -> {entities = [[slot, type, entry id, hp, x, y],
--                                          ...]} for live entities of that type (all types
--                                          without one); x and y are screen pixels
--   press   {buttons = {Right = true}, frames}   holds buttons for that many frames
--
-- While L and R are held, pressing Select or Start sends {event = "hotkey", name = "retry"} or
-- {event = "hotkey", name = "give_up"} to every client, and the game doesn't see that press.
-- Buttons held by the press command count too, so scripts can test the hotkeys.
--
-- Stepping mode is for a program that drives the game frame by frame:
--
--   hold    {on}                           -> {frame}. on = true pauses EmuHawk and starts
--                                          stepping mode, and on = false ends it and unpauses
--   step    {buttons = ["A", ...], frames} -> {frame}. Runs exactly that many frames with
--                                          exactly those buttons down, and no frame after them
--   reboot  {keep_save}                    -> {frame}. Switches the console off and on. The
--                                          cartridge's save is blanked first unless keep_save
--                                          is true, and every memory savestate is lost
--
-- In stepping mode a frame runs only for a step, and every other request is answered between
-- frames. The pad, the press command and the hotkeys have no effect on a stepped frame.
-- Stepping mode also ends when the client that started it disconnects.
--
-- The boot script sets ANNOTATOR_AP_LUA to Archipelago's data/lua folder, for json.lua and the
-- LuaSocket DLL. It may also set ANNOTATOR_PORT to listen somewhere other than 43090.

local AP_LUA = ANNOTATOR_AP_LUA
package.path = AP_LUA .. "/?.lua;" .. package.path
local major, minor = _VERSION:match("Lua (%d+)%.(%d+)")
if tonumber(major) > 5 or (tonumber(major) == 5 and tonumber(minor) >= 3) then
    require("lua_5_3_compat")
end
local json = require("json")
local core = assert(package.loadlib(AP_LUA .. "/x64/socket-windows-5-4.dll",
                                    "luaopen_socket_core"))()

local PORT = ANNOTATOR_PORT or 43090
local DOMAIN = "System Bus"
local SAVE_DOMAIN = "SRAM"
-- The entity table: 0xE0 slots of 0x84 bytes. A slot is live when its update function (+0x00)
-- is set. +0x34 is HP, +0x36 the enemy type, +0x37 the id of the room entry it spawned from, and
-- +0x42 and +0x46 the screen position.
local ENTITY_BASE, ENTITY_SIZE, ENTITY_COUNT = 0x020004E4, 0x84, 0xE0

-- BizHawk's client library. The code below also uses "client" for a connection, which hides
-- the library wherever such a variable is in scope.
local emuhawk = client

local server = nil
local clients = {}   -- {sock, pending, watch, watch_every, closed}
local freezes = {}
local states = {}
local overlay = ""
local held, held_frames = nil, 0
local HOTKEYS = {Select = "retry", Start = "give_up"}
local hotkey_down = {}

local BUTTONS = {"A", "B", "L", "R", "Start", "Select", "Up", "Down", "Left", "Right"}
local stepping = false   -- true in stepping mode
local holder = nil       -- the client that started stepping mode
local step_pad = nil     -- every button's state for the step that is running
local jobs = {}          -- the steps and reboots that the main loop has yet to run
local handled = 0        -- how many requests have been handled
-- A handler returns this when the main loop sends the reply later.
local DEFERRED = {}

local function start_server()
    server = assert(core.tcp4())
    server:setoption("reuseaddr", true)
    assert(server:bind("127.0.0.1", PORT))
    assert(server:listen(4))
    server:settimeout(0)
    print("annotator bridge listening on 127.0.0.1:" .. PORT)
end

local function send(client, message)
    if not client.closed then
        local ok, err = client.sock:send(json.encode(message) .. "\n")
        if not ok then
            print("send failed: " .. tostring(err))
            client.closed = true
        end
    end
end

local function read_all(reads)
    local data = {}
    for i, r in ipairs(reads) do
        data[i] = memory.read_bytes_as_array(r[1], r[2], DOMAIN)
    end
    return data
end

local function apply_freezes()
    for _, f in pairs(freezes) do
        memory.write_bytes_as_array(f.addr, f.bytes, DOMAIN)
    end
end

local handlers = {}

function handlers.ping(req)
    return {frame = emu.framecount()}
end

function handlers.read(req)
    return {data = read_all(req.reads)}
end

function handlers.write(req)
    for _, w in ipairs(req.writes) do
        memory.write_bytes_as_array(w[1], w[2], DOMAIN)
    end
    return {}
end

function handlers.freeze(req)
    freezes[req.key] = {addr = req.addr, bytes = req.bytes}
    apply_freezes()
    return {}
end

function handlers.unfreeze(req)
    if req.key then
        freezes[req.key] = nil
    else
        freezes = {}
    end
    return {}
end

function handlers.watch(req, client)
    client.watch = req.reads
    client.watch_every = req.every or 6
    return {}
end

function handlers.state_save(req)
    if states[req.slot] then
        memorysavestate.removestate(states[req.slot])
    end
    states[req.slot] = memorysavestate.savecorestate()
    return {}
end

function handlers.state_load(req)
    if not states[req.slot] then
        return {error = "no state " .. tostring(req.slot)}
    end
    memorysavestate.loadcorestate(states[req.slot])
    return {}
end

function handlers.state_drop(req)
    if states[req.slot] then
        memorysavestate.removestate(states[req.slot])
        states[req.slot] = nil
    end
    return {}
end

function handlers.speed(req)
    if req.percent then
        emuhawk.speedmode(req.percent)
    end
    if req.throttle ~= nil then
        emu.limitframerate(req.throttle)
    end
    if req.sound ~= nil then
        emuhawk.SetSoundOn(req.sound)
    end
    if req.frameskip then
        emuhawk.frameskip(req.frameskip)
    end
    return {}
end

local function set_stepping(on, client)
    stepping = on
    holder = on and client or nil
    held, held_frames, hotkey_down = nil, 0, {}
    jobs, step_pad = {}, nil
    if on then
        emuhawk.pause()
    else
        emuhawk.unpause()
    end
end

function handlers.hold(req, client)
    set_stepping(req.on ~= false, client)
    return {frame = emu.framecount()}
end

function handlers.step(req, client)
    if not stepping then
        error("step works only in stepping mode, which hold starts", 0)
    end
    local frames = req.frames or 1
    if type(frames) ~= "number" or frames < 0 or frames ~= math.floor(frames) then
        error("frames must be a whole number of frames, 0 or more", 0)
    end
    local pad = {}
    for _, name in ipairs(BUTTONS) do
        pad[name] = false
    end
    for _, name in ipairs(req.buttons or {}) do
        if pad[name] == nil then
            error("unknown button " .. tostring(name), 0)
        end
        pad[name] = true
    end
    jobs[#jobs + 1] = {client = client, id = req.id, pad = pad, frames = frames}
    return DEFERRED
end

function handlers.reboot(req, client)
    if not stepping then
        error("reboot works only in stepping mode, which hold starts", 0)
    end
    jobs[#jobs + 1] = {client = client, id = req.id, reboot = true, keep_save = req.keep_save}
    return DEFERRED
end

function handlers.press(req)
    held = req.buttons
    held_frames = req.frames or 1
    return {}
end

function handlers.screenshot(req)
    emuhawk.screenshot(req.path)
    return {}
end

function handlers.overlay(req)
    overlay = req.text or ""
    return {}
end

function handlers.entities(req)
    local found = {}
    for slot = 0, ENTITY_COUNT - 1 do
        local base = ENTITY_BASE + slot * ENTITY_SIZE
        if memory.read_u32_le(base, DOMAIN) ~= 0 then
            local kind = memory.read_u8(base + 0x36, DOMAIN)
            if req.type == nil or kind == req.type then
                found[#found + 1] = {slot, kind, memory.read_u8(base + 0x37, DOMAIN),
                                     memory.read_s16_le(base + 0x34, DOMAIN),
                                     memory.read_s16_le(base + 0x42, DOMAIN),
                                     memory.read_s16_le(base + 0x46, DOMAIN)}
            end
        end
    end
    return {entities = found}
end

local function handle(client, line)
    local ok, req = pcall(json.decode, line)
    if not ok or type(req) ~= "table" then
        send(client, {error = "bad request: " .. line})
        return
    end
    handled = handled + 1
    local handler = handlers[req.cmd]
    local reply
    if not handler then
        reply = {error = "unknown command " .. tostring(req.cmd)}
    else
        local good, result = pcall(handler, req, client)
        if good and result == DEFERRED then
            return
        end
        reply = good and result or {error = tostring(result)}
    end
    reply.id = req.id
    send(client, reply)
end

local function receive(client)
    while not client.closed do
        local line, err, partial = client.sock:receive("*l")
        if line then
            handle(client, client.pending .. line)
            client.pending = ""
        else
            if partial and #partial > 0 then
                client.pending = client.pending .. partial
            end
            if err == "closed" then
                client.closed = true
            end
            return
        end
    end
end

local function poll()
    while true do
        local accepted = server:accept()
        if not accepted then
            break
        end
        accepted:settimeout(0)
        clients[#clients + 1] = {sock = accepted, pending = "", watch_every = 6}
        print("annotator client connected")
    end
    local open = {}
    for _, client in ipairs(clients) do
        receive(client)
        if client.closed then
            client.sock:close()
            print("annotator client disconnected")
            if client == holder then
                set_stepping(false)
            end
        else
            open[#open + 1] = client
        end
    end
    clients = open
end

local function pressed(pad, button)
    return pad[button] == true or pad["P1 " .. button] == true
end

local function check_hotkeys(scripted)
    local pad = joypad.getimmediate()
    for button, value in pairs(scripted or {}) do
        pad[button] = pad[button] or value
    end
    if not (pressed(pad, "L") and pressed(pad, "R")) then
        hotkey_down = {}
        return
    end
    for button, name in pairs(HOTKEYS) do
        if pressed(pad, button) then
            if not hotkey_down[button] then
                hotkey_down[button] = true
                for _, client in ipairs(clients) do
                    send(client, {event = "hotkey", name = name})
                end
            end
        else
            hotkey_down[button] = nil
        end
    end
    joypad.set({Select = false, Start = false})
end

local function on_frame_start()
    apply_freezes()
    if stepping then
        if step_pad then
            joypad.set(step_pad)
        end
        return
    end
    local scripted = nil
    if held and held_frames > 0 then
        scripted = held
        joypad.set(held)
        held_frames = held_frames - 1
    end
    check_hotkeys(scripted)
end

local function on_frame_end()
    if not stepping then
        poll()
    end
    apply_freezes()
    for _, client in ipairs(clients) do
        if client.watch and emu.framecount() % client.watch_every == 0 then
            send(client, {event = "tick", frame = emu.framecount(), data = read_all(client.watch)})
        end
    end
    if overlay ~= "" then
        gui.text(2, 2, overlay, nil, "bottomleft")
    end
end

-- Fill the cartridge's save memory with 0xFF, which is what a cartridge with no save file holds.
-- EmuHawk writes the save memory to a file when a game closes and reads the file when a game
-- starts, so without this a reboot would start from the save that the last run left.
local function blank_save()
    local blank = {}
    for i = 1, memory.getmemorydomainsize(SAVE_DOMAIN) do
        blank[i] = 0xFF
    end
    memory.write_bytes_as_array(0, blank, SAVE_DOMAIN)
end

-- Run one queued step or reboot. This is called from the main loop because a frame can be
-- advanced only from there, and not from a frame callback.
local function run(job)
    if job.reboot then
        if not job.keep_save then
            local good, problem = pcall(blank_save)
            if not good then
                send(job.client, {id = job.id, error = "the save can't be blanked: " .. problem})
                return
            end
        end
        emuhawk.reboot_core()
        -- EmuHawk's memory savestates don't outlast the core they were taken from.
        states = {}
        emuhawk.pause()
    else
        step_pad = job.pad
        if emuhawk.ispaused() then
            emuhawk.unpause()
        end
        for _ = 1, job.frames do
            emu.frameadvance()
        end
        step_pad = nil
        -- EmuHawk stays unpaused here, because pausing and unpausing around every step costs
        -- more than a frame does. No frame runs until the script yields, and the main loop
        -- pauses EmuHawk before it yields.
    end
    send(job.client, {id = job.id, frame = emu.framecount()})
end

-- Serve requests in stepping mode until none is waiting. Return true when any was handled.
local function serve_stepping()
    local sockets = {server}
    for _, client in ipairs(clients) do
        sockets[#sockets + 1] = client.sock
    end
    -- Waiting here, inside the script, lets a request be answered as soon as it arrives.
    core.select(sockets, nil, 0.02)
    local before = handled
    poll()
    while stepping and #jobs > 0 do
        run(table.remove(jobs, 1))
    end
    return handled ~= before
end

start_server()
event.onframestart(on_frame_start, "annotator_frame_start")
event.onframeend(on_frame_end, "annotator_bridge")
local last_yield = core.gettime()
while true do
    if stepping then
        -- EmuHawk's window is repainted only while the script yields, so yield whenever no
        -- request is waiting, and a few times a second during a long run of requests.
        if not serve_stepping() or core.gettime() - last_yield > 0.25 then
            -- A step leaves EmuHawk unpaused, and a yield would then run frames freely.
            if stepping and not emuhawk.ispaused() then
                emuhawk.pause()
            end
            emu.yield()
            last_yield = core.gettime()
        end
    else
        -- The frame callbacks serve requests while the game runs. They don't run while
        -- EmuHawk is paused, so the paused case is served from here.
        if emuhawk.ispaused() then
            poll()
        end
        emu.yield()
    end
end
