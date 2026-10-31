/*
 * Pointer authentication sweep on one CPU of QEMU's virt machine, at EL1
 * with the MMU off (see pac-probe.S for the environment).
 *
 * For each entry of `configs` (a TCR_EL1 value and the SCTLR_EL1 key-enable
 * bits EnIA/EnIB/EnDA/EnDB) it writes both registers and then, for 64
 * pseudo-random pointers x and modifiers m (a 64-bit LCG), runs
 *
 *   AUTIA, AUTIB, AUTDA, AUTDB x, m   AUTIZA, AUTDZB x
 *   XPACI, XPACD x                    PACIA, PACDB x, m
 *   AUTIA1716 (x17 = x, x16 = m)      AUTIBSP (x30 = x, sp)
 *   XPACLRI (x30 = x)                 PACIBSP (x30 = x)
 *   LDRAA [x] with x = &cell | random bits 54:48 (only when EnDA is set:
 *   a disabled key would dereference the PAC bits)
 *
 * and folds every result into one checksum, acc = ror(acc, 7) ^ result,
 * printed as "c<entry>=<16 hex digits>". Each new TCR/SCTLR value has to
 * reach the instructions that follow: the configurations change the address
 * size of both halves (T0SZ/T1SZ, including values the architecture clamps),
 * the granules, top-byte ignore for data and instructions (TBI/TBID) and
 * which keys are enabled, and the last one repeats the first.
 * tests/python/test_engine_pac.py computes the same checksums from the
 * bypass's rules and compares.
 */
	.equ UART, 0x09000000
	.equ PSCI_SYSTEM_OFF, 0x84000008
	.equ EN_IA, (1 << 31)
	.equ EN_IB, (1 << 30)
	.equ EN_DA, (1 << 27)
	.equ EN_DB, (1 << 13)
	.equ ROUNDS, 64

	.text
_start:
	adr	x0, stack_top
	mov	sp, x0
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
	msr	apdbkeylo_el1, x1
	ror	x1, x1, #3
	msr	apdbkeyhi_el1, x1
	isb

	adr	x19, configs
	mov	x20, #0				/* entry index */
	/* LCG: x = x * 6364136223846793005 + 1442695040888963407 */
	movz	x24, #0x7f2d
	movk	x24, #0x4c95, lsl #16
	movk	x24, #0xf42d, lsl #32
	movk	x24, #0x5851, lsl #48
	movz	x25, #0x814f
	movk	x25, #0xf767, lsl #16
	movk	x25, #0x7b7e, lsl #32
	movk	x25, #0x1405, lsl #48
	movz	x26, #0x1234			/* LCG state, carried across entries */
next_config:
	ldp	x0, x1, [x19], #16
	cbz	x0, done
	msr	tcr_el1, x0
	mrs	x2, sctlr_el1
	movz	x3, #(EN_DB)
	movk	x3, #((EN_IA | EN_IB | EN_DA) >> 16), lsl #16
	bic	x2, x2, x3
	orr	x2, x2, x1
	msr	sctlr_el1, x2
	isb
	mov	x27, x1				/* key bits of this entry */
	mov	x21, #0				/* checksum */
	mov	x22, #ROUNDS
round:
	madd	x26, x26, x24, x25
	mov	x9, x26				/* x */
	madd	x26, x26, x24, x25
	mov	x10, x26			/* m */

	mov	x0, x9
	autia	x0, x10
	bl	fold
	mov	x0, x9
	autib	x0, x10
	bl	fold
	mov	x0, x9
	autda	x0, x10
	bl	fold
	mov	x0, x9
	autdb	x0, x10
	bl	fold
	mov	x0, x9
	autiza	x0
	bl	fold
	mov	x0, x9
	autdzb	x0
	bl	fold
	mov	x0, x9
	xpaci	x0
	bl	fold
	mov	x0, x9
	xpacd	x0
	bl	fold
	mov	x0, x9
	pacia	x0, x10
	bl	fold
	mov	x0, x9
	pacdb	x0, x10
	bl	fold
	mov	x17, x9
	mov	x16, x10
	autia1716
	mov	x0, x17
	bl	fold
	mov	x23, x30			/* the hint forms below use x30 */
	mov	x30, x9
	autibsp
	mov	x0, x30
	mov	x30, x23
	bl	fold
	mov	x30, x9
	xpaclri
	mov	x0, x30
	mov	x30, x23
	bl	fold
	mov	x30, x9
	pacibsp
	mov	x0, x30
	mov	x30, x23
	bl	fold
	tbz	x27, #27, 1f			/* LDRAA needs EnDA */
	adr	x0, cell
	and	x1, x9, #0x007f000000000000	/* bits 54:48: PAC bits in every entry's lower half */
	orr	x0, x0, x1
	ldraa	x0, [x0]
	bl	fold
1:	subs	x22, x22, #1
	b.ne	round

	/* "c<index>=<checksum> " */
	mov	w2, #'c'
	bl	putc
	add	w2, w20, #'0'
	bl	putc
	mov	w2, #'='
	bl	putc
	mov	x3, #60
2:	lsr	x2, x21, x3
	and	x2, x2, #0xf
	cmp	x2, #10
	add	x4, x2, #'0'
	add	x5, x2, #('a' - 10)
	csel	x2, x4, x5, lo
	bl	putc
	subs	x3, x3, #4
	b.ge	2b
	mov	w2, #' '
	bl	putc
	add	x20, x20, #1
	b	next_config

done:
	mov	w2, #'\n'
	bl	putc
	movz	x0, #(PSCI_SYSTEM_OFF & 0xffff)
	movk	x0, #(PSCI_SYSTEM_OFF >> 16), lsl #16
	hvc	#0
3:	wfi
	b	3b

/* acc = ror(acc, 7) ^ x0 */
fold:
	ror	x21, x21, #7
	eor	x21, x21, x0
	ret

/* Write w2 to the PL011 data register. */
putc:
	movz	x8, #(UART >> 16), lsl #16
	str	w2, [x8]
	ret

/*
 * TCR_EL1 fields: T0SZ 5:0, TG0 15:14 (0 4K, 1 64K, 2 16K), T1SZ 21:16,
 * TG1 31:30 (1 16K, 2 4K, 3 64K), TBI0 37, TBI1 38, TBID0 51, TBID1 52.
 */
	.balign	16
configs:
	/* 0: 39-bit halves, 4K, no TBI, every key. */
	.quad	(25 | (25 << 16) | (2 << 30)), (EN_IA | EN_IB | EN_DA | EN_DB)
	/* 1: iOS-like 16K, 47-bit user and 39-bit kernel halves, TBI0, keys A. */
	.quad	(17 | (2 << 14) | (25 << 16) | (1 << 30) | (1 << 37)), (EN_IA | EN_DA)
	/* 2: T0SZ 10 and T1SZ 50 (clamped to 16 and 48), TBI1 for data only, keys B. */
	.quad	(10 | (50 << 16) | (2 << 30) | (1 << 38) | (1 << 52)), (EN_IB | EN_DB)
	/* 3: 64K both halves, T1SZ 13, TBI both, TBID0, every key. */
	.quad	(25 | (1 << 14) | (13 << 16) | (3 << 30) | (3 << 37) | (1 << 51)), (EN_IA | EN_IB | EN_DA | EN_DB)
	/* 4: as 0 without any key: every instruction is a no-op. */
	.quad	(25 | (25 << 16) | (2 << 30)), 0
	/* 5: TBI both, only key IA. */
	.quad	(25 | (25 << 16) | (2 << 30) | (3 << 37)), EN_IA
	/* 6: 0 again. */
	.quad	(25 | (25 << 16) | (2 << 30)), (EN_IA | EN_IB | EN_DA | EN_DB)
	.quad	0, 0

	.balign	4096
cell:
	.quad	0x0123456789abcdef
	.balign	4096
	.zero	4096
stack_top:
