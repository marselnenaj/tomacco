/*
 * WFE in a load-exclusive spin loop, on two CPUs of QEMU's virt machine,
 * without an OS or firmware (environment as litmus-store-release-load-
 * acquire.S: MMU off, EL1, PSCI over HVC).
 *
 * handoff  ROUNDS times CPU 1 waits for a lock word the way XNU's ticket
 *          locks do (LDAXR; compare; WFE; retry) while CPU 0 holds it for
 *          HOLD (200 us of CNTVCT), then releases it with STLR. CPU 1
 *          counts its WFEs and the ticks from the release to its
 *          acquisition.
 * timeout  CPU 1 arms the monitor on a word nobody writes and executes one
 *          WFE: the ticks it took (no event can come, so a waiting engine
 *          returns at its bound and an upstream one at once).
 * changed  CPU 1 arms the monitor, writes the monitored word itself and
 *          executes WFE: the ticks it took (the location already changed,
 *          which must end the wait at once).
 * sev      CPU 1 loops on LDAXR of the unwritten word and WFE until a flag
 *          (another word) is set; CPU 0 sets it and executes SEV 20 us
 *          after CPU 1 is armed: the ticks from the SEV to CPU 1 leaving
 *          its loop (the SEV, not the bound, must end the wait).
 *
 * Prints "cntfrq=<hex> rounds=<hex> wfes=<hex> latency=<hex> timeout=<hex>
 * changed=<hex> sev=<hex>" on the PL011 and powers off through PSCI.
 * tests/python/test_engine_branch.py WaitForEventTests runs it. The runner loads
 * the raw image at 0x40200000; shared words live in their own page.
 */
	.equ ROUNDS, 200
	.equ UART, 0x09000000
	.equ PSCI_CPU_ON, 0xc4000003
	.equ PSCI_SYSTEM_OFF, 0x84000008

	.text
_start:
	mrs	x0, mpidr_el1
	and	x0, x0, #0xff
	adr	x10, shared
	add	x11, x10, #0x000		/* lock: round number once released */
	add	x12, x10, #0x040		/* ready: round CPU 1 waits for */
	add	x13, x10, #0x080		/* done: round CPU 1 acquired */
	add	x14, x10, #0x0c0		/* release time of the round */
	add	x15, x10, #0x100		/* results, written by CPU 1 */
	add	x16, x10, #0x200		/* a word nobody writes */
	cbnz	x0, secondary

	movz	x0, #(PSCI_CPU_ON & 0xffff)
	movk	x0, #(PSCI_CPU_ON >> 16), lsl #16
	mov	x1, #1
	adr	x2, _start
	mov	x3, #0
	hvc	#0

	mrs	x20, cntfrq_el0
	mov	x0, #5000
	udiv	x21, x20, x0			/* HOLD: 200 us in ticks */
	mov	x22, #1				/* round */
round:
1:	ldar	x0, [x12]			/* CPU 1 announced this round */
	cmp	x0, x22
	b.ne	1b
	isb
	mrs	x1, cntvct_el0
	add	x1, x1, x21
2:	isb					/* hold the lock for HOLD */
	mrs	x0, cntvct_el0
	cmp	x0, x1
	b.lo	2b
	isb
	mrs	x0, cntvct_el0
	str	x0, [x14]
	stlr	x22, [x11]			/* release */
3:	ldar	x0, [x13]
	cmp	x0, x22
	b.ne	3b
	add	x22, x22, #1
	cmp	x22, #ROUNDS
	b.ls	round
	mov	x0, #ROUNDS
	str	x0, [x15, #8]
	mov	x0, #-1
	stlr	x0, [x12]			/* CPU 1: run the single-CPU probes */
4:	ldar	x0, [x13]
	cmn	x0, #1
	b.ne	4b

	/* sev: CPU 1 announces -2 once armed; 20 us later flag and SEV */
11:	ldar	x0, [x12]
	cmn	x0, #2
	b.ne	11b
	mov	x0, #50000
	udiv	x0, x20, x0
	isb
	mrs	x1, cntvct_el0
	add	x1, x1, x0
12:	isb
	mrs	x0, cntvct_el0
	cmp	x0, x1
	b.lo	12b
	add	x17, x10, #0x240		/* flag */
	mov	x0, #1
	str	x0, [x17]
	isb
	mrs	x0, cntvct_el0
	str	x0, [x14]
	dsb	ish
	sev
13:	ldar	x0, [x13]
	cmn	x0, #2
	b.ne	13b

	str	x20, [x15, #0]
	adr	x21, names
	mov	x22, #0
5:	ldrb	w2, [x21]
	cbz	w2, 8f
6:	ldrb	w2, [x21], #1
	cbz	w2, 7f
	bl	putc
	b	6b
7:	mov	w2, #'='
	bl	putc
	ldr	x23, [x15, x22]
	mov	x3, #60
9:	lsr	x2, x23, x3
	and	x2, x2, #0xf
	cmp	x2, #10
	add	x4, x2, #'0'
	add	x5, x2, #('a' - 10)
	csel	x2, x4, x5, lo
	bl	putc
	subs	x3, x3, #4
	b.ge	9b
	mov	w2, #' '
	bl	putc
	add	x22, x22, #8
	b	5b
8:	mov	w2, #'\n'
	bl	putc
	movz	x0, #(PSCI_SYSTEM_OFF & 0xffff)
	movk	x0, #(PSCI_SYSTEM_OFF >> 16), lsl #16
	hvc	#0
10:	wfi
	b	10b

secondary:
	mov	x20, #0				/* WFEs */
	mov	x21, #0				/* release-to-acquire ticks */
	mov	x22, #1				/* round */
s_round:
	stlr	x22, [x12]			/* waiting for this round */
1:	ldaxr	x0, [x11]
	cmp	x0, x22
	b.eq	2f
	wfe
	add	x20, x20, #1
	b	1b
2:	clrex
	isb
	mrs	x1, cntvct_el0
	ldr	x0, [x14]
	sub	x1, x1, x0
	add	x21, x21, x1
	stlr	x22, [x13]
	add	x22, x22, #1
	cmp	x22, #ROUNDS
	b.ls	s_round
	str	x20, [x15, #16]
	str	x21, [x15, #24]
3:	ldar	x0, [x12]
	cmn	x0, #1
	b.ne	3b

	/* timeout: nobody writes [x16] */
	isb
	mrs	x1, cntvct_el0
	ldaxr	x0, [x16]
	wfe
	isb
	mrs	x2, cntvct_el0
	clrex
	sub	x2, x2, x1
	str	x2, [x15, #32]

	/* changed: the monitored word is written before the WFE */
	isb
	mrs	x1, cntvct_el0
	ldaxr	x0, [x16]
	add	x0, x0, #1
	str	x0, [x16]
	wfe
	isb
	mrs	x2, cntvct_el0
	clrex
	sub	x2, x2, x1
	str	x2, [x15, #40]

	mov	x0, #-1
	stlr	x0, [x13]

	/* sev */
	add	x17, x10, #0x240		/* flag */
	mov	x0, #-2
	stlr	x0, [x12]
5:	ldaxr	x0, [x16]
	ldr	x1, [x17]
	cbnz	x1, 6f
	wfe
	b	5b
6:	clrex
	isb
	mrs	x1, cntvct_el0
	ldr	x0, [x14]
	sub	x1, x1, x0
	str	x1, [x15, #48]
	mov	x0, #-2
	stlr	x0, [x13]
4:	wfi
	b	4b

/* Write w2 to the PL011 data register. */
putc:
	movz	x8, #(UART >> 16), lsl #16
	str	w2, [x8]
	ret

names:
	.asciz	"cntfrq"
	.asciz	"rounds"
	.asciz	"wfes"
	.asciz	"latency"
	.asciz	"timeout"
	.asciz	"changed"
	.asciz	"sev"
	.byte	0

	.balign	4096
shared:
	.zero	4096
