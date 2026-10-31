/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "mcc-t8140.h"

int main(void)
{
    TomaccoMcc s, before;
    uint32_t value;
    pmcc_reset(&s, 0xfeedface);
    for (unsigned channel = 1; channel <= 4; ++channel) {
        assert(pmcc_read(&s, channel, 0x1d48, 4, &value) && value == 0xfeedface);
        assert(pmcc_write(&s, channel, 0x1d48, 4, 0x10 + channel));
    }
    for (unsigned channel = 1; channel <= 4; ++channel) {
        assert(pmcc_read(&s, channel, 0x1d48, 4, &value) && value == 0x10 + channel);
    }
    for (unsigned plane = 0; plane < 4; ++plane) {
        uint64_t base = plane * 0x40000;
        for (unsigned group = 0; group < 32; ++group) {
            assert(pmcc_read(&s, 0, base + 0xa000 + group * 4, 4, &value) && value == 0);
            assert(pmcc_write(&s, 0, base + 0xa000 + group * 4, 4, (plane * 32 + group) % 64));
        }
        assert(pmcc_read(&s, 0, base + 0x2800, 4, &value) && value == 0x8000000c);
        assert(pmcc_read(&s, 0, base + 0x2804, 4, &value) && value == 0x0c000c00);
        for (unsigned ways = 0; ways <= 12; ++ways) {
            for (unsigned high = 0; high < 2; ++high) {
                uint32_t control = (high ? UINT32_C(0x80000000) : 0) | ways;
                assert(pmcc_write(&s, 0, base + 0x2800, 4, control));
                assert(pmcc_read(&s, 0, base + 0x2800, 4, &value) && value == control);
                assert(pmcc_read(&s, 0, base + 0x2804, 4, &value));
                assert(value == ways * UINT32_C(0x1000100));
            }
        }
        assert(pmcc_write(&s, 0, base + 0x2800, 4, plane));
    }
    for (unsigned plane = 0; plane < 4; ++plane) {
        assert(pmcc_read(&s, 0, plane * 0x40000 + 0x2804, 4, &value));
        assert(value == plane * UINT32_C(0x1000100));
    }
    assert(pmcc_write(&s, 0, 0x102800, 4, 7));
    assert(pmcc_write(&s, 0, 0x10a07c, 4, 63));
    assert(pmcc_write(&s, 0, 0x10082c, 4, 1));
    for (unsigned plane = 0; plane < 4; ++plane) {
        assert(pmcc_read(&s, 0, plane * 0x40000 + 0x82c, 4, &value) && value == 1);
        assert(pmcc_write(&s, 0, plane * 0x40000 + 0x82c, 4, plane % 2));
        assert(pmcc_read(&s, 0, plane * 0x40000 + 0x2804, 4, &value));
        assert(value == UINT32_C(0x7000700));
        assert(pmcc_read(&s, 0, plane * 0x40000 + 0xa07c, 4, &value) && value == 63);
    }
    before = s;
    for (unsigned plane = 0; plane < 4; ++plane) {
        assert(pmcc_read(&s, 0, plane * 0x40000 + 0x82c, 4, &value) && value == plane % 2);
    }
    for (unsigned plane = 0; plane < 4; ++plane) {
        for (unsigned group = 0; group < 32; ++group) {
            uint64_t reg = plane * 0x40000 + 0xa000 + group * 4;
            assert(pmcc_read(&s, 0, reg, 4, &value));
            assert(value == (group == 31 ? 63 : (plane * 32 + group) % 64));
            assert(!pmcc_write(&s, 0, reg, 4, 64));
            assert(!pmcc_write(&s, 0, reg, 8, 1));
            assert(!pmcc_write(&s, 0, reg + 1, 4, 1));
            assert(!pmcc_write(&s, 0, reg, 4, 0x80000000));
        }
    }
    for (unsigned size = 0; size <= 16; ++size) {
        if (size == 4) { continue; }
        for (unsigned bank = 0; bank <= 4; ++bank) {
            uint64_t reg = bank ? 0x1d48 : 0x2800;
            value = 0xfeed;
            assert(!pmcc_write(&s, bank, reg, size, 0));
            assert(!pmcc_read(&s, bank, reg, size, &value));
            assert(value == 0xfeed && !memcmp(&s, &before, sizeof(s)));
        }
    }
    const uint64_t bad_regs[] = {0, 0x2801, 0x2802, 0x2803, 0x2808, 0x100000,
                                0xa080, 0x49ffc, 0xca080, 0x10a080, 0x102808,
                                0x140000, UINT64_MAX, UINT64_C(1) << 63};
    for (unsigned i = 0; i < sizeof(bad_regs) / sizeof(bad_regs[0]); ++i) {
        value = 0xbeef;
        assert(!pmcc_read(&s, 0, bad_regs[i], 4, &value) && value == 0xbeef);
        assert(!pmcc_write(&s, 0, bad_regs[i], 4, 0));
    }
    for (unsigned channel = 1; channel <= 4; ++channel) {
        assert(!pmcc_read(&s, channel, 0x1d4c, 4, &value));
        assert(!pmcc_write(&s, channel, 0x1d4c, 4, 0));
    }
    assert(!pmcc_read(&s, 5, 0x1d48, 4, &value));
    assert(!pmcc_write(&s, 5, 0x1d48, 4, 0));
    assert(!pmcc_write(&s, 0, 0x2804, 4, 0));
    assert(!pmcc_read(&s, 0, 0x102800, 4, &value));
    assert(!pmcc_read(&s, 0, 0x10a07c, 4, &value));
    assert(!pmcc_write(&s, 0, 0x102800, 4, 13));
    assert(!pmcc_write(&s, 0, 0x102800, 8, 0));
    assert(!pmcc_write(&s, 0, 0x10a07c, 4, 64));
    assert(!pmcc_write(&s, 0, 0x10a078, 8, 0));
    assert(!pmcc_write(&s, 0, 0x10082c, 4, 2));
    for (unsigned plane = 0; plane < 4; ++plane) {
        uint64_t gate = plane * 0x40000 + 0x82c;
        assert(!pmcc_write(&s, 0, gate, 4, 2));
        assert(!pmcc_write(&s, 0, gate, 8, 0));
        assert(!pmcc_write(&s, 0, gate - 4, 8, 0));
    }
    for (unsigned ways = 13; ways <= 15; ++ways) {
        assert(!pmcc_write(&s, 0, 0x2800, 4, ways));
    }
    for (unsigned bit = 4; bit < 31; ++bit) {
        assert(!pmcc_write(&s, 0, 0x2800, 4, UINT32_C(1) << bit));
    }
    assert(!pmcc_write(&s, 1, 0x1d48, 4, UINT64_C(1) << 32));
    assert(!memcmp(&s, &before, sizeof(s)));
    pmcc_reset(&s, 0xcafebabe);
    for (unsigned channel = 1; channel <= 4; ++channel) {
        assert(pmcc_read(&s, channel, 0x1d48, 4, &value) && value == 0xcafebabe);
    }
    for (unsigned plane = 0; plane < 4; ++plane) {
        assert(pmcc_read(&s, 0, plane * 0x40000 + 0x82c, 4, &value) && value == 0);
        assert(pmcc_read(&s, 0, plane * 0x40000 + 0x2804, 4, &value) && value == 0x0c000c00);
        for (unsigned group = 0; group < 32; ++group) {
            assert(pmcc_read(&s, 0, plane * 0x40000 + 0xa000 + group * 4, 4, &value) && value == 0);
        }
    }
    puts("MCC channel isolation, cache transitions, strict faults and reset passed");
}
