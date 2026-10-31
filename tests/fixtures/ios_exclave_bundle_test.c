/* Synthetic inputs only: exercise the firmware parser without Apple bytes. */
#include <assert.h>
#include <stdio.h>
#include "exclave-bundle.h"

static uint8_t image[0x10000];
static const uint64_t base = UINT64_C(0xfffffc0000000000);

static void w32(size_t at, uint32_t value)
{
    for (unsigned i = 0; i < 4; i++) { image[at + i] = value >> (i * 8); }
}
static void w64(size_t at, uint64_t value)
{
    w32(at, value); w32(at + 4, value >> 32);
}
static void range(unsigned i, uint32_t flags, uint32_t name)
{
    size_t at = TOMACCO_CL4_TABLE + i * 24;
    w64(at, i * 0x4000); w64(at + 8, 0x4000);
    w32(at + 16, flags); w32(at + 20, name);
}
static void valid_image(void)
{
    memset(image, 0, sizeof(image));
    w32(0, 0x14000200); w32(8, 0x42554e44); w32(12, 0x40000);
    w32(16, 1); w32(20, 4); w64(32, 0x4000); w64(72, 0x4000);
    range(0, 10, 0x6b747874); /* ktxt: kernel code */
    range(1, 14, 0x75747874); /* utxt: user code */
    range(2, 17, 0x6b646174); /* kdat: kernel data */
    range(3, 21, 0x7562646c); /* ubdl: metadata */
    w32(0xc000, 0xfeedfacf); w32(0xc004, 0x0100000c); w32(0xc00c, 5);
    w32(0xc010, 3); w32(0xc014, 72 * 2 + 288);
    for (unsigned i = 0; i < 2; i++) {
        size_t at = 0xc020 + i * 72;
        w32(at, 0x19); w32(at + 4, 72);
        memcpy(image + at + 8, i ? "__DATA" : "__TEXT", 6);
        w64(at + 24, base + i * 0x4000); w64(at + 32, 0x4000);
    }
    size_t at = 0xc020 + 144;
    w32(at, 5); w32(at + 4, 288); w32(at + 8, 6); w32(at + 12, 68);
    w64(at + 272, base + 0x800);
}
static void rejected(size_t length)
{
    TomaccoCl4Layout layout, saved;
    memset(&layout, 0x5a, sizeof(layout));
    memcpy(&saved, &layout, sizeof(saved));
    assert(tomacco_cl4_parse(image, length, &layout));
    assert(!memcmp(&saved, &layout, sizeof(saved)));
}
static void mutate32(size_t at, uint32_t value)
{
    valid_image(); w32(at, value); rejected(sizeof(image));
}
static void mutate64(size_t at, uint64_t value)
{
    valid_image(); w64(at, value); rejected(sizeof(image));
}

int main(void)
{
    TomaccoCl4Layout layout;
    valid_image();
    assert(!tomacco_cl4_parse(image, sizeof(image), &layout));
    assert(layout.rx_size == 0x8000 && layout.rw_size == 0x8000);
    assert(layout.virt == base && layout.entry == base + 0x800 && layout.ranges == 4);
    assert(tomacco_cl4_parse(NULL, sizeof(image), &layout));
    assert(tomacco_cl4_parse(image, sizeof(image), NULL));
    for (size_t length = 0; length < sizeof(image); length++) { rejected(length); }
    rejected(SIZE_MAX); rejected(TOMACCO_CL4_MAX_SIZE + 1);
    mutate32(0, 0x14000201); mutate32(0, 0x17ffffff);
    mutate32(8, 0); mutate32(12, 0x30000); mutate32(16, 2);
    mutate32(20, 0); mutate32(20, 17); mutate32(20, UINT32_MAX);
    mutate64(32, 0x8000); mutate64(72, 0x8000);
    for (unsigned i = 0; i < 4; i++) {
        size_t at = TOMACCO_CL4_TABLE + i * 24;
        mutate64(at, 1); mutate64(at, UINT64_MAX);
        mutate64(at + 8, 0); mutate64(at + 8, 1); mutate64(at + 8, UINT64_MAX);
        mutate32(at + 16, 0); mutate32(at + 16, 3); mutate32(at + 16, UINT32_MAX);
    }
    mutate32(TOMACCO_CL4_TABLE + 24 + 16, 17); /* RX after RW */
    valid_image(); w32(TOMACCO_CL4_TABLE + 24 + 16, 17);
    w32(TOMACCO_CL4_TABLE + 48 + 16, 10); rejected(sizeof(image));
    mutate32(TOMACCO_CL4_TABLE + 20, 0x72747874); /* missing kernel code */
    mutate32(TOMACCO_CL4_TABLE + 24 + 20, 0x6b747874); /* duplicate kernel */
    mutate32(TOMACCO_CL4_TABLE + 48 + 20, 0x72646174); /* missing kernel data */
    mutate32(TOMACCO_CL4_TABLE + 72 + 20, 0x72747874); /* missing metadata */
    mutate32(0xc000, 0); mutate32(0xc004, 0); mutate32(0xc00c, 2);
    mutate32(0xc010, 129); mutate32(0xc010, 2); mutate32(0xc010, 4);
    mutate32(0xc014, UINT32_MAX); mutate32(0xc014, 144 + 280);
    mutate32(0xc024, 0); mutate32(0xc024, UINT32_MAX); mutate32(0xc024, 71);
    mutate32(0xc020 + 64, 1); /* segment sections beyond command */
    mutate64(0xc020 + 24, UINT64_MAX - 0x3fff); /* address overflow */
    valid_image();
    w64(0xc020 + 24, UINT64_MAX - 0x7fff);
    w64(0xc020 + 72 + 24, UINT64_MAX - 0x3fff);
    w64(0xc020 + 144 + 272, UINT64_MAX - 0x77ff);
    rejected(sizeof(image)); /* writable bank wraps even when text fits */
    mutate64(0xc020 + 24, base + 1);
    mutate64(0xc020 + 32, 0x8000);
    mutate64(0xc020 + 72 + 24, base + 0x8000);
    mutate64(0xc020 + 72 + 32, 0x8000);
    mutate32(0xc020 + 144 + 8, 1); mutate32(0xc020 + 144 + 12, 1);
    mutate64(0xc020 + 144 + 272, base - 4);
    mutate64(0xc020 + 144 + 272, base + 0x4000);
    mutate64(0xc020 + 144 + 272, base + 0x801);
    mutate64(0xc020 + 144 + 272, base + 0x200);
    puts("CL4 valid banks, truncations, overflows, permissions and entry guards passed");
    return 0;
}
