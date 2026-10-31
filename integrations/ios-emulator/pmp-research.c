/* SPDX-License-Identifier: GPL-2.0-or-later
 * Isolated ARM64 firmware execution, with optional experimental ASC/KIC.
 * Neither machine implements PMP power services. Unsupported hardware aborts.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/core/boards.h"
#include "hw/core/sysbus.h"
#include "hw/arm/machines-qom.h"
#include "target/arm/cpu.h"
#include "target/arm/cpregs.h"
#include "exec/cputlb.h"
#include "system/address-spaces.h"
#include "qemu/module.h"

static void pmp_tlbi_all(CPUARMState *env, const ARMCPRegInfo *ri,
                         uint64_t value)
{
    /* The verified PMP bootstrap executes this encoding from EL1 after
     * changing its page tables. Laboratory semantics: invalidate all local
     * translations, including EL0/EL1 and cached instruction translations.
     * This is an effective operation, not the upstream unknown-register NOP.
     * Its exact silicon scope remains unqualified; no other CPU is affected. */
    tlb_flush(env_cpu(env));
}

static const ARMCPRegInfo pmp_registers[] = {
    { .name = "PMP_TLBI_ALL", .state = ARM_CP_STATE_AA64,
      .opc0 = 1, .opc1 = 6, .crn = 8, .crm = 7, .opc2 = 0,
      .access = PL1_W, .type = ARM_CP_NO_RAW, .writefn = pmp_tlbi_all },
};

static bool pmp_has_tlbi_alias(Object *object, Error **errp)
{
    return true;
}

static void pmp_research_init(MachineState *machine)
{
    ARMCPU *cpu = ARM_CPU(object_new(machine->cpu_type));
    cpu->has_el2 = false;
    cpu->has_el3 = false;
    unset_feature(&cpu->env, ARM_FEATURE_EL2);
    unset_feature(&cpu->env, ARM_FEATURE_EL3);
    cpu->gt_cntfrq_hz = 24000000;
    cpu->tomacco_strict_sysregs = true;
    qdev_realize(DEVICE(cpu), NULL, &error_fatal);
    define_arm_cp_regs(cpu, pmp_registers);
    /* Explicit laboratory RAM policy, including the firmware's boot heap.
     * Not a measured SRAM/DRAM mapping of a physical T8140 controller. */
    memory_region_add_subregion(get_system_memory(), 0x1000000, machine->ram);
}

static void pmp_asc_research_init(MachineState *machine)
{
    pmp_research_init(machine);
    DeviceState *dev = qdev_new("tomacco-asc-controller");
    object_property_add_child(OBJECT(machine), "asc", OBJECT(dev));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(dev), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(dev), 0, 0x300e00000);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0,
                      qdev_get_gpio_in(DEVICE(first_cpu), ARM_CPU_IRQ));
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 1,
                      qdev_get_gpio_in(DEVICE(first_cpu), ARM_CPU_FIQ));
    qdev_connect_gpio_out(DEVICE(first_cpu), GTIMER_PHYS,
                         qdev_get_gpio_in_named(dev, "timer", 0));
    qdev_connect_gpio_out(DEVICE(first_cpu), GTIMER_VIRT,
                         qdev_get_gpio_in_named(dev, "timer", 1));
}

static void pmp_research_class(MachineClass *mc)
{
    object_class_property_add_bool(OBJECT_CLASS(mc), "pmp-tlbi-alias",
                                   pmp_has_tlbi_alias, NULL);
    mc->desc = "Tomacco PMP ARM64 firmware research (no ASC or power services)";
    mc->init = pmp_research_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("max");
    mc->default_ram_size = 2 * 1024 * 1024;
    mc->default_ram_id = "tomacco-pmp-research.ram";
    mc->no_serial = mc->no_parallel = mc->no_floppy = mc->no_cdrom = true;
}
DEFINE_MACHINE_WITH_INTERFACES("tomacco-pmp-research", pmp_research_class,
                               { TYPE_TARGET_AARCH64_MACHINE }, { })

static void pmp_asc_research_class(MachineClass *mc)
{
    pmp_research_class(mc);
    mc->desc = "Tomacco PMP with experimental ASC/KIC (no power services)";
    mc->init = pmp_asc_research_init;
}
DEFINE_MACHINE_WITH_INTERFACES("tomacco-pmp-asc-research", pmp_asc_research_class,
                               { TYPE_TARGET_AARCH64_MACHINE }, { })
