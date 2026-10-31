/* Synthetic object bytes prove bounds and bit preservation, not native pixels. */
#include <assert.h>
#include <limits.h>
#include <pthread.h>
#include <string.h>
#include "vphone-surface-source.h"

static void put32(uint8_t *bytes, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i) bytes[i] = (uint8_t)(value >> (8 * i));
}
static void put64(uint8_t *bytes, uint64_t value)
{
    for (unsigned i = 0; i < 8; ++i) bytes[i] = (uint8_t)(value >> (8 * i));
}
static void valid(uint8_t bytes[TOMACCO_SURFACE_OBJECT_BYTES])
{
    memset(bytes, 0x9d, TOMACCO_SURFACE_OBJECT_BYTES);
    put32(bytes + 0x10, UINT32_MAX);
    put32(bytes + 0x80, TOMACCO_SURFACE_TARGET_FORMAT);
    put32(bytes + 0x98, 2);
    put64(bytes + 0x58, 320);
    put64(bytes + 0x60, 320);
}
static unsigned slots, seen[129];
static void *worker(void *unused)
{
    (void)unused;
    for (unsigned i = 0; i < 10000; ++i) {
        unsigned slot = tomacco_surface_source_slot(&slots, 128);
        if (slot) {
            assert(slot <= 128);
            __atomic_fetch_add(&seen[slot], 1, __ATOMIC_RELAXED);
        }
    }
    return NULL;
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    uint8_t bytes[TOMACCO_SURFACE_OBJECT_BYTES];
    valid(bytes);
    TomaccoSurfaceSource value;
    if (!strcmp(argv[1], "decode")) {
        assert(tomacco_surface_source_decode(bytes, sizeof(bytes), &value));
        assert(value.native_id == UINT32_MAX && value.planes == 2);
        assert(value.pixel_format == TOMACCO_SURFACE_TARGET_FORMAT);
        assert(value.width == 320 && value.height == 320);
        put32(bytes + 0x10, 0);
        put32(bytes + 0x98, 0);
        put64(bytes + 0x58, 0);
        put64(bytes + 0x60, 0xffffff);
        assert(tomacco_surface_source_decode(bytes, sizeof(bytes), &value));
        assert(value.native_id == 0 && value.planes == 0 && value.width == 0);
        assert(value.height == 0xffffff);
    } else if (!strcmp(argv[1], "bounds")) {
        memset(&value, 0xa5, sizeof(value));
        uint8_t before[sizeof(value)];
        memcpy(before, &value, sizeof(value));
        for (unsigned length = 0; length < sizeof(bytes); ++length) {
            assert(!tomacco_surface_source_decode(bytes, length, &value));
            assert(!memcmp(before, &value, sizeof(value)));
        }
        assert(!tomacco_surface_source_decode(NULL, sizeof(bytes), &value));
        assert(!tomacco_surface_source_decode(bytes, sizeof(bytes), NULL));
        put32(bytes + 0x98, 8);
        assert(!tomacco_surface_source_decode(bytes, sizeof(bytes), &value));
        put32(bytes + 0x98, 7);
        put64(bytes + 0x58, 0x1000000);
        assert(!tomacco_surface_source_decode(bytes, sizeof(bytes), &value));
        put64(bytes + 0x58, 320);
        put64(bytes + 0x60, UINT64_MAX);
        assert(!tomacco_surface_source_decode(bytes, sizeof(bytes), &value));
        assert(!memcmp(before, &value, sizeof(value)));
    } else if (!strcmp(argv[1], "pc")) {
        for (unsigned el = 0; el <= 3; ++el)
            assert(tomacco_surface_source_pc(TOMACCO_SURFACE_PRODUCER_PC, el,
                TOMACCO_SURFACE_PRODUCER_INSN, TOMACCO_SURFACE_PRODUCER_PC) == (el == 1));
        for (unsigned bit = 0; bit < 32; ++bit)
            assert(!tomacco_surface_source_pc(TOMACCO_SURFACE_PRODUCER_PC, 1,
                TOMACCO_SURFACE_PRODUCER_INSN ^ (UINT32_C(1) << bit), TOMACCO_SURFACE_PRODUCER_PC));
        assert(!tomacco_surface_source_pc(TOMACCO_SURFACE_PRODUCER_PC + 4, 1,
                                        TOMACCO_SURFACE_PRODUCER_INSN, TOMACCO_SURFACE_PRODUCER_PC));
        assert(!tomacco_surface_source_pc(TOMACCO_SURFACE_PRODUCER_PC - 4, 1,
                                        TOMACCO_SURFACE_PRODUCER_INSN, TOMACCO_SURFACE_PRODUCER_PC));
        assert(!tomacco_surface_source_pc(0, 1, TOMACCO_SURFACE_PRODUCER_INSN, 0));
    } else if (!strcmp(argv[1], "placement")) {
        uint64_t entry = 123;
        assert(tomacco_surface_kernel_entry(TOMACCO_SURFACE_KERNEL_BASE,
                                          TOMACCO_SURFACE_KERNEL_BASE, &entry));
        assert(entry == TOMACCO_SURFACE_PRODUCER_PC);
        entry = 123;
        assert(!tomacco_surface_kernel_entry(0, TOMACCO_SURFACE_KERNEL_BASE, &entry));
        assert(!tomacco_surface_kernel_entry(TOMACCO_SURFACE_KERNEL_BASE,
                                           TOMACCO_SURFACE_KERNEL_BASE - 0x4000, &entry));
        assert(!tomacco_surface_kernel_entry(TOMACCO_SURFACE_KERNEL_BASE,
                                           TOMACCO_SURFACE_KERNEL_BASE + 4, &entry));
        assert(!tomacco_surface_kernel_entry(TOMACCO_SURFACE_KERNEL_BASE,
                                           UINT64_MAX & ~UINT64_C(0x3fff), &entry));
        assert(!tomacco_surface_kernel_entry(TOMACCO_SURFACE_KERNEL_BASE,
                                           TOMACCO_SURFACE_KERNEL_BASE, NULL));
        assert(entry == 123);
    } else if (!strcmp(argv[1], "relocated")) {
        uint64_t slide = UINT64_C(0x20000000), entry = 0;
        assert(tomacco_surface_kernel_entry(TOMACCO_SURFACE_KERNEL_BASE,
                                          TOMACCO_SURFACE_KERNEL_BASE + slide, &entry));
        assert(entry == TOMACCO_SURFACE_PRODUCER_PC + slide);
        assert(tomacco_surface_source_pc(entry, 1, TOMACCO_SURFACE_PRODUCER_INSN, entry));
        assert(!tomacco_surface_source_pc(TOMACCO_SURFACE_PRODUCER_PC, 1,
                                        TOMACCO_SURFACE_PRODUCER_INSN, entry));
        for (unsigned el = 0; el <= 3; ++el)
            assert(tomacco_surface_source_pc(entry, el, TOMACCO_SURFACE_PRODUCER_INSN,
                                            entry) == (el == 1));
        for (unsigned bit = 0; bit < 32; ++bit)
            assert(!tomacco_surface_source_pc(entry, 1,
                TOMACCO_SURFACE_PRODUCER_INSN ^ (UINT32_C(1) << bit), entry));
        assert(!tomacco_surface_source_pc(entry + 4, 1, TOMACCO_SURFACE_PRODUCER_INSN, entry));
        assert(!tomacco_surface_source_pc(entry - 4, 1, TOMACCO_SURFACE_PRODUCER_INSN, entry));
    } else {
        assert(!strcmp(argv[1], "budget"));
        pthread_t threads[8];
        for (unsigned i = 0; i < 8; ++i)
            assert(!pthread_create(&threads[i], NULL, worker, NULL));
        for (unsigned i = 0; i < 8; ++i) assert(!pthread_join(threads[i], NULL));
        assert(slots == 128 && !seen[0]);
        for (unsigned i = 1; i <= 128; ++i) assert(seen[i] == 1);
        assert(!tomacco_surface_source_slot(&slots, 128));
        slots = UINT_MAX;
        assert(!tomacco_surface_source_slot(&slots, 128) && slots == UINT_MAX);
        slots = 0;
        assert(!tomacco_surface_source_slot(&slots, 0) && !slots);
    }
    return 0;
}
