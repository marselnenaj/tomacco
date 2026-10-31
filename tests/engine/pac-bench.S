/*
 * Pointer authentication cost on one CPU of QEMU's virt machine, at EL1 with
 * the MMU off and every key enabled (environment as pac-probe.S). Each phase
 * runs ITERATIONS times and is timed with CNTVCT_EL0 (under TCG without
 * icount the virtual counter follows the host clock):
 *
 *   call    bl to a frame-only function: stp x29, x30 / mov x29, sp /
 *           ldp x29, x30 / ret (the baseline the others add to)
 *   pacret  the same function signed the arm64e way: pacibsp ... retab
 *   pacaut  paciasp ... autiasp; ret
 *   data    pacdza, autdza and xpaci on one register, four times over
 *   pacga   four dependent PACGA
 *
 * Prints "cntfrq=<hex> <phase>=<ticks in hex> ..." and "sum=<hex>" (every
 * result folded together, so a phase cannot be optimised away and two
 * engines can be compared), then powers off. tools/engine_pac_bench.py runs
 * it and converts the ticks into nanoseconds per iteration.
 */
	.equ UART, 0x09000000
	.equ PSCI_SYSTEM_OFF, 0x84000008
	.equ ITERATIONS, 2000000

	.text
_start:
	adr	x0, stack_top
	mov	sp, x0
	movz	x0, #25
	movk	x0, #25, lsl #16
	orr	x0, x0, #(2 << 30)
	msr	tcr_el1, x0
	movz	x1, #0x3210
	movk	x1, #0x7654, lsl #16
	movk	x1, #0xba98, lsl #32
	movk	x1, #0xfedc, lsl #48
	msr	apiakeylo_el1, x1
	msr	apiakeyhi_el1, x1
	msr	apibkeylo_el1, x1
	msr	apibkeyhi_el1, x1
	msr	apdakeylo_el1, x1
	msr	apdakeyhi_el1, x1
	msr	apgakeylo_el1, x1
	msr	apgakeyhi_el1, x1
	mrs	x0, sctlr_el1
	orr	x0, x0, #(1 << 31)
	orr	x0, x0, #(1 << 30)
	orr	x0, x0, #(1 << 27)
	orr	x0, x0, #(1 << 13)
	msr	sctlr_el1, x0
	isb
	adr	x20, results
	mov	x21, #0				/* sum */
	movz	x22, #(ITERATIONS & 0xffff)
	movk	x22, #(ITERATIONS >> 16), lsl #16

	mrs	x0, cntfrq_el0
	str	x0, [x20, #0]

	/* call */
	isb
	mrs	x23, cntvct_el0
	mov	x19, x22
1:	bl	plain
	subs	x19, x19, #1
	b.ne	1b
	isb
	mrs	x0, cntvct_el0
	sub	x0, x0, x23
	str	x0, [x20, #8]

	/* pacret */
	isb
	mrs	x23, cntvct_el0
	mov	x19, x22
1:	bl	pacret
	subs	x19, x19, #1
	b.ne	1b
	isb
	mrs	x0, cntvct_el0
	sub	x0, x0, x23
	str	x0, [x20, #16]

	/* pacaut */
	isb
	mrs	x23, cntvct_el0
	mov	x19, x22
1:	bl	pacaut
	subs	x19, x19, #1
	b.ne	1b
	isb
	mrs	x0, cntvct_el0
	sub	x0, x0, x23
	str	x0, [x20, #24]

	/* data */
	adr	x1, results
	isb
	mrs	x23, cntvct_el0
	mov	x19, x22
1:	.rept	4
	pacdza	x1
	autdza	x1
	xpaci	x1
	.endr
	subs	x19, x19, #1
	b.ne	1b
	isb
	mrs	x0, cntvct_el0
	sub	x0, x0, x23
	str	x0, [x20, #32]
	eor	x21, x21, x1

	/* pacga */
	mov	x3, #1
	isb
	mrs	x23, cntvct_el0
	mov	x19, x22
1:	.rept	4
	pacga	x3, x3, x19
	.endr
	subs	x19, x19, #1
	b.ne	1b
	isb
	mrs	x0, cntvct_el0
	sub	x0, x0, x23
	str	x0, [x20, #40]
	eor	x21, x21, x3
	str	x21, [x20, #48]

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

plain:
	stp	x29, x30, [sp, #-16]!
	mov	x29, sp
	ldp	x29, x30, [sp], #16
	ret

pacret:
	pacibsp
	stp	x29, x30, [sp, #-16]!
	mov	x29, sp
	ldp	x29, x30, [sp], #16
	retab

pacaut:
	paciasp
	stp	x29, x30, [sp, #-16]!
	mov	x29, sp
	ldp	x29, x30, [sp], #16
	autiasp
	ret

/* Write w2 to the PL011 data register. */
putc:
	movz	x8, #(UART >> 16), lsl #16
	str	w2, [x8]
	ret

names:
	.asciz	"cntfrq"
	.asciz	"call"
	.asciz	"pacret"
	.asciz	"pacaut"
	.asciz	"data"
	.asciz	"pacga"
	.asciz	"sum"
	.byte	0

	.balign	4096
results:
	.zero	64
	.balign	4096
	.zero	4096
stack_top:
