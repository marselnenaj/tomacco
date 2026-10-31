/* SPDX-License-Identifier: MIT
 * Firmware-free, actual EL2 read/write external aborts on an unmapped bus.
 * A diagnostic trace must never turn these faults into successful accesses.
 */
.global _start
.text
_start:
    msr daifset, #15
    adr x0, vectors
    msr vbar_el2, x0
    isb
    mov x19, #0
    mov x22, #0x50000
    ldr x0, =0x20001000
read_fault:
    ldr w1, [x0]
    ldr x0, =0x20002000
write_fault:
    str x1, [x0]
    cmp x19, #2
    b.ne fail
    str x19, [x22, #8]
    mov x0, #0xc14e
    str x0, [x22]
    dsb sy
1:  wfe
    b 1b

handle:
    mrs x10, esr_el2
    mrs x11, far_el2
    mrs x12, elr_el2
    lsr x13, x10, #26
    cmp x13, #0x25 /* Data abort at the current EL. */
    b.ne fail
    cmp x11, x0
    b.ne fail
    and x13, x10, #0x7f
    cbnz x19, write_check
    cmp x13, #0x10 /* Synchronous external abort, read. */
    b.ne fail
    adr x14, read_fault
    cmp x12, x14
    b.ne fail
    stp x10, x11, [x22, #16]
    str x12, [x22, #48]
    b return_from_fault
write_check:
    cmp x19, #1
    b.ne fail
    cmp x13, #0x50 /* Synchronous external abort, write. */
    b.ne fail
    adr x14, write_fault
    cmp x12, x14
    b.ne fail
    stp x10, x11, [x22, #32]
    str x12, [x22, #56]
return_from_fault:
    add x19, x19, #1
    add x12, x12, #4
    msr elr_el2, x12
    eret
fail:
    mov x22, #0x50000
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
    b handle /* Current EL, SPx, synchronous. */
    .balign 128
.rept 11
    b fail
    .balign 128
.endr
