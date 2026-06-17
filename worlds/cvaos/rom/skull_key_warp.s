@ Skull Key Warp - injected routine, linked at 0x08660100 (GBA), file 0x660100.
@ Reached from a trampoline that overwrites the first 8 bytes of sub_0804B36C.
@ On entry r0 = playerStats, r1 = menuState (live); r3 was clobbered by the trampoline.
.syntax unified
.thumb
.text
.global WarpHook

WarpHook:
    ldrb  r3, [r1, #0x17]        @ selected consumable index
    cmp   r3, #25                @ Skull Key?
    beq   DoWarp

ContinueVanilla:
    @ reconstruct the 8 displaced bytes of sub_0804B36C (0x0804B36C..0x0804B373)
    push  {r4, r5, r6, r7, lr}
    mov   r7, r8
    push  {r7}
    adds  r4, r0, #0
    ldr   r3, =(0x0804B374 + 1)  @ resume in sub_0804B36C after the displaced bytes
    bx    r3

DoWarp:
    push  {r4, r5, lr}
    @ The Skull Key is used from the pause menu, which suspends the overworld subsystems
    @ (sprites/objects/enemy spawner). The warp transfer un-pauses gameplay but does NOT resume
    @ those, so enemies stop spawning. sub_08047B44 is the game's own "resume gameplay after the
    @ menu" routine (the tail of the menu close); call it before launching so the new room spawns
    @ normally. It takes no arguments.
    ldr   r4, =(0x08047B44 + 1)
    bl    CallR4
    @ sub_08011F44(room_ptr, x, y, x_offset, y_offset)  -- 5th arg on the stack
    ldr   r4, =DestRecord
    ldr   r0, [r4, #0]           @ room_ptr
    ldrh  r1, [r4, #4]           @ x
    ldrh  r2, [r4, #6]           @ y
    ldrh  r3, [r4, #8]           @ x_offset
    ldrh  r5, [r4, #10]          @ y_offset
    push  {r5}
    ldr   r4, =(0x08011F44 + 1)
    bl    CallR4
    add   sp, #4
    @ sub_08011AD0(&gEwramData->unk_60)  (gEwramData base is constant 0x02000000)
    ldr   r0, =0x02000060
    ldr   r4, =(0x08011AD0 + 1)
    bl    CallR4
    movs  r0, #1                 @ return 1 -> caller decrements the Skull Key
    pop   {r4, r5, pc}           @ restore r4/r5 and return to caller (saved lr has thumb bit)

CallR4:
    bx    r4                     @ far-call trampoline; lr set by `bl CallR4`

    .align 2
DestRecord:
    .word  0x0850EF9C            @ room_ptr  (room 000, start corridor)
    .hword 272                   @ x
    .hword 512                   @ y
    .hword 0                     @ x_offset
    .hword 0                     @ y_offset
    .pool
