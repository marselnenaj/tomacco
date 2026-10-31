/* SPDX-License-Identifier: GPL-2.0-or-later
 * Bounded ASC/KIC programming model. See asc-controller.md for evidence,
 * reset/priority policies and the unimplemented timing and peripheral blocks.
 */
#ifndef TOMACCO_ASC_CONTROLLER_H
#define TOMACCO_ASC_CONTROLLER_H

#include "asc-mailbox.h"

#define PASC_KIC_WORDS 16
#define PASC_CONFIG_MASK UINT32_C(0x000fff01)
#define PASC_GLOBAL_MASK UINT32_C(0x60000001)

typedef struct TomaccoAscController {
    TomaccoAscMailbox mailbox;
    uint32_t config, firmware_control, global_config;
    uint32_t level[PASC_KIC_WORDS], software[PASC_KIC_WORDS];
    uint32_t masked[2][PASC_KIC_WORDS]; /* IRQ, FIQ */
    uint32_t mailbox_mask[2][2];       /* IOP/AP view, IRQ/FIQ */
    bool queue_enabled[2];
    uint32_t timer_config[2], timer_state[2], timer_enable, interrupt_reset;
    bool timer_level[2];              /* real ARM physical/virtual timer outputs */
} TomaccoAscController;

static inline void pascc_reset_interrupts(TomaccoAscController *s)
{
    memset(s->software, 0, sizeof(s->software));
    memset(s->masked, 0xff, sizeof(s->masked));
    memset(s->mailbox_mask, 0xff, sizeof(s->mailbox_mask));
    memset(s->timer_config, 0, sizeof(s->timer_config));
    s->timer_state[0] = s->timer_state[1] = 6;
    s->timer_enable = s->global_config = 0;
}

static inline void pascc_reset(TomaccoAscController *s)
{
    memset(s, 0, sizeof(*s));
    pascc_reset_interrupts(s);
}

static inline bool pascc_timer_level(TomaccoAscController *s, unsigned timer, bool high)
{
    if (timer > 1) { return false; }
    s->timer_level[timer] = high;
    return true;
}

static inline bool pascc_level(TomaccoAscController *s, unsigned source, bool high)
{
    if (source >= PASC_KIC_WORDS * 32) {
        return false;
    }
    uint32_t bit = UINT32_C(1) << (source % 32);
    if (high) {
        s->level[source / 32] |= bit;
    } else {
        s->level[source / 32] &= ~bit;
    }
    return true;
}

static inline uint32_t pascc_mailbox_pending(const TomaccoAscController *s)
{
    unsigned lines = pasc_irqs(&s->mailbox);
    uint32_t result = 0;
    for (unsigned i = 0; i < 4; i++) {
        if (s->queue_enabled[i / 2] && (lines & (1u << i))) {
            result |= UINT32_C(1) << (4 * i);
        }
    }
    return result;
}

static inline uint32_t pascc_event(TomaccoAscController *s, unsigned view,
                                 unsigned fiq, bool consume)
{
    if (view > 1 || fiq > 1 || !(s->global_config & 1) || s->interrupt_reset) {
        return 0;
    }
    if (view == 0 && s->timer_enable) {
        for (unsigned timer = 0; timer < 2; timer++) {
            uint32_t cfg = s->timer_config[timer], state = s->timer_state[timer];
            bool physical = s->timer_level[timer] && (((cfg & 2) ? 0u : 1u) == fiq);
            bool software = (state & 1) && (((cfg & 8) ? 0u : 1u) == fiq);
            uint32_t mask = 2u << fiq;
            if ((physical || software) && !(state & mask)) {
                if (consume) { s->timer_state[timer] |= mask; }
                return UINT32_C(0x70000) | (timer << 3);
            }
        }
    }
    /* Fixed lowest-number-first arbitration is a laboratory policy.
     * External KIC sources belong to the IOP; AP has mailbox outputs only. */
    if (view == 0) {
        for (unsigned word = 0; word < PASC_KIC_WORDS; word++) {
            uint32_t pending = (s->level[word] | s->software[word]) &
                               ~s->masked[fiq][word];
            if (pending) {
                unsigned bit = __builtin_ctz(pending);
                if (consume) {
                    s->masked[fiq][word] |= UINT32_C(1) << bit;
                }
                return UINT32_C(0x10000) | (word * 32 + bit);
            }
        }
    }
    uint32_t pending = pascc_mailbox_pending(s) & ~s->mailbox_mask[view][fiq];
    for (unsigned i = 0; i < 4; i++) {
        uint32_t bit = UINT32_C(1) << (4 * i);
        if (pending & bit) {
            if (consume) {
                s->mailbox_mask[view][fiq] |= bit;
            }
            return UINT32_C(0x40000) | i;
        }
    }
    return 0;
}

static inline bool pascc_read(TomaccoAscController *s, uint64_t reg,
                             unsigned size, uint64_t *value)
{
    if (reg >= 0x4000 && reg < 0xc000) {
        unsigned view = reg >= 0x8000;
        reg &= 0x3fff;
        if (size == 4 && reg >= 0x100 && reg <= 0x10c && !(reg & 3)) {
            *value = s->mailbox_mask[view][(reg >> 2) & 1];
            return true;
        }
        if (!pasc_read(&s->mailbox, reg, size, value)) {
            return false;
        }
        if (reg == 0x110 || reg == 0x114) {
            *value |= s->queue_enabled[(reg - 0x110) / 4];
        }
        return true;
    }
    if (size != 4 || (reg & 3)) {
        return false;
    }
    switch (reg) {
    case 0x40: *value = s->config; return true;
    case 0x400: *value = s->firmware_control; return true;
    case 0x808: *value = s->interrupt_reset; return true;
    case 0x80c: *value = s->global_config; return true;
    case 0x818: *value = pascc_event(s, 0, 0, true); return true;
    case 0x820: *value = pascc_event(s, 0, 1, true); return true;
    case 0xc88: *value = s->timer_enable; return true;
    case 0x1000: case 0x1004:
        *value = s->timer_config[(reg - 0x1000) / 4]; return true;
    case 0x1010: case 0x1014: case 0x1018: case 0x101c:
        *value = s->timer_state[(reg >> 2) & 1]; return true;
    default: break;
    }
    if (reg >= 0xa00 && reg < 0xc00 && (reg & 0x7f) < 0x40) {
        *value = s->masked[(reg - 0xa00) / 0x100][(reg & 0x7f) / 4];
        return true;
    }
    return false;
}

static inline bool pascc_write(TomaccoAscController *s, uint64_t reg,
                              unsigned size, uint64_t value)
{
    if (reg >= 0x4000 && reg < 0xc000) {
        unsigned view = reg >= 0x8000;
        reg &= 0x3fff;
        if (size == 4 && value <= UINT32_MAX) {
            if (reg >= 0x100 && reg <= 0x10c && !(reg & 3)) {
                uint32_t *mask = &s->mailbox_mask[view][(reg >> 2) & 1];
                /* Other channel bits are retained as mask storage only;
                 * their absent queues never generate an event. */
                if (reg & 8) {
                    *mask &= ~(uint32_t)value;
                } else {
                    *mask |= value;
                }
                return true;
            }
            if ((reg == 0x110 || reg == 0x114) && value <= 1) {
                s->queue_enabled[(reg - 0x110) / 4] = value;
                return true;
            }
        }
        return pasc_write(&s->mailbox, reg, size, value);
    }
    if (size != 4 || (reg & 3) || value > UINT32_MAX) {
        return false;
    }
    switch (reg) {
    case 0x40:
        if (value & ~PASC_CONFIG_MASK) { return false; }
        s->config = value;
        return true;
    case 0x400:
        if (value & ~UINT32_C(0x400)) { return false; }
        s->firmware_control = value;
        return true;
    case 0x80c:
        if (value & ~PASC_GLOBAL_MASK) { return false; }
        s->global_config = value;
        return true;
    case 0x808:
        if (value > 1) { return false; }
        if (value && !s->interrupt_reset) { pascc_reset_interrupts(s); }
        s->interrupt_reset = value;
        return true;
    case 0xc88:
        if (value > 1) { return false; }
        s->timer_enable = value;
        return true;
    case 0x1000: case 0x1004:
        if (value & ~UINT32_C(0xa)) { return false; }
        s->timer_config[(reg - 0x1000) / 4] = value;
        return true;
    case 0x1010: case 0x1014: case 0x1018: case 0x101c: {
        /* Only software-pending and IRQ/FIQ mask bits have modeled effects.
         * Broad all-ones mask writes used by the firmware are accepted. */
        uint32_t *state = &s->timer_state[(reg >> 2) & 1];
        if (reg & 8) { *state &= ~(value & 7); }
        else { *state |= value & 7; }
        return true;
    }
    default: break;
    }
    if (reg >= 0x900 && reg < 0xa00 && (reg & 0x7f) < 0x40) {
        uint32_t *pending = &s->software[(reg & 0x7f) / 4];
        if (reg & 0x80) {
            *pending &= ~(uint32_t)value;
        } else {
            *pending |= value;
        }
        return true;
    }
    if (reg >= 0xa00 && reg < 0xc00 && (reg & 0x7f) < 0x40) {
        uint32_t *mask = &s->masked[(reg - 0xa00) / 0x100][(reg & 0x7f) / 4];
        if (reg & 0x80) {
            *mask &= ~(uint32_t)value;
        } else {
            *mask |= value;
        }
        return true;
    }
    return false;
}
#endif
