/* SPDX-License-Identifier: MIT
 * Original firmware-free guest. Qualifies the isolated machine's RAM, EL1,
 * counter and external abort behavior, never an Apple controller or OS boot.
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
    ldr x22, =0x1100000
    mrs x0, CurrentEL
    str x0, [x22, #16]
    mrs x0, cntfrq_el0
    str x0, [x22, #24]
    mrs x0, midr_el1
    str x0, [x22, #32]
    mrs x0, mpidr_el1
    str x0, [x22, #232]
    ldr x0, =0x1000000
    ldr x1, [x0]
    str x1, [x0]
    ldr x2, [x0]
    cmp x1, x2
    b.ne fail
    str x2, [x22, #40]
    ldr x0, =0x11ffff8
    ldr x1, =0xfedcba9876543210
    str x1, [x0]
    ldr x2, [x0]
    cmp x1, x2
    b.ne fail
    str x2, [x22, #48]
    isb
    mrs x1, cntpct_el0
    mov x0, #1000
1:  subs x0, x0, #1
    b.ne 1b
    isb
    mrs x2, cntpct_el0
    cmp x2, x1
    b.ls fail
    mov x1, #1
    str x1, [x22, #56]

    ldr x0, =0xfffff8
    ldr x1, [x0]
    ldr x0, =0x1200000
    ldr x1, [x0]
    ldr x0, =0x300e00040
    ldr w1, [x0]
    ldr x0, =0x300e00400
    str w1, [x0]
    cmp x19, #4
    b.ne fail
    /* Unimplemented registers must execute a real UNDEF, not silently leave
     * a stale destination value or ignore the write. */
    ldr x5, =0xfedcba9876543210
    mrs x5, S3_7_C15_C4_6
    str x5, [x22, #192]
    mov x5, #7
    msr S3_5_C15_C7_0, x5
    mrs x5, cntfrq_el0
    str x5, [x22, #200]
    cmp x20, #2
    b.ne fail

    /* Actual translation-cache invalidation through the PMP-specific alias.
     * Identity-map 2 MiB, then remap one populated virtual translation. */
    ldr x0, =0x1108000
    ldr x1, =0x1109003
    str x1, [x0]
    ldr x0, =0x1109000
    ldr x1, =0x110a003
    str x1, [x0, #64]
    ldr x0, =0x110a000
    ldr x1, =0x1000703
    mov x2, #512
3:  str x1, [x0], #8
    add x1, x1, #0x1000
    subs x2, x2, #1
    b.ne 3b
    ldr x23, =0x110a818
    ldr x1, =0x1101703
    str x1, [x23]
    ldr x0, =0x1101000
    mov x1, #0x1111
    str x1, [x0]
    mov x1, #0x2222
    str x1, [x0, #4096]
    mov x0, #0xff
    msr mair_el1, x0
    ldr x0, =0x1108000
    msr ttbr0_el1, x0
    ldr x0, =0x803519
    msr tcr_el1, x0
    dsb sy
    isb
    mrs x0, sctlr_el1
    orr x0, x0, #1
    msr sctlr_el1, x0
    isb
    ldr x24, =0x1103000
    ldr x1, [x24]
    str x1, [x22, #216]
    ldr x0, =0x1102703
    str x0, [x23]
    dsb sy
    tlbi alle3
    isb
    ldr x1, [x24]
    str x1, [x22, #224]
    mov x0, #0x2222
    cmp x1, x0
    b.ne fail
    mov x0, #0xc14e
    b finish

handle:
    mrs x10, esr_el1
    mrs x11, far_el1
    mrs x12, elr_el1
    lsr x13, x10, #26
    cbz x13, sysreg_fault
    cmp x13, #0x25
    b.ne fail
    cmp x11, x0
    b.ne fail
    and x13, x10, #0x7f
    cmp x19, #3
    b.hi fail
    b.eq write_check
    cmp x13, #0x10
    b.ne fail
    b record
write_check:
    cmp x13, #0x50
    b.ne fail
record:
    mov x13, #24
    madd x13, x19, x13, x22
    stp x10, x11, [x13, #64]
    str x12, [x13, #80]
    add x19, x19, #1
    add x12, x12, #4
    msr elr_el1, x12
    eret
sysreg_fault:
    cmp x20, #2
    b.hs fail
    mov x13, #16
    madd x13, x20, x13, x22
    stp x10, x12, [x13, #160]
    add x20, x20, #1
    add x12, x12, #4
    msr elr_el1, x12
    eret
fail:
    mov x0, #0xbad
finish:
    str x20, [x22, #208]
    str x19, [x22, #8]
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
