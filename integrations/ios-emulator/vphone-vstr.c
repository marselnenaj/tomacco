/*
 * vphone600 SEP persistent block transport (VSTR) and DMA window (VSTD).
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Recovered from the original 23E5207q SEP driver at raw image offset
 * 0x48000: constructor 0x5154c, initialization 0x501e4, read 0x4fc10,
 * descriptor builder 0x4fdb4, submission 0x4fe1c and write 0x50068.
 * Guest firmware owns its record format, CRCs and key material. This device
 * only transfers actual sectors; it never formats or authenticates records.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "hw/core/sysbus.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "migration/vmstate.h"
#include "system/block-backend.h"
#include "vphone-vstr.h"

OBJECT_DECLARE_SIMPLE_TYPE(VPhoneVSTR, VPHONE_VSTR)

#define SECTOR_SIZE 512
#define DESC_SIZE 16
#define DESC_COUNT 3
#define DESC_NEXT 0x00010001
#define DESC_WRITE 0x00020001

struct VPhoneVSTR {
    SysBusDevice parent;
    MemoryRegion regs;
    MemoryRegion buffer;
    MemoryRegion window;
    AddressSpace dma;
    BlockBackend *drive;
    uint32_t sectors;
    uint32_t enabled, descriptors;
    uint64_t command;
    bool busy;
};

/* The original driver's physical buffer is a separate 16 KiB resource.
 * Restrict every descriptor and payload to it. Guest-controlled addresses
 * cannot reach AP/SEP RAM, MMIO callbacks, or wrap around the address space. */
static bool vstr_range(uint64_t address, size_t length)
{
    return address >= VPHONE_VSTD_BASE && length <= VPHONE_VSTD_SIZE &&
           address - VPHONE_VSTD_BASE <= VPHONE_VSTD_SIZE - length;
}

static MemTxResult vstr_dma(VPhoneVSTR *s, uint64_t address, void *buffer,
                           size_t length, bool write)
{
    if (!vstr_range(address, length)) {
        return MEMTX_DECODE_ERROR;
    }
    return address_space_rw(&s->dma, address - VPHONE_VSTD_BASE,
                            MEMTXATTRS_UNSPECIFIED, buffer, length, write);
}

static MemTxResult vstr_submit(VPhoneVSTR *s, uint64_t address)
{
    uint8_t desc[DESC_SIZE * DESC_COUNT], header[16], data[SECTOR_SIZE];
    uint64_t source, payload, completion, sector = 0;
    uint32_t type = UINT32_MAX;
    uint8_t status = 1;
    MemTxResult result;
    int ret;

    if (!s->enabled || s->descriptors != DESC_COUNT || s->busy ||
        (address & 15) ||
        vstr_dma(s, address, desc, sizeof(desc), false) != MEMTX_OK) {
        return MEMTX_DECODE_ERROR;
    }
    source = ldq_le_p(desc);
    payload = ldq_le_p(desc + DESC_SIZE);
    completion = ldq_le_p(desc + 2 * DESC_SIZE);
    /* No writable completion descriptor means no safe failure recipient. */
    if (ldl_le_p(desc + 2 * DESC_SIZE + 8) != 1 ||
        ldl_le_p(desc + 2 * DESC_SIZE + 12) != DESC_WRITE ||
        !vstr_range(completion, 1)) {
        return MEMTX_DECODE_ERROR;
    }
    s->busy = true;
    s->command = address;
    if (!s->drive || !blk_is_available(s->drive) ||
        ldl_le_p(desc + 8) != sizeof(header) ||
        ldl_le_p(desc + 12) != DESC_NEXT ||
        ldl_le_p(desc + DESC_SIZE + 8) != SECTOR_SIZE ||
        !vstr_range(payload, SECTOR_SIZE) ||
        vstr_dma(s, source, header, sizeof(header), false) != MEMTX_OK) {
        goto done;
    }
    type = ldl_le_p(header);
    sector = ldq_le_p(header + 8);
    if (ldl_le_p(header + 4) || sector >= s->sectors) {
        goto done;
    }
    if (type == 0 && ldl_le_p(desc + DESC_SIZE + 12) ==
                     (DESC_NEXT | DESC_WRITE)) {
        ret = blk_pread(s->drive, sector * SECTOR_SIZE, sizeof(data), data, 0);
        if (ret >= 0 && vstr_dma(s, payload, data, sizeof(data), true) == MEMTX_OK) {
            status = 0;
        }
    } else if (type == 1 && ldl_le_p(desc + DESC_SIZE + 12) == DESC_NEXT) {
        if (vstr_dma(s, payload, data, sizeof(data), false) != MEMTX_OK) {
            goto done;
        }
        ret = blk_pwrite(s->drive, sector * SECTOR_SIZE, sizeof(data), data, 0);
        /* The guest immediately verifies its write and relies on alternating
         * record copies across restarts. Never report success before flush. */
        if (ret >= 0 && blk_flush(s->drive) >= 0) {
            status = 0;
        }
    }
done:
    result = vstr_dma(s, completion, &status, 1, true);
    s->busy = false;
    qemu_log_mask(status ? LOG_GUEST_ERROR : LOG_UNIMP,
                  "tomacco-vphone VSTR: command=%u sector=%" PRIu64
                  " status=%u\n", type, sector, status);
    return result;
}

static MemTxResult vstr_read(void *opaque, hwaddr offset, uint64_t *value,
                            unsigned size, MemTxAttrs attrs)
{
    VPhoneVSTR *s = opaque;
    *value = 0;
    if (size == 4) {
        switch (offset) {
        case 0x0: *value = 1; return MEMTX_OK; /* custom device ID */
        case 0x4: *value = 2; return MEMTX_OK; /* block transport */
        case 0x10: *value = !s->busy; return MEMTX_OK;
        case 0x400: *value = s->enabled; return MEMTX_OK;
        case 0x404: *value = s->descriptors; return MEMTX_OK;
        case 0x420: *value = s->sectors; return MEMTX_OK;
        case 0x434: *value = SECTOR_SIZE; return MEMTX_OK;
        }
    } else if (size == 8) {
        switch (offset) {
        case 0x8: *value = 0x420; return MEMTX_OK; /* configuration offset */
        case 0x408: *value = s->command; return MEMTX_OK;
        }
    }
    qemu_log_mask(LOG_UNIMP, "tomacco-vphone VSTR: unsupported read 0x%"
                  HWADDR_PRIx " size=%u\n", offset, size);
    return MEMTX_DECODE_ERROR;
}

static MemTxResult vstr_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size, MemTxAttrs attrs)
{
    VPhoneVSTR *s = opaque;
    if (!s->busy && size == 4) {
        if (offset == 0x400 && value == 1) {
            s->enabled = value;
            return MEMTX_OK;
        }
        if (offset == 0x404 && value == DESC_COUNT) {
            s->descriptors = value;
            return MEMTX_OK;
        }
    }
    if (offset == 0x408 && size == 8) {
        return vstr_submit(s, value);
    }
    qemu_log_mask(LOG_UNIMP, "tomacco-vphone VSTR: unsupported write 0x%"
                  HWADDR_PRIx " size=%u value=0x%" PRIx64 "\n",
                  offset, size, value);
    return MEMTX_DECODE_ERROR;
}

static const MemoryRegionOps vstr_ops = {
    .read_with_attrs = vstr_read,
    .write_with_attrs = vstr_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 8 },
    .impl = { .min_access_size = 4, .max_access_size = 8 },
};

static void vstr_reset(DeviceState *dev)
{
    VPhoneVSTR *s = VPHONE_VSTR(dev);
    s->enabled = s->descriptors = 0;
    s->command = 0;
    s->busy = false;
    memset(memory_region_get_ram_ptr(&s->buffer), 0, VPHONE_VSTD_SIZE);
    memory_region_set_dirty(&s->buffer, 0, VPHONE_VSTD_SIZE);
    /* Persistent sectors deliberately survive a processor/device reset. */
}

static void vstr_realize(DeviceState *dev, Error **errp)
{
    VPhoneVSTR *s = VPHONE_VSTR(dev);
    if (s->drive) {
        int64_t size = blk_getlength(s->drive);
        if (size < 2 * SECTOR_SIZE || size % (2 * SECTOR_SIZE) ||
            size / SECTOR_SIZE > UINT32_MAX) {
            error_setg(errp, "VSTR requires a nonempty, even number of 512-byte sectors");
            return;
        }
        /* Reject another writer or resizer, including another VM. */
        if (blk_set_perm(s->drive, BLK_PERM_CONSISTENT_READ | BLK_PERM_WRITE,
                         BLK_PERM_CONSISTENT_READ, errp) < 0) {
            error_prepend(errp, "VSTR requires exclusive writable storage: ");
            return;
        }
        s->sectors = size / SECTOR_SIZE;
    }
    memory_region_init_ram(&s->buffer, OBJECT(s), "vphone-vstd",
                           VPHONE_VSTD_SIZE, &error_fatal);
    address_space_init(&s->dma, &s->buffer, "vphone-vstr-dma");
    /* Map an alias into the CPU. Mapping the DMA root itself would also
     * relocate that root's flat view, changing its zero-based offsets. */
    memory_region_init_alias(&s->window, OBJECT(s), "vphone-vstd-window",
                             &s->buffer, 0, VPHONE_VSTD_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->window);
}

static void vstr_init(Object *obj)
{
    VPhoneVSTR *s = VPHONE_VSTR(obj);
    memory_region_init_io(&s->regs, obj, &vstr_ops, s,
                          "vphone-vstr", VPHONE_VSTR_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->regs);
}

static void vstr_unrealize(DeviceState *dev)
{
    address_space_destroy(&VPHONE_VSTR(dev)->dma);
}

static const Property vstr_properties[] = {
    DEFINE_PROP_DRIVE("drive", VPhoneVSTR, drive),
};

static const VMStateDescription vstr_vmstate = {
    .name = TYPE_VPHONE_VSTR,
    .unmigratable = true,
};

static void vstr_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = vstr_realize;
    dc->unrealize = vstr_unrealize;
    dc->user_creatable = false;
    dc->vmsd = &vstr_vmstate;
    dc->desc = "vphone SEP persistent block transport";
    device_class_set_legacy_reset(dc, vstr_reset);
    device_class_set_props(dc, vstr_properties);
}

static const TypeInfo vstr_type = {
    .name = TYPE_VPHONE_VSTR,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(VPhoneVSTR),
    .instance_init = vstr_init,
    .class_init = vstr_class_init,
};

static void register_vstr(void)
{
    type_register_static(&vstr_type);
}
type_init(register_vstr)
