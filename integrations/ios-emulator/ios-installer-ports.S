// Opt-in compatibility for the pinned iOS installer service controller.
// Retain the original MIG handler and its real registered-port reply.
.text
.p2align 2
.global tomacco_installer_ports
tomacco_installer_ports:
    cbz w0, .Linstaller_allowed
    cmp w0, #53
    b.ne .Linstaller_denied
    cmp w28, #3404
    b.ne .Linstaller_denied
    ldr x8, [sp, #0x28]
    cbz x8, .Linstaller_denied
    stp x0, x30, [sp, #-16]!
    mov x0, x8
    bl original_csproc_get_blob
    cbz x0, .Linstaller_no_identity
    // Require CS_VALID | CS_SIGNED | CS_PLATFORM_BINARY on the real cs_blob.
    ldr w8, [x0, #0x20]
    mov w9, #1
    movk w9, #0x2400, lsl #16
    and w8, w8, w9
    cmp w8, w9
    b.ne .Linstaller_no_identity
    add x8, x0, #0x58
    adr x9, installer_code_hash
    mov w10, #5
.Linstaller_compare:
    ldr w11, [x8], #4
    ldr w12, [x9], #4
    cmp w11, w12
    b.ne .Linstaller_no_identity
    subs w10, w10, #1
    b.ne .Linstaller_compare
    ldp x0, x30, [sp], #16
    b .Linstaller_allowed
.Linstaller_no_identity:
    ldp x0, x30, [sp], #16
.Linstaller_denied:
    b original_continue
.Linstaller_allowed:
    b original_success
