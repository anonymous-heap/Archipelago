@ Guaranteed soul drops for Castlevania: Aria of Sorrow (USA): the Ancient Book soul-drop hook.
@ soul_guarantee_hook.py embeds the assembled bytes; this file is the source of truth for them.
@ Assembled at DEV time (not at AP generation time). To regenerate after editing, from this
@ directory (needs `pip install keystone-engine`; add --listing for a capstone disassembly):
@   python thumb_assembler.py soul_guarantee_hook.s 0x08670600   @ -> _TRAMPOLINE
@ test_thumb_assembler.py holds that blob and this file together, so neither can drift.
@
@ WHERE WE HOOK:
@   The enemy-death routine decides a soul drop at _080684C8..0x080684F0: it rolls
@   (Random() >> 2) mod max(16, rate*8 + 32 - LCK/16) and drops when the roll is below the
@   numerator (3 owned / 6 new / 7 on Hard, +8 with the Soul Eater Ring). A rate byte of 0 skips
@   the roll entirely (that is how bosses avoid a second, generic drop), so no table value can
@   force a drop; only the comparison can. We steal the 10 bytes at 0x080684EE
@   (cmp r4,r5 ; bhs nodrop ; ldrb r2,[r6,#0x17] ; ldrb r3,[r6,#0x18] ; subs r3,#1) for a far
@   jump here. On entry r4 = roll, r5 = numerator, r6 = the dying enemy's table row (ROM), and
@   r0..r3 are dead.
@
@ WHAT WE DO:
@   Each Ancient Book describes one true-ending soul: Book 1 the Flame Demon (red, index 44),
@   Book 2 the Giant Bat (blue, index 2), Book 3 the Succubus (yellow, index 7). If the dying
@   enemy's soul is one of those AND the matching book is in the inventory (its count byte in
@   the consumable array, items 26..28, is nonzero), the roll is forced to 0, which is below
@   every numerator. Matching on the soul rather than the enemy means Soul Shuffle needs no
@   special handling: whichever enemy carries the soul now is the one that always drops it.
@   Everything is read from the game's own memory, so no client is involved and the behaviour
@   is the same in any emulator. The stolen instructions then run and control returns to the
@   drop path (0x080684F8) or the no-drop path (0x0806852C) exactly as the original code would.

        .syntax unified
        .arch   armv4t
        .thumb

        .equ    BOOKS,          0x020132AE   @ consumable counts of Ancient Book 1, 2, 3 (items 26..28)
        .equ    DROP_RESUME,    0x080684F9   @ movs r0,#0 ... bl sub_0804459C (spawn the soul) | thumb
        .equ    NODROP_RESUME,  0x0806852D   @ _0806852C: item-drop logic | thumb

        .global guarantee_tramp
        .thumb_func
guarantee_tramp:                    @ r4 = roll, r5 = numerator, r6 = enemy table row
        ldrb    r2, [r6, #0x17]     @ stolen: soul type (0 red, 1 blue, 2 yellow, 3 ability)
        ldrb    r3, [r6, #0x18]     @ stolen: soul index (1-based)
        ldr     r0, =BOOKS
        cmp     r2, #0              @ Book 1: Flame Demon = red 44
        bne     .Lbook2
        cmp     r3, #44
        bne     .Lbook2
        ldrb    r1, [r0, #0]
        cmp     r1, #0
        bne     .Lforce
.Lbook2:
        cmp     r2, #1              @ Book 2: Giant Bat = blue 2
        bne     .Lbook3
        cmp     r3, #2
        bne     .Lbook3
        ldrb    r1, [r0, #1]
        cmp     r1, #0
        bne     .Lforce
.Lbook3:
        cmp     r2, #2              @ Book 3: Succubus = yellow 7
        bne     .Lroll
        cmp     r3, #7
        bne     .Lroll
        ldrb    r1, [r0, #2]
        cmp     r1, #0
        beq     .Lroll
.Lforce:
        movs    r4, #0              @ a roll of 0 is below every numerator (>= 3): always drop
.Lroll:
        cmp     r4, r5              @ stolen comparison
        bhs     .Lnodrop
        subs    r3, #1              @ stolen: 0-based soul index for the spawn call
        ldr     r0, =DROP_RESUME
        bx      r0
.Lnodrop:
        ldr     r0, =NODROP_RESUME
        bx      r0
        .ltorg
