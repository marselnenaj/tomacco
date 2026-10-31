/* SPDX-License-Identifier: GPL-2.0-or-later
 * Generated from restore-seal-pins.c using clang -target aarch64-linux-gnu
 * -O2 -ffreestanding -fno-builtin -fno-stack-protector -ffixed-x18 -S.
 * Local data uses ADR and resides in .text; only external call relocations
 * remain. The preparer fills the two zeroed pins after verifying firmware.
 * Fixed-arity calls share the Darwin ARM64 ABI. x18 remains untouched.
 * Callsite offsets 0x60e00 and 0x60e34, cave 0x2ce000 in the exact original
 * restored_external SHA-256 recorded by prepare_ios_paravirt_restore.py.
 * This explicitly changes authentication policy for those two pinned inputs.
 */
	.text
	.globl	tomacco_local_seal
	.p2align	2
	.type	tomacco_local_seal,@function
tomacco_local_seal:
	cbz	x0, .LBB0_23
	cbnz	x1, .LBB0_23
	cbnz	x3, .LBB0_23
	sub	sp, sp, #144
	stp	x29, x30, [sp, #48]
	add	x29, sp, #48
	stp	x28, x27, [sp, #64]
	stp	x26, x25, [sp, #80]
	stp	x24, x23, [sp, #96]
	adr	x24, tomacco_seal_pins
	stp	x22, x21, [sp, #112]
	stp	x20, x19, [sp, #128]
	nop
	ldr	w8, [x24]
	cmp	w2, w8
	b.ne	.LBB0_12
	ldr	w22, [x24, #12]
	cbz	w22, .LBB0_12
	ldp	w21, w23, [x24, #4]
	subs	w8, w21, w23
	b.lo	.LBB0_12
	cmp	w22, w8
	b.hi	.LBB0_12
	mov	x20, x0
	mov	x27, x6
	mov	x26, x5
	mov	x25, x4
	mov	w28, w2
	bl	CFDataGetLength
	mov	x8, x0
	mov	w2, w28
	mov	x4, x25
	mov	x5, x26
	mov	x6, x27
	mov	x0, x20
	cmp	x8, x21
	b.ne	.LBB0_12
	bl	CFDataGetBytePtr
	cbz	x0, .LBB0_11
	mov	x2, sp
	mov	w1, w21
	mov	x19, x0
	bl	CC_SHA384
	cbz	x0, .LBB0_11
	mov	x0, sp
	add	x1, x24, #16
	mov	w2, #48
	bl	memcmp
	cbz	w0, .LBB0_24
.LBB0_11:
	mov	x6, x27
	mov	x5, x26
	mov	x4, x25
	mov	w2, w28
	mov	x0, x20
.LBB0_12:
	ldr	w8, [x24, #64]
	cmp	w2, w8
	b.ne	.LBB0_21
	ldr	w22, [x24, #76]
	cbz	w22, .LBB0_21
	ldp	w21, w23, [x24, #68]
	subs	w8, w21, w23
	b.lo	.LBB0_21
	cmp	w22, w8
	b.hi	.LBB0_21
	mov	x20, x0
	mov	x27, x6
	mov	x26, x5
	mov	x25, x4
	mov	w28, w2
	bl	CFDataGetLength
	mov	x8, x0
	mov	w2, w28
	mov	x4, x25
	mov	x5, x26
	mov	x6, x27
	mov	x0, x20
	cmp	x8, x21
	b.ne	.LBB0_21
	bl	CFDataGetBytePtr
	cbz	x0, .LBB0_20
	mov	x2, sp
	mov	w1, w21
	mov	x19, x0
	bl	CC_SHA384
	cbz	x0, .LBB0_20
	mov	x0, sp
	add	x1, x24, #80
	mov	w2, #48
	bl	memcmp
	cbz	w0, .LBB0_24
.LBB0_20:
	mov	x6, x27
	mov	x5, x26
	mov	x4, x25
	mov	w2, w28
	mov	x0, x20
.LBB0_21:
	mov	x1, xzr
	mov	x3, xzr
	bl	original
.LBB0_22:
	ldp	x20, x19, [sp, #128]
	ldp	x22, x21, [sp, #112]
	ldp	x24, x23, [sp, #96]
	ldp	x26, x25, [sp, #80]
	ldp	x28, x27, [sp, #64]
	ldp	x29, x30, [sp, #48]
	add	sp, sp, #144
	ret
.LBB0_23:
	b	original
.LBB0_24:
	mov	x24, x27
	mov	x21, x26
	cbz	x25, .LBB0_27
	mov	w20, w22
	mov	w1, #32003
	mov	x0, x20
	movk	w1, #38875, lsl #16
	bl	malloc_type_malloc
	cbz	x0, .LBB0_22
	add	x1, x19, w23, uxtw
	mov	x2, x20
	mov	x19, x0
	bl	memcpy
	str	x19, [x25]
.LBB0_27:
	cbz	x21, .LBB0_29
	mov	w8, w22
	str	x8, [x21]
.LBB0_29:
	cbz	x24, .LBB0_31
	str	xzr, [x24]
.LBB0_31:
	adr	x0, .L.str
	nop
	bl	puts
	mov	w0, #1
	b	.LBB0_22
.Lfunc_end0:
	.size	tomacco_local_seal, .Lfunc_end0-tomacco_local_seal
	.type	.L.str,@object
	.text
.L.str:
	.asciz	"TOMACCO_LOCAL_SEAL_METADATA_PIN_MATCH: development hash policy; Apple signature NOT verified"
	.size	.L.str, 93
	.p2align 4
	.ascii "seal-pins-v1\0\0\0\0"
tomacco_seal_pins:
	.zero 128
