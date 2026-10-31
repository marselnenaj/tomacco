/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Tomacco contributors
 *
 * Read-only validation of the unwrapped T8140 BUND v4 boot image. The secure
 * kernel's bootstrap walks 24-byte {offset, size, flags, name} entries starting
 * at 0x270. Bit 1 selects the executable bank, bit 0 the writable bank. The
 * original image (including its header) must be retained; extracting just a
 * Mach-O loses the scheduler, services and the bootstrap range table.
 *
 * This parser does not authenticate firmware or implement any secure service.
 */
#ifndef TOMACCO_EXCLAVE_BUNDLE_H
#define TOMACCO_EXCLAVE_BUNDLE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define TOMACCO_CL4_MAX_SIZE (256u * 1024u * 1024u)
#define TOMACCO_CL4_PAGE 0x4000u
#define TOMACCO_CL4_TABLE 0x270u
#define TOMACCO_CL4_MAX_RANGES 16u

typedef struct TomaccoCl4Layout {
    uint64_t rx_size, rw_size, virt, entry;
    uint32_t ranges;
} TomaccoCl4Layout;

static inline uint32_t tomacco_cl4_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
           (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static inline uint64_t tomacco_cl4_u64(const uint8_t *p)
{
    return tomacco_cl4_u32(p) | (uint64_t)tomacco_cl4_u32(p + 4) << 32;
}

/* Output is unchanged on failure. All file arithmetic is bounded before use. */
static inline const char *tomacco_cl4_parse(const uint8_t *p, size_t length,
                                         TomaccoCl4Layout *result)
{
    TomaccoCl4Layout layout = {0};
    uint64_t cursor = 0, kernel_text = 0, kernel_data = 0, metadata = 0;
    uint64_t text_va = 0, data_va = 0, data_size = 0;
    bool writable = false, text_seen = false, data_seen = false, entry_seen = false;
    uint32_t count, commands, command_bytes;
    size_t pos, end;

    if (!p || !result || length < TOMACCO_CL4_PAGE ||
        length > TOMACCO_CL4_MAX_SIZE || length % TOMACCO_CL4_PAGE) {
        return "CL4 bundle size is invalid";
    }
    if (tomacco_cl4_u32(p + 8) != 0x42554e44 ||
        tomacco_cl4_u32(p + 12) != 0x00040000 ||
        tomacco_cl4_u32(p + 16) != 1) {
        return "CL4 requires an unwrapped BUND v4 image";
    }
    count = tomacco_cl4_u32(p + 20);
    if (count < 4 || count > TOMACCO_CL4_MAX_RANGES) {
        return "CL4 range count is unsupported";
    }
    for (uint32_t i = 0; i < count; i++) {
        const uint8_t *range = p + TOMACCO_CL4_TABLE + i * 24;
        uint64_t offset = tomacco_cl4_u64(range);
        uint64_t size = tomacco_cl4_u64(range + 8);
        uint32_t flags = tomacco_cl4_u32(range + 16);
        uint32_t name = tomacco_cl4_u32(range + 20);
        if (!size || size % TOMACCO_CL4_PAGE || offset != cursor ||
            size > length - cursor) {
            return "CL4 ranges must cover the image once, in page-aligned order";
        }
        if (flags == 10 || flags == 14) {
            if (writable) {
                return "CL4 executable range follows writable memory";
            }
            layout.rx_size += size;
        } else if (flags == 17 || flags == 21) {
            writable = true;
            layout.rw_size += size;
        } else {
            return "CL4 range permissions are unsupported";
        }
        if (name == 0x6b747874) { /* ktxt */
            if (i != 0 || flags != 10 || kernel_text) {
                return "CL4 kernel text range is invalid";
            }
            kernel_text = size;
        } else if (name == 0x6b646174) { /* kdat */
            if (flags != 17 || kernel_data || layout.rw_size != size) {
                return "CL4 kernel data must begin the writable bank";
            }
            kernel_data = size;
        } else if (name == 0x7562646c) { /* ubdl */
            if (i != count - 1 || flags != 21 || metadata) {
                return "CL4 metadata range is invalid";
            }
            metadata = offset;
        }
        cursor += size;
    }
    if (cursor != length || !kernel_text || !kernel_data || !metadata ||
        kernel_text != tomacco_cl4_u64(p + 32) ||
        kernel_data != tomacco_cl4_u64(p + 72)) {
        return "CL4 kernel banks disagree with the bundle header";
    }
    /* The kernel Mach-O header is metadata. Its text bytes are at bundle offset
     * zero, not at metadata + the Mach-O file offset. Never load that offset. */
    pos = (size_t)metadata;
    if (length - pos < 32 || tomacco_cl4_u32(p + pos) != 0xfeedfacf ||
        tomacco_cl4_u32(p + pos + 4) != 0x0100000c ||
        tomacco_cl4_u32(p + pos + 12) != 5) { /* MH_PRELOAD */
        return "CL4 kernel metadata is not an ARM64 preload image";
    }
    commands = tomacco_cl4_u32(p + pos + 16);
    command_bytes = tomacco_cl4_u32(p + pos + 20);
    pos += 32;
    if (!commands || commands > 128 || command_bytes > length - pos ||
        command_bytes > TOMACCO_CL4_PAGE - 32) {
        return "CL4 kernel load commands are invalid";
    }
    end = pos + command_bytes;
    for (uint32_t i = 0; i < commands; i++) {
        uint32_t cmd, size;
        if (end - pos < 8) {
            return "CL4 kernel load command is truncated";
        }
        cmd = tomacco_cl4_u32(p + pos);
        size = tomacco_cl4_u32(p + pos + 4);
        if (size < 8 || size % 8 || size > end - pos) {
            return "CL4 kernel load command size is invalid";
        }
        if (cmd == 0x19) { /* LC_SEGMENT_64 */
            if (size < 72 || tomacco_cl4_u32(p + pos + 64) > (size - 72) / 80) {
                return "CL4 kernel segment is truncated";
            }
            if (!memcmp(p + pos + 8, "__TEXT\0\0\0\0\0\0\0\0\0\0", 16)) {
                if (text_seen || tomacco_cl4_u64(p + pos + 32) != kernel_text) {
                    return "CL4 kernel text size mismatch";
                }
                text_seen = true;
                text_va = tomacco_cl4_u64(p + pos + 24);
            } else if (!memcmp(p + pos + 8, "__DATA\0\0\0\0\0\0\0\0\0\0", 16)) {
                if (data_seen) {
                    return "CL4 kernel data segment is duplicated";
                }
                data_seen = true;
                data_va = tomacco_cl4_u64(p + pos + 24);
                data_size = tomacco_cl4_u64(p + pos + 32);
            }
        } else if (cmd == 5) { /* LC_UNIXTHREAD, ARM_THREAD_STATE64 */
            if (entry_seen || size != 288 || tomacco_cl4_u32(p + pos + 8) != 6 ||
                tomacco_cl4_u32(p + pos + 12) != 68) {
                return "CL4 kernel entry state is invalid";
            }
            entry_seen = true;
            layout.entry = tomacco_cl4_u64(p + pos + 272);
        }
        pos += size;
    }
    if (pos != end || !text_seen || !data_seen || !entry_seen ||
        text_va % TOMACCO_CL4_PAGE || text_va > UINT64_MAX - kernel_text ||
        data_va != text_va + kernel_text || data_size != kernel_data ||
        data_va > UINT64_MAX - data_size ||
        layout.entry < text_va || layout.entry - text_va >= kernel_text ||
        layout.entry % 4 || layout.entry - text_va < TOMACCO_CL4_TABLE + count * 24) {
        return "CL4 kernel entry or virtual layout is invalid";
    }
    /* The bundle's first instruction must branch to that same entry point. */
    if (tomacco_cl4_u32(p) >> 26 != 5 ||
        (uint64_t)(tomacco_cl4_u32(p) & 0x03ffffff) * 4 != layout.entry - text_va) {
        return "CL4 bundle branch disagrees with the kernel entry";
    }
    layout.virt = text_va;
    layout.ranges = count;
    *result = layout;
    return NULL;
}

#endif
