/* SPDX-License-Identifier: GPL-2.0-or-later
 * Tomacco T8110 DART: QEMU MMIO, DMA translation and interrupt adapter.
 * It intentionally supplies no coprocessor, firmware, GPU or display endpoint.
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qapi/error.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "hw/core/boards.h"
#include "hw/core/cpu.h"
#include "hw/arm/machines-qom.h"
#include "target/arm/cpu-qom.h"
#include "system/address-spaces.h"
#include "system/dma.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "trace.h"
#include "dart-t8110.h"

#define TYPE_TOMACCO_DART "tomacco-dart-t8110"
#define TYPE_TOMACCO_DART_IOMMU "tomacco-dart-t8110-iommu"
OBJECT_DECLARE_SIMPLE_TYPE(TomaccoDartDevice, TOMACCO_DART)
OBJECT_DECLARE_SIMPLE_TYPE(TomaccoDartRegion, TOMACCO_DART_IOMMU)

struct TomaccoDartRegion {
    IOMMUMemoryRegion parent_obj;
    TomaccoDartDevice *device;
    unsigned sid;
};

struct TomaccoDartDevice {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    TomaccoDartRegion dma[PDART_STREAMS];
    TomaccoDart state;
    qemu_irq irq;
    unsigned log_count, trace_count;
};

static void dart_invalidate(TomaccoDartDevice *s)
{
    /* The core walks afresh; downstream DMA users can cache translations. */
    for (unsigned i = 0; i < PDART_STREAMS; ++i) {
        IOMMUTLBEvent event = {
            .type = IOMMU_NOTIFIER_UNMAP,
            .entry = {.target_as = &address_space_memory, .addr_mask = UINT64_MAX},
        };
        memory_region_notify_iommu(&s->dma[i].parent_obj, 0, event);
    }
}

static void dart_log(TomaccoDartDevice *s, const char *action, hwaddr reg,
                     uint64_t value, bool known)
{
    /* Bound diagnostics even when a guest repeatedly polls unknown hardware. */
    if (s->trace_count < 1024) {
        ++s->trace_count;
        trace_tomacco_dart_mmio(action, reg, value, known);
    }
    if (!known && s->log_count < 64) {
        ++s->log_count;
        qemu_log_mask(LOG_UNIMP,
            "TOMACCO_DART %s offset=0x%" HWADDR_PRIx " value=0x%" PRIx64
            " supported=%u\n", action, reg, value, known);
    }
}

static uint64_t dart_read(void *opaque, hwaddr reg, unsigned size)
{
    TomaccoDartDevice *s = opaque;
    bool known;
    uint32_t value = pdart_read(&s->state, reg, &known);
    dart_log(s, "read", reg, value, known);
    return value;
}

static void dart_write(void *opaque, hwaddr reg, uint64_t value, unsigned size)
{
    TomaccoDartDevice *s = opaque;
    bool known;
    if (pdart_write(&s->state, reg, value, &known)) { dart_invalidate(s); }
    qemu_set_irq(s->irq, pdart_irq(&s->state));
    dart_log(s, "write", reg, value, known);
}

static const MemoryRegionOps dart_ops = {
    .read = dart_read, .write = dart_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {.min_access_size = 4, .max_access_size = 4},
    .impl = {.min_access_size = 4, .max_access_size = 4},
};

static bool dart_table_read(void *opaque, uint64_t address, uint64_t *entry)
{
    /* Page walks must never trigger an MMIO device, including this IOMMU. */
    hwaddr length = 8;
    MemoryRegion *ram;
    hwaddr offset;
    ram = address_space_translate(&address_space_memory, address, &offset,
                                 &length, false, MEMTXATTRS_UNSPECIFIED);
    if (!memory_region_is_ram(ram) || length < 8) { return false; }
    return ldq_le_dma(&address_space_memory, address, entry,
                      MEMTXATTRS_UNSPECIFIED) == MEMTX_OK;
}

static IOMMUTLBEntry dart_translate(IOMMUMemoryRegion *iommu, hwaddr address,
                                    IOMMUAccessFlags flags, int index)
{
    TomaccoDartRegion *region = TOMACCO_DART_IOMMU(iommu);
    TomaccoDartDevice *s = region->device;
    TomaccoDartTranslation t = pdart_translate(&s->state, region->sid, address,
                                              flags, dart_table_read, s);
    qemu_set_irq(s->irq, pdart_irq(&s->state));
    return (IOMMUTLBEntry) {
        .target_as = &address_space_memory, .iova = address & ~t.mask,
        .translated_addr = t.address, .addr_mask = t.mask, .perm = t.permissions,
    };
}

static uint64_t dart_min_page(IOMMUMemoryRegion *iommu) { return 4; }

static void dart_iommu_class(ObjectClass *oc, const void *data)
{
    IOMMUMemoryRegionClass *ic = IOMMU_MEMORY_REGION_CLASS(oc);
    ic->translate = dart_translate;
    ic->get_min_page_size = dart_min_page;
}

static void dart_reset(DeviceState *dev)
{
    TomaccoDartDevice *s = TOMACCO_DART(dev);
    pdart_reset(&s->state);
    s->log_count = s->trace_count = 0;
    qemu_set_irq(s->irq, 0);
    dart_invalidate(s);
}

static void dart_init(Object *obj)
{
    TomaccoDartDevice *s = TOMACCO_DART(obj);
    SysBusDevice *bus = SYS_BUS_DEVICE(obj);
    memory_region_init_io(&s->mmio, obj, &dart_ops, s, TYPE_TOMACCO_DART, 0xc000);
    sysbus_init_mmio(bus, &s->mmio);
    sysbus_init_irq(bus, &s->irq);
    for (unsigned i = 0; i < PDART_STREAMS; ++i) {
        g_autofree char *name = g_strdup_printf("dart-stream-%u", i);
        memory_region_init_iommu(&s->dma[i].parent_obj, sizeof(s->dma[i]),
            TYPE_TOMACCO_DART_IOMMU, obj, name, UINT64_MAX);
        s->dma[i].device = s;
        s->dma[i].sid = i;
    }
}

static const VMStateDescription dart_vmstate = {
    .name = TYPE_TOMACCO_DART, .unmigratable = true,
};

static void dart_class(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->desc = "T8110 DART DMA translation (experimental Apple hardware)";
    dc->vmsd = &dart_vmstate;
    device_class_set_legacy_reset(dc, dart_reset);
}

static const TypeInfo dart_types[] = {
    {.name = TYPE_TOMACCO_DART_IOMMU, .parent = TYPE_IOMMU_MEMORY_REGION,
     .instance_size = sizeof(TomaccoDartRegion), .class_init = dart_iommu_class},
    {.name = TYPE_TOMACCO_DART, .parent = TYPE_SYS_BUS_DEVICE,
     .instance_size = sizeof(TomaccoDartDevice), .instance_init = dart_init,
     .class_init = dart_class},
};
DEFINE_TYPES(dart_types)

/* Firmware-free test rig: QTest drives a real DMA bus through stream 23, while
 * the other window accesses physical RAM. This is deliberately not an OS or a
 * simulated iPhone. Keep the DMA window above the DART's physical address limit
 * so an adversarial page table cannot create recursive IOMMU translation. */
static void dart_test_init(MachineState *machine)
{
    DeviceState *dev = qdev_new(TYPE_TOMACCO_DART);
    TomaccoDartDevice *dart = TOMACCO_DART(dev);
    MemoryRegion *dma_window = g_new(MemoryRegion, 1);
    /* QTest memory transactions use first_cpu->as, even with execution paused. */
    cpu_create(machine->cpu_type);
    memory_region_add_subregion(get_system_memory(), 0, machine->ram);
    object_property_add_child(OBJECT(machine), "dart", OBJECT(dev));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(dev), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(dev), 0, 0x10000000);
    memory_region_init_alias(dma_window, OBJECT(machine), "test-dma-stream-23",
        MEMORY_REGION(&dart->dma[23]), 0, UINT64_C(1) << 44);
    memory_region_add_subregion(get_system_memory(), UINT64_C(1) << 44, dma_window);
}

static void dart_test_class(MachineClass *mc)
{
    mc->desc = "Tomacco DART hardware test rig (no OS or firmware)";
    mc->init = dart_test_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("max");
    mc->default_ram_size = 64 * 1024 * 1024;
    mc->default_ram_id = "tomacco-dart-test.ram";
    mc->no_serial = mc->no_parallel = mc->no_floppy = mc->no_cdrom = true;
}
DEFINE_MACHINE_WITH_INTERFACES("tomacco-dart-test", dart_test_class,
                               { TYPE_TARGET_AARCH64_MACHINE }, { })
