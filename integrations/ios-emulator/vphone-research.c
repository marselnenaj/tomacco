/*
 * Tomacco's local vphone600 bring-up machine.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * This is a firmware research machine, not an iOS application runtime.
 * Addresses and interrupt IDs follow the vphone600 Apple device tree.
 * Only implemented devices are mapped. Optional SEP firmware runs on its
 * own CPU; there are no synthesized SEP service responses.
 */
#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qapi/error.h"
#include "qobject/qlist.h"
#include "qemu/error-report.h"
#include "qemu/guest-random.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qemu/units.h"
#include "hw/arm/boot.h"
#include "hw/arm/bsa.h"
#include "hw/arm/machines-qom.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "hw/intc/arm_gic_common.h"
#include "hw/intc/arm_gicv3_common.h"
#include "hw/char/pl011.h"
#include "hw/pci-host/gpex.h"
#include "hw/misc/pvpanic.h"
#include "hw/vmapple/vmapple.h"
#include "system/address-spaces.h"
#include "system/block-backend.h"
#include "block/block.h"
#include "system/reset.h"
#include "system/rtc.h"
#include "system/runstate.h"
#include "system/system.h"
#include "xnu/apple_dtree.h"
#include "xnu/apple_regs.h"
#include "xnu/boot/xnuboot.h"
#include "xnu/macho.h"
#include "cpu.h"
#include "vphone-sep.h"
#include "vphone-asmf.h"
#include "vphone-vstr.h"

#define TYPE_TOMACCO_VPHONE MACHINE_TYPE_NAME("tomacco-vphone")
OBJECT_DECLARE_SIMPLE_TYPE(TomaccoVPhone, TOMACCO_VPHONE)

#define VPHONE_DRAM_BASE 0x70000000ULL
#define VPHONE_IRQS 256
#define VPHONE_MAX_AP_CPUS 16
#define VPHONE_TOTAL_CPUS(ap_cpus, sep) ((ap_cpus) + !!(sep))
/* Pinned PCC26.4 AppleVirtualPlatformButtons arms PL061 bit6 and emits
 * Consumer Power (0x0c/0x30). The macOS VMApple pin3 binding is different. */
#define VPHONE_POWER_GPIO_PIN 6

struct TomaccoVPhone {
    MachineState parent;
    ARMCPU *cpus[VPHONE_MAX_AP_CPUS];
    unsigned ap_cpus;
    char *sep;
    char *sep_manifest;
    bool sep_measure_manifest;
    char *sep_role;
    char *sep_handoff;
    struct xnu_boot_info boot;
    MemoryRegion host_request;
    MemoryRegion rtc_regs;
    MemoryRegion pcie_ecam;
    MemoryRegion pcie_mmio;
    uint64_t rtc_control;
    uint64_t rtc_mask;
    int64_t rtc_epoch_us;
    int64_t rtc_start_us;
    DeviceState *power_key;
    Notifier powerdown;
};

/* AVP RTC register accesses from AppleVirtualPlatformRTC in 23E5207q:
 * counter 0x00 (microseconds), control 0x18, interrupt mask 0x20,
 * status 0x30, write-one-to-clear 0x38. The guest maintains its own time
 * correction in NVRAM; no guest write changes the host's wall clock.
 *
 * This board supplies a fixed-rate VM clock, initialized from QEMU's RTC
 * base. It never injects host time/rate-change events. Unsupported register
 * accesses fault instead of silently inventing another clock protocol.
 */
static MemTxResult vphone_rtc_read(void *opaque, hwaddr offset,
                                 uint64_t *value, unsigned size,
                                 MemTxAttrs attrs)
{
    TomaccoVPhone *s = opaque;

    switch (offset) {
    case 0x00:
        *value = s->rtc_epoch_us +
                 qemu_clock_get_us(QEMU_CLOCK_VIRTUAL) - s->rtc_start_us;
        return MEMTX_OK;
    case 0x18:
        *value = s->rtc_control;
        return MEMTX_OK;
    case 0x20:
        *value = s->rtc_mask;
        return MEMTX_OK;
    case 0x30:
        *value = 0; /* No host-initiated time/rate discontinuities. */
        return MEMTX_OK;
    default:
        qemu_log_mask(LOG_UNIMP, "tomacco-vphone RTC: read offset=0x%"
                      HWADDR_PRIx " size=%u\n", offset, size);
        return MEMTX_DECODE_ERROR;
    }
}

static MemTxResult vphone_rtc_write(void *opaque, hwaddr offset,
                                  uint64_t value, unsigned size,
                                  MemTxAttrs attrs)
{
    TomaccoVPhone *s = opaque;

    switch (offset) {
    case 0x18:
        s->rtc_control = value & 1;
        return MEMTX_OK;
    case 0x20:
        s->rtc_mask = value & 6;
        return MEMTX_OK;
    case 0x38:
        return MEMTX_OK; /* W1C of an empty event latch. */
    default:
        qemu_log_mask(LOG_UNIMP, "tomacco-vphone RTC: write offset=0x%"
                      HWADDR_PRIx " size=%u\n", offset, size);
        return MEMTX_DECODE_ERROR;
    }
}

static const MemoryRegionOps vphone_rtc_ops = {
    .read_with_attrs = vphone_rtc_read,
    .write_with_attrs = vphone_rtc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 8, .max_access_size = 8 },
    .impl = { .min_access_size = 8, .max_access_size = 8 },
};

/* AppleVirtualPlatform's synchronous host-property transport, observed in
 * kernelcache.research.vphone600 (23E5207q), at unslid 0xfffffe0008102a58.
 *
 * A 64-bit write to 0x4ffff8 submits a 24-byte, little-endian descriptor:
 *   u16 version, u8 operation, u8 completed, u32 property,
 *   u64 buffer/result_errno, u64 capacity/result_length.
 * The guest checks version == 1 and completed != 0 after the MMIO store.
 * It retries ENOMEM (12) with result_length bytes. Property 1 is _hostKey
 * (AppleVirtualPlatformARMPE, 0xfffffe00080f2f38).
 *
 * This machine has no provisioned AVP Identity service. Report a missing
 * host key, never invent one or advertise successful host authentication.
 * Guest errno values are Darwin ABI constants, not the host's errno.h.
 */
static bool vphone_ram_range(TomaccoVPhone *s, uint64_t address, uint64_t size)
{
    return address >= VPHONE_DRAM_BASE && size <= MACHINE(s)->ram_size &&
           address - VPHONE_DRAM_BASE <= MACHINE(s)->ram_size - size;
}

static MemTxResult host_request_write(void *opaque, hwaddr offset,
                                     uint64_t address, unsigned size,
                                     MemTxAttrs attrs)
{
    TomaccoVPhone *s = opaque;
    uint8_t request[24];
    uint64_t result;
    uint16_t version;
    uint32_t property;
    MemTxResult tx;

    if (offset || size != 8 ||
        !vphone_ram_range(s, address, sizeof(request))) {
        qemu_log_mask(LOG_GUEST_ERROR,
                      "tomacco-vphone: invalid host descriptor 0x%" PRIx64 "\n",
                      address);
        return MEMTX_DECODE_ERROR;
    }
    tx = address_space_read(&address_space_memory, address, attrs,
                            request, sizeof(request));
    if (tx != MEMTX_OK) {
        return tx;
    }
    version = lduw_le_p(request);
    property = ldl_le_p(request + 4);
    if (version != 1 || request[2] != 1) {
        result = 45; /* ENOTSUP: unsupported version or operation */
    } else if (request[3]) {
        result = 22; /* EINVAL: a completion is not a fresh request */
    } else if (!vphone_ram_range(s, ldq_le_p(request + 8),
                                ldq_le_p(request + 16))) {
        result = 14; /* EFAULT: never let DMA target another MMIO device */
    } else {
        result = property == 1 ? 2 : 45; /* ENOENT / ENOTSUP */
    }
    stq_le_p(request + 8, result);
    stq_le_p(request + 16, 0);
    /* Publish the response before its completion byte. */
    tx = address_space_write(&address_space_memory, address + 8, attrs,
                             request + 8, 16);
    if (tx != MEMTX_OK) {
        return tx;
    }
    request[3] = 1;
    tx = address_space_write(&address_space_memory, address + 3, attrs,
                             request + 3, 1);
    qemu_log_mask(LOG_UNIMP,
                  "tomacco-vphone host request: version=%u operation=%u "
                  "property=%u result=%" PRIu64 "\n",
                  version, request[2], property, result);
    return tx;
}

static const MemoryRegionOps host_request_ops = {
    .write_with_attrs = host_request_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 8, .max_access_size = 8 },
    .impl = { .min_access_size = 8, .max_access_size = 8 },
};

#define BOOT_PROPERTY(field) \
static char *get_##field(Object *obj, Error **errp) \
{ \
    return g_strdup(TOMACCO_VPHONE(obj)->boot.field); \
} \
static void set_##field(Object *obj, const char *value, Error **errp) \
{ \
    g_free(TOMACCO_VPHONE(obj)->boot.field); \
    TOMACCO_VPHONE(obj)->boot.field = g_strdup(value); \
}

BOOT_PROPERTY(bootkc)
BOOT_PROPERTY(dtree)
BOOT_PROPERTY(sptm)
BOOT_PROPERTY(txm)
BOOT_PROPERTY(tc)
BOOT_PROPERTY(ramdisk)
BOOT_PROPERTY(args)

/*
 * Replace the boot's trust-cache module in guest RAM with the file named, while
 * every CPU is stopped: the host's way to trust one more program on a running
 * guest. The module keeps its size, so its entry count and what the monitors
 * derived from its location stay right; the host rewrites spare entries the
 * boot's module reserved for this. Whether the guest looks again is the guest's
 * matter, and a file of another size or a running guest is refused.
 */
static void set_trust_cache_update(Object *obj, const char *value, Error **errp)
{
    TomaccoVPhone *machine = TOMACCO_VPHONE(obj);
    g_autofree char *contents = NULL;
    g_autoptr(GError) error = NULL;
    gsize length = 0;

    if (!machine->boot.tc_module_phys) {
        error_setg(errp, "This guest has no trust-cache module");
        return;
    }
    if (runstate_is_running()) {
        error_setg(errp, "The trust cache is rewritten only while the guest is stopped");
        return;
    }
    if (!g_file_get_contents(value, &contents, &length, &error)) {
        error_setg(errp, "%s", error->message);
        return;
    }
    if (length != machine->boot.tc_module_bytes) {
        error_setg(errp, "A trust-cache module of %" PRIu64 " bytes is expected, not %zu",
                   machine->boot.tc_module_bytes, length);
        return;
    }
    if (address_space_write(&address_space_memory, machine->boot.tc_module_phys,
                            MEMTXATTRS_UNSPECIFIED, contents, length) != MEMTX_OK) {
        error_setg(errp, "The trust-cache module could not be written");
    }
}

static char *get_sep(Object *obj, Error **errp)
{
    return g_strdup(TOMACCO_VPHONE(obj)->sep);
}

static void set_sep(Object *obj, const char *value, Error **errp)
{
    g_free(TOMACCO_VPHONE(obj)->sep);
    TOMACCO_VPHONE(obj)->sep = g_strdup(value);
}

static char *get_sep_manifest(Object *obj, Error **errp)
{
    return g_strdup(TOMACCO_VPHONE(obj)->sep_manifest);
}

static void set_sep_manifest(Object *obj, const char *value, Error **errp)
{
    g_free(TOMACCO_VPHONE(obj)->sep_manifest);
    TOMACCO_VPHONE(obj)->sep_manifest = g_strdup(value);
}

static bool get_sep_measure_manifest(Object *obj, Error **errp)
{
    return TOMACCO_VPHONE(obj)->sep_measure_manifest;
}

static void set_sep_measure_manifest(Object *obj, bool value, Error **errp)
{
    TOMACCO_VPHONE(obj)->sep_measure_manifest = value;
}

static char *get_sep_role(Object *obj, Error **errp)
{
    const char *role = TOMACCO_VPHONE(obj)->sep_role;
    return g_strdup(role ? role : "normal");
}

static void set_sep_role(Object *obj, const char *value, Error **errp)
{
    if (strcmp(value, "normal") && strcmp(value, "restore")) {
        error_setg(errp, "sep-role must be normal or restore");
        return;
    }
    g_free(TOMACCO_VPHONE(obj)->sep_role);
    TOMACCO_VPHONE(obj)->sep_role = g_strdup(value);
}

static char *get_sep_handoff(Object *obj, Error **errp)
{
    const char *mode = TOMACCO_VPHONE(obj)->sep_handoff;
    return g_strdup(mode ? mode : "preloaded");
}

static void set_sep_handoff(Object *obj, const char *value, Error **errp)
{
    if (strcmp(value, "preloaded") && strcmp(value, "userspace")) {
        error_setg(errp, "SEP handoff must be preloaded or userspace");
        return;
    }
    g_free(TOMACCO_VPHONE(obj)->sep_handoff);
    TOMACCO_VPHONE(obj)->sep_handoff = g_strdup(value);
}

static mmap_file_t map_input(const char *path, const char *kind)
{
    struct stat st;
    void *p;
    int fd = path ? open(path, O_RDONLY | O_CLOEXEC) : -1;

    if (fd < 0) {
        error_report("tomacco-vphone: cannot open %s: %s", kind,
                     path ? strerror(errno) : "missing machine property");
        exit(EXIT_FAILURE);
    }
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size <= 0) {
        error_report("tomacco-vphone: %s must be a nonempty regular file", kind);
        exit(EXIT_FAILURE);
    }
    p = mmap(NULL, st.st_size, PROT_READ | PROT_WRITE, MAP_PRIVATE, fd, 0);
    close(fd);
    if (p == MAP_FAILED) {
        error_report("tomacco-vphone: cannot map %s: %s", kind, strerror(errno));
        exit(EXIT_FAILURE);
    }
    if (is_im4p(p)) {
        error_report("tomacco-vphone: unwrap the %s IM4P before booting", kind);
        exit(EXIT_FAILURE);
    }
    return (mmap_file_t) { p, st.st_size, st.st_dev, st.st_ino };
}

static void vphone_reset(void *opaque)
{
    TomaccoVPhone *s = opaque;
    CPUARMState *env = &s->cpus[0]->env;
    struct tm tm;

    qemu_get_timedate(&tm, 0);
    s->rtc_epoch_us = (int64_t)mktimegm(&tm) * 1000000;
    s->rtc_start_us = qemu_clock_get_us(QEMU_CLOCK_VIRTUAL);
    s->rtc_control = 0;
    s->rtc_mask = 0;

    for (unsigned i = 0; i < s->ap_cpus; i++) {
        cpu_reset(CPU(s->cpus[i]));
    }
    /* vresearch1 SPTM's cold entry programs TTBR/TCR/SCTLR_EL1, unlike
     * retail A18 SPTM's EL2 entry. It subsequently installs its GL1 vectors. */
    pstate_write(env, PSTATE_MODE_EL1h | PSTATE_DAIF);
    env->xregs[0] = s->boot.init_x0;
    env->pc = s->boot.init_pc;
    env->cp15.cpacr_el1 |= 3ULL << 20;
    arm_rebuild_hflags(env);
}

DeviceState *vphone_create_interrupts(ARMCPU **cpus, unsigned int cpu_count,
                                      MemoryRegion *memory, Object *owner)
{
    DeviceState *dev = qdev_new(gicv3_class_name());
    SysBusDevice *gic = SYS_BUS_DEVICE(dev);
    QList *regions = qlist_new();
    const int timers[] = {
        [GTIMER_PHYS] = ARCH_TIMER_NS_EL1_IRQ,
        [GTIMER_VIRT] = ARCH_TIMER_VIRT_IRQ,
        [GTIMER_HYP] = ARCH_TIMER_NS_EL2_IRQ,
        [GTIMER_SEC] = ARCH_TIMER_S_EL1_IRQ,
        [GTIMER_HYPVIRT] = ARCH_TIMER_NS_EL2_VIRT_IRQ,
    };
    const int outputs[] = { ARM_CPU_IRQ, ARM_CPU_FIQ, ARM_CPU_VIRQ,
                            ARM_CPU_VFIQ, ARM_CPU_NMI, ARM_CPU_VINMI };

    object_property_add_child(owner, "interrupts", OBJECT(dev));
    qdev_prop_set_uint32(dev, "revision", 3);
    assert(cpu_count > 0);
    qdev_prop_set_uint32(dev, "first-cpu-index", CPU(cpus[0])->cpu_index);
    qdev_prop_set_uint32(dev, "num-cpu", cpu_count);
    qdev_prop_set_uint32(dev, "num-irq", VPHONE_IRQS + GIC_INTERNAL);
    qlist_append_int(regions, cpu_count);
    qdev_prop_set_array(dev, "redist-region-count", regions);
    object_property_set_link(OBJECT(dev), "sysmem", OBJECT(memory),
                              &error_fatal);
    sysbus_realize_and_unref(gic, &error_fatal);
    memory_region_add_subregion(memory, 0x10000000,
                                 sysbus_mmio_get_region(gic, 0));
    memory_region_add_subregion(memory, 0x10010000,
                                 sysbus_mmio_get_region(gic, 1));
    for (unsigned cpu_index = 0; cpu_index < cpu_count; cpu_index++) {
        DeviceState *cpudev = DEVICE(cpus[cpu_index]);
        unsigned int ppi_base = VPHONE_IRQS + cpu_index * GIC_INTERNAL;
        for (unsigned i = 0; i < G_N_ELEMENTS(timers); i++) {
            qdev_connect_gpio_out(cpudev, i,
                                  qdev_get_gpio_in(dev, ppi_base + timers[i]));
        }
        qdev_connect_gpio_out_named(cpudev, "gicv3-maintenance-interrupt", 0,
            qdev_get_gpio_in(dev, ppi_base + ARCH_GIC_MAINT_IRQ));
        for (unsigned i = 0; i < G_N_ELEMENTS(outputs); i++) {
            sysbus_connect_irq(gic, i * cpu_count + cpu_index,
                               qdev_get_gpio_in(cpudev, outputs[i]));
        }
    }
    return dev;
}

static void create_pcie(TomaccoVPhone *s, DeviceState *gic)
{
    DeviceState *msi = qdev_new("arm-gicv2m");
    DeviceState *bridge = qdev_new(TYPE_GPEX_HOST);

    object_property_add_child(OBJECT(s), "msi", OBJECT(msi));
    qdev_prop_set_uint32(msi, "base-spi", 128);
    qdev_prop_set_uint32(msi, "num-spi", 128);
    qdev_prop_set_uint32(msi, "macos-compat", 1);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(msi), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(msi), 0, 0x1fff0000);
    for (unsigned i = 0; i < 128; i++) {
        sysbus_connect_irq(SYS_BUS_DEVICE(msi), i,
                           qdev_get_gpio_in(gic, 128 + i));
    }

    object_property_add_child(OBJECT(s), "pcie", OBJECT(bridge));
    qdev_prop_set_uint8(bridge, "num-irqs", 16);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(bridge), &error_fatal);
    memory_region_init_alias(&s->pcie_ecam, OBJECT(s), "vphone-pcie-ecam",
        sysbus_mmio_get_region(SYS_BUS_DEVICE(bridge), 0), 0, 0x10000000);
    memory_region_add_subregion(get_system_memory(), 0x40000000, &s->pcie_ecam);
    /* End below DRAM (0x70000000). prepare_ios_paravirt.py also narrows
     * the advertised aperture so PCI BAR allocation cannot overlap RAM. */
    memory_region_init_alias(&s->pcie_mmio, OBJECT(s), "vphone-pcie-mmio",
        sysbus_mmio_get_region(SYS_BUS_DEVICE(bridge), 1), 0x50000000, 0x1fff0000);
    memory_region_add_subregion(get_system_memory(), 0x50000000, &s->pcie_mmio);
    for (unsigned i = 0; i < 16; i++) {
        sysbus_connect_irq(SYS_BUS_DEVICE(bridge), i,
                           qdev_get_gpio_in(gic, 32 + i));
        gpex_set_irq_num(GPEX_HOST(bridge), i, 64 + i);
    }
}

static void vphone_powerdown(Notifier *notifier, void *data)
{
    TomaccoVPhone *s = container_of(notifier, TomaccoVPhone, powerdown);

    qemu_set_irq(qdev_get_gpio_in(s->power_key, 0), 1);
}

static void create_buttons(TomaccoVPhone *s, DeviceState *gic)
{
    DeviceState *gpio = qdev_new("pl061");
    DeviceState *panic = qdev_new(TYPE_PVPANIC_MMIO_DEVICE);

    object_property_add_child(OBJECT(s), "buttons", OBJECT(gpio));
    qdev_prop_set_uint32(gpio, "pullups", 0);
    qdev_prop_set_uint32(gpio, "pulldowns", 0xff);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(gpio), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(gpio), 0, 0x20060000);
    sysbus_connect_irq(SYS_BUS_DEVICE(gpio), 0, qdev_get_gpio_in(gic, 37 - 32));
    s->power_key = qdev_new("gpio-key");
    object_property_add_child(OBJECT(s), "power-key", OBJECT(s->power_key));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(s->power_key), &error_fatal);
    sysbus_connect_irq(SYS_BUS_DEVICE(s->power_key), 0,
                       qdev_get_gpio_in(gpio, VPHONE_POWER_GPIO_PIN));
    s->powerdown.notify = vphone_powerdown;
    qemu_register_powerdown_notifier(&s->powerdown);

    object_property_add_child(OBJECT(s), "panic", OBJECT(panic));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(panic), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(panic), 0, 0x20070000);
}

/* Validate the complete AP topology before creating any CPU objects. */
static bool vphone_validate_ap_topology(struct dtree_node *root,
                                       unsigned ap_cpus)
{
    struct dtree_node *cpus;

    if (ap_cpus < 1 || ap_cpus > VPHONE_MAX_AP_CPUS) {
        error_report("tomacco-vphone: prepared AP CPU count must be between 1 and 16");
        return false;
    }
    cpus = root ? adt_find_node(root, "cpus") : NULL;
    if (!cpus || cpus->nChildren != ap_cpus) {
        error_report("tomacco-vphone: prepared tree CPU nodes must match -smp exactly");
        return false;
    }
    for (unsigned i = 0; i < ap_cpus; i++) {
        char cpu_path[sizeof("cpus/cpu4294967295")];
        struct dtree_node *cpu_node;
        const void *reg;

        snprintf(cpu_path, sizeof(cpu_path), "cpus/cpu%u", i);
        cpu_node = adt_find_node(root, cpu_path);
        reg = cpu_node ? adt_get_prop_val(cpu_node, "reg") : NULL;
        /* adt_get_prop_len requires the property to exist. */
        if (!reg || adt_get_prop_len(cpu_node, "reg") != sizeof(uint32_t)) {
            error_report("tomacco-vphone: prepared tree is missing an enabled AP CPU affinity ID");
            return false;
        }
        if (ldl_le_p(reg) != i) {
            error_report("tomacco-vphone: AP CPU affinity IDs must be contiguous from zero");
            return false;
        }
    }
    return true;
}

static void vphone_init(MachineState *ms)
{
    TomaccoVPhone *s = TOMACCO_VPHONE(ms);
    struct xnu_boot_info *info = &s->boot;
    struct dtree_node *root, *chosen, *sep_node;
    const char *firmware;
    DeviceState *gic, *uart, *gpu, *aes, *config, *storage, *asmf;
    ARMCPU *cpu;
    uint32_t random;
    uint64_t ecid;
    unsigned ap_cpus = ms->smp.cpus;
    g_autofree char *serial = NULL;
    g_autofree uint8_t *sep_image = NULL;
    bool userspace_sep = s->sep_handoff && !strcmp(s->sep_handoff, "userspace");

    if (s->sep_manifest && !s->sep) {
        error_report("tomacco-vphone: sep-manifest requires sep firmware");
        exit(EXIT_FAILURE);
    }
    if (s->sep_measure_manifest && !s->sep_manifest) {
        error_report("tomacco-vphone: sep-measure-manifest requires sep-manifest");
        exit(EXIT_FAILURE);
    }
    if ((blk_by_name("tomacco-sep-storage") || s->sep_role || s->sep_handoff) && !s->sep) {
        error_report("tomacco-vphone: SEP storage/role/handoff requires SEP firmware");
        exit(EXIT_FAILURE);
    }
    if (ap_cpus < 1 || ap_cpus > VPHONE_MAX_AP_CPUS ||
        ms->smp.max_cpus != VPHONE_TOTAL_CPUS(ap_cpus, s->sep)) {
        error_report("tomacco-vphone: use 1–16 AP cores; with SEP reserve one extra -smp slot");
        exit(EXIT_FAILURE);
    }
    s->ap_cpus = ap_cpus;
    info->bootkc_f = map_input(info->bootkc, "bootkc");
    info->dtree_f = map_input(info->dtree, "dtree");
    info->sptm_f = map_input(info->sptm, "sptm");
    info->txm_f = map_input(info->txm, "txm");
    info->tc_f = map_input(info->tc, "tc");
    info->ramdisk_f = map_input(info->ramdisk, "ramdisk");
    if (s->sep) {
        /* One bounded read supplies both processors with identical firmware. */
        sep_image = vphone_sep_read_image(s->sep, &info->sep_image_size,
                                          &error_fatal);
        /* Only preload mode gives the AP a bootloader firmware object. The
         * userspace mode waits for the original seputil IMG4 load. Both modes
         * execute the explicit, unchanged ELFour image in private SEP RAM. */
        info->sep_image = userspace_sep ? NULL : sep_image;
    }
    if (!info->args) {
        info->args = g_strdup_printf("debug=0x8 cpus=%u rd=md0 serial=3 -v -noprogress keepsyms=1 wdt=-1",
                                     ap_cpus);
    }
    root = info->dtree_f.buf;
    chosen = adt_find_node(root, "chosen");
    sep_node = adt_find_node(root, "arm-io/sep");
    if (!sep_node ||
        !!adt_get_prop_val(sep_node, "sepfw-loaded") != !!info->sep_image ||
        !!adt_get_prop_val(sep_node, "sepfw-booted") != !!sep_image) {
        error_report("tomacco-vphone: prepared SEP tree, firmware and handoff mode must agree");
        exit(EXIT_FAILURE);
    }
    firmware = chosen ? adt_get_prop_val(chosen, "firmware-version") : NULL;
    if (!firmware || strcmp(firmware, "tomacco-vphone-research-v2") ||
        !adt_get_prop_val(chosen, "dram-base") ||
        !adt_get_prop_val(chosen, "dram-size")) {
        error_report("tomacco-vphone: prepare the tree with prepare_ios_paravirt.py");
        exit(EXIT_FAILURE);
    }
    if (userspace_sep) {
        const char recovery[32] = "device-recovery";
        if (!s->sep_role || strcmp(s->sep_role, "restore") ||
            adt_get_prop_len(chosen, "sepfw-load-at-boot") != 4 ||
            ldl_le_p(adt_get_prop_val(chosen, "sepfw-load-at-boot")) != 0 ||
            adt_get_prop_len(chosen, "osenvironment") != sizeof(recovery) ||
            memcmp(adt_get_prop_val(chosen, "osenvironment"), recovery,
                   sizeof(recovery))) {
            error_report("tomacco-vphone: userspace SEP load requires the explicit device-recovery restore profile");
            exit(EXIT_FAILURE);
        }
    }
    info->dram_base = ldq_le_p(adt_get_prop_val(chosen, "dram-base"));
    info->dram_size = ldq_le_p(adt_get_prop_val(chosen, "dram-size"));
    if (info->dram_base != VPHONE_DRAM_BASE || info->dram_size != ms->ram_size) {
        error_report("tomacco-vphone: -m must match the prepared device tree");
        exit(EXIT_FAILURE);
    }
    if (!vphone_validate_ap_topology(root, ap_cpus)) {
        exit(EXIT_FAILURE);
    }
    info->preserve_kernel = true;
    for (unsigned i = 0; i < ap_cpus; i++) {
        cpu = ARM_CPU(object_new(ms->cpu_type));
        s->cpus[i] = cpu;
        CPU(cpu)->cpu_index = i;
        object_property_set_int(OBJECT(cpu), "mp-affinity", i,
                                &error_fatal);
        /* This guest boots SPTM in GL1, TXM in EL0 and XNU in EL1. There is
         * no guest EL2 hypervisor; exposing one would trap EL1 registers into
         * uninitialised EL2 vectors. Retail SPTM's EL2 machine stays separate. */
        cpu->has_el2 = false;
        cpu->has_el3 = false;
        unset_feature(&cpu->env, ARM_FEATURE_CBAR);
        unset_feature(&cpu->env, ARM_FEATURE_CBAR_RO);
        unset_feature(&cpu->env, ARM_FEATURE_EL3);
        unset_feature(&cpu->env, ARM_FEATURE_PMU);
        unset_feature(&cpu->env, ARM_FEATURE_EL2);
        cpu->gt_cntfrq_hz = 24000000;
        object_property_set_bool(OBJECT(cpu), "tomacco-strict-sysregs", true,
                                 &error_fatal);
        if (i) {
            object_property_set_bool(OBJECT(cpu), "tomacco-ap-reset-fpen", true,
                                     &error_fatal);
        }
        /* Use the real SMCCC/PSCI dispatcher. Unknown Apple OEM calls return
         * NOT_SUPPORTED, which XNU's availability probe handles explicitly.
         * Do not claim successful PAC/OEM services that this board lacks. */
        object_property_set_int(OBJECT(cpu), "psci-conduit", QEMU_PSCI_CONDUIT_HVC,
                                &error_fatal);
        if (i) {
            object_property_set_bool(OBJECT(cpu), "start-powered-off", true,
                                     &error_fatal);
        }
    }
    cpu = s->cpus[0];
    arm_load_xnu(cpu, ms, info);
    for (unsigned i = 0; i < ap_cpus; i++) {
        apple_regs_init(s->cpus[i], NULL, root, info);
        qdev_realize(DEVICE(s->cpus[i]), NULL, &error_fatal);
    }
    gic = vphone_create_interrupts(s->cpus, ap_cpus,
                                   get_system_memory(), OBJECT(s));
    create_pcie(s, gic);
    create_buttons(s, gic);

    /* The virtual platform reads its configuration at physical 0x400000.
     * Keep this VM's synthetic identity stable for a prepared tree. */
    ecid = ldq_le_p(adt_get_prop_val(chosen, "random-seed"));
    const char *tree_serial = adt_get_prop_val(root, "serial-number");
    if (tree_serial) {
        size_t serial_size = adt_get_prop_len(root, "serial-number");
        if (serial_size < 2 || serial_size > 32 ||
            tree_serial[serial_size - 1] || memchr(tree_serial, 0, serial_size - 1)) {
            error_report("tomacco-vphone: invalid prepared VM serial-number");
            exit(EXIT_FAILURE);
        }
        serial = g_strndup(tree_serial, serial_size - 1);
    } else {
        serial = g_strdup_printf("PSV%016" PRIx64, ecid);
    }
    qemu_guest_getrandom_nofail(&random, sizeof(random));
    config = qdev_new(TYPE_VMAPPLE_CFG);
    object_property_add_child(OBJECT(s), "configuration", OBJECT(config));
    qdev_prop_set_uint32(config, "nr-cpus", ap_cpus);
    qdev_prop_set_uint64(config, "ram-size", ms->ram_size);
    qdev_prop_set_uint64(config, "ecid", ecid);
    qdev_prop_set_uint32(config, "rnd", random);
    qdev_prop_set_string(config, "serial", serial);
    qdev_prop_set_string(config, "model", "Tomacco vphone600");
    qdev_prop_set_string(config, "soc_name", "Virtual ARM64 (TCG)");
    sysbus_realize_and_unref(SYS_BUS_DEVICE(config), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(config), 0, 0x400000);

    memory_region_init_io(&s->host_request, OBJECT(s), &host_request_ops, s,
                          "vphone-host-request", 8);
    memory_region_add_subregion(get_system_memory(), 0x4ffff8,
                                &s->host_request);

    asmf = qdev_new(TYPE_VPHONE_ASMF);
    object_property_add_child(OBJECT(s), "boot-measurements", OBJECT(asmf));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(asmf), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(asmf), 0, VPHONE_ASMF_BASE);

    /* Real read-only firmware block transport. The OS can attach to the same
     * block nodes through Apple VirtIO with independent access permissions.
     * Without an attached image,
     * data requests complete with failure rather than fabricated disk bytes. */
    storage = qdev_new(TYPE_VMAPPLE_BDIF);
    object_property_add_child(OBJECT(s), "firmware-storage", OBJECT(storage));
    if (blk_by_name("tomacco-aux") || bdrv_find_node("tomacco-aux")) {
        qdev_prop_set_string(storage, "aux", "tomacco-aux");
    }
    if (blk_by_name("tomacco-root") || bdrv_find_node("tomacco-root")) {
        qdev_prop_set_string(storage, "root", "tomacco-root");
    }
    sysbus_realize_and_unref(SYS_BUS_DEVICE(storage), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(storage), 0, 0x30000000);

    uart = qdev_new(TYPE_PL011);
    qdev_prop_set_chr(uart, "chardev", serial_hd(0));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(uart), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(uart), 0, 0x20010000);
    sysbus_connect_irq(SYS_BUS_DEVICE(uart), 0, qdev_get_gpio_in(gic, 33 - 32));

    /* The fallback RTC is the actual PL031 contract, including its running
     * counter, load/match registers and interrupt acknowledgement. */
    DeviceState *rtc = qdev_new("pl031");
    object_property_add_child(OBJECT(s), "pl031-rtc", OBJECT(rtc));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(rtc), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(rtc), 0, 0x20050000);
    sysbus_connect_irq(SYS_BUS_DEVICE(rtc), 0, qdev_get_gpio_in(gic, 34 - 32));

    memory_region_init_io(&s->rtc_regs, OBJECT(s), &vphone_rtc_ops, s,
                          "vphone-avp-rtc", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x30240000, &s->rtc_regs);

    aes = qdev_new(TYPE_APPLE_AES);
    object_property_add_child(OBJECT(s), "aes", OBJECT(aes));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(aes), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(aes), 0, 0x30220000);
    sysbus_mmio_map(SYS_BUS_DEVICE(aes), 1, 0x30230000);
    sysbus_connect_irq(SYS_BUS_DEVICE(aes), 0, qdev_get_gpio_in(gic, 50 - 32));

    gpu = qdev_new("reims-vgpu-mmio");
    object_property_add_child(OBJECT(s), "paravirtual-gpu", OBJECT(gpu));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(gpu), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(gpu), 0, 0x30200000);
    sysbus_mmio_map(SYS_BUS_DEVICE(gpu), 1, 0x30210000);
    sysbus_connect_irq(SYS_BUS_DEVICE(gpu), 0, qdev_get_gpio_in(gic, 49 - 32));
    sysbus_connect_irq(SYS_BUS_DEVICE(gpu), 1, qdev_get_gpio_in(gic, 48 - 32));
    if (s->sep) {
        MemoryRegionSection dram = memory_region_find(get_system_memory(),
                                                        VPHONE_DRAM_BASE,
                                                        ms->ram_size);
        DeviceState *sep = qdev_new(TYPE_VPHONE_SEP);
        DeviceState *vstr = qdev_new(TYPE_VPHONE_VSTR);
        if (!dram.mr || !memory_region_is_ram(dram.mr) ||
            dram.offset_within_region || int128_get64(dram.size) != ms->ram_size) {
            error_report("tomacco-vphone: SEP requires the complete AP DRAM mapping");
            exit(EXIT_FAILURE);
        }
        object_property_add_child(OBJECT(s), "sep-processor", OBJECT(sep));
        object_property_add_child(OBJECT(s), "sep-storage", OBJECT(vstr));
        if (blk_by_name("tomacco-sep-storage")) {
            qdev_prop_set_drive(vstr, "drive", blk_by_name("tomacco-sep-storage"));
        }
        sysbus_realize_and_unref(SYS_BUS_DEVICE(vstr), &error_fatal);
        object_property_set_link(OBJECT(sep), "storage-registers",
                                  OBJECT(sysbus_mmio_get_region(SYS_BUS_DEVICE(vstr), 0)),
                                  &error_fatal);
        object_property_set_link(OBJECT(sep), "storage-buffer",
                                  OBJECT(sysbus_mmio_get_region(SYS_BUS_DEVICE(vstr), 1)),
                                  &error_fatal);
        vphone_sep_set_image(sep, sep_image, info->sep_image_size);
        qdev_prop_set_bit(sep, "restore", s->sep_role &&
                          !strcmp(s->sep_role, "restore"));
        if (s->sep_manifest) {
            qdev_prop_set_string(sep, "manifest", s->sep_manifest);
        }
        qdev_prop_set_bit(sep, "measure-manifest", s->sep_measure_manifest);
        qdev_prop_set_uint64(sep, "ecid", ecid);
        object_property_set_link(OBJECT(sep), "ap-ram", OBJECT(dram.mr),
                                  &error_fatal);
        object_property_set_link(OBJECT(sep), "boot-measurements",
                                  OBJECT(sysbus_mmio_get_region(SYS_BUS_DEVICE(asmf), 1)),
                                  &error_fatal);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(sep), &error_fatal);
        memory_region_unref(dram.mr);
        sysbus_mmio_map(SYS_BUS_DEVICE(sep), 0, 0x30250000);
        sysbus_connect_irq(SYS_BUS_DEVICE(sep), 0, qdev_get_gpio_in(gic, 52 - 32));
        sysbus_connect_irq(SYS_BUS_DEVICE(sep), 1, qdev_get_gpio_in(gic, 53 - 32));
    }
    qemu_register_reset(vphone_reset, s);
    fprintf(stderr, "TOMACCO_VPHONE_BOOT pc=0x%" HWADDR_PRIx
            " args=0x%" HWADDR_PRIx " el=1 gpu=reims-vgpu-vulkan\n",
            info->init_pc, info->init_x0);

    mmap_file_t *inputs[] = { &info->bootkc_f, &info->dtree_f, &info->sptm_f,
                             &info->txm_f, &info->tc_f, &info->ramdisk_f };
    for (unsigned i = 0; i < G_N_ELEMENTS(inputs); i++) {
        munmap(inputs[i]->buf, inputs[i]->len);
    }
    info->sep_image = NULL;
}

static void vphone_class_init(ObjectClass *oc, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);
    mc->desc = "Tomacco vphone600 research board (no app runtime)";
    mc->init = vphone_init;
    mc->max_cpus = VPHONE_MAX_AP_CPUS + 1; /* Final slot is reserved for SEP. */
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("max");
    mc->default_ram_size = 8 * GiB;
#define ADD_PROPERTY(field) \
    object_class_property_add_str(oc, #field, get_##field, set_##field)
    ADD_PROPERTY(bootkc);
    ADD_PROPERTY(dtree);
    ADD_PROPERTY(sptm);
    ADD_PROPERTY(txm);
    ADD_PROPERTY(tc);
    ADD_PROPERTY(ramdisk);
    ADD_PROPERTY(args);
    ADD_PROPERTY(sep);
#undef ADD_PROPERTY
    object_class_property_add_str(oc, "trust-cache-update", NULL, set_trust_cache_update);
    object_class_property_add_str(oc, "sep-manifest", get_sep_manifest, set_sep_manifest);
    object_class_property_add_bool(oc, "sep-measure-manifest",
                                   get_sep_measure_manifest, set_sep_measure_manifest);
    object_class_property_add_str(oc, "sep-role", get_sep_role, set_sep_role);
    object_class_property_add_str(oc, "sep-handoff", get_sep_handoff, set_sep_handoff);
}

static const TypeInfo vphone_type = {
    .name = TYPE_TOMACCO_VPHONE,
    .parent = TYPE_MACHINE,
    .instance_size = sizeof(TomaccoVPhone),
    .class_init = vphone_class_init,
    .interfaces = arm_machine_interfaces,
};

static void register_vphone(void)
{
    type_register_static(&vphone_type);
}
type_init(register_vphone)
