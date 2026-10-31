/*
 * The translation granules a CPU reports, on one CPU of QEMU's virt machine
 * at EL1 (MMU off): prints "mmfr0=<ID_AA64MMFR0_EL1 in hex>" and powers off
 * through PSCI. qemu-arm-target-page-16k's tomacco-granule-16k=on CPU reports
 * TGRAN4 = TGRAN64 = 0xf and TGRAN4_2 = TGRAN64_2 = 1 (not implemented),
 * TGRAN16 and TGRAN16_2 implemented.
 */
	.equ UART, 0x09000000
	.equ PSCI_SYSTEM_OFF, 0x84000008

	.text
_start:
	mrs	x23, id_aa64mmfr0_el1
	adr	x21, name
1:	ldrb	w2, [x21], #1
	cbz	w2, 2f
	bl	putc
	b	1b
2:	mov	w2, #'='
	bl	putc
	mov	x3, #60
3:	lsr	x2, x23, x3
	and	x2, x2, #0xf
	cmp	x2, #10
	add	x4, x2, #'0'
	add	x5, x2, #('a' - 10)
	csel	x2, x4, x5, lo
	bl	putc
	subs	x3, x3, #4
	b.ge	3b
	mov	w2, #'\n'
	bl	putc
	movz	x0, #(PSCI_SYSTEM_OFF & 0xffff)
	movk	x0, #(PSCI_SYSTEM_OFF >> 16), lsl #16
	hvc	#0
4:	wfi
	b	4b

/* Write w2 to the PL011 data register. */
putc:
	movz	x8, #(UART >> 16), lsl #16
	str	w2, [x8]
	ret

name:
	.asciz	"mmfr0"
