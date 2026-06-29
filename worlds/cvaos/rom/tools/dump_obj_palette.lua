-- Dump all 16 OBJ palette banks from a running AoS in BizHawk.
--
-- Why: custom-pickup icons render through OBJ palette bank 6 -- the shared items palette, loaded for
-- every floor pickup (so it is reliable in every room; we no longer point pickups at spare banks).
-- Bank 6 is items-palette sub-palette 2, static in the ROM at 0x08209A40 (= 0x082099FC + 4 header
-- + 2*0x20). This tool prints every bank so we can confirm bank 6 in-game matches that static data
-- (and inspect the others). Custom icon tiles are quantised against bank 6 -- see custom_pickups.py.
--
-- Use: in BizHawk's Lua console, with AoS running and a pickup (e.g. the Study Sealswitch) ON SCREEN,
-- run this. Bank 6 should show the items rainbow (gold #F8D848, tan #F8B070, greys). Paste output back.
--
-- OBJ palette RAM = 0x05000200; bank N = 0x05000200 + N*0x20 (16 BGR555 colours).

local ok = pcall(memory.usememorydomain, "PALRAM")     -- mGBA exposes PALRAM (0x000 BG, 0x200 OBJ)
local function read16(addr)
    if ok then return memory.read_u16_le(0x200 + addr) end -- PALRAM-relative
    return memory.read_u16_le(0x05000200 + addr, "System Bus")
end

print("=== OBJ palette banks (BGR555) ===")
for bank = 0, 15 do
    local parts = {}
    for i = 0, 15 do
        parts[#parts + 1] = string.format("%04x", read16(bank * 0x20 + i * 2))
    end
    print(string.format("bank %2d: %s", bank, table.concat(parts, " ")))
end
