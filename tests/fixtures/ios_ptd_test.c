/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <stdio.h>
#include "ptd-t8140.h"

int main(void)
{
    TomaccoPtd s, before;
    uint64_t value;
    pptd_reset(&s);
    for (unsigned i = 0; i < 512; ++i) {
        assert(pptd_read(&s, i * 16 + 8, 8, &value) && value == 0);
        assert(pptd_write(&s, 0x10000 + i * 8, 8, i + 0x12340000, i + 7));
    }
    for (unsigned word = 0; word < 16; ++word) {
        assert(pptd_read(&s, 0x18000 + word * 4, 4, &value) && value == UINT32_MAX);
    }
    assert(pptd_irqs(&s) == 0); /* Pending status is independent of masking. */
    assert(pptd_write(&s, 0x18400, 4, 0x1f030201, 0));
    assert(pptd_write(&s, 0x18600, 4, 0x0504031f, 0));
    assert(pptd_write(&s, 0x18200, 4, 0xff, 0));
    assert(pptd_irqs(&s) == UINT32_C(0x8000003e));
    assert(pptd_write(&s, 0x18300, 4, 0x18, 0));
    assert(pptd_irqs(&s) == 0x3e);
    assert(pptd_write(&s, 0x18200, 4, 0x18, 0));
    for (unsigned i = 0; i < 512; ++i) {
        assert(pptd_read(&s, i * 16, 8, &value) && value == i + 0x12340000);
        assert(pptd_read(&s, i * 16 + 8, 8, &value) && value == (((uint64_t)i + 7) << 10 | 2));
        assert(pptd_read(&s, i * 16 + 8, 8, &value) && value == ((uint64_t)i + 7) << 10);
    }
    assert(!pptd_irqs(&s));
    assert(pptd_write(&s, 0x10000, 8, 1, 11));
    assert(pptd_write(&s, 0x10000, 8, 0, 12)); /* Zero is a valid new message. */
    assert(pptd_read(&s, 0, 8, &value) && value == 0);
    assert(pptd_read(&s, 8, 8, &value) && value == (12 << 10 | 3));
    assert(pptd_read(&s, 8, 8, &value) && value == 12 << 10);
    assert(pptd_write(&s, 0x10000, 8, 42, UINT64_MAX));
    before = s;
    const uint64_t unknown[] = {0x2000, 0x11000, 0x18040, 0x18240, 0x18340, 0x18500, 0x18700, UINT64_MAX};
    for (unsigned i = 0; i < sizeof(unknown) / sizeof(*unknown); ++i) {
        value = 99;
        assert(!pptd_read(&s, unknown[i], 8, &value) && value == 99);
        assert(!pptd_write(&s, unknown[i], 8, 0, 0));
        assert(!pptd_write(&s, unknown[i], 4, 0, 0));
    }
    for (unsigned size = 0; size <= 16; ++size) {
        if (size == 8) { continue; }
        assert(!pptd_read(&s, 8, size, &value));
        assert(!pptd_write(&s, 0x10000, size, 0, 0));
    }
    assert(!pptd_write(&s, 0, 8, 0, 0));
    assert(!pptd_write(&s, 0x18000, 4, 0, 0));
    assert(!pptd_write(&s, 0x18400, 8, 0, 0));
    assert(!pptd_write(&s, 0x18400, 4, 0x20000000, 0));
    assert(!pptd_write(&s, 0x18200, 4, UINT64_C(1) << 32, 0));
    assert(!pptd_read(&s, 9, 8, &value));
    assert(!memcmp(&s, &before, sizeof(s)));
    assert(pptd_read(&s, 8, 8, &value) && value == ((PPTD_TICK_MASK << 10) | 2));
    pptd_reset(&s);
    assert(!pptd_irqs(&s));
    for (unsigned i = 0; i < 512; ++i) {
        assert(pptd_read(&s, i * 16, 8, &value) && value == 0);
        assert(pptd_read(&s, i * 16 + 8, 8, &value) && value == 0);
    }
    puts("PTD messages, consumption, dropped updates, timestamps and IRQ routing passed");
}
