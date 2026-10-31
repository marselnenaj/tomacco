/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "qobject/qdict.h"
#include "qobject/qlist.h"
#include "qobject/qjson.h"
#include "trace.h"
#include "asc-controller.h"

#define TYPE_TOMACCO_ASC_CONTROLLER "tomacco-asc-controller"
OBJECT_DECLARE_SIMPLE_TYPE(TomaccoAscControllerDevice, TOMACCO_ASC_CONTROLLER)

struct TomaccoAscControllerDevice {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    TomaccoAscController state;
    qemu_irq irq[4]; /* IOP IRQ/FIQ, diagnostic AP IRQ/FIQ */
    unsigned trace_count;
    uint64_t reads, writes, rejected, irq_transitions[4];
    bool irq_level[4];
};

static void controller_irqs(TomaccoAscControllerDevice *s)
{
    for (unsigned i = 0; i < 4; i++) {
        bool level = pascc_event(&s->state, i / 2, i % 2, false) != 0;
        if (level != s->irq_level[i]) {
            s->irq_transitions[i]++;
            s->irq_level[i] = level;
        }
        qemu_set_irq(s->irq[i], level);
    }
}

static void controller_input(void *opaque, int source, int level)
{
    TomaccoAscControllerDevice *s = opaque;
    pascc_level(&s->state, source, level);
    controller_irqs(s);
}

static void controller_timer_input(void *opaque, int timer, int level)
{
    TomaccoAscControllerDevice *s = opaque;
    pascc_timer_level(&s->state, timer, level);
    controller_irqs(s);
}

static void controller_trace(TomaccoAscControllerDevice *s, bool write, hwaddr reg,
                             uint64_t value, unsigned size, bool supported)
{
    if (s->trace_count < 2048) {
        s->trace_count++;
        trace_tomacco_asc_controller_mmio(write, reg, value, size, supported);
    }
}

static MemTxResult controller_read(void *opaque, hwaddr reg, uint64_t *value,
                                   unsigned size, MemTxAttrs attrs)
{
    TomaccoAscControllerDevice *s = opaque;
    uint64_t result = 0;
    bool supported = pascc_read(&s->state, reg, size, &result);
    s->reads++;
    s->rejected += !supported;
    *value = result;
    controller_irqs(s);
    controller_trace(s, false, reg, result, size, supported);
    return supported ? MEMTX_OK : MEMTX_DECODE_ERROR;
}

static MemTxResult controller_write(void *opaque, hwaddr reg, uint64_t value,
                                    unsigned size, MemTxAttrs attrs)
{
    TomaccoAscControllerDevice *s = opaque;
    bool supported = pascc_write(&s->state, reg, size, value);
    s->writes++;
    s->rejected += !supported;
    controller_irqs(s);
    controller_trace(s, true, reg, value, size, supported);
    return supported ? MEMTX_OK : MEMTX_DECODE_ERROR;
}

static const MemoryRegionOps controller_ops = {
    .read_with_attrs = controller_read, .write_with_attrs = controller_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
    .impl = {.min_access_size = 1, .max_access_size = 8, .unaligned = true},
};

static void controller_reset(DeviceState *dev)
{
    TomaccoAscControllerDevice *s = TOMACCO_ASC_CONTROLLER(dev);
    pascc_reset(&s->state);
    s->trace_count = 0;
    s->reads = s->writes = s->rejected = 0;
    memset(s->irq_transitions, 0, sizeof(s->irq_transitions));
    memset(s->irq_level, 0, sizeof(s->irq_level));
    controller_irqs(s);
}

static char *controller_snapshot(Object *obj, Error **errp)
{
    TomaccoAscControllerDevice *dev = TOMACCO_ASC_CONTROLLER(obj);
    TomaccoAscController *s = &dev->state;
    QDict *result = qdict_new();
    qdict_put_int(result, "revision", 1);
    qdict_put_int(result, "configuration", s->config);
    qdict_put_int(result, "firmware_control", s->firmware_control);
    qdict_put_int(result, "global_configuration", s->global_config);
    qdict_put_bool(result, "interrupt_reset", s->interrupt_reset);
    qdict_put_bool(result, "timer_enable", s->timer_enable);
    qdict_put_int(result, "reads", dev->reads);
    qdict_put_int(result, "writes", dev->writes);
    qdict_put_int(result, "rejected_accesses", dev->rejected);
    qdict_put_int(result, "traced_accesses", dev->trace_count);
    qdict_put_bool(result, "trace_budget_exhausted", dev->trace_count == 2048);
    QList *queues = qlist_new(), *timers = qlist_new(), *outputs = qlist_new();
    QList *banks = qlist_new();
    for (unsigned i = 0; i < PASC_KIC_WORDS; i++) {
        QDict *bank = qdict_new();
        qdict_put_int(bank, "first_source", i * 32);
        qdict_put_int(bank, "input_levels", s->level[i]);
        qdict_put_int(bank, "software_pending", s->software[i]);
        qdict_put_int(bank, "irq_mask", s->masked[0][i]);
        qdict_put_int(bank, "fiq_mask", s->masked[1][i]);
        qlist_append(banks, bank);
    }
    for (unsigned i = 0; i < 2; i++) {
        TomaccoAscFifo *q = &s->mailbox.queue[i];
        QDict *queue = qdict_new(), *timer = qdict_new();
        qdict_put_str(queue, "direction", i ? "I2A" : "A2I");
        qdict_put_bool(queue, "enabled", s->queue_enabled[i]);
        qdict_put_int(queue, "count", q->count);
        qdict_put_int(queue, "read_pointer", q->read_pointer);
        qdict_put_int(queue, "write_pointer", q->write_pointer);
        qdict_put_bool(queue, "staged", q->staged_valid);
        qlist_append(queues, queue);
        qdict_put_str(timer, "source", i ? "ARM virtual timer" : "ARM physical timer");
        qdict_put_int(timer, "configuration", s->timer_config[i]);
        qdict_put_int(timer, "state", s->timer_state[i]);
        qdict_put_bool(timer, "input_asserted", s->timer_level[i]);
        qlist_append(timers, timer);
    }
    for (unsigned i = 0; i < 4; i++) {
        QDict *output = qdict_new();
        qdict_put_str(output, "side", i / 2 ? "AP diagnostic" : "IOP");
        qdict_put_str(output, "kind", i % 2 ? "FIQ" : "IRQ");
        qdict_put_bool(output, "asserted", dev->irq_level[i]);
        qdict_put_int(output, "next_event", pascc_event(s, i / 2, i % 2, false));
        qdict_put_int(output, "transitions", dev->irq_transitions[i]);
        qdict_put_int(output, "mailbox_mask", s->mailbox_mask[i / 2][i % 2]);
        qlist_append(outputs, output);
    }
    qdict_put(result, "queues", queues);
    qdict_put(result, "timers", timers);
    qdict_put(result, "outputs", outputs);
    qdict_put(result, "external_banks", banks);
    GString *json = qobject_to_json(QOBJECT(result));
    qobject_unref(result);
    return g_string_free(json, false);
}

static void controller_init(Object *obj)
{
    TomaccoAscControllerDevice *s = TOMACCO_ASC_CONTROLLER(obj);
    memory_region_init_io(&s->mmio, obj, &controller_ops, s, "asc-controller", 0xc000);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->mmio);
    qdev_init_gpio_in(DEVICE(s), controller_input, PASC_KIC_WORDS * 32);
    qdev_init_gpio_in_named(DEVICE(s), controller_timer_input, "timer", 2);
    object_property_add_str(obj, "controller-state", controller_snapshot, NULL);
    for (unsigned i = 0; i < 4; i++) {
        sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irq[i]);
    }
}

static const VMStateDescription controller_vmstate = {
    .name = TYPE_TOMACCO_ASC_CONTROLLER, .unmigratable = true,
};

static void controller_class(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->desc = "Experimental ASC configuration, KIC and channel-zero transport";
    dc->vmsd = &controller_vmstate;
    device_class_set_legacy_reset(dc, controller_reset);
}

static const TypeInfo controller_types[] = {
    {.name = TYPE_TOMACCO_ASC_CONTROLLER, .parent = TYPE_SYS_BUS_DEVICE,
     .instance_size = sizeof(TomaccoAscControllerDevice), .instance_init = controller_init,
     .class_init = controller_class},
};
DEFINE_TYPES(controller_types)
