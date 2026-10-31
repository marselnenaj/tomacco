/* SPDX-License-Identifier: MIT
 * Synthetic bare-metal program: independent HUPERM/VUPERM banks, ERET from
 * GL1/GL2/EL1/EL2 to user mode, then real page permission faults.
 * No Apple firmware, host callbacks, or patched guest code.
 * Result at 0x50000: completion magic, successful checks, ESR/FAR on failure.
 */
#ifndef GUARDED
#define GUARDED 1
#endif
#ifndef HOST
#define HOST 0
#endif
#if GUARDED
#define USER_PERMISSIONS 0xc040808000000000
#else
#define USER_PERMISSIONS 0x3010202000000000
#endif
#if GUARDED
#define ESR_BANK s3_6_c15_c10_5
#define FAR_BANK s3_6_c15_c10_7
#define ELR_BANK s3_6_c15_c10_6
#define SPSR_BANK s3_6_c15_c10_3
#else
#define ESR_BANK esr_el1
#define FAR_BANK far_el1
#define ELR_BANK elr_el1
#define SPSR_BANK spsr_el1
#endif

.global _start
.text
_start:
    msr daifset, #15
    mov x20, #0
    adr x0, vectors
    msr vbar_el2, x0
    /* The Exclave MMIO clock must agree with the physical CPU counter. */
    ldr x0, =0x10000000
    mrs x2, cntpct_el0
    ldr x3, [x0]
    mrs x4, cntpct_el0
    cmp x3, x2
    b.lo fail
    cmp x3, x4
    b.hi fail
    /* Explicit banks and the VHE alias must preserve independent values. */
    ldr x25, =USER_PERMISSIONS
    msr s3_4_c15_c5_2, x25 /* VUPERM */
    ldr x0, =0x480000000 /* E2H, RW */
    msr hcr_el2, x0
    isb
    mov x0, #0x3333
    msr s3_6_c15_c1_5, x0 /* UPERM from host EL2 aliases HUPERM */
    mrs x1, s3_4_c15_c5_1
    cmp x0, x1
    b.ne fail
    mrs x1, s3_4_c15_c5_2
    cmp x1, x25
    b.ne fail
#if HOST
    msr s3_4_c15_c5_1, x25
    msr s3_4_c15_c5_2, xzr /* wrong bank denies every user access */
    ldr x0, =0x488000000 /* E2H, TGE, RW */
    msr hcr_el2, x0
    isb
    b setup_el1 /* EL1 aliases redirect to host EL2 registers */
#else
    msr s3_4_c15_c5_1, xzr /* wrong bank denies every user access */
    mov x0, #0x80000000
    msr hcr_el2, x0
    adr x0, setup_el1
    msr elr_el2, x0
    mov x0, #0x3c5
    msr spsr_el2, x0
    eret
#endif

setup_el1:
    mov x0, #0x90000
    mov sp, x0
    /* 4 KiB tables, identity mapped first 2 MiB, 39-bit VA. */
    mov x0, #0x60000
    ldr x1, =0x61003
    str x1, [x0]
    ldr x0, =0x61000
    ldr x1, =0x62003
    str x1, [x0]
    ldr x0, =0x62000
    ldr x1, =0x0060000000000703 /* kernel RW: SPRR index 3 */
    mov x2, #512
1:  str x1, [x0], #8
    add x1, x1, #0x1000
    subs x2, x2, #1
    b.ne 1b
    ldr x0, =0x62000
    ldr x1, =0x10703 /* kernel RX: index 0 */
    str x1, [x0, #0x80]
    add x1, x1, #0x1000
    str x1, [x0, #0x88] /* vector table */
    ldr x1, =0x00200000000207c3 /* user RX: index 13 */
    str x1, [x0, #0x100]
    ldr x1, =0x0020000000030783 /* user R: index 9 */
    str x1, [x0, #0x180]
    ldr x1, =0x00600000000317c3 /* user RW: index 15 */
    str x1, [x0, #0x188]
    ldr x1, =0x0060000000040783 /* user R/NX: index 11 */
    str x1, [x0, #0x200]
    ldr x1, =0x00600000000507c3 /* result: index 15 */
    str x1, [x0, #0x280]
    mov x0, #0x30000
    mov x1, #0x3a71
    str x1, [x0]
    ldr x0, =0xf050a0a00000f005 /* privileged EL/GL permissions */
    msr s3_6_c15_c1_6, x0
    mrs x0, s3_6_c15_c1_5
    cmp x0, x25
    b.ne fail
    mov x0, #1
    msr s3_6_c15_c1_0, x0
    mov x0, #0xff
    msr mair_el1, x0
    mov x0, #0x60000
    msr ttbr0_el1, x0
    ldr x0, =0x803519
    msr tcr_el1, x0
    adr x0, vectors
    msr vbar_el1, x0
    msr s3_6_c15_c10_2, x0
    mov x0, #0x90000
    msr s3_6_c15_c10_0, x0 /* guarded stack */
    adr x0, enter_user
    msr s3_6_c15_c8_1, x0
    mov x0, #2
    msr s3_6_c15_c1_2, x0
    dsb sy
    isb
    mrs x0, sctlr_el1
    orr x0, x0, #1
    msr sctlr_el1, x0
    isb
#if GUARDED
    .inst 0x00201420 /* genter #0 */
    b fail
#else
    b enter_user
#endif

enter_user:
    mov x20, #0
    mov x21, #0
    mov x22, #0x50000
    mov x0, #0x20000
    msr ELR_BANK, x0
    msr SPSR_BANK, xzr
    eret

handle_sync:
    mrs x10, ESR_BANK
    mrs x11, FAR_BANK
    lsr x12, x10, #26
    cmp x21, #1
    b.eq expect_write_abort
    cmp x21, #2
    b.eq expect_exec_abort
    cmp x21, #3
    b.ne fail
    cmp x12, #0x15 /* SVC, returning from successfully tested EL0 */
    b.ne fail
    cmp x20, #15
    b.ne fail
    mov x0, #0x30000
    ldr x1, [x0]
    /* Compare full value without a 12-bit immediate limitation. */
    mov x2, #0x3a71
    cmp x1, x2
    b.ne fail
    ldr x0, =0x31000
    ldr x1, [x0]
    mov x2, #0x7183
    cmp x1, x2
    b.ne fail
    str x20, [x22, #8]
    mov x0, #0xc14e
    str x0, [x22]
    dsb sy
2:  wfe
    b 2b

expect_write_abort:
    cmp x12, #0x24
    b.ne fail
    and x12, x10, #0x7f
    cmp x12, #0x4f /* write + permission fault at level 3 */
    b.ne fail
    mov x12, #0x30000
    cmp x11, x12
    b.ne fail
    orr x20, x20, #4
    mrs x12, ELR_BANK
    add x12, x12, #4
    msr ELR_BANK, x12
    eret

expect_exec_abort:
    cmp x12, #0x20
    b.ne fail
    and x12, x10, #0x3f
    cmp x12, #0xf
    b.ne fail
    mov x12, #0x40000
    cmp x11, x12
    b.ne fail
    orr x20, x20, #8
    ldr x12, =after_nx
    msr ELR_BANK, x12
    eret

fail:
    mov x22, #0x50000
    mrs x10, ESR_BANK
    mrs x11, FAR_BANK
    str x20, [x22, #8]
    str x10, [x22, #16]
    str x11, [x22, #24]
    mov x0, #0xbad
    str x0, [x22]
    dsb sy
3:  wfe
    b 3b
    .ltorg

.org 0x1000
.balign 2048
vectors:
.rept 8
    b fail
    .balign 128
.endr
    b handle_sync /* lower EL, AArch64, synchronous */
    .balign 128
.rept 7
    b fail
    .balign 128
.endr

.org 0x10000
user:
    orr x20, x20, #1 /* successful EL0 instruction fetch */
    mov x0, #0x30000
    ldr x1, [x0]
    mov x2, #0x3a71
    cmp x1, x2
    b.ne user_failure
    mov x0, #0x30000
    add x0, x0, #0x1000
    mov x1, #0x7183
    str x1, [x0]
    ldr x2, [x0]
    cmp x1, x2
    b.ne user_failure
    orr x20, x20, #2 /* permitted read and write */
    mov x21, #1
    mov x0, #0x30000
    str x1, [x0] /* must fault and leave the read-only word untouched */
    mov x21, #2
    mov x0, #0x40000
    br x0 /* must fault: the NX page contains real instructions */
after_nx:
    mov x21, #3
    svc #0x27
user_failure:
    mov x21, #0
    svc #0x28
4:  b 4b
    .ltorg

.org 0x30000
nonexecutable:
    mov x21, #0
    svc #0x29 /* reaching this instruction is a failed permission check */
5:  b 5b
