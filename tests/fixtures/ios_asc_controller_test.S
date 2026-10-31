/* SPDX-License-Identifier: MIT
 * Original ARM EL1 guest. Actual KIC IRQ/FIQ delivery, architectural timers,
 * message transport and precise aborts; no Apple instructions or boot claims.
 */
.global _start
.text
_start:
    msr daifset, #15
    adr x0, vectors
    msr vbar_el1, x0
    isb
    ldr x19, =0x300e00000
    ldr x20, =0x1100000
    mov x21, #0                  /* IRQ count */
    mov x22, #0                  /* FIQ count */
    mov x23, #0                  /* abort count */
    mrs x0, CurrentEL
    str x0, [x20, #32]
    ldr w0, =0xa0401
    str w0, [x19, #0x40]
    mov w0, #1
    str w0, [x19, #0x80c]
    str w0, [x19, #0xc88]

    mov x24, #1
    mov w0, #0x20
    str w0, [x19, #0x904]       /* source 37, pending while masked */
    str w0, [x19, #0xa84]
    msr daifclr, #2
1:  cmp x21, #1
    b.eq 2f
    wfi
    b 1b
2:  msr daifset, #2
    ldr w0, [x19, #0x818]
    cbnz w0, fail              /* event read automatically masked it */
    mov x24, #2
    mov w0, #0x20
    str w0, [x19, #0xa84]      /* still pending; rearm */
    msr daifclr, #2
1:  cmp x21, #2
    b.eq 2f
    wfi
    b 1b
2:  msr daifset, #2

    mov x24, #3
    mov w0, #0x80000000
    str w0, [x19, #0x93c]      /* highest external source, FIQ */
    str w0, [x19, #0xbbc]
    msr daifclr, #1
1:  cmp x22, #1
    b.eq 2f
    wfi
    b 1b
2:  msr daifset, #1

    mov x24, #4
    add x1, x19, #0x4000
    mov w0, #1
    str w0, [x1, #0x110]
    mov w0, #0x10
    str w0, [x1, #0x108]       /* A2I nonempty event */
    msr daifclr, #2
1:  cmp x21, #3
    b.eq 2f
    wfi
    b 1b
2:  msr daifset, #2

    mov x24, #5
    mov w0, #0xa
    str w0, [x19, #0x1000]
    mov w0, #2
    str w0, [x19, #0x1018]
    mov x0, #24000
    msr cntp_tval_el0, x0
    mov x0, #1
    msr cntp_ctl_el0, x0
    isb
    msr daifclr, #2
1:  cmp x21, #4
    b.eq 2f
    wfi
    b 1b
2:  msr daifset, #2

    mov x24, #6
    str wzr, [x19, #0x1004]    /* virtual timer routes to FIQ */
    mov w0, #4
    str w0, [x19, #0x101c]
    mov x0, #24000
    msr cntv_tval_el0, x0
    mov x0, #1
    msr cntv_ctl_el0, x0
    isb
    msr daifclr, #1
1:  cmp x22, #2
    b.eq 2f
    wfi
    b 1b
2:  msr daifset, #1

    mov x24, #7
    add x0, x19, #0x44
    ldr w1, [x0]              /* unknown controller register */
    add x0, x19, #0x40
    mov w1, #2
    str w1, [x0]              /* unsupported configuration bits */
    add x0, x19, #0x4000
    add x0, x0, #0x110
    ldr x1, [x0]              /* status has strict 32-bit width */
    ldr w0, [x19, #0x40]
    ldr w1, =0xa0401
    cmp w0, w1
    b.ne fail
    cmp x23, #3
    b.ne fail
    mov x0, #0xa5cc
    b finish

irq_handler:
    ldr w9, [x19, #0x818]
    add x10, x20, #64
    str x9, [x10, x24, lsl #3]
    add x21, x21, #1
    cmp x24, #2
    b.hi irq_other
    ldr w10, =0x10025
    cmp w9, w10
    b.ne fail
    cmp x24, #2
    b.ne irq_done
    mov w10, #0x20
    str w10, [x19, #0x984]
    b irq_done
irq_other:
    cmp x24, #4
    b.ne irq_timer
    ldr w10, =0x40001
    cmp w9, w10
    b.ne fail
    add x10, x19, #0x4000
    add x10, x10, #0x810
    ldp x11, x12, [x10]
    ldr x13, =0x123456789abcdef0
    cmp x11, x13
    b.ne fail
    mov w13, #0x1234
    cmp w12, w13
    b.ne fail
    add x11, x11, #1
    mov x12, #0x4321
    add x10, x10, #0x10
    stp x11, x12, [x10]
    b irq_done
irq_timer:
    cmp x24, #5
    b.ne fail
    mov w10, #0x70000
    cmp w9, w10
    b.ne fail
    msr cntp_ctl_el0, xzr
irq_done:
    dsb sy
    isb
    eret

fiq_handler:
    ldr w9, [x19, #0x820]
    add x10, x20, #64
    str x9, [x10, x24, lsl #3]
    add x22, x22, #1
    cmp x24, #3
    b.ne fiq_timer
    ldr w10, =0x101ff
    cmp w9, w10
    b.ne fail
    mov w10, #0x80000000
    str w10, [x19, #0x9bc]
    b fiq_done
fiq_timer:
    cmp x24, #6
    b.ne fail
    ldr w10, =0x70008
    cmp w9, w10
    b.ne fail
    msr cntv_ctl_el0, xzr
fiq_done:
    dsb sy
    isb
    eret

sync_handler:
    mrs x9, esr_el1
    mrs x10, far_el1
    mrs x11, elr_el1
    lsr x12, x9, #26
    cmp x12, #0x25
    b.ne fail
    and x12, x9, #0x3f
    cmp x12, #0x10
    b.ne fail
    cmp x10, x0
    b.ne fail
    cmp x23, #3
    b.hs fail
    mov x12, #24
    madd x12, x23, x12, x20
    stp x9, x10, [x12, #128]
    str x11, [x12, #144]
    add x23, x23, #1
    add x11, x11, #4
    msr elr_el1, x11
    eret
fail:
    msr daifset, #15
    mov x0, #0xbad
finish:
    stp x21, x22, [x20, #8]
    str x23, [x20, #24]
    str x24, [x20, #40]
    str x0, [x20]
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
    b fiq_handler
    .balign 128
.rept 9
    b fail
    .balign 128
.endr
