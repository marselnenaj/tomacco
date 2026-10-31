// Explicit research policy for locally pinned native libraries.
// Enter after Sandbox's original file_check_mmap decision. x20 is its vnode.
// Only EPERM may change. XNU retrieves the validated code-signing blob with
// its normal vnode locking and pointer authentication; other MAC policies,
// AMFI page validation, mapping protections and sandbox operations remain.
// The preparer appends two/three graphics hashes and, only when explicitly
// selected, the verified owned installer adapter's hash. Four entries fit
// within this adapter's exclusive executable-padding allocation.
.ifndef tomacco_gpu_hash_count
.set tomacco_gpu_hash_count, 2
.endif
.text
.p2align 2
.global tomacco_gpu_mmap
tomacco_gpu_mmap:
    cmp w0, #1
    b.ne done
    stp x0, x30, [sp, #-16]!
    mov x0, x20
    mov w1, #-1
    mov w2, #-1
    mov x3, #0
    bl original_cs_blob_get
    cbz x0, denied
    // CS_VALID | CS_ADHOC | CS_PLATFORM_BINARY | CS_SIGNED.
    ldr w8, [x0, #0x20]
    mov w9, #3
    movk w9, #0x2400, lsl #16
    and w8, w8, w9
    cmp w8, w9
    b.ne denied
    adr x11, allowed_code_hashes
    mov w15, #tomacco_gpu_hash_count
compare_hash:
    add x8, x0, #0x58
    mov x9, x11
    mov w10, #5
compare_word:
    ldr w12, [x8], #4
    ldr w13, [x9], #4
    cmp w12, w13
    b.ne next_hash
    subs w10, w10, #1
    b.ne compare_word
    b allowed
next_hash:
    add x11, x11, #20
    subs w15, w15, #1
    b.ne compare_hash
denied:
    ldp x0, x30, [sp], #16
    b done
allowed:
    ldp x0, x30, [sp], #16
    mov w0, #0
done:
    b original_continue
