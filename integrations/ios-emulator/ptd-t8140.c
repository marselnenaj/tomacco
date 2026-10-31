/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/core/sysbus.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/boards.h"
#include "hw/core/cpu.h"
#include "hw/core/irq.h"
#include "hw/arm/machines-qom.h"
#include "target/arm/cpu.h"
#include "target/arm/internals.h"
#include "system/address-spaces.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "trace.h"
#include "ptd-t8140.h"

#define TYPE_TOMACCO_PTD "tomacco-ptd-t8140"
OBJECT_DECLARE_SIMPLE_TYPE(TomaccoPtdDevice, TOMACCO_PTD)

struct TomaccoPtdDevice {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    ARMCPU *cpu;
    TomaccoPtd state;
    qemu_irq irq[PPTD_IRQS];
    bool irq_connected;
    unsigned trace_count;
};

static void ptd_update_irqs(TomaccoPtdDevice *s)
{
    uint32_t lines = pptd_irqs(&s->state);
    for (unsigned i = 0; i < PPTD_IRQS; ++i) {
        qemu_set_irq(s->irq[i], (lines >> i) & 1);
    }
}

static void ptd_trace(TomaccoPtdDevice *s, bool write, hwaddr reg,
                      uint64_t value, unsigned size, bool supported)
{
    if (s->trace_count < 1024) {
        s->trace_count++;
        trace_tomacco_ptd_mmio(write, reg, value, size, supported);
    }
}

static MemTxResult ptd_read(void *opaque, hwaddr reg, uint64_t *value,
                            unsigned size, MemTxAttrs attrs)
{
    TomaccoPtdDevice *s = opaque;
    uint64_t result = 0;
    bool supported = pptd_read(&s->state, reg, size, &result);
    *value = result;
    ptd_update_irqs(s);
    ptd_trace(s, false, reg, result, size, supported);
    return supported ? MEMTX_OK : MEMTX_DECODE_ERROR;
}

static MemTxResult ptd_write(void *opaque, hwaddr reg, uint64_t value,
                             unsigned size, MemTxAttrs attrs)
{
    TomaccoPtdDevice *s = opaque;
    /* The full Darwin platform has no qualified PTD interrupt wiring yet.
     * Do not silently accept an enable that could never reach its consumer. */
    bool supported = !(!s->irq_connected && value &&
                        reg >= PPTD_ENABLE && reg < PPTD_ENABLE + PPTD_WORDS * 4);
    if (supported) {
        supported = pptd_write(&s->state, reg, size, value,
                               gt_get_countervalue(&s->cpu->env));
    }
    ptd_update_irqs(s);
    ptd_trace(s, true, reg, value, size, supported);
    return supported ? MEMTX_OK : MEMTX_DECODE_ERROR;
}

static const MemoryRegionOps ptd_ops = {
    .read_with_attrs = ptd_read, .write_with_attrs = ptd_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    /* Keep invalid partial and spanning accesses intact. LDP reads the
     * value then metadata as two 64-bit device transactions. */
    .valid = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
    .impl = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
};

static void ptd_reset(DeviceState *dev)
{
    TomaccoPtdDevice *s = TOMACCO_PTD(dev);
    pptd_reset(&s->state);
    s->trace_count = 0;
    ptd_update_irqs(s);
}

static void ptd_realize(DeviceState *dev, Error **errp)
{
    if (!TOMACCO_PTD(dev)->cpu) {
        error_setg(errp, "PTD requires a CPU physical-counter clock");
    }
}

static void ptd_init(Object *obj)
{
    TomaccoPtdDevice *s = TOMACCO_PTD(obj);
    memory_region_init_io(&s->mmio, obj, &ptd_ops, s, "ptd-t8140", 0x20000);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->mmio);
    for (unsigned i = 0; i < PPTD_IRQS; ++i) {
        sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irq[i]);
    }
}

static const Property ptd_properties[] = {
    DEFINE_PROP_LINK("cpu", TomaccoPtdDevice, cpu, TYPE_ARM_CPU, ARMCPU *),
    DEFINE_PROP_BOOL("irq-connected", TomaccoPtdDevice, irq_connected, false),
};
static const VMStateDescription ptd_vmstate = {
    .name = TYPE_TOMACCO_PTD, .unmigratable = true,
};
static void ptd_class(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->desc = "T8140 PTD message transport (no PMP controller firmware)";
    dc->realize = ptd_realize;
    dc->vmsd = &ptd_vmstate;
    device_class_set_props(dc, ptd_properties);
    device_class_set_legacy_reset(dc, ptd_reset);
}
static const TypeInfo ptd_types[] = {
    {.name = TYPE_TOMACCO_PTD, .parent = TYPE_SYS_BUS_DEVICE,
     .instance_size = sizeof(TomaccoPtdDevice), .instance_init = ptd_init,
     .class_init = ptd_class},
};
DEFINE_TYPES(ptd_types)

static void ptd_test_init(MachineState *machine)
{
    DeviceState *dev = qdev_new(TYPE_TOMACCO_PTD);
    CPUState *cpu = cpu_create(machine->cpu_type);
    memory_region_add_subregion(get_system_memory(), 0, machine->ram);
    object_property_add_child(OBJECT(machine), "ptd", OBJECT(dev));
    object_property_set_link(OBJECT(dev), "cpu", OBJECT(cpu), &error_fatal);
    /* QTest observes all 32 real output lines in this isolated test machine. */
    qdev_prop_set_bit(dev, "irq-connected", true);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(dev), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(dev), 0, 0x3003c0000);
}
static void ptd_test_class(MachineClass *mc)
{
    mc->desc = "Tomacco PTD register and ARM test rig (no OS or firmware)";
    mc->init = ptd_test_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("max");
    mc->default_ram_size = 64 * 1024 * 1024;
    mc->default_ram_id = "tomacco-ptd-test.ram";
    mc->no_serial = mc->no_parallel = mc->no_floppy = mc->no_cdrom = true;
}
DEFINE_MACHINE_WITH_INTERFACES("tomacco-ptd-test", ptd_test_class,
                               { TYPE_TARGET_AARCH64_MACHINE }, { })
