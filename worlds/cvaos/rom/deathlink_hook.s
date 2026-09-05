@ DeathLink "real kill" ROM hook for Castlevania: Aria of Sorrow (USA).
@ Design rationale is inline below; deathlink_hook.py embeds the assembled bytes.
@
@ SOURCE of truth for the trampoline bytes embedded in deathlink_hook.py. Assembled at DEV time
@ (not at AP generation time). To regenerate after editing:
@   python thumb_assembler.py deathlink_hook.s 0x08670040   @ -> _TRAMPOLINE (from this directory;
@                                                            @ needs pip install keystone-engine)
@
@ WHERE WE HOOK (and why not the obvious spot):
@   AoS has no per-frame "if HP<=0 die" check. Death is driven by the damage routine: the player
@   update sub_0801B0D8 only runs its HP<=0 -> bl sub_0801AF20 block when the "damage active" guard
@   gEwramData[0x131D6] != 0. So hooking that block only fires the kill when Soma takes damage (the
@   bug we hit first). Instead we hook _0801B9D0 (0x0801B9D0) -- the per-frame position/animation
@   tail that runs every normal frame (the guard's else-branch `b _0801B9D0`) and is exactly where
@   the engine's own death path lands after `bl sub_0801AF20`. There r6 = player. If the client's
@   kill-request flag is set we call the real death handler, then fall through into the tail, which
@   reads the death animation index (entity+0xa = 0x10, set by the handler) and plays it -- i.e. the
@   identical flow a damage death takes. When the flag is clear we reproduce the stolen prologue of
@   _0801B9D0 and resume, so normal play (and normal damage deaths, which also pass through here) is
@   untouched.

        .syntax unified
        .arch   armv4t
        .thumb

        .equ    KILL_REQUEST,  0x0201324C   @ EWRAM kill-request flag (pad_1324C, verified dead)
        .equ    DEATH_HANDLER, 0x0801AF21   @ sub_0801AF20 | thumb bit
        .equ    GEWRAMDATA,    0x084F0B14   @ ROM word holding the EWRAM struct base (=gEwramData)
        .equ    STATUS_OFF,    0x000131B8   @ gEwramData player status bitfield (used by the tail)
        .equ    RESUME,        0x0801B9D9   @ _0801B9D0 + 8 (after the 4 stolen insns) | thumb bit

        .global deathlink_tramp
        .thumb_func
deathlink_tramp:                    @ r6 = player entity; entered every frame at _0801B9D0
        ldr     r1, =KILL_REQUEST
        ldrb    r3, [r1]
        cmp     r3, #0
        beq     .Lresume            @ no request -> just run the tail
        adds    r3, r6, #0          @ already dying? (entity state byte +0x6d == 0x33)
        adds    r3, #0x6d
        ldrb    r3, [r3]
        cmp     r3, #0x33
        beq     .Lresume
        movs    r3, #0
        strb    r3, [r1]            @ one-shot: clear the request
        adds    r0, r6, #0          @ r0 = player entity (death-handler arg)
        ldr     r3, =DEATH_HANDLER
        ldr     r1, =(.Lresume + 1) @ THUMB return addr after the call
        mov     lr, r1
        bx      r3                  @ call sub_0801AF20(player) (ARMv4T: manual LR + BX)
.Lresume:
        @ reproduce the 4 stolen insns of _0801B9D0:
        @   ldr r5,=gEwramData ; ldr r0,[r5] ; ldr r4,=0x131B8 ; adds r0,r0,r4
        ldr     r5, =GEWRAMDATA
        ldr     r0, [r5]
        ldr     r4, =STATUS_OFF
        adds    r0, r0, r4
        ldr     r1, =RESUME
        bx      r1                  @ continue into the _0801B9D0 tail at +8
        .ltorg
