# Format-aware image copies into guest pages

`reims-texel-copy-regions.patch` fixes padded image writeback for texels wider
or narrower than four bytes. It applies after the fixed-panel patch.

The guest owns each page run independently. Vulkan image-to-buffer copies must
stay within the requested run, even when the host imports one large RAM block.
Import bounds do not grant access to unrelated pages inside that block.

The paging owner now carries bytes per texel together with pixel dimensions
and byte pitch. All column, row and merge calculations use that width. Its
fallible planner rejects zero-sized texels, overlapping rows, pitch or ranges
that split a texel, and overflowing extents with typed reasons. Invalid plans
never reach GPU submission. Padding is preserved; contiguous full rows may
merge only when their entire byte span remains inside the same run.

The Vulkan destination derives this width from its existing image-format
layout, carries a planner refusal through GuestWriteDecline, and counts the
actual number of transferred bytes in its readback metrics.

## Regression

A 1170x2532 RGBA16Float surface has eight-byte texels and a 9408-byte padded
pitch. In a 16 KiB page run, the previous four-byte planner merged two full
rows, producing an 18,768-byte Vulkan copy. This overwrote adjacent guest
memory. The regression test failed on that exact span before the fix.

The owner tests check exact coverage and bounds for 1, 2, 4, 8 and 16 bytes per
texel, dense and padded rows, 4 KiB and 16 KiB page runs and allocation heads
inside a page. An engine test uses the original half-float display dimensions
and format. Malformed layouts must return their specific refusal.

These tests establish the copy-boundary invariant, not unrelated formats or GPU paths.
