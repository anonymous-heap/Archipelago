@ Received-item announcement -- Castlevania: Aria of Sorrow.
@
@ Shows the vanilla acquisition banner and plays the vanilla pickup SFX for something the PLAYER
@ did not collect: an Archipelago item or soul sent by another world. The ROM owns the whole
@ sequence; the client only leaves a request in a mailbox. That split matters because the game's
@ banner request has a busy state (BUSY_MASK below) -- a client poking the request fields
@ directly would have its box silently dropped whenever a transition or another box was up,
@ whereas this hook sees the busy flag and retries on a later frame.
@
@ ---- How vanilla announces a pickup (verified against the USA ROM) ----
@ sub_080441F8 (0x080441F8) is the pickup collect callback, a switch on the pickup category at
@ entity[0x36]. Two of its branches matter here.
@
@ Items (categories 2, 3, 5..8) queue a textbox and play a sound:
@     sub_0800EF98(name_text_id)     @ queue the "Got <name>" textbox
@     PlaySong(0xB5)                 @ SE_181, the item-pickup sound
@ Name text-ids are the u16 table at 0x08506734 indexed by item global-id. sub_0800EF98 is only
@ two stores behind a guard:
@     if (!(gEwramData->unk_60.unk_42C & 0x03000200)) { unk_423 = 1; unk_420 = text_id; }
@ and sub_0800EFD4 (called per frame from code_0800B700) dispatches request kind 1 to the real
@ textbox, sub_0800E0E8. So queueing a box is exactly "call sub_0800EF98 when not busy".
@
@ Souls use a different renderer entirely -- not the kind-1/2/3 request machinery:
@     SoulInventory_AddAmountToSoulTotal(soulType, soulIndex, 1)
@     sub_08032DBC(1)                            @ bumps totalNbrSoulsCollected (a statistic;
@                                                @ the client does this in give_item, since it
@                                                @ belongs to granting a soul, not announcing it)
@     sub_08045C34(soulType)                     @ NEW souls only: the acquisition effect
@     sub_0800E708(soulIndex, soulType, isNew)   @ starts the soul banner
@     sub_08049E64(soulType, soulIndex)          @ queues the soul for the banner to show
@     PlaySong(0xBC)                             @ SE_188, the soul-acquired sound
@
@ sub_08045C34 is what makes a NEW soul stop the action. It creates an effect entity whose
@ update function (sub_08045654) raises gEwramData->unk_A074_0 on its first frame, and
@ sub_08000B64 ("update all entities") skips every entity update while that bit is set. That is
@ the pause the player dismisses with A. Without it, sub_0800E708's isNew path still draws the
@ blocking box and sets unk_422 = 2, but sub_0800EFD4 decrements that to 0 within two frames and
@ tears the box down, so the box flashes and nothing pauses. This hook therefore makes the call
@ for a new soul and skips it for a duplicate, exactly as vanilla does. It is deliberately the
@ only call here with world-visible side effects: everything else only writes display state.
@ Both banner calls are needed. sub_08049E64 stores soulIndex+1 into a 4-entry queue per soul
@ type (gEwramData+0x133DC/0x133E0/0x133E4, skipping one already listed); without it the banner
@ appears for a single frame and vanishes, which is what a playtest showed.
@
@ MIND THE ARGUMENT ORDER, which differs three ways: sub_0800E708 takes (index, type),
@ sub_08049E64 takes (type, index), and the SoulInventory_* functions take soulType first. r5/r6
@ in the vanilla branch pin which is which. isNew is 1 for a soul you did not already own
@ (vanilla computes it from SoulInventory_GetSoulTotal). This hook announces only: it does NOT
@ add the soul to the inventory, so whatever grants the soul stays the caller's business.
@
@ (For completeness, request kind 2 -- sub_0800EA98 -> sub_0800DE4C -- is the enemy/boss NAME
@ banner, reading the enemy-name text-id table at 0x080EA628. It is not involved here.)
@
@ ---- Mailbox (free high EWRAM, transient) ----
@ 0x0203F000 is in the documented free tail (0x02025554..0x02040000: zero-filled once at boot by
@ InitializeGame and never touched by game code), clear of this world's other claims there -- the
@ Item-Use shadow array at 0x02030000..0x0203002B and Classicvania Movement's scratch at
@ 0x0203E000. It is NOT SRAM-saved, which is correct: a pending announcement must not survive a
@ reload.
@     +0 u16 arg0      item: name text-id (0 = no box)   soul: soulIndex
@     +2 u16 sfx       PlaySong id; 0 = silent
@     +4 u8  kind      0 = item textbox, 1 = soul banner
@     +5 u8  arg1      soul: soulType                    (unused for items)
@     +6 u8  arg2      soul: isNew (0/1)                 (unused for items)
@     +7 u8  pending   client writes 1 to request; this hook writes 0 once it has fired
@
@ pending is deliberately the LAST byte, so writing the whole 8-byte block in one transfer is
@ already correctly ordered: the request cannot be seen half-filled.
@
@ Protocol: the client fills the block, then does not overwrite it while pending still reads 1.
@ One slot is deliberate -- if several items arrive at once the client queues them and feeds the
@ next when pending clears, which also paces the banners so they do not stomp each other.
@
@ ---- Install ----
@ Registered in the shared per-frame update-hook framework:
@   * dispatcher at 0x087D0000 calls vanilla 0x0804306D, then every non-zero entry of the 12-slot
@     pointer list at 0x087D0040;
@   * slot 1 (list +0x00) is Classicvania Movement's no-air-control hook, body 0x087D0100;
@   * this hook takes slot 2: list entry at 0x087D0044 = 0x087D0301, body at 0x087D0300
@     (bodies live at 0x087D0100 + 0x200*(slot-1), so 0x200 bytes are available here).
@ The dispatcher's loop keeps its list pointer and offset live in r0/r1 across the call, so a hook
@ body must preserve r0-r7 and lr exactly as the slot-1 body does; that is the push/pop pair below.
@
@ Relocatable to any WORD-ALIGNED address: the literal pool holds absolute addresses of things
@ elsewhere in the ROM, never this hook's own, so the bytes come out the same wherever it is
@ linked. (They do change at a 2-mod-4 base, because `ldr rN,[pc,#imm]` rounds the pc down to a
@ word boundary.) Unlike the trampolines here, which bake their own resume label.
@
@ To (re)assemble, from the Archipelago root (needs `pip install keystone-engine`):
@   cd worlds/cvaos/rom
@   python thumb_assembler.py received_item_box.s 0x087D0300   @ = HOOK_BODY_GBA
@ test_thumb_assembler.py asserts the blob in received_item_box.py is exactly what this file
@ assembles to, so the source and the shipped bytes cannot drift apart. arm-none-eabi-as would
@ also work but lays the literal pool out its own way, so expect different bytes from it.
.syntax unified
.thumb
.text
.global ReceivedItemBox

    .equ MAILBOX,      0x0203F000   @ MAILBOX_GBA in received_item_box.py
    .equ MB_ARG0,      0            @ u16
    .equ MB_SFX,       2            @ u16
    .equ MB_KIND,      4            @ u8
    .equ MB_ARG1,      5            @ u8
    .equ MB_ARG2,      6            @ u8
    .equ MB_PENDING,   7            @ u8

    .equ TEXTBOX_STATE, 0x0200042C  @ gEwramData + 0x42C (the unk_60 field offsets in the
                                    @ decomp's ewram.h are ABSOLUTE from gEwramData, not
                                    @ relative to unk_60: currentSaveSlot reads sane at
                                    @ 0x02000428, and the pickup-flag bitfield the RAM map
                                    @ documents at 0x02000360 is unk_60.unk_360.)
    .equ BUSY_MASK,     0x03000200  @ the bits sub_0800EF98 itself refuses to queue over
    .equ GOTITEM,       0x0800EF98  @ sub_0800EF98(text_id): queues the "Got <name>" textbox
    .equ SOULBANNER,    0x0800E708  @ sub_0800E708(soulIndex, soulType, isNew)
    .equ SOULQUEUE,     0x08049E64  @ sub_08049E64(soulType, soulIndex): queue it for display
    .equ SOULEFFECT,    0x08045C34  @ sub_08045C34(soulType): new-soul effect; freezes entities
    .equ PLAYSONG,      0x080D7910  @ PlaySong(song)

ReceivedItemBox:
    @ Slot ABI: preserve r0-r7 (the dispatcher's loop still needs r0/r1) and lr.
    push {r0-r7}
    push {lr}

    ldr  r4, =MAILBOX
    ldrb r0, [r4, #MB_PENDING]
    cmp  r0, #0
    beq  .Ldone                     @ nothing requested

    @ Is the game able to take a banner request this frame? sub_0800EF98 drops the request
    @ silently when these bits are set, so check them here and keep pending set to retry. The
    @ soul banner is gated on the same bits: sub_0800E708's body is not decompiled, so waiting
    @ for a quiet frame is the conservative choice rather than firing during a transition.
    ldr  r0, =TEXTBOX_STATE
    ldr  r0, [r0]
    ldr  r1, =BUSY_MASK
    tst  r0, r1
    bne  .Ldone

    @ r4 survives every call below: these are ordinary AAPCS functions, so they preserve r4-r11.
    ldrb r0, [r4, #MB_KIND]
    cmp  r0, #0
    bne  .Lsoul

    @ kind 0 -- item: sub_0800EF98(name_text_id). 0 means "sound only".
    ldrh r0, [r4, #MB_ARG0]
    cmp  r0, #0
    beq  .Lsfx
    ldr  r3, =(GOTITEM + 1)
    bl   .Lcall_r3
    b    .Lsfx

.Lsoul:
    @ kind 1 -- soul. A new soul gets vanilla's acquisition effect first: it freezes the entity
    @ update loop, which is what holds the box up until the player presses A.
    ldrb r0, [r4, #MB_ARG2]         @ isNew
    cmp  r0, #0
    beq  .Lsoul_banner
    ldrb r0, [r4, #MB_ARG1]         @ soulType
    ldr  r3, =(SOULEFFECT + 1)
    bl   .Lcall_r3

.Lsoul_banner:
    @ sub_0800E708(soulIndex, soulType, isNew). soulIndex 0 is valid, so no zero check here.
    ldrh r0, [r4, #MB_ARG0]         @ soulIndex
    ldrb r1, [r4, #MB_ARG1]         @ soulType
    ldrb r2, [r4, #MB_ARG2]         @ isNew
    ldr  r3, =(SOULBANNER + 1)
    bl   .Lcall_r3

    @ Queue it so the banner has something to display. Note the reversed argument order.
    ldrb r0, [r4, #MB_ARG1]         @ soulType
    ldrh r1, [r4, #MB_ARG0]         @ soulIndex
    ldr  r3, =(SOULQUEUE + 1)
    bl   .Lcall_r3

.Lsfx:
    ldrh r0, [r4, #MB_SFX]
    cmp  r0, #0
    beq  .Lconsume                  @ 0 = silent
    ldr  r3, =(PLAYSONG + 1)
    bl   .Lcall_r3

.Lconsume:
    @ Fired: release the slot so the client may post the next announcement.
    movs r0, #0
    strb r0, [r4, #MB_PENDING]

.Ldone:
    pop  {r1}
    mov  lr, r1
    pop  {r0-r7}
    bx   lr

.Lcall_r3:
    bx   r3                         @ far-call veneer; lr set by `bl .Lcall_r3`

    .align 2
    .pool
