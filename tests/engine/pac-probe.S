/*
 * Pointer authentication probe on one CPU of QEMU's virt machine, without an
 * OS or firmware, at EL1 with the MMU off.
 *
 * Sets a 39-bit virtual address size in both halves (TCR_EL1.T0SZ/T1SZ 25,
 * top-byte ignore off, as iOS does), loads non-zero instruction, data and
 * generic keys, enables all four PAC keys in SCTLR_EL1 and then runs the
 * instructions a guest depends on against a user pointer P (bit 55 clear)
 * and a kernel pointer K (bit 55 set):
 *
 *   pac      PACIZA P                   aut      AUTIZA of that result
 *   badaut   AUTIZA of the unsigned P   xpac     XPACI of the signed P
 *   pacga    PACGA P, 0                 dpac     PACDZA P
 *   daut     AUTDZA of that result      dbad     AUTDZA of the unsigned P
 *   kpac     PACIZA K                   kxpac    XPACI of that result
 *   wrongkey AUTIZB of the PACIZA result (signed with key A, checked with B)
 *
 * A failed authentication either raises a PAC-fail exception (FEAT_FPAC,
 * ESR EC 0x1c; the vector below records it and continues after the
 * instruction) or returns the pointer with an error code in its PAC field.
 * Prints every result as "<name>=<16 hex digits>" plus "faults=<count>" and
 * "esr=<last ESR_EL1>" on the PL011, then powers off through PSCI.
 *
 * Position-independent and one section: the runner loads the raw bytes and
 * starts the CPU at the first instruction. The results live one page after
 * the code, so stores never touch a translated code page.
 */
	.equ UART, 0x09000000
	.equ PSCI_SYSTEM_OFF, 0x84000008

	.text
_start:
	adr	x0, vectors
	msr	vbar_el1, x0
	/* TCR_EL1: T0SZ = T1SZ = 25 (39-bit halves), TG1 = 4 KB, TBI off. */
	movz	x0, #25
	movk	x0, #25, lsl #16
	orr	x0, x0, #(2 << 30)
	msr	tcr_el1, x0
	movz	x1, #0x3210
	movk	x1, #0x7654, lsl #16
	movk	x1, #0xba98, lsl #32
	movk	x1, #0xfedc, lsl #48
	msr	apiakeylo_el1, x1
	ror	x1, x1, #17
	msr	apiakeyhi_el1, x1
	ror	x1, x1, #11
	msr	apibkeylo_el1, x1
	ror	x1, x1, #7
	msr	apibkeyhi_el1, x1
	ror	x1, x1, #13
	msr	apdakeylo_el1, x1
	ror	x1, x1, #5
	msr	apdakeyhi_el1, x1
	ror	x1, x1, #19
	msr	apgakeylo_el1, x1
	ror	x1, x1, #3
	msr	apgakeyhi_el1, x1
	/* SCTLR_EL1: EnIA (31), EnIB (30), EnDA (27), EnDB (13). */
	mrs	x0, sctlr_el1
	orr	x0, x0, #(1 << 31)
	orr	x0, x0, #(1 << 30)
	orr	x0, x0, #(1 << 27)
	orr	x0, x0, #(1 << 13)
	msr	sctlr_el1, x0
	isb

	adr	x20, results
	movz	x10, #0x7890
	movk	x10, #0x3456, lsl #16
	movk	x10, #0x12, lsl #32		/* P = 0x0000001234567890 */
	movz	x11, #0x5678
	movk	x11, #0x1234, lsl #16
	movk	x11, #0xff80, lsl #32
	movk	x11, #0xffff, lsl #48		/* K = 0xffffff8012345678 */

	mov	x1, x10
	paciza	x1
	str	x1, [x20, #0]			/* pac */
	mov	x2, x1
	autiza	x2
	str	x2, [x20, #8]			/* aut */
	mov	x3, x10
	autiza	x3
	str	x3, [x20, #16]			/* badaut */
	mov	x4, x1
	xpaci	x4
	str	x4, [x20, #24]			/* xpac */
	mov	x16, #0
	pacga	x5, x10, x16
	str	x5, [x20, #32]			/* pacga */
	mov	x6, x10
	pacdza	x6
	str	x6, [x20, #40]			/* dpac */
	mov	x7, x6
	autdza	x7
	str	x7, [x20, #48]			/* daut */
	mov	x8, x10
	autdza	x8
	str	x8, [x20, #56]			/* dbad */
	mov	x9, x11
	paciza	x9
	str	x9, [x20, #64]			/* kpac */
	mov	x12, x9
	xpaci	x12
	str	x12, [x20, #72]			/* kxpac */
	mov	x13, x1
	autizb	x13
	str	x13, [x20, #80]			/* wrongkey */
	adr	x15, faults
	ldr	x14, [x15]
	str	x14, [x20, #88]			/* faults */
	ldr	x14, [x15, #8]
	str	x14, [x20, #96]			/* esr */

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
	mov	x3, #60			/* sixteen hex digits, most significant first */
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

/* Write w2 to the PL011 data register. */
putc:
	movz	x8, #(UART >> 16), lsl #16
	str	w2, [x8]
	ret

names:
	.asciz	"pac"
	.asciz	"aut"
	.asciz	"badaut"
	.asciz	"xpac"
	.asciz	"pacga"
	.asciz	"dpac"
	.asciz	"daut"
	.asciz	"dbad"
	.asciz	"kpac"
	.asciz	"kxpac"
	.asciz	"wrongkey"
	.asciz	"faults"
	.asciz	"esr"
	.byte	0

/* Every exception records ESR_EL1, counts, and resumes after the instruction. */
	.balign	2048
vectors:
	.rept	16
	b	fault
	.balign	128
	.endr
fault:
	mrs	x14, esr_el1
	adr	x15, faults
	str	x14, [x15, #8]
	ldr	x14, [x15]
	add	x14, x14, #1
	str	x14, [x15]
	mrs	x14, elr_el1
	add	x14, x14, #4
	msr	elr_el1, x14
	eret

	.balign	4096
results:
	.zero	128
faults:
	.zero	16
