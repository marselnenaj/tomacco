/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdio.h>
#include "asc-mailbox.h"

static uint64_t read_reg(TomaccoAscMailbox *s, uint64_t reg, unsigned size)
{
    uint64_t value = UINT64_MAX;
    assert(pasc_read(s, reg, size, &value));
    return value;
}

static void rejects(TomaccoAscMailbox *s, bool write, uint64_t reg,
                    unsigned size, uint64_t value)
{
    TomaccoAscMailbox before = *s;
    uint64_t result = UINT64_MAX;
    assert(!(write ? pasc_write(s, reg, size, value) :
                     pasc_read(s, reg, size, &result)));
    assert(memcmp(&before, s, sizeof(before)) == 0);
    if (!write) {
        assert(result == UINT64_MAX);
    }
}

int main(void)
{
    TomaccoAscMailbox s;
    pasc_reset(&s);
    assert(pasc_irqs(&s) == 5);
    assert(read_reg(&s, 0x110, 4) == PASC_EMPTY);
    assert(read_reg(&s, 0x114, 4) == PASC_EMPTY);
    for (unsigned direction = 0; direction < 2; ++direction) {
        unsigned base = 0x800 + direction * 0x20;
        rejects(&s, false, base + 0x10, 8, 0);
        rejects(&s, false, base + 0x18, 8, 0);
        rejects(&s, true, base + 8, 8, 0);
        for (unsigned cycle = 0; cycle < 64; ++cycle) {
            /* Exercise full rings, wraparound, zero data and all endpoint
             * bits; both directions are live at the same time below. */
            for (unsigned i = 0; i < PASC_DEPTH; ++i) {
                uint64_t data = ((uint64_t)cycle << 40) | i;
                assert(pasc_write(&s, base, 8, data));
                assert(pasc_irqs(&s) == (i ? (direction ? 9 : 6) : 5));
                assert(pasc_write(&s, base + 8, 8, UINT32_MAX - i));
            }
            uint32_t status = read_reg(&s, 0x110 + direction * 4, 4);
            assert((status & (PASC_FULL | PASC_EMPTY)) == PASC_FULL);
            assert((status >> 20) == PASC_DEPTH);
            assert(pasc_write(&s, base, 8, UINT64_C(0xfeed00000000)));
            rejects(&s, true, base + 8, 8, 42);
            for (unsigned i = 0; i < PASC_DEPTH; ++i) {
                uint64_t data = ((uint64_t)cycle << 40) | i;
                assert(read_reg(&s, base + 0x10, 8) == data);
                assert(read_reg(&s, base + 0x10, 8) == data);
                uint64_t meta = read_reg(&s, base + 0x18, 8);
                assert((uint32_t)meta == UINT32_MAX - i);
                assert(((meta >> 52) & 31) == PASC_DEPTH - i);
            }
            /* The rejected full commit must preserve the staged value for
             * a producer that retries once the consumer makes space. */
            assert(pasc_write(&s, base + 8, 8, 42));
            assert(read_reg(&s, base + 0x10, 8) == UINT64_C(0xfeed00000000));
            assert((uint32_t)read_reg(&s, base + 0x18, 8) == 42);
            assert(pasc_irqs(&s) == 5);
            rejects(&s, true, base + 8, 8, 1);
        }
    }
    pasc_reset(&s);
    assert(pasc_write(&s, 0x800, 8, 0x123456789abcdef0));
    assert(pasc_write(&s, 0x820, 8, 0xfedcba9876543210));
    assert(pasc_irqs(&s) == 5);
    assert(pasc_write(&s, 0x808, 8, 0x12345678));
    assert(pasc_irqs(&s) == 6);
    assert(pasc_write(&s, 0x828, 8, 0x87654321));
    assert(pasc_irqs(&s) == 10);
    for (unsigned size = 0; size < 17; ++size) {
        if (size == 8) {
            continue;
        }
        for (unsigned reg = 0x800; reg <= 0x838; reg += 8) {
            rejects(&s, false, reg, size, 0);
            rejects(&s, true, reg, size, 0);
        }
    }
    for (unsigned reg = 0; reg < 0x4000; ++reg) {
        if (reg != 0x810 && reg != 0x818 && reg != 0x830 && reg != 0x838) {
            rejects(&s, false, reg, 8, 0);
        }
        if (reg != 0x800 && reg != 0x808 && reg != 0x820 && reg != 0x828) {
            rejects(&s, true, reg, 8, 0);
        }
        if (reg != 0x110 && reg != 0x114) {
            rejects(&s, false, reg, 4, 0);
        }
        rejects(&s, true, reg, 4, 1);
    }
    assert(pasc_write(&s, 0x800, 8, 0x9988));
    rejects(&s, true, 0x808, 8, UINT64_C(1) << 32);
    assert(read_reg(&s, 0x810, 8) == UINT64_C(0x123456789abcdef0));
    assert((uint32_t)read_reg(&s, 0x818, 8) == 0x12345678);
    assert(pasc_irqs(&s) == 9);
    assert(read_reg(&s, 0x830, 8) == UINT64_C(0xfedcba9876543210));
    assert((uint32_t)read_reg(&s, 0x838, 8) == 0x87654321);
    assert(pasc_irqs(&s) == 5);
    assert(pasc_write(&s, 0x808, 8, 0));
    assert(read_reg(&s, 0x810, 8) == 0x9988);
    pasc_reset(&s);
    TomaccoAscMailbox empty;
    memset(&empty, 0, sizeof(empty));
    assert(memcmp(&empty, &s, sizeof(s)) == 0);
    assert(pasc_irqs(&s) == 5);
    rejects(&s, true, 0x808, 8, 0);
    puts("ASC bidirectional queues, wraparound, atomic messages, IRQ levels and strict bus errors passed");
    return 0;
}
