@ Item-Use menu extension -- Castlevania: Aria of Sorrow.
@
@ Lets the pause "Item Use" subscreen show extra "key items" (var_b 32..46) beyond the 32 vanilla
@ consumables, without touching the fixed 32-byte itemInventory[0x20] @ gEwramData+0x13294 (which is
@ immediately followed by weaponInventory, so index 32 would corrupt weapons).
@
@ Mechanism (verified vs cvaos-decomp + USA ROM):
@   * The menu list builder sub_0804B494 (0x0804B494) and recount sub_0804B648 (0x0804B648) read the
@     consumable counts as base+0x38 (base passed in r0 = gEwramData+0x1325C, so base+0x38 = 0x13294),
@     iterating index 0..0x1F and showing any row with count!=0. Per row they read item-table
@     (0x08505B3C), name (0x08506734) and the count digit; the use-gate sub_0804B36C treats item-table
@     +8 >= 4 as a non-usable key item.
@   * We keep the real 32 consumables at 0x13294 and store the new key items' "owned" bytes in the
@     unused, SRAM-saved pad_133A0 (gEwramData+0x133A0, 76 bytes inside the 0x190 player struct). The
@     menu is pointed at a 47-slot shadow at 0x133A0: [0..31] mirror the real counts, [32..46] are the
@     new items. base+0x38 == 0x133A0 requires base = gEwramData+0x13368.
@
@ Each trampoline (installed over the first 8 bytes of sub_0804B494 / sub_0804B648):
@   1. copies the 32 real consumable counts 0x13294..0x132B3 -> 0x133A0..0x133BF (so used potions etc.
@      refresh on every rebuild -- this runs on every menu (re)build / recount);
@   2. overrides the base pointer (r0) to gEwramData+0x13368 so the loop reads the 0x133A0 shadow;
@   3. replays the stolen prologue and resumes the original function.
@ The caller's other uses of the real base (e.g. the use-gate's HP/MP heal, the decrement at 0x13294)
@ are untouched -- only the list/recount readers are redirected.
@
@ The bound (cmp #0x1f) is bumped to #0x2e separately by inventory_menu.py (10 single-byte edits).
@
@ Position-DEPENDENT (literal pool bakes absolute addresses). To (re)assemble:
@   arm-none-eabi-as -mcpu=arm7tdmi -mthumb inventory_menu.s -o /tmp/m.o
@   arm-none-eabi-ld -Ttext=0x08660500 /tmp/m.o -o /tmp/m.elf
@   arm-none-eabi-objcopy -O binary /tmp/m.elf /tmp/m.bin   # -> INVENTORY_MENU_BLOB
.syntax unified
.thumb
.text
.global MenuListHook
.global MenuRecountHook

    .equ REAL_CONSUMABLES,  0x02013294   @ gEwramData + 0x13294 (itemInventory[0])
    .equ SHADOW,            0x020133A0   @ gEwramData + 0x133A0 (pad_133A0, the 47-slot menu array)
    .equ SHADOW_BASE,       0x02013368   @ gEwramData + 0x13368 (so base+0x38 == SHADOW)
    .equ B494_RESUME,       0x0804B49C   @ sub_0804B494 + 8 (after 4 stolen insns)
    .equ B648_RESUME,       0x0804B650   @ sub_0804B648 + 8

@ ---- sub_0804B494 list builder: stolen prologue = push {r4,r5,r6,r7,lr}; mov r7,sl; mov r6,sb; mov r5,r8
MenuListHook:
    @ r0 = base (real), r1 = menuobj. Sync the 32 real counts into the shadow's [0..31].
    push {r1}                    @ free up r1 as a copy temp (menuobj preserved)
    ldr  r2, =REAL_CONSUMABLES
    ldr  r3, =SHADOW
    movs r0, #32
.LcopyA:
    ldrb r1, [r2]
    strb r1, [r3]
    adds r2, #1
    adds r3, #1
    subs r0, #1
    bne  .LcopyA
    pop  {r1}                    @ restore menuobj
    ldr  r0, =SHADOW_BASE        @ override base so base+0x38 = SHADOW
    @ replay stolen prologue, then resume (r0 flows into the original `str r0,[sp]`):
    push {r4, r5, r6, r7, lr}
    mov  r7, sl
    mov  r6, sb
    mov  r5, r8
    ldr  r3, =(B494_RESUME + 1)  @ r3 dead at this point; not read at the resume site
    bx   r3

@ ---- sub_0804B648 recount: stolen prologue = push {r4,r5,r6,lr}; mov ip,r0; movs r4,#0; movs r3,#0
MenuRecountHook:
    push {r1}
    ldr  r2, =REAL_CONSUMABLES
    ldr  r3, =SHADOW
    movs r0, #32
.LcopyB:
    ldrb r1, [r2]
    strb r1, [r3]
    adds r2, #1
    adds r3, #1
    subs r0, #1
    bne  .LcopyB
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
