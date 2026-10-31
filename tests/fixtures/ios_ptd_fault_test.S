/* SPDX-License-Identifier: MIT
 * Original guest: timestamps use CNTPCT, LDP consumes a message, and malformed
 * accesses must fault without partially updating or acknowledging the entry.
 */
.global _start
.text
_start:
    msr daifset, #15
    adr x0, vectors
    msr vbar_el3, x0
    isb
    mov x19, #0
    mov x22, #0x50000
    ldr x16, =0x3003d0d88 /* Entry 433 update. */
    ldr x17, =0x3003c1b10 /* Entry 433 dashboard. */
    mov x3, #0xbeef
    str x3, [x16]
    isb
    mrs x4, cntpct_el0
    str xzr, [x16] /* Zero is a message; overwrites unread data. */
    dsb sy
    isb
    mrs x5, cntpct_el0
    add x0, x17, #8
    ldr w6, [x0]
    ldrh w6, [x0]
    mov x0, x17
    str x3, [x0]
    mov x0, x16
    str w3, [x0]
    strb w3, [x0]
    ldr x6, [x0]
    ldr x0, =0x3003c2000
    ldr x6, [x0]
    ldr x0, =0x3003d8040
    ldr w6, [x0]
    ldr x0, =0x3003d8000
    str w3, [x0]
    ldr x0, =0x3003d8400
    str x3, [x0]
    mov w6, #0xe0
    str w6, [x0]
    ldr x0, =0x3003d8500
    str w3, [x0]
    ldr x0, =0x3003d8200
    ldr w6, [x0]
    add x0, x16, #1
    str w3, [x0]
    add x0, x17, #4
    ldr x6, [x0]
    cmp x19, #15
    b.ne fail
    ldr x0, =0x3003d8034
    ldr w6, [x0]
    mov w7, #0x20000
    cmp w6, w7 /* Pending remains set despite invalid reads. */
    b.ne fail
    ldp x6, x7, [x17]
    cbnz x6, fail
    and x8, x7, #3
    cmp x8, #3
    b.ne fail
    lsr x7, x7, #10
    cmp x7, x4
    b.lo fail
    cmp x7, x5
    b.hi fail
    ldr w6, [x0]
    cbnz w6, fail
    ldp x6, x7, [x17]
    tst x7, #3
    b.ne fail
    ldr x0, =0x3003d8400
    ldr w6, [x0]
    cbnz w6, fail
    mov x0, #1
    stp x8, x0, [x22, #16]
    str x19, [x22, #8]
    mov x0, #0x5054
    str x0, [x22]
    dsb sy
1:  wfe
    b 1b

handle:
    mrs x10, esr_el3
    mrs x11, far_el3
    mrs x12, elr_el3
    stp x10, x11, [x22, #32]
    lsr x13, x10, #26
    cmp x13, #0x25
    b.ne fail
    cmp x11, x0
    b.ne fail
    and x13, x10, #0x3f
    mov x14, #0x10
    mov x15, #0x21
    cmp x19, #13
    csel x14, x15, x14, hs
    cmp x13, x14
    b.ne fail
    add x19, x19, #1
    add x12, x12, #4
    msr elr_el3, x12
    eret
fail:
    str x19, [x22, #8]
    mov x0, #0xbad
    str x0, [x22]
    dsb sy
2:  wfe
    b 2b
    .ltorg

.org 0x1000
.balign 2048
vectors:
.rept 4
    b fail
    .balign 128
.endr
    b handle
    .balign 128
.rept 11
    b fail
    .balign 128
.endr
