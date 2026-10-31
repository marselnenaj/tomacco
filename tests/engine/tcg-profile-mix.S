/*
 * TCG profile plugin probe on one CPU of QEMU's virt machine, without an OS
 * or firmware, at EL1 with the MMU off.
 *
 * Runs a loop of LOOPS (1000) iterations whose body executes, once per
 * iteration, exactly one of each instruction the profile counts by name:
 *
 *   ldr / str          plain load and store
 *   ldadd              an LSE atomic
 *   ldxr / stxr        an exclusive pair
 *   ldar / stlr        ordered (acquire/release) accesses
 *   dc zva             cache-block zeroing
 *   mrs cntvct_el0     a timer register read
 *   fadd d0            scalar floating point
 *   add v2.4s          integer SIMD
 *   fmul v4.4s         SIMD floating point
 *   dmb ish            a barrier
 *   svc #0x80          a system call; the vector below returns with eret
 *
 * Then it spins for the 32-bit count the runner stores at SPIN (0: none),
 * so a test can observe windows while the guest runs, prints
 * "faults=<16 hex digits>" (exceptions other than the SVC) on the PL011 and
 * powers off through PSCI.
 *
 * Position-independent and one section: the runner loads the raw bytes at
 * 0x40200000 and starts the CPU at the first instruction. Data lives in its
 * own page after the code, so stores never touch a translated code page.
 */
	.equ UART, 0x09000000
	.equ PSCI_SYSTEM_OFF, 0x84000008
	.equ LOOPS, 1000
	.equ SPIN, 0x40300000

	.text
_start:
	adr	x0, vectors
	msr	vbar_el1, x0
	/* CPACR_EL1.FPEN = 3: floating point and SIMD at EL1. */
	mov	x0, #(3 << 20)
	msr	cpacr_el1, x0
	isb
	adr	x1, data
	add	x5, x1, #256
	fmov	d1, #1.0
	movi	v3.4s, #1
	fmov	v5.4s, #2.0
	mov	x3, #1
	mov	x0, #LOOPS
	b	loop

	.balign	64
loop:
	ldr	x2, [x1]
	str	x2, [x1, #8]
	ldadd	x3, x4, [x1]
	add	x6, x1, #64
	ldxr	x7, [x6]
	stxr	w8, x7, [x6]
	ldar	x9, [x6]
	stlr	x9, [x6]
	dc	zva, x5
	mrs	x10, cntvct_el0
	fadd	d0, d0, d1
	add	v2.4s, v2.4s, v3.4s
	fmul	v4.4s, v4.4s, v5.4s
	dmb	ish
	svc	#0x80
	subs	x0, x0, #1
	b.ne	loop

	movz	x11, #(SPIN & 0xffff)
	movk	x11, #(SPIN >> 16), lsl #16
	ldr	w0, [x11]
	cbz	w0, 2f
1:	subs	x0, x0, #1
	b.ne	1b
2:
	/* "faults=" then the count as 16 hex digits. */
	adr	x12, label
3:	ldrb	w2, [x12], #1
	cbz	w2, 4f
	bl	putc
	b	3b
4:	adr	x13, faults
	ldr	x13, [x13]
	mov	x14, #60
5:	lsr	x2, x13, x14
	and	x2, x2, #0xf
	cmp	x2, #10
	add	x15, x2, #'0'
	add	x16, x2, #('a' - 10)
	csel	x2, x15, x16, lo
	bl	putc
	subs	x14, x14, #4
	b.ge	5b
	mov	w2, #'\n'
	bl	putc
	movz	x0, #(PSCI_SYSTEM_OFF & 0xffff)
	movk	x0, #(PSCI_SYSTEM_OFF >> 16), lsl #16
	hvc	#0
6:	wfi
	b	6b

/* Write w2 to the PL011 data register. */
putc:
	movz	x17, #(UART >> 16), lsl #16
	str	w2, [x17]
	ret

label:
	.asciz	"faults="

/* An SVC returns to the next instruction. Any other synchronous exception
 * is counted and skipped. */
	.balign	2048
vectors:
	.rept	4
	b	other
	.balign	128
	.endr
	b	sync		/* current EL with SP_ELx, synchronous */
	.balign	128
	.rept	11
	b	other
	.balign	128
	.endr
sync:
	mrs	x18, esr_el1
	lsr	x18, x18, #26
	cmp	x18, #0x15
	b.ne	other
	eret
other:
	adr	x18, faults
	ldr	x19, [x18]
	add	x19, x19, #1
	str	x19, [x18]
	mrs	x18, elr_el1
	add	x18, x18, #4
	msr	elr_el1, x18
	eret

	.balign	4096
data:
	.zero	512
faults:
	.zero	16
