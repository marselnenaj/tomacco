/*
 * Virtual iPhone AP/SEP boot measurement registers.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * AVPBooter writes a 68-byte bank at +0x90, uses +0xd4 bit 0 as its valid
 * flag, and bit 16 to lock it. Its +0x400 read/modify/writes accumulate boot
 * flags. The 23E5207q AESSEP consumer reads +0x400 and 52-byte records at
 * +0x00, +0x90 and +0xd8. These offsets are read from the producer and
 * consumer; other behavior is inferred, and the state is unprovisioned.
 *
 * Both processors observe one device. Only the AP view accepts writes;
 * no SEP-side write contract has been recovered. Reset clears all records:
 * this device does not manufacture measurements, signatures, keys or a claim
 * that a firmware image has been authenticated.
 */
#include "qemu/osdep.h"
#include "hw/core/sysbus.h"
#include "migration/vmstate.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "vphone-asmf.h"

#define BANK_BYTES 0x48
#define BANK_DATA_WORDS 17
#define BANK_COUNT 4
#define BANK_VALID 1
#define BANK_LOCKED (1U << 16)
#define BOOT_FLAGS 0x400

OBJECT_DECLARE_SIMPLE_TYPE(VPhoneASMF, VPHONE_ASMF)

typedef struct ASMFView {
    VPhoneASMF *parent;
    MemoryRegion memory;
    bool writable;
} ASMFView;

struct VPhoneASMF {
    SysBusDevice parent;
    uint32_t data[BANK_COUNT][BANK_DATA_WORDS];
    uint32_t control[BANK_COUNT];
    uint32_t boot_flags;
    ASMFView view[2]; /* AP producer, SEP consumer */
};

static bool asmf_bank(hwaddr offset, unsigned *bank, unsigned *word)
{
    *bank = offset / BANK_BYTES;
    *word = (offset % BANK_BYTES) / 4;
    /* Bank 1 and the remainder of the aperture have no observed contract. */
    return *bank < BANK_COUNT && *bank != 1 && !(offset & 3);
}

static MemTxResult asmf_read(void *opaque, hwaddr offset, uint64_t *value,
                             unsigned size, MemTxAttrs attrs)
{
    ASMFView *view = opaque;
    VPhoneASMF *s = view->parent;
    unsigned bank, word;

    if (size != 4) {
        return MEMTX_DECODE_ERROR;
    }
    if (offset == BOOT_FLAGS) {
        *value = s->boot_flags;
    } else if (asmf_bank(offset, &bank, &word)) {
        *value = word == BANK_DATA_WORDS ? s->control[bank] : s->data[bank][word];
    } else {
        qemu_log_mask(LOG_UNIMP, "tomacco-vphone ASMF: unsupported read offset=0x%"
                      HWADDR_PRIx " size=%u\n", offset, size);
        return MEMTX_DECODE_ERROR;
    }
    return MEMTX_OK;
}

static MemTxResult asmf_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size, MemTxAttrs attrs)
{
    ASMFView *view = opaque;
    VPhoneASMF *s = view->parent;
    unsigned bank, word;

    if (!view->writable || size != 4) {
        return MEMTX_DECODE_ERROR;
    }
    if (offset == BOOT_FLAGS) {
        /* Only bits 0 and 1 have an observed producer. Preserve their
         * accumulated state for this boot, including on a clearing write. */
        if (value & ~3U) {
            return MEMTX_DECODE_ERROR;
        }
        s->boot_flags |= value;
    } else if (asmf_bank(offset, &bank, &word)) {
        if (s->control[bank] & BANK_LOCKED) {
            return MEMTX_OK; /* The locked bank is immutable until reset. */
        }
        if (word == BANK_DATA_WORDS) {
            if (value & ~(BANK_VALID | BANK_LOCKED)) {
                return MEMTX_DECODE_ERROR;
            }
            s->control[bank] = value;
        } else {
            s->data[bank][word] = value;
        }
    } else {
        qemu_log_mask(LOG_UNIMP, "tomacco-vphone ASMF: unsupported write offset=0x%"
                      HWADDR_PRIx " size=%u\n", offset, size);
        return MEMTX_DECODE_ERROR;
    }
    return MEMTX_OK;
}

static const MemoryRegionOps asmf_ops = {
    .read_with_attrs = asmf_read,
    .write_with_attrs = asmf_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void asmf_reset(DeviceState *dev)
{
    VPhoneASMF *s = VPHONE_ASMF(dev);

    memset(s->data, 0, sizeof(s->data));
    memset(s->control, 0, sizeof(s->control));
    s->boot_flags = 0;
}

static void asmf_init(Object *obj)
{
    VPhoneASMF *s = VPHONE_ASMF(obj);

    for (unsigned i = 0; i < 2; i++) {
        ASMFView *view = &s->view[i];
        view->parent = s;
        view->writable = i == 0;
        memory_region_init_io(&view->memory, obj, &asmf_ops, view,
                              i ? "asmf-sep" : "asmf-ap", VPHONE_ASMF_SIZE);
        sysbus_init_mmio(SYS_BUS_DEVICE(s), &view->memory);
    }
}

static const VMStateDescription asmf_vmstate = {
    .name = TYPE_VPHONE_ASMF,
    .unmigratable = true, /* The surrounding SEP has no migration contract. */
};

static void asmf_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->user_creatable = false;
    dc->desc = "vphone shared AP/SEP boot measurement registers";
    dc->vmsd = &asmf_vmstate;
    device_class_set_legacy_reset(dc, asmf_reset);
}

static const TypeInfo asmf_type = {
    .name = TYPE_VPHONE_ASMF,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(VPhoneASMF),
    .instance_init = asmf_init,
    .class_init = asmf_class_init,
};

static void register_asmf(void)
{
    type_register_static(&asmf_type);
}
type_init(register_asmf)
