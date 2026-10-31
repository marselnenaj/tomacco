/* Match the split-ring layout used by AppleVirtIOQueue's virtual accessor.
 * x0: available-ring physical address; x8: queue alignment; x19: queue owner.
 * Include flags, index, all available entries and used_event before rounding
 * up. Rounding the available-ring header alone can overlap the two rings.
 * The original continuation applies the alignment mask and returns normally.
 */
.text
.p2align 2
.global tomacco_virtio_used_ring
tomacco_virtio_used_ring:
    ldr w9, [x19, #0x3c]
    add x9, x0, x9, lsl #1
    add x9, x9, x8
    add x9, x9, #5
    b original_continue
