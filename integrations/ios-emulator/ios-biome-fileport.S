// Private compatibility for one pinned original iOS 27.0.1 service.
// The original MAC check has already executed. Only its EPERM for syscall
// 430 and this exact platform code identity is adapted. The native fileport
// handler still checks descriptor ownership, type, guards and copyout.
.text
.p2align 2
.global tomacco_biome_fileport
tomacco_biome_fileport:
    cmp w0, #1
    b.ne .Loriginal
    and w8, w27, #0xffff
    cmp w8, #430
    b.ne .Loriginal
    cbz x21, .Loriginal
    // The pinned proc prefix carries the cached effective uid at +0x28.
    ldr w8, [x21, #0x28]
    cmp w8, #501
    b.ne .Loriginal
    stp x0, x30, [sp, #-16]!
    mov x0, x21
    bl original_csproc_get_blob
    cbz x0, .Lno_identity
    ldr w8, [x0, #0x20]
    mov w9, #1
    movk w9, #0x2400, lsl #16
    and w8, w8, w9
    cmp w8, w9
    b.ne .Lno_identity
    add x8, x0, #0x58
    adr x9, biome_code_hash
    mov w10, #5
.Lcompare:
    ldr w11, [x8], #4
    ldr w12, [x9], #4
    cmp w11, w12
    b.ne .Lno_identity
    subs w10, w10, #1
    b.ne .Lcompare
    ldp x0, x30, [sp], #16
    mov w0, #0
    b .Loriginal
.Lno_identity:
    ldp x0, x30, [sp], #16
.Loriginal:
    // Replay the displaced instruction and its original continuation.
    mov x25, x0
    b original_continue
