/* SPDX-License-Identifier: GPL-2.0-or-later
 * T8140 MCC register state for QEMU's coherent memory system.
 *
 * Register evidence and the deliberately limited scope are documented in
 * mcc-t8140.md. This is not a timing model of the physical DRAM or SLC. Unknown
 * registers/encodings fail rather than acknowledging an unimplemented device.
 */
#ifndef TOMACCO_MCC_T8140_H
#define TOMACCO_MCC_T8140_H

#include <stdbool.h>
#include <stdint.h>

#define PMCC_PLANES 4
#define PMCC_CHANNELS 4
#define PMCC_PLANE_STRIDE UINT32_C(0x40000)
#define PMCC_BROADCAST UINT32_C(0x100000)
#define PMCC_CACHE_CONTROL UINT32_C(0x2800)
#define PMCC_CACHE_STATUS UINT32_C(0x2804)
#define PMCC_MAINTENANCE_GATE UINT32_C(0x82c)
#define PMCC_GATE_WINDOW UINT32_C(0x820)
#define PMCC_GATE_WINDOW_SIZE UINT32_C(0x20)
#define PMCC_DRAM_CONFIG UINT32_C(0x1d48)
#define PMCC_CACHE_BOOT UINT32_C(0x8000000c)
#define PMCC_DSID_GROUPS 32
#define PMCC_DSID_QUOTA UINT32_C(0xa000)

typedef struct TomaccoMcc {
    uint32_t cache_control[PMCC_PLANES];
    uint32_t maintenance_gate[PMCC_PLANES];
    uint32_t dram_config[PMCC_CHANNELS];
    uint32_t dsid_quota[PMCC_PLANES][PMCC_DSID_GROUPS];
} TomaccoMcc;

static inline void pmcc_reset(TomaccoMcc *s, uint32_t dram_config)
{
    for (unsigned i = 0; i < PMCC_PLANES; ++i) {
        s->cache_control[i] = PMCC_CACHE_BOOT;
        s->maintenance_gate[i] = 0;
        /* The virtual boot starts with no per-client cache allocation. This
         * is model policy, not a claimed silicon/iBoot reset snapshot. The
         * guest owns subsequent quota programming and readback. */
        for (unsigned group = 0; group < PMCC_DSID_GROUPS; ++group) {
            s->dsid_quota[i][group] = 0;
        }
    }
    for (unsigned i = 0; i < PMCC_CHANNELS; ++i) {
        s->dram_config[i] = dram_config;
    }
}

/* Bank 0: AMCC, banks 1..4: separate DCS channels. Invalid reads leave the
 * output untouched; the bus adapter must return a transaction error. */
static inline bool pmcc_read(const TomaccoMcc *s, unsigned bank, uint64_t reg,
                             unsigned size, uint32_t *value)
{
    if (size != 4 || (reg & 3)) {
        return false;
    }
    if (bank == 0) {
        unsigned plane = reg / PMCC_PLANE_STRIDE;
        if (reg >= PMCC_PLANES * PMCC_PLANE_STRIDE) {
            return false;
        }
        reg %= PMCC_PLANE_STRIDE;
        if (reg >= PMCC_DSID_QUOTA && reg < PMCC_DSID_QUOTA + 4 * PMCC_DSID_GROUPS) {
            *value = s->dsid_quota[plane][(reg - PMCC_DSID_QUOTA) / 4];
            return true;
        }
        switch (reg) {
        case PMCC_MAINTENANCE_GATE:
            *value = s->maintenance_gate[plane];
            return true;
        case PMCC_CACHE_CONTROL:
            *value = s->cache_control[plane];
            return true;
        case PMCC_CACHE_STATUS: {
            uint32_t ways = s->cache_control[plane] & 15;
            /* Coherent QEMU RAM has no dirty SLC lines to drain. A supported
             * transition completes synchronously; both way counts agree. */
            *value = (ways << 24) | (ways << 8);
            return true;
        }
        default:
            return false;
        }
    }
    if (bank <= PMCC_CHANNELS && reg == PMCC_DRAM_CONFIG) {
        *value = s->dram_config[bank - 1];
        return true;
    }
    return false;
}

static inline bool pmcc_write(TomaccoMcc *s, unsigned bank, uint64_t reg,
                              unsigned size, uint64_t value)
{
    if (size != 4 || (reg & 3) || value > UINT32_MAX) {
        return false;
    }
    if (bank == 0) {
        bool broadcast = reg >= PMCC_BROADCAST;
        if (reg >= PMCC_BROADCAST + PMCC_PLANE_STRIDE) {
            return false;
        }
        unsigned first = broadcast ? 0 : reg / PMCC_PLANE_STRIDE;
        unsigned end = broadcast ? PMCC_PLANES : first + 1;
        reg %= PMCC_PLANE_STRIDE;
        if (reg == PMCC_MAINTENANCE_GATE) {
            if (value > 1) {
                return false;
            }
            /* The original driver asserts this gate around cache flushing,
             * and releases it after setting the way count. Retain both
             * evidenced states. Coherent RAM has no SLC lines to allocate;
             * this does not disable DRAM or change memory permissions. */
            for (unsigned plane = first; plane < end; ++plane) {
                s->maintenance_gate[plane] = value;
            }
            return true;
        }
        if (reg >= PMCC_DSID_QUOTA && reg < PMCC_DSID_QUOTA + 4 * PMCC_DSID_GROUPS) {
            if (value > 63) {
                return false;
            }
            for (unsigned plane = first; plane < end; ++plane) {
                s->dsid_quota[plane][(reg - PMCC_DSID_QUOTA) / 4] = value;
            }
            return true;
        }
        if (reg != PMCC_CACHE_CONTROL ||
            (value & ~UINT64_C(0x8000000f)) || (value & 15) > 12) {
            return false;
        }
        for (unsigned plane = first; plane < end; ++plane) {
            s->cache_control[plane] = value;
        }
        return true;
    }
    if (bank <= PMCC_CHANNELS && reg == PMCC_DRAM_CONFIG) {
        /* XNU saves/restores the complete word. Timings do not alter the
         * functional, coherent RAM backing; retain all guest-written bits. */
        s->dram_config[bank - 1] = value;
        return true;
    }
    return false;
}
#endif
