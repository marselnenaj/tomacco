/*
 * Jump-cache and TB-exit correctness under remapping and code writes, on
 * one CPU of QEMU's virt machine at EL1 with the MMU on (4 KiB granule,
 * 39-bit VA; RAM identity-mapped by a 1 GiB block, a 4 KiB-page alias
 * window at VA 0x80000000).
 *
 * Every phase calls through the alias window ROUNDS times from one loop,
 * so the caller's indirect exit and the callee's RET are served by the
 * jump cache (and translated-code probes) after the first round, then
 * changes what the window maps and calls again:
 *
 *   page     BLR to 0x80000000 -> code page A (x0 = 1), then the PTE points
 *            at page B (x0 = 2), TLBI VAE1 of that page only
 *   full     back to A, TLBI VMALLE1
 *   asid     to B again with no TLBI: a fresh ASID in TTBR0_EL1
 *   direct   BLR to 0x80002000, whose code is a direct B to 0x80003000
 *            (another page: the exit is a lookup with a known target);
 *            0x80003000 maps C (x0 = 3), then D (x0 = 4), TLBI VAE1 of
 *            0x80003000 only
 *   smc      back to A through 0x80000000, then A's first instruction is
 *            rewritten (through the identity map) to x0 = 5, DC CVAU,
 *            IC IVAU, ISB
 *
 * Prints "<name>=<sum of x0 over ROUNDS calls>" for each step (before and
 * after each change) and powers off through PSCI.
 */
	.equ UART, 0x09000000
	.equ PSCI_SYSTEM_OFF, 0x84000008
	.equ ROUNDS, 100
	.equ ALIAS, 0x80000000

	.text
_start:
	adr	x0, stack_top
	mov	sp, x0
	adr	x20, results

	/* MAIR: attr0 Device-nGnRnE, attr1 Normal write-back. */
	mov	x0, #0xff00
	msr	mair_el1, x0
	/* L1[0]: device block 0-1 GiB; L1[1]: RAM block 1-2 GiB; L1[2]: table. */
	adr	x1, l1
	mov	x0, #0x401			/* block, AttrIdx 0, AF */
	str	x0, [x1, #0]
	mov	x0, #0x40000000
	movk	x0, #0x705			/* block, AttrIdx 1, SH inner, AF */
	str	x0, [x1, #8]
	adr	x0, l2
	orr	x0, x0, #3
	str	x0, [x1, #16]
	adr	x1, l2
	adr	x0, l3
	orr	x0, x0, #3
	str	x0, [x1, #0]
	adr	x0, page_a
	bl	map0
	adr	x0, jump_src
	adr	x1, l3
	bl	pte
	str	x0, [x1, #16]			/* 0x80002000 -> jump_src */
	adr	x0, page_c
	bl	pte
	str	x0, [x1, #24]			/* 0x80003000 -> C */
	dsb	ish
	/* TCR: T0SZ 25, inner/outer WB, inner shareable, 4 KiB, EPD1, 40-bit PA. */
	movz	x0, #(25 | (1 << 8) | (1 << 10) | (3 << 12))
	movk	x0, #(1 << (23 - 16)), lsl #16
	movk	x0, #2, lsl #32
	msr	tcr_el1, x0
	adr	x0, l1
	orr	x0, x0, #(1 << 48)		/* ASID 1 */
	msr	ttbr0_el1, x0
	isb
	mrs	x0, sctlr_el1
	orr	x0, x0, #1			/* M */
	orr	x0, x0, #(1 << 2)		/* C */
	orr	x0, x0, #(1 << 12)		/* I */
	msr	sctlr_el1, x0
	isb

	mov	x8, #ALIAS
	/* page */
	bl	calls
	str	x0, [x20, #0]
	adr	x0, page_b
	bl	map0
	dsb	ishst
	lsr	x0, x8, #12
	tlbi	vae1, x0
	dsb	ish
	isb
	bl	calls
	str	x0, [x20, #8]
	/* full */
	adr	x0, page_a
	bl	map0
	dsb	ishst
	tlbi	vmalle1
	dsb	ish
	isb
	bl	calls
	str	x0, [x20, #16]
	/* asid */
	adr	x0, page_b
	bl	map0
	dsb	ishst
	adr	x0, l1
	orr	x0, x0, #(2 << 48)		/* ASID 2, never used before */
	msr	ttbr0_el1, x0
	isb
	bl	calls
	str	x0, [x20, #24]
	/* direct */
	mov	x8, #ALIAS
	add	x8, x8, #0x2000
	bl	calls
	str	x0, [x20, #32]
	adr	x0, page_d
	adr	x1, l3
	bl	pte
	str	x0, [x1, #24]
	dsb	ishst
	mov	x0, #ALIAS
	add	x0, x0, #0x3000
	lsr	x0, x0, #12
	tlbi	vae1, x0
	dsb	ish
	isb
	bl	calls
	str	x0, [x20, #40]
	/* smc */
	mov	x8, #ALIAS
	adr	x0, page_a
	bl	map0
	dsb	ishst
	tlbi	vmalle1
	dsb	ish
	isb
	bl	calls
	str	x0, [x20, #48]
	adr	x1, page_a
	movz	w0, #0x00a0			/* movz x0, #5 */
	movk	w0, #0xd280, lsl #16
	str	w0, [x1]
	dc	cvau, x1
	dsb	ish
	ic	ivau, x1
	dsb	ish
	isb
	bl	calls
	str	x0, [x20, #56]

	adr	x21, names
	mov	x22, #0
1:	ldrb	w2, [x21]
	cbz	w2, 3f
2:	ldrb	w2, [x21], #1
	cbz	w2, 4f
	bl	putc
	b	2b
4:	mov	w2, #'='
	bl	putc
	ldr	x23, [x20, x22]
	mov	x3, #60
5:	lsr	x2, x23, x3
	and	x2, x2, #0xf
	cmp	x2, #10
	add	x4, x2, #'0'
	add	x5, x2, #('a' - 10)
	csel	x2, x4, x5, lo
	bl	putc
	subs	x3, x3, #4
	b.ge	5b
	mov	w2, #' '
	bl	putc
	add	x22, x22, #8
	b	1b
3:	mov	w2, #'\n'
	bl	putc
	movz	x0, #(PSCI_SYSTEM_OFF & 0xffff)
	movk	x0, #(PSCI_SYSTEM_OFF >> 16), lsl #16
	hvc	#0
6:	wfi
	b	6b

/* x0 = sum over ROUNDS calls of x8(); keeps x8. */
calls:
	stp	x29, x30, [sp, #-32]!
	stp	x19, x21, [sp, #16]
	mov	x19, #ROUNDS
	mov	x21, #0
1:	blr	x8
	add	x21, x21, x0
	subs	x19, x19, #1
	b.ne	1b
	mov	x0, x21
	ldp	x19, x21, [sp, #16]
	ldp	x29, x30, [sp], #32
	ret

/* x0 = a normal, inner-shareable, accessed page descriptor for page x0. */
pte:
	mov	x2, #0x707			/* page, AttrIdx 1, SH inner, AF */
	orr	x0, x0, x2
	ret

/* Map VA 0x80000000 to page x0. */
map0:
	stp	x29, x30, [sp, #-16]!
	bl	pte
	adr	x1, l3
	str	x0, [x1]
	ldp	x29, x30, [sp], #16
	ret

/* Write w2 to the PL011 data register. */
putc:
	movz	x8, #(UART >> 16), lsl #16
	str	w2, [x8]
	ret

names:
	.asciz	"pagea"
	.asciz	"pageb"
	.asciz	"full"
	.asciz	"asid"
	.asciz	"directc"
	.asciz	"directd"
	.asciz	"smca"
	.asciz	"smcb"
	.byte	0

	.balign	4096
page_a:
	mov	x0, #1
	ret
	.balign	4096
page_b:
	mov	x0, #2
	ret
	.balign	4096
jump_src:
	b	. + 0x1000
	.balign	4096
page_c:
	mov	x0, #3
	ret
	.balign	4096
page_d:
	mov	x0, #4
	ret

	.balign	4096
l1:
	.zero	4096
l2:
	.zero	4096
l3:
	.zero	4096
results:
	.zero	4096
	.zero	4096
stack_top:
