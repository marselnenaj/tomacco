/*
 * Runs integrations/ios-emulator/ios27-workloop-discover.S unchanged on
 * QEMU's virt machine, without an OS or firmware, in filt_wlupdate's frame:
 * [sp, #24] knote, [sp, #36] operation, x27 the kevent. Each case sets the
 * knote's kn_sfflags (byte 84) and the kevent's fflags (byte 24) and expects
 * either ESTALE (70) or the continuation with the byte the replaced
 * `ldrb w9, [x27, #24]` loads (0x100 + that byte). Prints
 * "workloop=<hex failures>/<hex cases>" on the PL011 and powers off.
 *
 * The runner prepends the adapter with its continuations renamed to
 * workloop_continue and workloop_stale. Position-independent, one section,
 * loaded raw; the data the cases write lives a page after the code.
 */
	.equ UART, 0x09000000
	.equ PSCI_SYSTEM_OFF, 0x84000008
	.text
_start:
	adr	x0, stack_top
	mov	sp, x0
	adr	x19, cases
	adr	x20, cases_end
	adr	x21, kevent
	adr	x22, knote
	mov	x23, #0			/* failures */
	mov	x24, #0			/* cases */
1:	cmp	x19, x20
	b.hs	2f
	ldp	w0, w1, [x19]		/* operation, kn_sfflags */
	ldp	w2, w3, [x19, #8]	/* fflags, knote present */
	ldr	w25, [x19, #16]		/* expected */
	str	w1, [x22, #84]
	str	w2, [x21, #24]
	mov	x1, x22
	cbnz	w3, 3f
	mov	x1, #0
3:	mov	x2, x21
	bl	workloop_probe
	cmp	w0, w25
	cinc	x23, x23, ne
	/* The adapter only reads. */
	ldp	w0, w1, [x19]
	ldr	w4, [x22, #84]
	ldr	w5, [x21, #24]
	ldr	w2, [x19, #8]
	cmp	w4, w1
	ccmp	w5, w2, #0, eq
	cinc	x23, x23, ne
	add	x24, x24, #1
	add	x19, x19, #20
	b	1b
2:	mov	x0, x23
	mov	x1, x24
	bl	report
	ldr	w0, =PSCI_SYSTEM_OFF
	hvc	#0
	b	.

/* u64 workloop_probe(u64 op, void *knote, void *kev) */
workloop_probe:
	sub	sp, sp, #64
	stp	x27, x30, [sp, #48]
	str	x1, [sp, #24]
	str	w0, [sp, #36]
	mov	x27, x2
	b	tomacco_workloop_discover
workloop_continue:
	add	w0, w9, #0x100
	b	4f
workloop_stale:
	mov	w0, #70
4:	ldp	x27, x30, [sp, #48]
	add	sp, sp, #64
	ret

/* Prints "workloop=<x0 hex>/<x1 hex>\n". */
report:
	mov	x9, #UART
	adr	x10, banner
5:	ldrb	w11, [x10], #1
	cbz	w11, 6f
	strb	w11, [x9]
	b	5b
6:	mov	x12, x0
	mov	x13, #60
7:	lsr	x11, x12, x13
	and	x11, x11, #0xf
	cmp	x11, #10
	add	x14, x11, #'0'
	add	x15, x11, #('a' - 10)
	csel	x11, x14, x15, lo
	strb	w11, [x9]
	subs	x13, x13, #4
	b.pl	7b
	mov	w11, #'/'
	strb	w11, [x9]
	mov	x12, x1
	mov	x13, #60
8:	lsr	x11, x12, x13
	and	x11, x11, #0xf
	cmp	x11, #10
	add	x14, x11, #'0'
	add	x15, x11, #('a' - 10)
	csel	x11, x14, x15, lo
	strb	w11, [x9]
	subs	x13, x13, #4
	b.pl	8b
	mov	w11, #'\n'
	strb	w11, [x9]
	ret

banner:	.asciz	"workloop="
	.p2align 2
/* operation, kn_sfflags, fflags, knote present, expected */
cases:
	.word	1, 0x4, 0x388, 1, 70		/* iOS 27 inner-queue owner discovery of a waiter */
	.word	1, 0x4, 0x188, 1, 70		/* base-queue owner discovery of a waiter */
	.word	0, 0x4, 0x188, 1, 0x188		/* attach */
	.word	2, 0x4, 0x188, 1, 0x188		/* drop */
	.word	1, 0x4, 0x188, 0, 0x188		/* no knote */
	.word	1, 0xc, 0x388, 1, 0x188		/* waiter already woken */
	.word	1, 0x188, 0x388, 1, 0x188	/* the owner's own discovery knote */
	.word	1, 0x0, 0x388, 1, 0x188		/* a knote that never waited */
	.word	1, 0x4, 0x88, 1, 0x188		/* a real handoff wakes the waiter */
	.word	1, 0x4, 0x28, 1, 0x128		/* SYNC_END */
	.word	1, 0x4, 0x108, 1, 0x108		/* no DISCOVER_OWNER */
	.word	1, 0x4, 0x180, 1, 0x180		/* no SYNC_WAKE */
	.word	1, 0x4, 0x8, 1, 0x108		/* SYNC_FAKE */
	.word	3, 0x4, 0x388, 1, 0x188		/* an unknown operation */
cases_end:
	.ltorg
	.p2align 12
kevent:	.fill	72, 1, 0
	.p2align 3
knote:	.fill	96, 1, 0
	.p2align 4
	.fill	4096, 1, 0
stack_top:
