/* SPDX-License-Identifier: GPL-2.0-or-later
 * ARM64e adapter for restored_external 23E5207q, callsite 0x100029c50.
 * Generated from restore-format-retry.c with clang -target arm64e-apple-ios26.0 -O2.
 * The final x19 assignment updates clean_NAND's live descriptor after reopen.
 * It is deliberately specific to this hash-guarded callsite, not a C ABI export.
 */
.text
	.globl	_tomacco_format_retry
	.p2align	2
_tomacco_format_retry:
	pacibsp
	stp	x28, x27, [sp, #-96]!
	stp	x26, x25, [sp, #16]
	stp	x24, x23, [sp, #32]
	stp	x22, x21, [sp, #48]
	stp	x20, x19, [sp, #64]
	stp	x29, x30, [sp, #80]
	add	x29, sp, #80
	sub	sp, sp, #1072
	mov	x20, x1
	mov	x19, x0
	add	x8, x29, #16
	orr	x8, x8, #0x8
	stur	x8, [x29, #-88]
	ldr	x23, [x29, #16]
	add	x8, sp, #40
	str	x8, [sp]
	mov	w1, #50
	bl	_fcntl
	mov	x21, x0
	mov	w24, #0
	cmp	w0, #0
	cset	w26, eq
	mov	w25, #1
Lloh0:
	adr x22, l_.str.1
Lloh1:
	nop
LBB0_1:
	tbz	w19, #31, LBB0_3
	add	x0, sp, #40
	mov	w1, #0
	bl	_open
	mov	x19, x0
	tbnz	w0, #31, LBB0_9
LBB0_3:
	str	x23, [sp]
	mov	x0, x19
	mov	x1, x20
	bl	_ioctl
	cmn	w0, #1
	b.ne	LBB0_13
	bl	___error
	ldr	w27, [x0]
	stp	x27, x26, [sp, #16]
	stp	x19, x25, [sp]
	mov	x0, x22
	bl	_printf
	bl	___error
	str	w27, [x0]
	mov	w0, #-1
	cmp	w25, #50
	b.eq	LBB0_15
	cmp	w27, #16
	b.ne	LBB0_15
	cbnz	w21, LBB0_15
	add	w24, w24, #1
	mov	x0, x19
	bl	_close
	mov	w0, #10000
	bl	_usleep
	mov	w19, #-1
LBB0_8:
	add	w25, w25, #1
	cmp	w25, #51
	b.ne	LBB0_1
	b	LBB0_12
LBB0_9:
	bl	___error
	mov	x8, x0
	mov	w0, #-1
	cmp	w25, #50
	b.eq	LBB0_15
	ldr	w8, [x8]
	and	w8, w8, #0xfffffffb
	cmp	w8, #2
	b.ne	LBB0_15
	mov	w0, #10000
	bl	_usleep
	add	w24, w24, #1
	b	LBB0_8
LBB0_12:
	mov	w0, #-1
	b	LBB0_15
LBB0_13:
	cmp	w25, #1
	b.eq	LBB0_15
	mov	x8, x19
	stp	x8, x25, [sp]
Lloh2:
	adr x8, l_.str
Lloh3:
	nop
	mov	x20, x0
	mov	x0, x8
	bl	_printf
	mov	x0, x20
LBB0_15:
	mov	w8, w0
	orr	x0, x8, x19, lsl #32
	add	sp, sp, #1072
	ldp	x29, x30, [sp, #80]
	ldp	x20, x19, [sp, #64]
	ldp	x22, x21, [sp, #48]
	ldp	x24, x23, [sp, #32]
	ldp	x26, x25, [sp, #16]
	ldp	x28, x27, [sp], #96
	lsr x19, x0, #32
	mov w0, w0
	retab
l_.str:
	.asciz	"TOMACCO_FORMAT_RECOVERED fd=%d attempt=%d\n"
l_.str.1:
	.asciz	"TOMACCO_FORMAT_RETRY fd=%d attempt=%d errno=%d path_ready=%d\n"
