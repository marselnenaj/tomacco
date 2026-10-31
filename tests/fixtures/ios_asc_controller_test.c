/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdio.h>
#include "asc-controller.h"

static void put(TomaccoAscController *s, uint64_t reg, unsigned size, uint64_t value)
{
    assert(pascc_write(s, reg, size, value));
}

static uint64_t get(TomaccoAscController *s, uint64_t reg, unsigned size)
{
    uint64_t value = UINT64_MAX;
    assert(pascc_read(s, reg, size, &value));
    return value;
}

int main(void)
{
    TomaccoAscController s;
    pascc_reset(&s);
    assert(get(&s, 0x40, 4) == 0 && get(&s, 0x400, 4) == 0);
    put(&s, 0x40, 4, 0xa0401);
    put(&s, 0x400, 4, 0x400);
    assert(get(&s, 0x40, 4) == 0xa0401 && get(&s, 0x400, 4) == 0x400);
    /* Every one of the 512 external sources supports independent masking,
     * IRQ/FIQ selection, acknowledgement and reassertion of a live level. */
    for (unsigned fiq = 0; fiq < 2; fiq++) {
        for (unsigned source = 0; source < 512; source++) {
            unsigned bank = 0xa00 + fiq * 0x100 + source / 32 * 4;
            uint32_t bit = UINT32_C(1) << (source % 32);
            assert(pascc_level(&s, source, true));
            assert(!pascc_event(&s, 0, fiq, false));
            put(&s, bank + 0x80, 4, bit);
            put(&s, 0x80c, 4, 0);
            assert(!pascc_event(&s, 0, fiq, false));
            put(&s, 0x80c, 4, PASC_GLOBAL_MASK);
            assert(pascc_event(&s, 0, fiq, false) == (0x10000 | source));
            assert(!pascc_event(&s, 0, !fiq, false));
            assert(get(&s, fiq ? 0x820 : 0x818, 4) == (0x10000 | source));
            assert(!pascc_event(&s, 0, fiq, false));
            assert(get(&s, bank, 4) & bit);
            put(&s, bank + 0x80, 4, bit);
            assert(pascc_event(&s, 0, fiq, false));
            assert(pascc_level(&s, source, false));
            assert(!pascc_event(&s, 0, fiq, false));
            /* Clearing software state never clears an asserted input wire. */
            put(&s, 0x900 + source / 32 * 4, 4, bit);
            assert(pascc_event(&s, 0, fiq, false));
            assert(pascc_level(&s, source, true));
            put(&s, 0x980 + source / 32 * 4, 4, bit);
            assert(pascc_event(&s, 0, fiq, false));
            assert(pascc_level(&s, source, false));
            assert(!pascc_event(&s, 0, fiq, false));
            put(&s, bank, 4, bit);
        }
    }
    /* Mailbox events have their own type and masks, not the error vectors
     * 0x100a5..0x100a8 found in the original overflow/underflow callbacks. */
    for (unsigned direction = 0; direction < 2; direction++) {
        for (unsigned view = 0; view < 2; view++) {
            for (unsigned fiq = 0; fiq < 2; fiq++) {
                unsigned base = 0x4000 + view * 0x4000;
                unsigned empty = 2 * direction, nonempty = empty + 1;
                uint32_t empty_bit = 1u << (4 * empty), nonempty_bit = 1u << (4 * nonempty);
                put(&s, base + 0x108 + fiq * 4, 4, empty_bit | nonempty_bit);
                assert(!pascc_event(&s, view, fiq, false)); /* disabled */
                put(&s, base + 0x110 + direction * 4, 4, 1);
                assert(pascc_event(&s, view, fiq, true) == (0x40000 | empty));
                put(&s, base + 0x800 + direction * 0x20, 8, 0x123456789abcdef0);
                assert(!pascc_event(&s, view, fiq, false)); /* only staged */
                put(&s, base + 0x808 + direction * 0x20, 8, 0xfedcba98);
                assert(pascc_event(&s, view, fiq, true) == (0x40000 | nonempty));
                assert(!pascc_event(&s, view, fiq, false)); /* auto-masked */
                put(&s, base + 0x108 + fiq * 4, 4, nonempty_bit);
                assert(pascc_event(&s, view, fiq, false));
                unsigned other = view ? 0x4000 : 0x8000;
                assert(get(&s, other + 0x810 + direction * 0x20, 8) == UINT64_C(0x123456789abcdef0));
                assert(pascc_event(&s, view, fiq, false)); /* peek does not pop */
                assert((uint32_t)get(&s, other + 0x818 + direction * 0x20, 8) == UINT32_C(0xfedcba98));
                assert(!pascc_event(&s, view, fiq, false));
                put(&s, base + 0x100 + fiq * 4, 4, UINT32_MAX);
                put(&s, base + 0x110 + direction * 4, 4, 0);
            }
        }
    }
    /* Real timer inputs and software pending share IRQ/FIQ routing and masks;
     * event reads acknowledge without clearing the input wire. */
    for (unsigned timer = 0; timer < 2; timer++) {
        for (unsigned fiq = 0; fiq < 2; fiq++) {
            unsigned set = 0x1010 + timer * 4, clear = set + 8;
            unsigned mask = 2u << fiq, event = 0x70000 | timer << 3;
            put(&s, 0x1000 + timer * 4, 4, fiq ? 0 : 0xa);
            assert(pascc_timer_level(&s, timer, true));
            put(&s, clear, 4, mask);
            assert(!pascc_event(&s, 0, fiq, false)); /* timer global off */
            put(&s, 0xc88, 4, 1);
            assert(pascc_event(&s, 0, fiq, false) == event);
            assert(!pascc_event(&s, 0, !fiq, false));
            assert(!pascc_event(&s, 1, fiq, false)); /* not AP */
            assert(get(&s, fiq ? 0x820 : 0x818, 4) == event);
            assert(get(&s, set, 4) & mask);
            assert(!pascc_event(&s, 0, fiq, false));
            put(&s, clear, 4, mask);
            assert(pascc_event(&s, 0, fiq, false) == event);
            assert(pascc_timer_level(&s, timer, false));
            assert(!pascc_event(&s, 0, fiq, false));
            put(&s, set, 4, 1);
            assert(pascc_event(&s, 0, fiq, false) == event);
            put(&s, clear, 4, 1);
            assert(!pascc_event(&s, 0, fiq, false));
            put(&s, set, 4, UINT32_MAX);
            assert(get(&s, clear, 4) == 7);
            put(&s, clear, 4, 1);
            put(&s, 0xc88, 4, 0);
        }
    }
    /* Unsupported transactions, including next boot dependencies, are
     * atomic errors. No arbitrary read/write fallback or ready response. */
    const uint64_t absent[] = {0, 0x44, 0x48, 0x804, 0xc84, 0x1008, 0x10000,
                              0xc000, UINT64_MAX};
    TomaccoAscController before = s;
    for (unsigned i = 0; i < sizeof(absent) / sizeof(*absent); i++) {
        for (unsigned size = 1; size <= 8; size++) {
            uint64_t value = 42;
            assert(!pascc_read(&s, absent[i], size, &value) && value == 42);
            assert(!pascc_write(&s, absent[i], size, 1));
            assert(!memcmp(&s, &before, sizeof(s)));
        }
    }
    const uint64_t bad_writes[][2] = {{0x40, 2}, {0x400, 1}, {0x80c, 2},
                                     {0x808, 2}, {0xc88, 2}, {0x1000, 1},
                                     {0x4110, 2}, {0x8114, UINT64_C(0x100000001)}};
    for (unsigned i = 0; i < sizeof(bad_writes) / sizeof(*bad_writes); i++) {
        assert(!pascc_write(&s, bad_writes[i][0], 4, bad_writes[i][1]));
        assert(!memcmp(&s, &before, sizeof(s)));
    }
    assert(!pascc_level(&s, 512, true));
    assert(!pascc_timer_level(&s, 2, true));
    assert(!memcmp(&s, &before, sizeof(s)));
    put(&s, 0x8800, 8, 123);
    put(&s, 0x8808, 8, 1);
    put(&s, 0x8820, 8, 456);
    assert(pascc_level(&s, 33, true) && pascc_timer_level(&s, 0, true));
    put(&s, 0x904, 4, 2);
    put(&s, 0x808, 4, 1);
    assert(get(&s, 0x808, 4) == 1);
    assert(s.level[1] == 2 && s.timer_level[0] && !s.software[1]);
    assert(get(&s, 0x40, 4) == 0xa0401 && get(&s, 0x400, 4) == 0x400);
    assert(s.mailbox.queue[0].count == 1 && s.mailbox.queue[1].staged_valid);
    assert(get(&s, 0x1010, 4) == 6 && !get(&s, 0xc88, 4));
    put(&s, 0x80c, 4, 1);
    put(&s, 0xa84, 4, 2);
    assert(!pascc_event(&s, 0, 0, false)); /* held reset */
    put(&s, 0x808, 4, 0);
    assert(pascc_event(&s, 0, 0, false) == 0x10021);
    pascc_reset(&s);
    assert(get(&s, 0x4110, 4) == PASC_EMPTY && get(&s, 0x8114, 4) == PASC_EMPTY);
    assert(!s.mailbox.queue[1].staged_valid);
    assert(get(&s, 0x40, 4) == 0 && get(&s, 0xa00, 4) == UINT32_MAX);
    assert(!pascc_event(&s, 0, 0, false) && !pascc_event(&s, 0, 1, false));
    assert(!s.timer_level[0] && !s.level[1]);
    puts("ASC controller masks, 512 sources, IRQ/FIQ events, timers, queues, reset and strict errors passed");
}
