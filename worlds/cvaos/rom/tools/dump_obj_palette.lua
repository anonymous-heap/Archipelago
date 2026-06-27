-- Dump OBJ palette bank 6 (the shared item-pickup palette) from a running AoS in BizHawk.
--
-- Why: custom pickups reuse OBJ palette bank 6, so their icon tiles must be recoloured to bank 6's
-- 16 colours. That palette is loaded into PAL RAM by a runtime slot manager, so it's read live here.
--
-- Use: load this in BizHawk's Lua console with AoS running and a pickup/item icon ON SCREEN (so the
-- items palette is resident). It prints 16 BGR555 hwords. Paste them into:
--   python png_to_icon.py button_pickup.png --palette "<the 16 hwords>"
-- to regenerate the final ICON_TILES for custom_pickups.py.
--
-- OBJ palette RAM = 0x05000200; bank 6 = 0x05000200 + 6*0x20 = 0x050002C0 (16 colours).

local function dump()
    local ok = pcall(memory.usememorydomain, "PALRAM")   -- mGBA exposes PALRAM (0x000 BG, 0x200 OBJ)
    local base = ok and (0x200 + 6 * 0x20) or (0x050002C0) -- PALRAM-relative, else System Bus
    local parts = {}
    for i = 0, 15 do
        local c = ok and memory.read_u16_le(base + i * 2)
                     or memory.read_u16_le(0x050002C0 + i * 2, "System Bus")
        parts[#parts + 1] = string.format("0x%04x", c)
    end
    print("OBJ palette bank 6: " .. table.concat(parts, ","))
end

dump()
