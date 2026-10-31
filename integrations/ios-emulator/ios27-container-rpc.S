// The iOS 27 service no longer implements the research kernel's legacy
// container RPC. At this platform-app-only call site, tolerate precisely
// EBADRPC (72). Keep every other failure and the remaining sandbox setup.
.text
.p2align 2
.global tomacco_container_rpc
tomacco_container_rpc:
    cbz w0, success
    cmp w0, #72
    b.ne failure
    mov w0, #0
success:
    b original_success
failure:
    b original_continue
