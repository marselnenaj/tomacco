/* SPDX-License-Identifier: MIT
 * Real AArch64 accesses must abort on unsupported MCC operations. State and
 * adjacent RAM must survive; in particular 64-bit stores must not be split.
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
    ldr x1, =0x220002800
    ldr x2, =0x220401d48
    mov x3, #0
    ldr w4, [x1]
    ldr w5, [x2]
    ldr x0, =0x220002808
    ldr w6, [x0] /* Unimplemented AMCC register. */
    ldr x0, =0x220401d4c
    ldr w6, [x0] /* Unimplemented DCS register. */
    add x0, x1, #4
    str w3, [x0] /* Read-only status. */
    mov x0, x1
    str x3, [x0] /* Wrong width must not modify the lower word. */
    strb w3, [x0]
    strh w3, [x0]
    add x0, x1, #1
    str w3, [x0] /* Unaligned store. */
    mov x0, x1
    mov w6, #13
    str w6, [x0] /* Unsupported cache way count. */
    mov w6, #0x10
    str w6, [x0] /* Unsupported control bits. */
    mov x0, x2
    str x3, [x0] /* Wrong DCS width. */
    ldr x7, =0x22004a028
    mov w6, #37
    str w6, [x7] /* Program a nonzero group in the second cache plane. */
    mov x0, x7
    mov w6, #64
    str w6, [x0] /* The quota field is six bits. */
    str x3, [x0] /* A split 64-bit access would erase this quota. */
    strb w3, [x0]
    ldr x0, =0x22004a080
    ldr w6, [x0] /* Beyond the 32 evidenced groups. */
    ldr x8, =0x22000082c
    mov w6, #1
    str w6, [x8]
    sub x0, x8, #4
    str x3, [x0] /* Spanning store must not reach the gate's upper word. */
    mov x0, x8
    strb w3, [x0]
    ldr x0, =0x22010082c
    mov w6, #2
    str w6, [x0] /* Bad broadcast value must leave all planes intact. */
    ldr x0, =0x220102800
    mov w6, #13
    str w6, [x0]
    cmp x19, #18
    b.ne fail
    ldr w6, [x8]
    cmp w6, #1
    b.ne fail
    ldr x0, =0x22004082c
    ldr w6, [x0]
    cbnz w6, fail
    ldr w6, [x7]
    cmp w6, #37
    b.ne fail
    ldr x0, =0x22000a028
    ldr w6, [x0] /* The same group in another plane stays independent. */
    cbnz w6, fail
    ldr w6, [x1]
    cmp w6, w4
    b.ne fail
    ldr w6, [x2]
    cmp w6, w5
    b.ne fail
    stp w4, w5, [x22, #16]
    str x19, [x22, #8]
    mov x0, #0xc14e
    str x0, [x22]
    dsb sy
1:  wfe
    b 1b

handle:
    mrs x10, esr_el3
    mrs x11, far_el3
    mrs x12, elr_el3
    lsr x13, x10, #26
    cmp x13, #0x25
    b.ne fail
    cmp x11, x0
    b.ne fail
    and x13, x10, #0x3f
    /* Device memory rejects unaligned accesses architecturally before MMIO.
     * The other operations must reach the device and abort externally. */
    mov x14, #0x10
    mov x15, #0x21
    cmp x19, #6
    csel x14, x15, x14, eq
    cmp x13, x14
    b.ne fail
    add x19, x19, #1
    add x12, x12, #4
    msr elr_el3, x12
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
    b handle
    .balign 128
.rept 11
    b fail
    .balign 128
.endr
