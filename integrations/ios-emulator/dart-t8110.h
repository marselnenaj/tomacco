/* SPDX-License-Identifier: MIT
 * Tomacco's software T8110 DART translation core.
 * Register/page-table ABI references: Asahi Linux m1n1 hw/dart8110.py
 * (MIT) and Linux drivers/iommu/apple-dart.c. This is an IOMMU, not a GPU.
 * All state is owned by the emulator; table reads must access guest RAM only.
 */
#ifndef TOMACCO_DART_T8110_H
#define TOMACCO_DART_T8110_H
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define PDART_STREAMS 256
#define PDART_PAGE 0x4000u
#define PDART_PA_MASK ((UINT64_C(1) << 42) - 1)
#define PDART_FLAG (1u << 31)

typedef struct TomaccoDart {
    uint32_t tcr[PDART_STREAMS], ttbr[PDART_STREAMS], enabled[8];
    uint32_t error, error_mask, error_streams[8];
    uint64_t error_address;
    uint32_t protect, protect_lock, tlb_command;
} TomaccoDart;

typedef struct TomaccoDartTranslation {
    uint64_t address, mask;
    unsigned permissions; /* 1 = read, 2 = write */
} TomaccoDartTranslation;

typedef bool (*TomaccoDartReadTable)(void *, uint64_t, uint64_t *);

static inline void pdart_reset(TomaccoDart *s)
{
    memset(s, 0, sizeof(*s));
}

static inline bool pdart_irq(const TomaccoDart *s)
{
    return (s->error & PDART_FLAG) &&
           (s->error & ~s->error_mask & 0x7fffu);
}

static inline void pdart_fault(TomaccoDart *s, unsigned sid, uint64_t address,
                               unsigned access, uint32_t code)
{
    if (sid < PDART_STREAMS) {
        s->error_streams[sid / 32] |= 1u << (sid % 32);
    }
    if (s->error & PDART_FLAG) {
        s->error |= 1u << 19;
        return;
    }
    s->error = PDART_FLAG | ((sid & 255) << 20) | code;
    if (access & 2) {
        s->error |= 1u << 28;
    }
    s->error_address = address;
}

/* No translation cache: every walk sees guest memory. The QEMU adapter must
 * invalidate its downstream IOMMU listeners when this function returns true.
 * Unimplemented diagnostic/performance registers are not advertised as features.
 */
static inline bool pdart_write(TomaccoDart *s, uint32_t reg, uint32_t value,
                                bool *known)
{
    *known = true;
    if (reg & 3) { *known = false; return false; }
    if (reg >= 0x1000 && reg < 0x1800) {
        unsigned sid = ((reg - 0x1000) / 4) & 255;
        if (!(s->protect & 1)) {
            if (reg < 0x1400) { s->tcr[sid] = value; }
            else { s->ttbr[sid] = value & 0x3ffffffdu; }
            return true;
        }
        return false;
    }
    if (reg >= 0xc00 && reg < 0xc40) {
        unsigned bank = ((reg - 0xc00) / 4) & 7;
        if (reg < 0xc20) { s->enabled[bank] |= value; }
        else { s->enabled[bank] &= ~value; }
        return true;
    }
    if (reg >= 0x1c0 && reg < 0x1e0) {
        s->error_streams[(reg - 0x1c0) / 4] &= ~value;
        return false;
    }
    switch (reg) {
    case 0x80:
        /* Synchronous flushes. Unsupported diagnostic opcodes do not masquerade
         * as a completed flush; the adapter reports them as unimplemented. */
        if (((value >> 8) & 7) > 1) { *known = false; return false; }
        s->tlb_command = value & ~PDART_FLAG;
        return true;
    case 0x100:
        if (value & PDART_FLAG) { s->error = 0; }
        return false;
    case 0x104: s->error_mask = value; return false;
    case 0x200: s->protect |= value & 1; return false;
    case 0x204: s->protect &= ~(value & ~s->protect_lock & 1); return false;
    case 0x208: s->protect_lock |= value & 1; return false;
    case 0: case 4: case 8: case 12: /* read-only hardware parameters */
        return false;
    default: *known = false; return false;
    }
}

static inline uint32_t pdart_read(const TomaccoDart *s, uint32_t reg, bool *known)
{
    *known = true;
    if (reg & 3) { *known = false; return 0; }
    if (reg >= 0x1000 && reg < 0x1400) { return s->tcr[(reg - 0x1000) / 4]; }
    if (reg >= 0x1400 && reg < 0x1800) { return s->ttbr[(reg - 0x1400) / 4]; }
    if (reg >= 0xc00 && reg < 0xc40) { return s->enabled[((reg - 0xc00) / 4) & 7]; }
    if (reg >= 0x1c0 && reg < 0x1e0) { return s->error_streams[(reg - 0x1c0) / 4]; }
    switch (reg) {
    case 0: return (14u << 24) | 1; /* 16 KiB, one TLB set */
    case 4: return 3; /* full bypass + register protection, no other engines */
    case 8: return (42u << 24) | (44u << 16) | (2u << 8);
    case 12: return (PDART_STREAMS << 16) | PDART_STREAMS;
    case 0x80: return s->tlb_command;
    case 0x100: return s->error;
    case 0x104: return s->error_mask;
    case 0x170: return (uint32_t)s->error_address;
    case 0x174: return s->error_address >> 32;
    case 0x200: case 0x204: return s->protect;
    case 0x208: return s->protect_lock;
    default: *known = false; return 0;
    }
}

static inline TomaccoDartTranslation pdart_translate(TomaccoDart *s, unsigned sid,
    uint64_t address, unsigned access, TomaccoDartReadTable read_table, void *opaque)
{
    TomaccoDartTranslation result = {0, 3, 0};
    uint32_t error = 1; /* missing/disabled TTBR */
    uint32_t tcr;
    uint64_t table, pte = 0;
    unsigned first, permissions;
    if (sid >= PDART_STREAMS || (access & ~3u) ||
        !(s->enabled[sid / 32] & (1u << (sid % 32)))) { goto fault; }
    tcr = s->tcr[sid];
    /* SID remapping is not advertised by this model. There are no DAPF
     * engines (PARAMS4.NUM_APFS=0), so BYPASS_DAPF has no additional effect. */
    if (tcr & ~0x0fu) { error = 1u << 10; goto fault; }
    if ((tcr & 3) == 2) {
        if (address > PDART_PA_MASK) { error = 1u << 6; goto fault; }
        result.address = address & ~(uint64_t)(PDART_PAGE - 1);
        result.mask = PDART_PAGE - 1;
        result.permissions = 3;
        return result;
    }
    if ((tcr & 3) != 1 || !(s->ttbr[sid] & 1)) { goto fault; }
    if (address >= (UINT64_C(1) << ((tcr & 8) ? 44 : 36))) {
        error = 1u << 6; goto fault;
    }
    table = (uint64_t)(s->ttbr[sid] >> 2) << 14;
    first = (tcr & 8) ? 0 : 1;
    for (unsigned level = first; level < 3; ++level) {
        unsigned shift = 36 - 11 * level;
        uint64_t entry = table + ((address >> shift) & 2047) * 8;
        if (!read_table(opaque, entry, &pte)) { error = 1u << 7; goto fault; }
        if (!(pte & 1)) { error = 1u << (level + 1); goto fault; }
        table = ((pte >> 10) & 0x0fffffffu) << 14;
    }
    permissions = ((pte & 8) ? 0 : 1) | ((pte & 4) ? 0 : 2);
    if ((permissions & access) != access) {
        error = (access & 2) && !(permissions & 2) ? 1u << 4 : 1u << 5;
        goto fault;
    }
    /* Sub-page limits use inclusive four-byte units. Limit the returned IOTLB
     * entry to a naturally aligned block wholly inside that permitted range. */
    {
        uint64_t lower = ((pte >> 52) & 4095) * 4;
        uint64_t upper = (((pte >> 40) & 4095) + 1) * 4;
        uint64_t offset = address & (PDART_PAGE - 1);
        if (offset < lower || offset >= upper) {
            error = (access & 2) ? 1u << 4 : 1u << 5; goto fault;
        }
        result.mask = PDART_PAGE - 1;
        while ((offset & ~result.mask) < lower ||
               (offset | result.mask) >= upper) { result.mask >>= 1; }
        result.address = table | (offset & ~result.mask);
    }
    result.permissions = permissions;
    return result;
fault:
    /* IOMMU_NONE is an introspection request, not an actual DMA transaction. */
    if (access) { pdart_fault(s, sid, address, access, error); }
    return result;
}
#endif
