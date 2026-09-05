@ Custom-pickup dispatcher -- Castlevania: Aria of Sorrow.
@
@ A small, extensible framework for adding *pickups that run custom on-collection behaviour*
@ (e.g. set an arbitrary memory flag + play a sound) without granting an item and without
@ touching any existing item identity. The first user is the "button pickup": collecting it
@ has the same net effect as pressing the A01 Forbidden-Area press-button -- it sets MISC flag
@ #48 so the paired barrier sinks/opens -- and plays the button's SFX (song 0x133).
@
@ ---- How a pickup is collected (verified against the USA ROM) ----
@ A room pickup entity is 12 bytes: x,y(s16) id,type,subtype,unk(u8) var_a,var_b(u16), where
@ type=4 (PICKUP), var_a = the per-location "collected" save-flag index (0x02000360 bitfield),
@ var_b = item_offset. The spawn dispatcher sub_0800F1FC (case 4) spawns the floating pickup via
@ sub_08044054(x,y,subtype,var_b) when the 0x360 flag is clear; its on-collision callback (stored
@ at entity[0x7C] by sub_0804277C) is sub_080441F8 (0x080441F8), a per-category grant routine that
@ ends at _08044508 (0x08044508), where it writes the 0x360 collected flag from var_a (entity[0x30])
@ and returns. EntityData element-relative offsets used here:
@     [0x30] = var_a (unk_514)   [0x32] = var_b/item_offset (unk_516)   [0x36] = subtype/category
@ (category 2 = consumable). (Element base is absolute struct offset 0x4E4, so +0x30 == 0x514.)
@
@ ---- How the A01 button's effect is reproduced ----
@ The button (object 0x35, varB=48) sets MISC flag #48 with an INLINE write to the 0x02000344
@ misc-flag field -- NOT SetEventFlag (which writes the 0x33C *event* field). Flag #48 ->
@ 0x02000344 + (48>>5)*4 = 0x02000348, bit 48&0x1F = 16. The barrier (object 0x35, varA=1) polls
@ misc #48 and sinks. We replicate that exact field write generically (any flag field/number) and
@ PlaySong(0x133).
@
@ ---- Install ----
@ Overwrite the first 8 bytes of sub_080441F8 with a Thumb far-jump trampoline
@ (ldr r3,[pc,#0]; bx r3; .word CustomPickupHook|1 -- r3 is dead at entry). This routine replicates
@ the 4 stolen prologue insns + the first real insn (adds r4,r0,#0), then:
@   - if the pickup is a consumable (category 2) whose var_b matches a row in DESC_TABLE: (optionally)
@     queue its "Got <name>" textbox, set its flag, play its SFX, despawn the floating pickup, and jump
@     to the vanilla finish at 0x08044508 (writes the 0x360 collected flag from var_a, returns) -- no
@     normal item is granted. (Owned-state for the Item-Use menu is NOT written here; the menu derives
@     it from the flag this sets -- see inventory_menu.s.);
@   - else -> resume vanilla pickup-collect at 0x08044202.
@ Custom items live at consumable var_b >= 32 (new item space added by repointing the spawn-path
@ icon-table literal at file 0x440B4 to an extended table; existing consumables 0..31 are untouched).
@ "Inventory" items additionally appear at Item-Use menu slot == var_b via the relocated
@ item/name/desc tables; the menu derives their owned-state from this item's behaviour flag into a
@ transient shadow array (see inventory_menu.s) -- no per-item saved storage is used.
@
@ ---- DESC_TABLE (built + written by custom_pickups.py at CUSTOM_DESC_TABLE_GBA) ----
@ Array of 12-byte rows, terminated by key == 0xFFFF:
@     +0 u16 key            = var_b (item_offset) of the custom item
@     +2 u16 flag_field     = byte offset of the flag field from gEwramData (0x344 misc / 0x33C event
@                             / 0x360 pickup / 0x37E boss)
@     +4 u16 flag_number    = flag bit index within that field
@     +6 u16 sfx            = song id for PlaySong (0 = silent)
@     +8 u16 name_text_id   = string id for the "Got <name>" textbox + Item-Use row; 0 = behaviour-only
@                             (no inventory row, no textbox)
@     +10 u16 (reserved)
@
@ Position-DEPENDENT: the literal pool bakes absolute addresses, so keep the link address in sync
@ with CUSTOMHOOK_BASE_GBA / CUSTOM_DESC_TABLE_GBA in custom_pickups.py. To (re)assemble, from
@ this directory (needs `pip install keystone-engine`):
@   python thumb_assembler.py custom_pickups.s 0x08670300   @ -> CUSTOMHOOK_BLOB
.syntax unified
.thumb
.text
.global CustomPickupHook

    .equ GEWRAM,          0x02000000
    .equ DESC_TABLE,      0x08670400   @ CUSTOM_DESC_TABLE_GBA in custom_pickups.py
    .equ DESC_TERMINATOR, 0xFFFF
    .equ DESC_STRIDE,     12           @ row = {var_b, flag_field, flag_number, sfx, name_text_id, _}
    .equ CONSUMABLE_CAT,  2
    .equ GOTITEM,         0x0800EF98   @ sub_0800EF98(text_id): queues the "Got <name>" textbox
    .equ PLAYSONG,        0x080D7910
    .equ VANILLA_RESUME,  0x08044202   @ sub_080441F8 + 0xA (after the 5 stolen/replicated insns)
    .equ VANILLA_FINISH,  0x08044508   @ _08044508: writes 0x360 collected flag from var_a, returns

CustomPickupHook:
    @ r0 = pickup entity. Replicate sub_080441F8's stolen prologue + first insn (0x080441F8..0x08044200).
    push {r4, r5, r6, r7, lr}
    mov  r7, r8
    push {r7}
    sub  sp, #4
    adds r4, r0, #0                 @ r4 = entity

    movs r0, #0x36                 @ ldrb imm offset max is 31, so use a register offset
    ldrb r0, [r4, r0]             @ category; custom items are consumables (2)
    cmp  r0, #CONSUMABLE_CAT
    bne  .Lvanilla
    ldrh r5, [r4, #0x32]           @ r5 = var_b (item_offset)
    ldr  r6, =DESC_TABLE

.Lscan:
    ldrh r0, [r6, #0]              @ row key
    ldr  r1, =DESC_TERMINATOR
    cmp  r0, r1
    beq  .Lvanilla                @ end of table -> not a custom pickup
    cmp  r0, r5
    beq  .Lfound
    adds r6, #DESC_STRIDE          @ next row
    b    .Lscan

.Lvanilla:
    @ Not ours: resume vanilla pickup-collect at 0x08044202 (r0 must still be the entity).
    adds r0, r4, #0
    ldr  r3, =(VANILLA_RESUME + 1)
    bx   r3

.Lfound:
    @ r6 -> matched row, r4 = entity, r5 = var_b. Inventory item (name_text_id != 0): queue the
    @ "Got <name>" textbox. We do NOT store owned-state here: the Item-Use menu trampoline re-derives
    @ it from this item's behaviour flag (set just below in .Lflag) into a transient shadow, so the
    @ saved flag is the single source of truth and no inventory array is written on collection.
    ldrh r0, [r6, #8]             @ name_text_id
    cmp  r0, #0
    beq  .Lflag
    ldr  r3, =(GOTITEM + 1)      @ sub_0800EF98(name_text_id); r0 already holds name_text_id
    bl   .Lcall_r3

.Lflag:
    @ Set flag: *(gEwram + field + (n>>5)*4) |= 1 << (n & 0x1F).
    ldrh r1, [r6, #2]             @ flag_field byte offset
    ldrh r2, [r6, #4]             @ flag_number
    ldr  r0, =GEWRAM
    adds r0, r0, r1
    lsrs r3, r2, #5
    lsls r3, r3, #2
    adds r0, r0, r3              @ r0 = &flag dword
    movs r1, #0x1F
    ands r1, r2                 @ r1 = bit index
    movs r3, #1
    lsls r3, r1                 @ r3 = 1 << bit
    ldr  r1, [r0]
    orrs r1, r3
    str  r1, [r0]

    @ Play SFX if nonzero.
    ldrh r0, [r6, #6]
    cmp  r0, #0
    beq  .Lfinish
    ldr  r3, =(PLAYSONG + 1)
    bl   .Lcall_r3

.Lfinish:
    @ Despawn the floating pickup like a normal collect: set unk_53D_3 (entity[0x59] |= 0x08), the
    @ "delete me next tick" flag the entity-update loop acts on. Vanilla sub_080441F8 sets this at its
    @ top (which we skipped), so without this the collected pickup would linger on the floor.
    movs r0, #0x59               @ ldrb/strb imm offset max is 31, so use a register offset
    ldrb r1, [r4, r0]
    movs r2, #8
    orrs r1, r2
    strb r1, [r4, r0]

    @ Finish like a vanilla collected pickup: 0x08044508 writes the 0x360 flag (from var_a at
    @ entity[0x30]) and returns -- no item granted. Needs r4 = entity (set) + the balanced stack.
    ldr  r3, =(VANILLA_FINISH + 1)
    bx   r3

.Lcall_r3:
    bx   r3                       @ far-call veneer; lr set by `bl .Lcall_r3`

    .align 2
    .pool
