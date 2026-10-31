/* SPDX-License-Identifier: GPL-2.0-or-later
 * Functional T8140 power/telemetry dashboard, not a PMP firmware substitute.
 * Register evidence, read side effects and unsupported scope: ptd-t8140.md.
 */
#ifndef TOMACCO_PTD_T8140_H
#define TOMACCO_PTD_T8140_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define PPTD_ENTRIES 512
#define PPTD_IRQS 32
#define PPTD_WORDS (PPTD_ENTRIES / 32)
#define PPTD_UPDATE UINT64_C(0x10000)
#define PPTD_PENDING UINT64_C(0x18000)
#define PPTD_ENABLE UINT64_C(0x18200)
#define PPTD_DISABLE UINT64_C(0x18300)
#define PPTD_ROUTE UINT64_C(0x18400)
#define PPTD_DROPPED UINT64_C(1)
#define PPTD_NEW UINT64_C(2)
#define PPTD_TICK_MASK UINT64_C(0x3fffffffffffff)

typedef struct TomaccoPtd {
    uint64_t message[PPTD_ENTRIES];
    uint64_t metadata[PPTD_ENTRIES];
    uint32_t enabled[PPTD_WORDS];
    uint8_t route[PPTD_ENTRIES];
} TomaccoPtd;

static inline void pptd_reset(TomaccoPtd *s)
{
    /* An empty transport. Firmware must publish every status/response. */
    memset(s, 0, sizeof(*s));
}

static inline uint32_t pptd_pending(const TomaccoPtd *s, unsigned word)
{
    uint32_t value = 0;
    for (unsigned bit = 0; bit < 32; ++bit) {
        if (s->metadata[word * 32 + bit] & PPTD_NEW) {
            value |= UINT32_C(1) << bit;
        }
    }
    return value;
}

static inline uint32_t pptd_irqs(const TomaccoPtd *s)
{
    uint32_t lines = 0;
    for (unsigned entry = 0; entry < PPTD_ENTRIES; ++entry) {
        if ((s->metadata[entry] & PPTD_NEW) &&
            (s->enabled[entry / 32] & (UINT32_C(1) << (entry % 32)))) {
            lines |= UINT32_C(1) << s->route[entry];
        }
    }
    return lines;
}

/* v1: four eight-bit fields per word, alternating groups in two windows.
 * Only the low five bits of each field select one of 32 interrupt outputs. */
static inline bool pptd_route_base(uint64_t reg, unsigned *entry)
{
    if (reg < PPTD_ROUTE || reg >= PPTD_ROUTE + 0x300 || (reg & 3)) {
        return false;
    }
    uint64_t offset = reg - PPTD_ROUTE;
    unsigned second = offset >= 0x200;
    if (second) {
        offset -= 0x200;
    }
    if (offset >= PPTD_ENTRIES / 8 * 4) {
        return false;
    }
    *entry = (offset / 4) * 8 + second * 4;
    return true;
}

static inline bool pptd_read(TomaccoPtd *s, uint64_t reg, unsigned size,
                            uint64_t *value)
{
    if (reg < PPTD_ENTRIES * 16 && size == 8 && !(reg & 7)) {
        unsigned entry = reg / 16;
        if (reg & 8) {
            /* Complete the guest's value/metadata pair before consuming the
             * event. Rejected/partial reads must never acknowledge it. */
            *value = s->metadata[entry];
            s->metadata[entry] &= ~(PPTD_NEW | PPTD_DROPPED);
        } else {
            *value = s->message[entry];
        }
        return true;
    }
    if ((size == 4 || size == 8) && !(reg & (size - 1)) &&
        reg >= PPTD_PENDING && reg <= PPTD_PENDING + PPTD_WORDS * 4 - size) {
        unsigned word = (reg - PPTD_PENDING) / 4;
        *value = pptd_pending(s, word);
        if (size == 8) {
            *value |= (uint64_t)pptd_pending(s, word + 1) << 32;
        }
        return true;
    }
    unsigned entry;
    if (size == 4 && pptd_route_base(reg, &entry)) {
        *value = 0;
        for (unsigned i = 0; i < 4; ++i) {
            *value |= (uint64_t)s->route[entry + i] << (i * 8);
        }
        return true;
    }
    return false;
}

static inline bool pptd_write(TomaccoPtd *s, uint64_t reg, unsigned size,
                             uint64_t value, uint64_t ticks)
{
    if (reg >= PPTD_UPDATE && reg < PPTD_UPDATE + PPTD_ENTRIES * 8 &&
        size == 8 && !(reg & 7)) {
        unsigned entry = (reg - PPTD_UPDATE) / 8;
        bool dropped = s->metadata[entry] & PPTD_NEW;
        s->message[entry] = value;
        s->metadata[entry] = ((ticks & PPTD_TICK_MASK) << 10) | PPTD_NEW |
                             (dropped ? PPTD_DROPPED : 0);
        return true;
    }
    if (size != 4 || (reg & 3) || value > UINT32_MAX) {
        return false;
    }
    if (reg >= PPTD_ENABLE && reg < PPTD_ENABLE + PPTD_WORDS * 4) {
        s->enabled[(reg - PPTD_ENABLE) / 4] |= value;
        return true;
    }
    if (reg >= PPTD_DISABLE && reg < PPTD_DISABLE + PPTD_WORDS * 4) {
        s->enabled[(reg - PPTD_DISABLE) / 4] &= ~(uint32_t)value;
        return true;
    }
    unsigned entry;
    if (pptd_route_base(reg, &entry)) {
        if (value & UINT64_C(0xe0e0e0e0)) {
            return false;
        }
        for (unsigned i = 0; i < 4; ++i) {
            s->route[entry + i] = (value >> (i * 8)) & 31;
        }
        return true;
    }
    return false;
}
#endif
