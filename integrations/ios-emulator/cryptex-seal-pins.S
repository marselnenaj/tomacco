/* Local vphone600 research policy, not Apple authentication.
 * Exact original IM4P bytes + exact locally selected IM4M; all other requests
 * continue through validate_payload_and_manifest with unchanged arguments.
 * No stack use, allocation, external calls, or unsigned return-address edits.
 * The preparer supplies the three byte tables and TOMACCO_MANIFEST_BYTES.
 */
.text
.p2align 2
.global tomacco_cryptex_pins
tomacco_cryptex_pins:
    cmp w0, #1
    b.ne fallback
    cmp x3, #229
    b.ne fallback
    mov x8, #TOMACCO_MANIFEST_BYTES
    cmp x5, x8
    b.ne fallback
    cbz x2, fallback
    cbz x4, fallback
    cbz x6, fallback
    cbz x7, fallback
    mov w8, #0x7379
    movk w8, #0x6373, lsl #16
    cmp w1, w8
    adr x13, os_pin
    b.eq compare_manifest
    movk w8, #0x6361, lsl #16
    cmp w1, w8
    b.ne fallback
    adr x13, app_pin
compare_manifest:
    mov x8, x4
    adr x9, manifest_pin
    mov x10, x5
manifest_loop:
    ldrb w11, [x8], #1
    ldrb w12, [x9], #1
    cmp w11, w12
    b.ne fallback
    subs x10, x10, #1
    b.ne manifest_loop
    mov x8, x2
    mov x9, x13
    mov x10, x3
payload_loop:
    ldrb w11, [x8], #1
    ldrb w12, [x9], #1
    cmp w11, w12
    b.ne fallback
    subs x10, x10, #1
    b.ne payload_loop
    add x8, x2, #21
    mov x9, #208
    str x8, [x6]
    str x9, [x7]
    mov w0, #0
    ret
fallback:
    hint #27 /* PACIBSP displaced from the original function entry. */
    b original_continue
