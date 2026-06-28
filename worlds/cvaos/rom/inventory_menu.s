@ Item-Use menu extension -- Castlevania: Aria of Sorrow.
@
@ Lets the pause "Item Use" subscreen show extra "key items" (var_b 32..0x2b) beyond the 32 vanilla
@ consumables, without touching the fixed 32-byte itemInventory[0x20] @ gEwramData+0x13294 (which is
@ immediately followed by weaponInventory, so index 32 would corrupt weapons).
@
@ Mechanism (verified vs the USA ROM):
@   * The menu list builder sub_0804B494 (0x0804B494) and recount sub_0804B648 (0x0804B648) read the
@     consumable counts as base+0x38 (base passed in r0 = gEwramData+0x1325C, so base+0x38 = 0x13294),
@     iterating index 0..0x1F and showing any row with count!=0. The count array is re-read LIVE on
@     open, on every page-crossing cursor move, and after every use -- never cached -- so a relocated
@     "shadow" count array must stay valid for the whole time the menu is open.
@   * Storage: there is NO free SRAM-saved scratch (pad_133A0 @ 0x133A0..0x133EB is ENTIRELY soul
@     state -- the enemy/bestiary bitfield 0x133A0..0x133BF plus the equip working-lists/ATK-DEF
@     preview 0x133D0..0x133E7), and no >=44-byte menu-local buffer (the menuobj is a live entity).
@     So the shadow is TRANSIENT, living in the free high-EWRAM tail above the gEwramData struct
@     (struct ends at 0x02025554; EWRAM ends 0x02040000) at 0x02030000 -- a region no game code reads
@     or writes (only the boot-time full-EWRAM zero-fill touches it). It is NOT saved; instead each
@     trampoline REBUILDS it from persistent sources before the menu reads it.
@   * Custom "owned" state is persistent because the collect hook (custom_pickups.s) sets each item's
@     saved behaviour flag (DESC_TABLE row flag_field/flag_number). The trampolines re-derive owned
@     from that flag every build, so the volatile shadow never needs saving.
@
@ Each trampoline (installed over the first 8 bytes of sub_0804B494 / sub_0804B648):
@   1. copies the 32 real consumable counts 0x13294..0x132B3 -> shadow[0..31] (so used potions etc.
@      refresh on every rebuild);
@   2. walks DESC_TABLE (0x08660400, 12-byte rows, key==0xFFFF terminator); for each inventory row
@      (name_text_id != 0) sets shadow[key] = bit flag_number of *(gEwram + flag_field + ...) -- i.e.
@      "owned" iff the item's saved behaviour flag is set;
@   3. overrides the base pointer (r0) to SHADOW_BASE so the loop reads the shadow;
@   4. replays the stolen prologue and resumes the original function.
@ The caller's other uses of the real base (the use-gate's HP/MP heal, the decrement at 0x13294) are
@ untouched -- only the list/recount readers are redirected, and the decrement writes a hardcoded
@ 0x13294 (not the overridden base), so the shadow stays read-only from the engine side.
@
@ The bound (cmp #0x1f) is bumped to menu_slot_bound() separately by inventory_menu.py.
@
@ Position-DEPENDENT (literal pool bakes absolute addresses). To (re)assemble:
@   arm-none-eabi-as -mcpu=arm7tdmi -mthumb inventory_menu.s -o /tmp/m.o
@   arm-none-eabi-ld -Ttext=0x08660500 /tmp/m.o -o /tmp/m.elf
@   arm-none-eabi-objcopy -O binary /tmp/m.elf /tmp/m.bin   # -> MENU_BLOB
.syntax unified
.thumb
.text
.global MenuListHook
.global MenuRecountHook

    .equ REAL_CONSUMABLES,  0x02013294   @ gEwramData + 0x13294 (itemInventory[0])
    .equ SHADOW,            0x02030000   @ transient shadow array, in the free high-EWRAM tail above
                                         @ the gEwramData struct (struct end 0x25554); NOT SRAM-saved
    .equ SHADOW_BASE,       0x0202FFC8   @ SHADOW - 0x38 (so base+0x38 == SHADOW)
    .equ GEWRAM,            0x02000000   @ for the per-item flag read (gEwram + flag_field + ...)
    .equ DESC_TABLE,        0x08660400   @ CUSTOM_DESC_TABLE_GBA (custom_pickups.py): 12-byte rows
    .equ DESC_TERM,         0xFFFF       @ DESC_TABLE key terminator
    .equ DESC_STRIDE,       12
    .equ B494_RESUME,       0x0804B49C   @ sub_0804B494 + 8 (after 4 stolen insns)
    .equ B648_RESUME,       0x0804B650   @ sub_0804B648 + 8

@ ---- Rebuild the shadow: mirror 32 real consumable counts, then derive custom slots from flags.
@ Uses r0-r7 as scratch but SAVES/RESTORES r4-r7 (the stolen prologue replayed afterwards pushes the
@ caller's r4-r7, so they must still hold the caller's values here); r1 is preserved by the caller's
@ surrounding push/pop. Net effect: only r0,r2,r3 are left clobbered, exactly as the vanilla prologue
@ tolerates before it sets them up.
.macro REBUILD_SHADOW copy, desc, next, done
    push {r4, r5, r6, r7}        @ preserve caller's r4-r7 across the derive loop
    ldr  r2, =REAL_CONSUMABLES
    ldr  r3, =SHADOW
    movs r0, #32
\copy:
    ldrb r1, [r2]
    strb r1, [r3]
    adds r2, #1
    adds r3, #1
    subs r0, #1
    bne  \copy
    @ derive custom owned-state from each DESC row's saved behaviour flag
    ldr  r4, =DESC_TABLE          @ row pointer
    ldr  r5, =SHADOW              @ shadow base
    ldr  r6, =GEWRAM             @ flag-field base
    ldr  r7, =DESC_TERM
\desc:
    ldrh r0, [r4, #0]            @ key = var_b (= menu slot)
    cmp  r0, r7
    beq  \done                   @ 0xFFFF -> end of table
    ldrh r2, [r4, #8]            @ name_text_id (0 = behaviour-only, no menu slot)
    cmp  r2, #0
    beq  \next
    ldrh r2, [r4, #2]            @ flag_field byte offset
    ldrh r3, [r4, #4]            @ flag_number
    adds r2, r6, r2             @ gEwram + flag_field
    lsrs r1, r3, #5            @ flag_number >> 5
    lsls r1, r1, #2            @ * 4 (word index)
    adds r2, r2, r1            @ &flag dword
    ldr  r2, [r2]              @ flag dword
    movs r1, #0x1F
    ands r1, r3               @ flag_number & 0x1F
    lsrs r2, r1               @ dword >> bit
    movs r3, #1
    ands r2, r3               @ owned (0/1)
    strb r2, [r5, r0]          @ shadow[key] = owned
\next:
    adds r4, #DESC_STRIDE
    b    \desc
\done:
    pop  {r4, r5, r6, r7}        @ restore caller's r4-r7
.endm

@ ---- sub_0804B494 list builder: stolen prologue = push {r4,r5,r6,r7,lr}; mov r7,sl; mov r6,sb; mov r5,r8
MenuListHook:
    push {r1}                    @ free r1 as a temp (menuobj preserved on stack)
    REBUILD_SHADOW .LcopyA, .LdescA, .LnextA, .LdoneA
    pop  {r1}                    @ restore menuobj
    ldr  r0, =SHADOW_BASE        @ override base so base+0x38 = SHADOW
    @ replay stolen prologue, then resume (r0 flows into the original `str r0,[sp]`):
    push {r4, r5, r6, r7, lr}
    mov  r7, sl
    mov  r6, sb
    mov  r5, r8
    ldr  r3, =(B494_RESUME + 1)  @ r3 dead at the resume site
    bx   r3

@ ---- sub_0804B648 recount: stolen prologue = push {r4,r5,r6,lr}; mov ip,r0; movs r4,#0; movs r3,#0
MenuRecountHook:
    push {r1}
    REBUILD_SHADOW .LcopyB, .LdescB, .LnextB, .LdoneB
    pop  {r1}
    ldr  r0, =SHADOW_BASE        @ override; `mov ip,r0` below makes the loop read SHADOW
    push {r4, r5, r6, lr}
    mov  ip, r0
    movs r4, #0
    movs r3, #0
    ldr  r0, =(B648_RESUME + 1)  @ use r0 (free after mov ip,r0; B648 sets r0 next); keeps r3 = 0
    bx   r0

    .align 2
    .pool
