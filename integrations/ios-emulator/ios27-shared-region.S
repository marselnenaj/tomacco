/* vphone600 23E5207q ARM64 shared-region creation, original base and ASLR.
 * x26: base, x22: size, stack pair: base/size for region + nesting metadata.
 * Original pmap, TXM and VM map functions consume the enlarged size normally.
 */
.text
    mov x26, #0x180000000
    mov x22, #0xa0000000
    movk x22, #1, lsl #32
    fmov d0, x26
    ins v0.d[1], x22
    str q0, [sp, #0x70]
    b original_continue
