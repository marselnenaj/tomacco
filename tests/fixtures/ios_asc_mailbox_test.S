/* SPDX-License-Identifier: MIT
 * Original ARM guest: two MMIO views, message pairs, actual EL1 IRQ delivery,
 * backpressure and precise aborts. No Apple instructions or OS claims.
 */
.global _start
.text
_start:
    msr daifset, #15
    adr x0, vectors
    msr vbar_el1, x0
    isb
    mov x19, #0
    mov x20, #0
    ldr x22, =0x50000
    mrs x0, CurrentEL
    str x0, [x22, #24]
    ldr x23, =0x300e04000
    ldr x24, =0x300e08000

    /* QTest publishes this A2I message through the AP register view. */
    add x0, x23, #0x810
    ldp x1, x2, [x0]
    ldr x3, =0x123456789abcdef0
    cmp x1, x3
    b.ne fail
    mov w4, #0x1234
    cmp w2, w4
    b.ne fail
    str x1, [x22, #32]
    add x1, x1, #1
    mov x2, #0x4321
    add x0, x23, #0x820
    stp x1, x2, [x0]
    dsb sy
    msr daifclr, #2
    isb
wait_irq:
    cmp x20, #1
    b.eq got_irq
    wfi
    b wait_irq
got_irq:
    msr daifset, #2
    /* IRQ handler consumed the I2A reply through the other view. */
    ldr w1, [x24, #0x114]
    tbz w1, #17, fail

    mov x1, #9
    mov x2, #42
    add x0, x23, #0x820
    stp x1, x2, [x0]
    add x0, x24, #0x810
    ldr x1, [x0]                 /* 1: empty A2I read */
    add x0, x24, #0x830
    ldr w1, [x0]                 /* 2: partial I2A read */
    add x0, x24, #0x800
    strb w1, [x0]                /* 3: partial data write */
    add x0, x24, #0x830
    str x1, [x0]                 /* 4: receive register is read-only */
    add x0, x24, #0x110
    ldr x1, [x0]                 /* 5: spanning status read */
    mov x1, #1
    str w1, [x0]                 /* 6: unsupported control write */
    add x0, x24, #0x808
    str x1, [x0]                 /* 7: commit without data */
    ldr x0, =0x300e00040
    ldr w1, [x0]                 /* 8: controller configuration unmapped */
    mov x1, #0x9988
    str x1, [x24, #0x800]
    mov x1, #0x100000000
    add x0, x24, #0x808
    str x1, [x0]                 /* 9: unqualified upper metadata bits */
    /* The aborted reads/writes must not consume the pending reply. */
    add x0, x24, #0x830
    ldp x1, x2, [x0]
    cmp x1, #9
    b.ne fail
    cmp w2, #42
    b.ne fail
    /* All eight entries must survive an attempted ninth commit. */
    mov x3, #0
fill:
    add x2, x3, #31
    add x0, x24, #0x800
    stp x3, x2, [x0]
    add x3, x3, #1
    cmp x3, #8
    b.ne fill
    ldr w1, [x24, #0x110]
    tbz w1, #16, fail
    mov x1, #0xfeed
    str x1, [x24, #0x800]
    add x0, x24, #0x808
    mov x1, #0xaa
    str x1, [x0]                 /* 10: full queue, staging retained */
    mov x3, #0
drain:
    add x0, x23, #0x810
    ldp x1, x2, [x0]
    cmp x1, x3
    b.ne fail
    add x4, x3, #31
    cmp w2, w4
    b.ne fail
    add x3, x3, #1
    cmp x3, #8
    b.ne drain
    mov x1, #0xaa
    str x1, [x24, #0x808]
    add x0, x23, #0x810
    ldp x1, x2, [x0]
    mov x4, #0xfeed
    cmp x1, x4
    b.ne fail
    cmp w2, #0xaa
    b.ne fail
    cmp x19, #10
    b.ne fail
    mov x0, #0xa5c0
    b finish

sync_handler:
    mrs x10, esr_el1
    mrs x11, far_el1
    mrs x12, elr_el1
    lsr x13, x10, #26
    cmp x13, #0x25
    b.ne fail
    cmp x11, x0
    b.ne fail
    and x13, x10, #0x3f
    cmp x13, #0x10
    b.ne fail
    cmp x19, #10
    b.hs fail
    mov x13, #24
    madd x13, x19, x13, x22
    stp x10, x11, [x13, #128]
    str x12, [x13, #144]
    add x19, x19, #1
    add x12, x12, #4
    msr elr_el1, x12
    eret

irq_handler:
    add x10, x24, #0x830
    ldp x11, x12, [x10]
    ldr x13, =0x123456789abcdef1
    cmp x11, x13
    b.ne fail
    mov w13, #0x4321
    cmp w12, w13
    b.ne fail
    str x11, [x22, #40]
    add x20, x20, #1
    cmp x20, #1
    b.ne fail
    dsb sy
    eret
fail:
    msr daifset, #15
    mov x0, #0xbad
finish:
    stp x19, x20, [x22, #8]
    str x0, [x22]
    dsb sy
1:  wfe
    b 1b
    .ltorg

.org 0x1000
.balign 2048
vectors:
.rept 4
    b fail
    .balign 128
.endr
    b sync_handler
    .balign 128
    b irq_handler
    .balign 128
.rept 10
    b fail
    .balign 128
.endr
