/*
 * Branch and translation-block entry cost on one CPU of QEMU's virt machine,
 * at EL1 with the MMU off (environment as pac-bench.S). Models the guest
 * profile of a Home-screen swipe (4.3 instructions per TB, 22 %
 * branches, objc_msgSend-style dispatch). Each phase runs ITERATIONS times and
 * is timed with CNTVCT_EL0:
 *
 *   chain    eight short blocks ending in a direct B inside one 4 KiB page
 *            (chained TBs: the floor every other phase adds to)
 *   page4k   four blocks in the four 4 KiB pages of one 16 KiB page, each
 *            ending in a direct branch to the next page
 *   page16k  four blocks 16 KiB apart, each branching to the next
 *   call     BL to a leaf in the same page, RET back
 *   msgsend  BL to a stub in another page that loads an IMP from a 16-entry
 *            table and branches to it with BR x16; the IMP returns with RET
 *   blr      BLR through a 16-entry function-pointer table, RET back
 *   wide     BLR to 8192 distinct leaves (64 KiB of code) in LCG order:
 *            more indirect targets than a 4096-entry jump cache holds
 *   flush    BLR to 512 leaves with TLBI VMALLE1 every 256 iterations
 *            (an ASID switch flushes QEMU's TLB and jump cache)
 *   smc      BLR to a leaf; every 64 iterations an instruction of a hot
 *            code page is rewritten with itself (TB invalidation)
 *
 * Prints "cntfrq=<hex> <phase>=<ticks in hex> ..." and "sum=<hex>" (every
 * result folded together, so a phase cannot be optimised away and two
 * engines can be compared), then powers off. tools/engine_branch_bench.py
 * runs it and converts the ticks into nanoseconds per iteration.
 *
 * Position-independent: the runner loads the raw .text at 0x40200000 (16 KiB
 * aligned, which the page phases rely on).
 */
	.equ UART, 0x09000000
	.equ PSCI_SYSTEM_OFF, 0x84000008
	.equ ITERATIONS, 1000000

	.macro	begin
	isb
	mrs	x23, cntvct_el0
	mov	x19, x22
	.endm

	.macro	end slot
	isb
	mrs	x1, cntvct_el0
	sub	x1, x1, x23
	str	x1, [x20, #\slot]
	ror	x21, x21, #7
	eor	x21, x21, x0
	mov	x0, #0
	.endm

	.text
_start:
	adr	x0, stack_top
	mov	sp, x0
	adr	x20, results
	mov	x21, #0				/* sum */
	movz	x22, #(ITERATIONS & 0xffff)
	movk	x22, #(ITERATIONS >> 16), lsl #16
	mrs	x0, cntfrq_el0
	str	x0, [x20, #0]
	mov	x0, #0

	/* Function-pointer tables (no linker: fill them at run time). */
	adr	x24, imp_table
	.irp	k, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
	adr	x1, imp\k
	str	x1, [x24, #(\k * 8)]
	.endr
	adr	x26, wide_leaves

	/* chain */
	begin
	b	chain_0
chain_done:
	end	8

	/* page4k */
	begin
	b	p4k_0
p4k_done:
	end	16

	/* page16k */
	begin
	b	p16k_0
p16k_done:
	end	24

	/* call */
	begin
1:	bl	leaf
	subs	x19, x19, #1
	b.ne	1b
	end	32

	/* msgsend */
	mov	x9, #0
	begin
1:	bl	msgsend_stub
	subs	x19, x19, #1
	b.ne	1b
	end	40

	/* blr */
	mov	x9, #0
	begin
1:	ldr	x8, [x24, x9, lsl #3]
	add	x9, x9, #5
	and	x9, x9, #15
	blr	x8
	subs	x19, x19, #1
	b.ne	1b
	end	48

	/* wide */
	mov	x9, #0
	begin
1:	add	x9, x9, x9, lsl #2		/* x9 = (5 * x9 + 1) mod 8192 */
	add	x9, x9, #1
	and	x9, x9, #8191
	add	x8, x26, x9, lsl #3
	blr	x8
	subs	x19, x19, #1
	b.ne	1b
	end	56

	/* flush */
	mov	x9, #0
	begin
1:	add	x9, x9, x9, lsl #2
	add	x9, x9, #1
	and	x9, x9, #8191
	and	x10, x9, #511
	add	x8, x26, x10, lsl #3
	blr	x8
	tst	x19, #255
	b.ne	2f
	tlbi	vmalle1
	dsb	nsh
	isb
2:	subs	x19, x19, #1
	b.ne	1b
	end	64

	/* smc */
	adr	x27, smc_leaf
	ldr	w28, [x27]
	begin
1:	blr	x27
	tst	x19, #63
	b.ne	2f
	str	w28, [x27]
	dc	cvau, x27
	dsb	ish
	ic	ivau, x27
	dsb	ish
	isb
2:	subs	x19, x19, #1
	b.ne	1b
	end	72

	str	x21, [x20, #80]

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

leaf:
	add	x0, x0, #3
	ret

/* Write w2 to the PL011 data register. */
putc:
	movz	x8, #(UART >> 16), lsl #16
	str	w2, [x8]
	ret

names:
	.asciz	"cntfrq"
	.asciz	"chain"
	.asciz	"page4k"
	.asciz	"page16k"
	.asciz	"call"
	.asciz	"msgsend"
	.asciz	"blr"
	.asciz	"wide"
	.asciz	"flush"
	.asciz	"smc"
	.asciz	"sum"
	.byte	0

	.balign	4096
chain_0:
	add	x0, x0, #1
	b	chain_1
chain_1:
	add	x0, x0, #2
	b	chain_2
chain_2:
	eor	x0, x0, #1
	b	chain_3
chain_3:
	add	x0, x0, #3
	b	chain_4
chain_4:
	add	x0, x0, #4
	b	chain_5
chain_5:
	eor	x0, x0, #2
	b	chain_6
chain_6:
	add	x0, x0, #5
	b	chain_7
chain_7:
	subs	x19, x19, #1
	b.ne	chain_0
	b	chain_done

/* objc_msgSend-like: cache probe stand-in, then a tail branch to the IMP. */
msgsend_stub:
	ldr	x16, [x24, x9, lsl #3]
	add	x9, x9, #7
	and	x9, x9, #15
	br	x16

	/* The IMPs, four per 4 KiB page over four pages. */
	.balign	4096
	.irp	k, 0,1,2,3
	.balign	1024
imp\k:
	add	x0, x0, #(\k + 1)
	ret
	.endr
	.balign	4096
	.irp	k, 4,5,6,7
	.balign	1024
imp\k:
	add	x0, x0, #(\k + 1)
	ret
	.endr
	.balign	4096
	.irp	k, 8,9,10,11
	.balign	1024
imp\k:
	add	x0, x0, #(\k + 1)
	ret
	.endr
	.balign	4096
	.irp	k, 12,13,14,15
	.balign	1024
imp\k:
	add	x0, x0, #(\k + 1)
	ret
	.endr

	.balign	4096
smc_leaf:
	add	x0, x0, #1
	ret

	.balign	16384
p4k_0:
	add	x0, x0, #1
	b	p4k_1
	.balign	4096
p4k_1:
	add	x0, x0, #2
	b	p4k_2
	.balign	4096
p4k_2:
	eor	x0, x0, #1
	b	p4k_3
	.balign	4096
p4k_3:
	subs	x19, x19, #1
	b.ne	p4k_0
	b	p4k_done

	.balign	16384
p16k_0:
	add	x0, x0, #1
	b	p16k_1
	.balign	16384
p16k_1:
	add	x0, x0, #2
	b	p16k_2
	.balign	16384
p16k_2:
	eor	x0, x0, #1
	b	p16k_3
	.balign	16384
p16k_3:
	subs	x19, x19, #1
	b.ne	p16k_0
	b	p16k_done

	.balign	16384
wide_leaves:
	.rept	8192
	add	x0, x0, #1
	ret
	.endr

	.balign	4096
imp_table:
	.zero	128
results:
	.zero	128
	.balign	4096
	.zero	4096
stack_top:
