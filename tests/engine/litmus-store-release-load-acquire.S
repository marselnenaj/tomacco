/*
 * Store buffering through Store-Release and Load-Acquire, on two CPUs of
 * QEMU's virt machine, without an OS or firmware.
 *
 *   CPU 0: STLR 1 -> x; LDAR y -> r0        CPU 1: STLR 1 -> y; LDAR x -> r1
 *
 * A Load-Acquire may not be observed before an earlier Store-Release (RCsc),
 * so r0 == 0 && r1 == 0 is forbidden by the architecture. The program runs
 * ITERATIONS rounds, meeting at a counter before and after each, prints
 * "forbidden=<hex count>" on the PL011 and powers off through PSCI.
 *
 * Position-independent and one section: the runner loads the raw bytes at
 * 0x40000000 and starts CPU 0 there. The shared words live one page after
 * the code, so stores to them never touch a translated code page.
 */
	.equ ITERATIONS, 2000000
	.equ UART, 0x09000000
	.equ PSCI_CPU_ON, 0xc4000003
	.equ PSCI_SYSTEM_OFF, 0x84000008

	.text
/* Local, so every adr below is resolved here: a raw image has no linker. */
_start:
	mrs	x0, mpidr_el1
	and	x0, x0, #0xff
	adr	x10, shared
	add	x13, x10, #0x00		/* x */
	add	x14, x10, #0x40		/* y */
	add	x15, x10, #0x80		/* r1 */
	add	x16, x10, #0xc0		/* arrivals */
	mov	w12, #1
	cbnz	x0, secondary

	movz	x0, #(PSCI_CPU_ON & 0xffff)
	movk	x0, #(PSCI_CPU_ON >> 16), lsl #16
	mov	x1, #1
	adr	x2, _start
	mov	x3, #0
	hvc	#0

	mov	x20, #0			/* forbidden outcomes */
	mov	x21, #1			/* round */
	movz	x22, #(ITERATIONS & 0xffff)
	movk	x22, #(ITERATIONS >> 16), lsl #16
primary_round:
	lsl	x9, x21, #2
	sub	x9, x9, #2		/* first meeting: 4 * round - 2 arrivals */
	bl	meet
	stlr	w12, [x13]		/* x = 1 */
	ldar	w5, [x14]		/* r0 = y */
	lsl	x9, x21, #2		/* second meeting: 4 * round */
	bl	meet
	ldr	w6, [x15]		/* r1, stored by CPU 1 before it met us */
	orr	w7, w5, w6
	cbnz	w7, 1f
	add	x20, x20, #1
1:	str	wzr, [x13]
	str	wzr, [x14]
	add	x21, x21, #1
	cmp	x21, x22
	b.ls	primary_round

	adr	x1, message
2:	ldrb	w2, [x1], #1
	cbz	w2, 3f
	bl	putc
	b	2b
3:	mov	x3, #60			/* sixteen hex digits, most significant first */
4:	lsr	x2, x20, x3
	and	x2, x2, #0xf
	cmp	x2, #10
	add	x4, x2, #'0'
	add	x5, x2, #('a' - 10)
	csel	x2, x4, x5, lo
	bl	putc
	subs	x3, x3, #4
	b.ge	4b
	mov	x2, #'\n'
	bl	putc
	movz	x0, #(PSCI_SYSTEM_OFF & 0xffff)
	movk	x0, #(PSCI_SYSTEM_OFF >> 16), lsl #16
	hvc	#0
5:	wfi
	b	5b

secondary:
	mov	x21, #1
	movz	x22, #(ITERATIONS & 0xffff)
	movk	x22, #(ITERATIONS >> 16), lsl #16
secondary_round:
	lsl	x9, x21, #2
	sub	x9, x9, #2
	bl	meet
	stlr	w12, [x14]		/* y = 1 */
	ldar	w5, [x13]		/* r1 = x */
	str	w5, [x15]
	lsl	x9, x21, #2
	bl	meet
	add	x21, x21, #1
	cmp	x21, x22
	b.ls	secondary_round
6:	wfi
	b	6b

/* Arrive, then wait until x9 arrivals in all (w12 holds 1). */
meet:
	ldaddal	w12, w11, [x16]
7:	ldar	w11, [x16]
	cmp	x11, x9
	b.lo	7b
	ret

/* Write w2 to the PL011 data register. */
putc:
	movz	x8, #(UART >> 16), lsl #16
	str	w2, [x8]
	ret

message:
	.asciz	"forbidden="

	.balign	4096
shared:
	.zero	0x100
