@ Item-Use menu extension for Castlevania: Aria of Sorrow.
@
@ Lets the pause "Item Use" subscreen list custom key items at menu slots 32..0x2B, after the 32
@ vanilla consumables. The consumable counts are a fixed 32-byte array at gEwramData+0x13294, and
@ weaponInventory follows it directly, so a count stored at index 32 would overwrite a weapon.
@ The two trampolines in this file point the menu at a larger shadow copy of the counts instead.
@
@ ---- How the menu reads the counts (verified against the USA ROM) ----
@ The list builder sub_0804B494 (0x0804B494) and the recount sub_0804B648 (0x0804B648) take a
@ base pointer in r0 and the menu object in r1. The base is gEwramData+0x1325C, so base+0x38 is
@ the count array at 0x13294. Both functions walk index 0..0x1F, and a slot appears in the menu
@ only when its count is nonzero. They read the array again on open, on every page-crossing
@ cursor move and after every use, and never cache it. The shadow therefore has to hold correct
@ counts for as long as the menu is open.
@
@ ---- Where the shadow lives ----
@ No SRAM-saved scratch space is free. pad_133A0 (0x133A0..0x133EB) is entirely soul state: the
@ bestiary bitfield starts at 0x133A0, and the equip and sorted-soul lists are at
@ 0x133D0..0x133E7. The menu object is a live entity, so it has no spare 44-byte buffer either.
@ The shadow is therefore transient. It sits at 0x02030000, in the free tail of EWRAM between the
@ end of the gEwramData struct (0x02025554) and the end of EWRAM (0x02040000). The boot-time
@ zero-fill is the only game code that touches that region, and the shadow is not saved.
@
@ A custom item's owned state persists through its behavior flag instead. custom_pickups.s sets
@ that saved flag when the item is collected, using the flag_field and flag_number in the item's
@ DESC_TABLE row. The trampolines read the flag again every time they rebuild the shadow.
@
@ ---- What each trampoline does ----
@   1. It copies the 32 real counts at 0x13294..0x132B3 into shadow[0..31]. The copy is fresh on
@      every call, so a consumable shows its lower count as soon as one is used.
@   2. It walks DESC_TABLE. For each row with a nonzero name_text_id, it stores 1 in shadow[key]
@      when the row's flag is set and 0 when it is clear. A row whose name_text_id is 0 has no
@      menu slot, so it is skipped.
@   3. It sets r0 to SHADOW_BASE, so base+0x38 is the shadow.
@   4. It replays the instructions its veneer overwrote, then resumes the original function.
@ DESC_TABLE's full row layout is in custom_pickups.s. These trampolines read four of its fields:
@ the key at +0, flag_field at +2, flag_number at +4 and name_text_id at +8.
@
@ Only the two readers see the overridden base, so the use-gate's heal and the decrement of a
@ used item still work on the real counts. The decrement writes the hardcoded address 0x13294
@ rather than going through the base, so game code never writes the shadow.
@
@ Both functions stop at index 0x1F. inventory_menu.py raises that cap to menu_slot_bound() by
@ patching the immediate of each cmp that checks it, so this file leaves the cap alone.
@
@ ---- Registers ----
@ The veneer clobbers r3. Each trampoline saves r1 (the menu object) around the rebuild, and the
@ rebuild saves r4-r7 because the replayed prologue pushes the caller's r4-r7. The rebuild leaves
@ only r0, r2 and r3 changed. r0 then receives the shadow base, which is the intended change, and
@ neither function reads r2 or r3 before setting it.
@
@ ---- Install ----
@ inventory_menu.py writes an 8-byte veneer (ldr r3,[pc,#0]; bx r3; .word hook|1) over the start
@ of each function. The veneer over sub_0804B494 jumps to MenuListHook, and the veneer over
@ sub_0804B648 jumps to MenuRecountHook. MENU_LIST_HOOK_OFF and MENU_RECOUNT_HOOK_OFF in
@ inventory_menu.py are those two labels' offsets from the start of the blob, and
@ test_thumb_assembler.py fails if either constant disagrees with this file.
@
@ The shadow rebuild appears twice, once in each trampoline, because thumb_assembler.py has no
@ .macro. The two copies must stay identical apart from their labels.
@
@ No literal in the pool is an address inside this file, and every branch is local, so the bytes
@ are the same at any word-aligned link address. The pool does hold DESC_TABLE's address and
@ SHADOW_BASE, which must match CUSTOM_DESC_TABLE_GBA and SHADOW_BASE_GBA in custom_pickups.py.
@ To (re)assemble, from this directory (needs `pip install keystone-engine`; add --listing for a
@ capstone disassembly):
@   python thumb_assembler.py inventory_menu.s 0x08670500   @ -> MENU_BLOB
@ test_thumb_assembler.py fails if that blob and this file disagree.
.syntax unified
.thumb
.text
.global MenuListHook
.global MenuRecountHook

    .equ REAL_CONSUMABLES, 0x02013294   @ gEwramData+0x13294: the 32 vanilla consumable counts
    .equ SHADOW,           0x02030000   @ SHADOW_INVENTORY_GBA in custom_pickups.py
    .equ SHADOW_BASE,      SHADOW - 0x38  @ SHADOW_BASE_GBA: base+0x38 is SHADOW
    .equ GEWRAM,           0x02000000   @ gEwramData; each flag_field is an offset from it
    .equ DESC_TABLE,       0x08670400   @ CUSTOM_DESC_TABLE_GBA in custom_pickups.py
    .equ DESC_TERM,        0xFFFF       @ the key of the row that ends DESC_TABLE
    .equ DESC_STRIDE,      12           @ bytes per DESC_TABLE row
    .equ B494_RESUME,      0x0804B49C   @ sub_0804B494 + 8, past the 4 overwritten instructions
    .equ B648_RESUME,      0x0804B650   @ sub_0804B648 + 8, past the 4 overwritten instructions

@ ---- sub_0804B494, the list builder ----
@ The veneer overwrote: push {r4, r5, r6, r7, lr}; mov r7, sl; mov r6, sb; mov r5, r8
MenuListHook:
    push {r1}                    @ save the menu object, so r1 is free as scratch

    @ Rebuild the shadow (copy 1 of 2).
    push {r4, r5, r6, r7}        @ the replayed prologue pushes these, so keep the caller's values
    ldr  r2, =REAL_CONSUMABLES
    ldr  r3, =SHADOW
    movs r0, #32
.Llist_copy:                     @ shadow[0..31] = the 32 real counts
    ldrb r1, [r2]
    strb r1, [r3]
    adds r2, #1
    adds r3, #1
    subs r0, #1
    bne  .Llist_copy
    ldr  r4, =DESC_TABLE         @ r4 = current row
    ldr  r5, =SHADOW
    ldr  r6, =GEWRAM
    ldr  r7, =DESC_TERM
.Llist_row:
    ldrh r0, [r4, #0]            @ key: the item's var_b, which is also its menu slot
    cmp  r0, r7
    beq  .Llist_rebuilt          @ the terminator row ends the table
    ldrh r2, [r4, #8]            @ name_text_id
    cmp  r2, #0
    beq  .Llist_next             @ 0 means a behavior-only row with no menu slot
    ldrh r2, [r4, #2]            @ flag_field: a byte offset from gEwramData
    ldrh r3, [r4, #4]            @ flag_number
    adds r2, r6, r2              @ r2 = gEwramData + flag_field
    lsrs r1, r3, #5
    lsls r1, r1, #2              @ r1 = (flag_number >> 5) * 4, the offset of the flag's word
    adds r2, r2, r1
    ldr  r2, [r2]                @ r2 = the word that holds the flag
    movs r1, #0x1F
    ands r1, r3                  @ r1 = flag_number & 0x1F, the flag's bit in that word
    lsrs r2, r1
    movs r3, #1
    ands r2, r3                  @ r2 = 1 if the item is owned, else 0
    strb r2, [r5, r0]            @ shadow[key] = owned
.Llist_next:
    adds r4, #DESC_STRIDE
    b    .Llist_row
.Llist_rebuilt:
    pop  {r4, r5, r6, r7}

    pop  {r1}                    @ restore the menu object
    ldr  r0, =SHADOW_BASE        @ the function stores r0 at [sp] and reads base+0x38 from it
    push {r4, r5, r6, r7, lr}    @ replay the overwritten prologue
    mov  r7, sl
    mov  r6, sb
    mov  r5, r8
    ldr  r3, =(B494_RESUME + 1)  @ the function does not read r3 before setting it
    bx   r3

@ ---- sub_0804B648, the recount ----
@ The veneer overwrote: push {r4, r5, r6, lr}; mov ip, r0; movs r4, #0; movs r3, #0
MenuRecountHook:
    push {r1}                    @ save the menu object, so r1 is free as scratch

    @ Rebuild the shadow (copy 2 of 2).
    push {r4, r5, r6, r7}        @ the replayed prologue pushes these, so keep the caller's values
    ldr  r2, =REAL_CONSUMABLES
    ldr  r3, =SHADOW
    movs r0, #32
.Lrecount_copy:                  @ shadow[0..31] = the 32 real counts
    ldrb r1, [r2]
    strb r1, [r3]
    adds r2, #1
    adds r3, #1
    subs r0, #1
    bne  .Lrecount_copy
    ldr  r4, =DESC_TABLE         @ r4 = current row
    ldr  r5, =SHADOW
    ldr  r6, =GEWRAM
    ldr  r7, =DESC_TERM
.Lrecount_row:
    ldrh r0, [r4, #0]            @ key: the item's var_b, which is also its menu slot
    cmp  r0, r7
    beq  .Lrecount_rebuilt       @ the terminator row ends the table
    ldrh r2, [r4, #8]            @ name_text_id
    cmp  r2, #0
    beq  .Lrecount_next          @ 0 means a behavior-only row with no menu slot
    ldrh r2, [r4, #2]            @ flag_field: a byte offset from gEwramData
    ldrh r3, [r4, #4]            @ flag_number
    adds r2, r6, r2              @ r2 = gEwramData + flag_field
    lsrs r1, r3, #5
    lsls r1, r1, #2              @ r1 = (flag_number >> 5) * 4, the offset of the flag's word
    adds r2, r2, r1
    ldr  r2, [r2]                @ r2 = the word that holds the flag
    movs r1, #0x1F
    ands r1, r3                  @ r1 = flag_number & 0x1F, the flag's bit in that word
    lsrs r2, r1
    movs r3, #1
    ands r2, r3                  @ r2 = 1 if the item is owned, else 0
    strb r2, [r5, r0]            @ shadow[key] = owned
.Lrecount_next:
    adds r4, #DESC_STRIDE
    b    .Lrecount_row
.Lrecount_rebuilt:
    pop  {r4, r5, r6, r7}

    pop  {r1}                    @ restore the menu object
    ldr  r0, =SHADOW_BASE        @ the replayed `mov ip, r0` makes the loop read the shadow
    push {r4, r5, r6, lr}        @ replay the overwritten prologue
    mov  ip, r0
    movs r4, #0
    movs r3, #0
    @ Resume through r0 rather than r3, because r3 must stay 0. The function sets r0 next.
    ldr  r0, =(B648_RESUME + 1)
    bx   r0

    .align 2
    .pool
