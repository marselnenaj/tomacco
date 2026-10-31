/* Keep an owner discovery from waking that owner's own sync wait.
 *
 * Runs in filt_wlupdate in place of `ldrb w9, [x27, #24]`, after the debounce
 * value matched. x27: the struct kevent_qos_s being applied; [sp, #24]: the
 * knote; [sp, #36]: the operation (FILT_WLTOUCH is 1).
 *
 * libdispatch announces the owner of a queue held in uncontended sync with
 * SYNC_WAKE | DISCOVER_OWNER | IGNORE_ESTALE, a knote for that owner on the
 * waiter's workloop. On iOS 27 the owner of an inner queue can itself be
 * blocked in SYNC_WAIT on that same workloop (its knote has SYNC_WAIT and no
 * SYNC_WAKE yet); the discovery then touches the waiting knote and its
 * SYNC_WAKE wakes the owner as if the workloop had been handed to it, so two
 * threads own one queue (seen in locationd). A discovery that would
 * touch a still-waiting knote is answered like a debounce mismatch instead:
 * ESTALE, which the IGNORE_ESTALE caller drops without effect, as libdispatch
 * already handles. A real handoff (SYNC_WAKE | DISCOVER_OWNER, no
 * IGNORE_ESTALE), attach, drop and every other operation run unchanged.
 * Clobbers only w9 and the flags, like the instruction it replaces.
 */
.text
.p2align 2
.global tomacco_workloop_discover
tomacco_workloop_discover:
    ldr w9, [sp, #36]
    cmp w9, #1
    b.ne 1f
    ldr w9, [x27, #24]
    tbz w9, #3, 1f
    tbz w9, #7, 1f
    tbz w9, #8, 1f
    ldr x9, [sp, #24]
    cbz x9, 1f
    ldr w9, [x9, #84]
    and w9, w9, #0xc
    cmp w9, #4
    b.ne 1f
    b original_success
1:
    ldrb w9, [x27, #24]
    b original_continue
