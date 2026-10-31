/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/core/sysbus.h"
#include "hw/core/boards.h"
#include "hw/core/irq.h"
#include "hw/arm/machines-qom.h"
#include "target/arm/cpu.h"
#include "system/address-spaces.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "trace.h"
#include "asc-mailbox.h"

#define TYPE_TOMACCO_ASC_MAILBOX "tomacco-asc-mailbox"
OBJECT_DECLARE_SIMPLE_TYPE(TomaccoAscMailboxDevice, TOMACCO_ASC_MAILBOX)

struct TomaccoAscMailboxDevice {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    TomaccoAscMailbox state;
    qemu_irq irq[PASC_IRQS];
    unsigned trace_count;
};

static void asc_update_irqs(TomaccoAscMailboxDevice *s)
{
    unsigned lines = pasc_irqs(&s->state);
    for (unsigned i = 0; i < PASC_IRQS; ++i) {
        qemu_set_irq(s->irq[i], (lines >> i) & 1);
    }
}

static void asc_trace(TomaccoAscMailboxDevice *s, bool write, hwaddr reg,
                      uint64_t value, unsigned size, bool supported)
{
    if (s->trace_count < 1024) {
        s->trace_count++;
        trace_tomacco_asc_mailbox_mmio(write, reg, value, size, supported);
    }
}

static MemTxResult asc_read(void *opaque, hwaddr reg, uint64_t *value,
                            unsigned size, MemTxAttrs attrs)
{
    TomaccoAscMailboxDevice *s = opaque;
    uint64_t result = 0;
    bool supported = pasc_read(&s->state, reg, size, &result);
    *value = result;
    asc_update_irqs(s);
    asc_trace(s, false, reg, result, size, supported);
    return supported ? MEMTX_OK : MEMTX_DECODE_ERROR;
}

static MemTxResult asc_write(void *opaque, hwaddr reg, uint64_t value,
                             unsigned size, MemTxAttrs attrs)
{
    TomaccoAscMailboxDevice *s = opaque;
    bool supported = pasc_write(&s->state, reg, size, value);
    asc_update_irqs(s);
    asc_trace(s, true, reg, value, size, supported);
    return supported ? MEMTX_OK : MEMTX_DECODE_ERROR;
}

static const MemoryRegionOps asc_ops = {
    .read_with_attrs = asc_read, .write_with_attrs = asc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
    .impl = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
};

static void asc_reset(DeviceState *dev)
{
    TomaccoAscMailboxDevice *s = TOMACCO_ASC_MAILBOX(dev);
    pasc_reset(&s->state);
    s->trace_count = 0;
    asc_update_irqs(s);
}

static void asc_init(Object *obj)
{
    TomaccoAscMailboxDevice *s = TOMACCO_ASC_MAILBOX(obj);
    memory_region_init_io(&s->mmio, obj, &asc_ops, s, "asc-mailbox", 0x4000);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->mmio);
    for (unsigned i = 0; i < PASC_IRQS; ++i) {
        sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irq[i]);
    }
}

static const VMStateDescription asc_vmstate = {
    .name = TYPE_TOMACCO_ASC_MAILBOX, .unmigratable = true,
};

static void asc_class(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->desc = "ASC bidirectional message transport (no controller firmware)";
    dc->vmsd = &asc_vmstate;
    device_class_set_legacy_reset(dc, asc_reset);
}

static const TypeInfo asc_types[] = {
    {.name = TYPE_TOMACCO_ASC_MAILBOX, .parent = TYPE_SYS_BUS_DEVICE,
     .instance_size = sizeof(TomaccoAscMailboxDevice), .instance_init = asc_init,
     .class_init = asc_class},
};
DEFINE_TYPES(asc_types)

static void asc_test_init(MachineState *machine)
{
    ARMCPU *cpu = ARM_CPU(object_new(machine->cpu_type));
    cpu->has_el2 = false;
    cpu->has_el3 = false;
    unset_feature(&cpu->env, ARM_FEATURE_EL2);
    unset_feature(&cpu->env, ARM_FEATURE_EL3);
    qdev_realize(DEVICE(cpu), NULL, &error_fatal);
    memory_region_add_subregion(get_system_memory(), 0, machine->ram);
    DeviceState *dev = qdev_new(TYPE_TOMACCO_ASC_MAILBOX);
    object_property_add_child(OBJECT(machine), "asc", OBJECT(dev));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(dev), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(dev), 0, 0x300e08000);
    /* A test-only IOP register alias. No T8140 interrupt-controller wiring
     * is asserted: the rig connects one real FIFO level directly to IRQ. */
    MemoryRegion *alias = g_new0(MemoryRegion, 1);
    memory_region_init_alias(alias, OBJECT(machine), "asc-test-iop-view",
                             sysbus_mmio_get_region(SYS_BUS_DEVICE(dev), 0),
                             0, 0x4000);
    memory_region_add_subregion(get_system_memory(), 0x300e04000, alias);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 3,
                       qdev_get_gpio_in(DEVICE(cpu), ARM_CPU_IRQ));
}

static void asc_test_class(MachineClass *mc)
{
    mc->desc = "Tomacco ASC queue, interrupt and ARM test rig (no OS)";
    mc->init = asc_test_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("max");
    mc->default_ram_size = 64 * 1024 * 1024;
    mc->default_ram_id = "tomacco-asc-mailbox-test.ram";
    mc->no_serial = mc->no_parallel = mc->no_floppy = mc->no_cdrom = true;
}
DEFINE_MACHINE_WITH_INTERFACES("tomacco-asc-mailbox-test", asc_test_class,
                               { TYPE_TARGET_AARCH64_MACHINE }, { })
