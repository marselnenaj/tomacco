/* SPDX-License-Identifier: GPL-2.0-or-later
 * ASC message transport. No RTKit endpoint or controller-ready responses.
 * The supported contract and explicit model policies are in asc-mailbox.md.
 */
#ifndef TOMACCO_ASC_MAILBOX_H
#define TOMACCO_ASC_MAILBOX_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define PASC_DEPTH 8
#define PASC_IRQS 4
#define PASC_FULL (UINT32_C(1) << 16)
#define PASC_EMPTY (UINT32_C(1) << 17)

typedef struct TomaccoAscMessage {
    uint64_t data;
    uint32_t endpoint;
} TomaccoAscMessage;

typedef struct TomaccoAscFifo {
    TomaccoAscMessage messages[PASC_DEPTH];
    uint64_t staged;
    uint8_t read_pointer, write_pointer, count;
    bool staged_valid;
} TomaccoAscFifo;

typedef struct TomaccoAscMailbox {
    /* A2I and I2A. Both register views access these same queues. */
    TomaccoAscFifo queue[2];
} TomaccoAscMailbox;

static inline void pasc_reset(TomaccoAscMailbox *s)
{
    memset(s, 0, sizeof(*s));
}

static inline unsigned pasc_irqs(const TomaccoAscMailbox *s)
{
    /* Unmasked level outputs: A2I empty/nonempty, I2A empty/nonempty.
     * Interrupt masking belongs to the platform's interrupt controller. */
    return (s->queue[0].count ? 2 : 1) | (s->queue[1].count ? 8 : 4);
}

static inline uint32_t pasc_status(const TomaccoAscFifo *q)
{
    return ((uint32_t)q->count << 20) |
           ((uint32_t)q->read_pointer << 12) |
           ((uint32_t)q->write_pointer << 8) |
           (q->count == PASC_DEPTH ? PASC_FULL : 0) |
           (q->count == 0 ? PASC_EMPTY : 0);
}

static inline bool pasc_read(TomaccoAscMailbox *s, uint64_t reg, unsigned size,
                            uint64_t *value)
{
    if (size == 4 && (reg == 0x110 || reg == 0x114)) {
        *value = pasc_status(&s->queue[(reg - 0x110) / 4]);
        return true;
    }
    if (size != 8 || (reg != 0x810 && reg != 0x818 &&
                      reg != 0x830 && reg != 0x838)) {
        return false;
    }
    TomaccoAscFifo *q = &s->queue[(reg - 0x810) / 0x20];
    if (!q->count) {
        /* Empty reads have no qualified hardware value. Do not manufacture
         * a zero message or return an earlier message as new input. */
        return false;
    }
    TomaccoAscMessage *message = &q->messages[q->read_pointer % PASC_DEPTH];
    if (!(reg & 8)) {
        *value = message->data;
    } else {
        /* The low 32 bits are the message. The original bulk receiver tests
         * OUTCNT for more entries. This model snapshots the pre-pop count
         * and the ring pointers; asynchronous clock-domain counters are
         * deliberately not synthesized (see the contract). */
        *value = message->endpoint | ((uint64_t)q->count << 52) |
                 ((uint64_t)q->read_pointer << 44) |
                 ((uint64_t)q->write_pointer << 40);
        memset(message, 0, sizeof(*message));
        q->read_pointer = (q->read_pointer + 1) & 15;
        q->count--;
    }
    return true;
}

static inline bool pasc_write(TomaccoAscMailbox *s, uint64_t reg, unsigned size,
                             uint64_t value)
{
    if (size != 8 || (reg != 0x800 && reg != 0x808 &&
                      reg != 0x820 && reg != 0x828)) {
        return false;
    }
    TomaccoAscFifo *q = &s->queue[(reg - 0x800) / 0x20];
    if (!(reg & 8)) {
        q->staged = value;
        q->staged_valid = true;
        return true;
    }
    if (value > UINT32_MAX || !q->staged_valid || q->count == PASC_DEPTH) {
        /* Strict model policy for unsupported/invalid transactions: preserve
         * the queue and staging latch, report a real bus fault to the CPU. */
        return false;
    }
    q->messages[q->write_pointer % PASC_DEPTH] =
        (TomaccoAscMessage){.data = q->staged, .endpoint = value};
    q->write_pointer = (q->write_pointer + 1) & 15;
    q->count++;
    q->staged = 0;
    q->staged_valid = false;
    return true;
}
#endif
