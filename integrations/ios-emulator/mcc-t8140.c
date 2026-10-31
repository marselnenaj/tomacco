/* SPDX-License-Identifier: GPL-2.0-or-later
 * Strict MMIO adapter for the evidenced T8140 MCC register subset.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/core/sysbus.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/boards.h"
#include "hw/core/cpu.h"
#include "hw/arm/machines-qom.h"
#include "hw/arm/apple_amcc.h"
#include "target/arm/cpu-qom.h"
#include "system/address-spaces.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "trace.h"
#include "mcc-t8140.h"

#define TYPE_TOMACCO_MCC "tomacco-mcc-t8140"
OBJECT_DECLARE_SIMPLE_TYPE(TomaccoMccDevice, TOMACCO_MCC)

typedef struct TomaccoMccBank {
    MemoryRegion mmio;
    TomaccoMccDevice *device;
    unsigned index;
    hwaddr offset;
} TomaccoMccBank;

struct TomaccoMccDevice {
    SysBusDevice parent_obj;
    TomaccoMccBank bank[2 + PMCC_CHANNELS];
    TomaccoMcc state;
    uint64_t dram_config_boot;
    unsigned trace_count;
};

static void mcc_trace(TomaccoMccBank *b, bool write, hwaddr reg, uint64_t value,
                       unsigned size, bool supported)
{
    if (b->device->trace_count < 1024) {
        b->device->trace_count++;
        trace_tomacco_mcc_mmio(write, b->index, reg, value, size, supported);
    }
}

static MemTxResult mcc_read(void *opaque, hwaddr reg, uint64_t *value,
                            unsigned size, MemTxAttrs attrs)
{
    TomaccoMccBank *b = opaque;
    reg += b->offset;
    uint32_t result = 0;
    bool supported = pmcc_read(&b->device->state, b->index, reg, size, &result);
    *value = result;
    mcc_trace(b, false, reg, result, size, supported);
    return supported ? MEMTX_OK : MEMTX_DECODE_ERROR;
}

static MemTxResult mcc_write(void *opaque, hwaddr reg, uint64_t value,
                             unsigned size, MemTxAttrs attrs)
{
    TomaccoMccBank *b = opaque;
    reg += b->offset;
    bool supported = pmcc_write(&b->device->state, b->index, reg, size, value);
    mcc_trace(b, true, reg, value, size, supported);
    return supported ? MEMTX_OK : MEMTX_DECODE_ERROR;
}

static const MemoryRegionOps mcc_ops = {
    .read_with_attrs = mcc_read, .write_with_attrs = mcc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    /* Deliver each bus access intact to the validator. Splitting a rejected
     * 64-bit store into two 32-bit writes would mutate the control register. */
    .valid = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
    .impl = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
};

static void mcc_reset(DeviceState *dev)
{
    TomaccoMccDevice *s = TOMACCO_MCC(dev);
    pmcc_reset(&s->state, s->dram_config_boot);
    s->trace_count = 0;
}

static void mcc_realize(DeviceState *dev, Error **errp)
{
    TomaccoMccDevice *s = TOMACCO_MCC(dev);
    if (s->dram_config_boot > UINT32_MAX) {
        error_setg(errp, "T8140 MCC requires an explicit 32-bit dram-config boot value");
    }
}

static void mcc_init(Object *obj)
{
    TomaccoMccDevice *s = TOMACCO_MCC(obj);
    for (unsigned i = 0; i < G_N_ELEMENTS(s->bank); ++i) {
        TomaccoMccBank *b = &s->bank[i];
        bool gate = i == 1 + PMCC_CHANNELS;
        g_autofree char *name = g_strdup_printf("mcc-bank-%u", i);
        b->device = s;
        b->index = gate ? 0 : i;
        /* The first AMCC page belongs to the existing CTRR register device.
         * Do not shadow its range registers or their CPU permission checks. */
        b->offset = gate ? PMCC_GATE_WINDOW : (i ? 0 : 0x1000);
        /* The small first-page window only owns the maintenance gate.
         * Padding faults and keeps a wrong-width access intact even when
         * it straddles the four-byte gate; CTRR limits remain elsewhere. */
        memory_region_init_io(&b->mmio, obj, &mcc_ops, b, name,
                              gate ? PMCC_GATE_WINDOW_SIZE :
                              (i ? 0x8000 : 0x140000 - b->offset));
        sysbus_init_mmio(SYS_BUS_DEVICE(s), &b->mmio);
    }
}

static const Property mcc_properties[] = {
    DEFINE_PROP_UINT64("dram-config", TomaccoMccDevice, dram_config_boot, UINT64_MAX),
};
static const VMStateDescription mcc_vmstate = {
    .name = TYPE_TOMACCO_MCC, .unmigratable = true,
};

static void mcc_class(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->desc = "T8140 MCC cache, quota and DRAM registers (research subset)";
    dc->realize = mcc_realize;
    dc->vmsd = &mcc_vmstate;
    device_class_set_props(dc, mcc_properties);
    device_class_set_legacy_reset(dc, mcc_reset);
}

static const TypeInfo mcc_types[] = {
    {.name = TYPE_TOMACCO_MCC, .parent = TYPE_SYS_BUS_DEVICE,
     .instance_size = sizeof(TomaccoMccDevice), .instance_init = mcc_init,
     .class_init = mcc_class},
};
DEFINE_TYPES(mcc_types)

/* Real CPU, real MMIO, no Apple firmware. The arbitrary boot timing word is
 * deliberately different from the research tree, to test parameter flow. */
static void mcc_test_init(MachineState *machine)
{
    DeviceState *dev = qdev_new(TYPE_TOMACCO_MCC);
    DeviceState *ctrr_device = qdev_new(TYPE_AMCC);
    AMCCState *ctrr = AMCC(ctrr_device);
    cpu_create(machine->cpu_type);
    memory_region_add_subregion(get_system_memory(), 0, machine->ram);
    object_property_add_child(OBJECT(machine), "ctrr", OBJECT(ctrr_device));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(ctrr_device), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(ctrr_device), 0, 0x220000000);
    ctrr->lwr_reg[0] = 0x10;
    ctrr->upr_reg[0] = 0x14;
    object_property_add_child(OBJECT(machine), "mcc", OBJECT(dev));
    qdev_prop_set_uint64(dev, "dram-config", 0x12345678);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(dev), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(dev), 0, 0x220001000);
    sysbus_mmio_map_overlap(SYS_BUS_DEVICE(dev), 1 + PMCC_CHANNELS,
                           0x220000000 + PMCC_GATE_WINDOW, 1);
    for (unsigned i = 0; i < PMCC_CHANNELS; ++i) {
        sysbus_mmio_map(SYS_BUS_DEVICE(dev), i + 1, 0x220400000 + i * 0x200000);
    }
}

static void mcc_test_class(MachineClass *mc)
{
    mc->desc = "Tomacco MCC register test rig (no OS or firmware)";
    mc->init = mcc_test_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("max");
    mc->default_ram_size = 64 * 1024 * 1024;
    mc->default_ram_id = "tomacco-mcc-test.ram";
    mc->no_serial = mc->no_parallel = mc->no_floppy = mc->no_cdrom = true;
}
DEFINE_MACHINE_WITH_INTERFACES("tomacco-mcc-test", mcc_test_class,
                               { TYPE_TARGET_AARCH64_MACHINE }, { })
