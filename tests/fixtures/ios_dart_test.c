/* Exercise DMA semantics against artificial page tables, independently of
 * Apple firmware. Guest qualification is a separate integration test. */
#include <assert.h>
#include <stdio.h>
#include "dart-t8110.h"

static uint64_t ram[0x20000 / 8];
static unsigned reads;
static bool table_read(void *unused, uint64_t address, uint64_t *value)
{
    (void)unused;
    ++reads;
    if ((address & 7) || address >= sizeof(ram)) { return false; }
    *value = ram[address / 8];
    return true;
}
static uint64_t table(uint64_t address) { return (address >> 4) | 1; }
static uint64_t leaf(uint64_t address) { return table(address) | (UINT64_C(0xfff) << 40); }
static void put(TomaccoDart *s, unsigned reg, uint32_t value)
{
    bool known;
    pdart_write(s, reg, value, &known);
    assert(known);
}
static uint32_t get(TomaccoDart *s, unsigned reg)
{
    bool known;
    uint32_t value = pdart_read(s, reg, &known);
    assert(known);
    return value;
}
static TomaccoDartTranslation walk(TomaccoDart *s, unsigned sid, uint64_t va, unsigned access)
{
    return pdart_translate(s, sid, va, access, table_read, NULL);
}
static void clear_error(TomaccoDart *s)
{
    put(s, 0x100, PDART_FLAG);
    put(s, 0x104, 0);
    for (unsigned i = 0; i < 8; ++i) { put(s, 0x1c0 + i * 4, UINT32_MAX); }
}
static void failed(TomaccoDart *s, unsigned sid, uint64_t va, unsigned access, unsigned error)
{
    clear_error(s);
    TomaccoDartTranslation t = walk(s, sid, va, access);
    assert(!t.permissions && pdart_irq(s));
    assert((s->error & 0x7fff) == error && s->error_address == va);
}
static void map(TomaccoDart *s, unsigned sid, uint64_t va, uint64_t pa, bool wide)
{
    memset(ram, 0, sizeof(ram));
    put(s, 0xc00 + (sid / 32) * 4, 1u << (sid % 32));
    put(s, 0x1000 + sid * 4, wide ? 9 : 1);
    put(s, 0x1400 + sid * 4, 5); /* 0x4000 root, valid */
    uint64_t base = 0x4000;
    if (wide) {
        ram[(base + ((va >> 36) & 2047) * 8) / 8] = table(0x10000);
        base = 0x10000;
    }
    ram[(base + ((va >> 25) & 2047) * 8) / 8] = table(0x8000);
    ram[(0x8000 + ((va >> 14) & 2047) * 8) / 8] = leaf(pa);
}

int main(void)
{
    TomaccoDart s;
    pdart_reset(&s);
    assert((get(&s, 0) >> 24) == 14 && (get(&s, 12) & 511) == 256);
    put(&s, 0, 0);
    assert((get(&s, 0) >> 24) == 14);
    bool known = true;
    pdart_read(&s, 1, &known); assert(!known);
    pdart_write(&s, 0x1800, 1, &known); assert(!known);

    const unsigned sid = 23;
    const uint64_t va = 0x12345054, pa = UINT64_C(0x234560000);
    map(&s, sid, va, pa, false);
    reads = 0;
    TomaccoDartTranslation t = walk(&s, sid, va, 3);
    assert(t.address == pa && t.mask == 0x3fff && t.permissions == 3 && reads == 2);
    assert(!pdart_irq(&s));
    assert(walk(&s, sid, va, 0).permissions == 3);
    assert(!walk(&s, 256, va, 0).permissions && !pdart_irq(&s));
    uint64_t *pte = &ram[(0x8000 + ((va >> 14) & 2047) * 8) / 8];
    *pte |= 4;
    assert(walk(&s, sid, va, 1).permissions == 1);
    failed(&s, sid, va, 2, 1u << 4);
    assert(get(&s, 0x170) == (uint32_t)va && get(&s, 0x174) == 0);
    assert(get(&s, 0x1c0) == (1u << sid));
    put(&s, 0x104, 1u << 4); assert(!pdart_irq(&s));
    put(&s, 0x104, 0); assert(pdart_irq(&s));
    uint32_t original = s.error;
    walk(&s, 255, va + 0x10000, 1);
    assert(s.error == (original | (1u << 19)) && s.error_address == va);
    assert(get(&s, 0x1dc) == (1u << 31));
    clear_error(&s); assert(!pdart_irq(&s));
    *pte = leaf(pa) | 8;
    failed(&s, sid, va, 1, 1u << 5);
    *pte = 0;
    failed(&s, sid, va, 1, 1u << 3);
    ram[(0x4000 + ((va >> 25) & 2047) * 8) / 8] = 0;
    failed(&s, sid, va, 1, 1u << 2);
    put(&s, 0x1400 + sid * 4, 0);
    failed(&s, sid, va, 1, 1);
    put(&s, 0x1400 + sid * 4, (0x40000 >> 12) | 1);
    failed(&s, sid, va, 1, 1u << 7);
    failed(&s, 256, va, 1, 1);
    failed(&s, sid, UINT64_C(1) << 36, 1, 1u << 6);

    const uint64_t wide = UINT64_C(0xa1234500054);
    map(&s, 255, wide, pa, true);
    reads = 0;
    t = walk(&s, 255, wide, 3);
    assert(t.address == pa && t.permissions == 3 && reads == 3);
    ram[(0x4000 + ((wide >> 36) & 2047) * 8) / 8] = 0;
    failed(&s, 255, wide, 1, 1u << 1);
    failed(&s, 255, UINT64_C(1) << 44, 1, 1u << 6);

    map(&s, sid, va, pa, false);
    pte = &ram[(0x8000 + ((va >> 14) & 2047) * 8) / 8];
    /* Every sub-page region returns an aligned IOTLB block contained in it. */
    for (unsigned start = 0; start < 4096; start += 7) {
        unsigned end = start + 23 < 4096 ? start + 23 : 4095;
        *pte = table(pa) | ((uint64_t)start << 52) | ((uint64_t)end << 40);
        for (unsigned unit = start; unit <= end; ++unit) {
            uint64_t address = (va & ~UINT64_C(0x3fff)) | (unit * 4 + 3);
            t = walk(&s, sid, address, 3);
            assert(t.permissions == 3 && t.mask >= 3 && (t.mask & (t.mask + 1)) == 0);
            uint64_t offset = t.address - pa;
            assert(offset >= start * 4 && offset + t.mask < (end + 1) * 4);
            assert(t.address + (address & t.mask) == pa + (address & 0x3fff));
        }
        if (start) { failed(&s, sid, (va & ~UINT64_C(0x3fff)) | (start * 4 - 1), 1, 1u << 5); }
        if (end < 4095) { failed(&s, sid, (va & ~UINT64_C(0x3fff)) | ((end + 1) * 4), 2, 1u << 4); }
    }
    *pte = table(pa) | (UINT64_C(30) << 52) | (UINT64_C(10) << 40);
    failed(&s, sid, va, 1, 1u << 5);

    put(&s, 0x1000 + sid * 4, 6);
    t = walk(&s, sid, pa + 11, 3);
    assert(t.address == pa && t.permissions == 3);
    failed(&s, sid, PDART_PA_MASK + 1, 1, 1u << 6);
    put(&s, 0xc20, 1u << sid);
    failed(&s, sid, va, 1, 1);
    put(&s, 0xc00, 1u << sid);
    put(&s, 0x1000 + sid * 4, 0x81);
    failed(&s, sid, va, 1, 1u << 10);

    put(&s, 0x1000, 1);
    put(&s, 0x200, 1);
    put(&s, 0x1000, 2); assert(get(&s, 0x1000) == 1);
    put(&s, 0x204, 1);
    put(&s, 0x1000, 2); assert(get(&s, 0x1000) == 2);
    put(&s, 0x200, 1); put(&s, 0x208, 1); put(&s, 0x204, 1);
    assert(get(&s, 0x200) == 1);
    assert(pdart_write(&s, 0x80, 0x117, &known) && known);
    assert(get(&s, 0x80) == 0x117);
    assert(!pdart_write(&s, 0x80, 0x200, &known) && !known);
    pdart_reset(&s);
    assert(!s.protect && !s.protect_lock && !s.error && !s.enabled[0]);
    puts("DART: DMA, page permissions, sub-page boundaries, faults, IRQ and locks passed");
}
