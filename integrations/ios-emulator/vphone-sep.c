/*
 * vphone600 virtual IOP mailbox and isolated SEP firmware processor.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * The mailbox transports bytes between two real CPUs. It does not implement
 * or synthesize SEP service replies. Firmware bring-up is experimental.
 * Register accesses and the ELFour boot layout were recovered from the
 * unmodified 23E5207q AppleVPIOP and sep-firmware.vphone600 images.
 */
#include "qemu/osdep.h"
#include "crypto/hash.h"
#include "qapi/error.h"
#include "qemu/guest-random.h"
#include "qemu/log.h"
#include "hw/core/cpu.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/vmapple/vmapple.h"
#include "migration/vmstate.h"
#include "system/address-spaces.h"
#include "system/cpus.h"
#include "internals.h"
#include "vphone-sep.h"
#include "vphone-asmf.h"
#include "vphone-vstr.h"

#define SEP_RAM_BASE 0x04000000
#define SEP_RAM_SIZE 0x04000000
#define SEP_IMAGE_BASE 0x06000000
#define SEP_ARGS_BASE 0x04020000
#define SEP_SHARED_BASE 0x04428000
#define SEP_SHARED_SIZE (SEP_IMAGE_BASE - SEP_SHARED_BASE)
#define MAIL_DEPTH 8

OBJECT_DECLARE_SIMPLE_TYPE(VPhoneSEP, VPHONE_SEP)

typedef struct MailQueue {
    uint8_t messages[MAIL_DEPTH][16];
    uint8_t pending[16];
    uint16_t written;
    unsigned head, count;
} MailQueue;

typedef struct MailEndpoint {
    VPhoneSEP *parent;
    unsigned side;
    MemoryRegion regs;
    uint32_t interrupt_mask;
    qemu_irq receive_irq;
    qemu_irq space_irq;
} MailEndpoint;

struct VPhoneSEP {
    SysBusDevice parent;
    uint8_t *image;
    size_t image_size;
    uint8_t *manifest;
    size_t manifest_size;
    char *manifest_path;
    uint8_t manifest_digest[48];
    bool measure_manifest;
    ARMCPU *cpu;
    MemoryRegion memory;
    MemoryRegion ram;
    MemoryRegion shared;
    MemoryRegion random;
    MemoryRegion *ap_ram;
    MemoryRegion *boot_measurements;
    MemoryRegion *storage_registers;
    MemoryRegion *storage_buffer;
    uint64_t ecid;
    MailEndpoint endpoint[2]; /* 0: application processor, 1: SEP */
    MailQueue queue[2];     /* queue[i] is incoming to endpoint i */
    uint32_t control, status;
    unsigned empty_polls;
    bool poll_yield_logged;
    bool started;
    bool restore;
};

/* The TRNG driver at raw 0x4f2fc reads a new word from ACFG +0x38, then
 * consumes its four bytes. A static VMApple configuration seed is not an
 * entropy device. Overlay only this register in the private SEP mapping. */
static MemTxResult sep_random_read(void *opaque, hwaddr offset, uint64_t *value,
                                 unsigned size, MemTxAttrs attrs)
{
    uint32_t random;
    if (offset || size != sizeof(random)) {
        return MEMTX_DECODE_ERROR;
    }
    qemu_guest_getrandom_nofail(&random, sizeof(random));
    *value = random;
    return MEMTX_OK;
}

static MemTxResult sep_random_write(void *opaque, hwaddr offset, uint64_t value,
                                  unsigned size, MemTxAttrs attrs)
{
    return MEMTX_DECODE_ERROR;
}

static const MemoryRegionOps sep_random_ops = {
    .read_with_attrs = sep_random_read,
    .write_with_attrs = sep_random_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static uint32_t mail_status(const MailQueue *q)
{
    return (q->count << 20) | (!q->count << 17) |
           ((q->count == MAIL_DEPTH) << 16);
}

static void mail_irqs(VPhoneSEP *s)
{
    for (unsigned i = 0; i < 2; i++) {
        MailEndpoint *e = &s->endpoint[i];
        qemu_set_irq(e->receive_irq,
                     (e->interrupt_mask & (1 << 5)) && s->queue[i].count);
        qemu_set_irq(e->space_irq,
                     (e->interrupt_mask & (1 << 4)) &&
                     s->queue[1 - i].count < MAIL_DEPTH);
    }
}

static void sep_start(CPUState *cs, run_on_cpu_data data)
{
    VPhoneSEP *s = data.host_ptr;
    CPUARMState *env = &s->cpu->env;

    cpu_reset(cs);
    pstate_write(env, PSTATE_MODE_EL1h | PSTATE_DAIF);
    env->pc = SEP_IMAGE_BASE;
    env->xregs[0] = SEP_ARGS_BASE;
    arm_rebuild_hflags(env);
    arm_set_cpu_power_state(s->cpu, PSCI_ON);
    cs->halted = false;
    qemu_log_mask(LOG_UNIMP, "tomacco-vphone SEP: start pc=0x%x args=0x%x\n",
                  SEP_IMAGE_BASE, SEP_ARGS_BASE);
}

static MemTxResult mail_read(void *opaque, hwaddr offset, uint64_t *value,
                            unsigned size, MemTxAttrs attrs)
{
    MailEndpoint *e = opaque;
    VPhoneSEP *s = e->parent;
    MailQueue *q = &s->queue[e->side];

    *value = 0;
    if (offset >= 0x100 && offset + size <= 0x110) {
        uint8_t message[16];
        if (!q->count) {
            return MEMTX_DECODE_ERROR; /* No invented message on underflow. */
        }
        memcpy(message, q->messages[q->head], sizeof(message));
        /* The fourth word is a hardware queue snapshot, consumed by
         * AppleVPIOP::getMailboxBulk, not peer-supplied payload. */
        stl_le_p(message + 12, mail_status(q));
        memcpy(value, message + offset - 0x100, size);
        *value = le64_to_cpu(*value);
        if (offset + size == 0x110) {
            q->head = (q->head + 1) % MAIL_DEPTH;
            q->count--;
            mail_irqs(s);
        }
        return MEMTX_OK;
    }
    if (size != 4) {
        return MEMTX_DECODE_ERROR;
    }
    switch (offset) {
    case 0x10: *value = s->control; break;
    case 0x14: *value = s->status; break;
    case 0x18: *value = e->interrupt_mask; break;
    case 0x1c:
        *value = mail_status(q);
        /* ELFour polls this FIFO before its first AP configuration message.
         * Give the AP another TCG time slice instead of spending the entire
         * coprocessor quantum on an empty FIFO. Do not halt the CPU, change
         * register values, manufacture an interrupt or advance guest state.
         * A debugger/qtest read must not affect processor scheduling. */
        if (e->side == 1 && !q->count && current_cpu == CPU(s->cpu)) {
            if (++s->empty_polls == 64) {
                s->empty_polls = 0;
                if (!s->poll_yield_logged) {
                    qemu_log_mask(LOG_UNIMP, "tomacco-vphone SEP: yielding empty mailbox polls\n");
                    s->poll_yield_logged = true;
                }
                cpu_exit(CPU(s->cpu));
            }
        } else if (e->side == 1) {
            s->empty_polls = 0;
        }
        break;
    case 0x20: *value = mail_status(&s->queue[1 - e->side]); break;
    default:
        qemu_log_mask(LOG_UNIMP, "tomacco-vphone SEP mailbox %u: read 0x%"
                      HWADDR_PRIx " size=%u\n", e->side, offset, size);
        return MEMTX_DECODE_ERROR;
    }
    return MEMTX_OK;
}

static MemTxResult mail_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size, MemTxAttrs attrs)
{
    MailEndpoint *e = opaque;
    VPhoneSEP *s = e->parent;
    MailQueue *q = &s->queue[1 - e->side];

    if (offset >= 0x200 && offset + size <= 0x210) {
        uint64_t little = cpu_to_le64(value);
        if (q->count == MAIL_DEPTH) {
            return MEMTX_DECODE_ERROR;
        }
        memcpy(q->pending + offset - 0x200, &little, size);
        q->written |= ((1U << size) - 1) << (offset - 0x200);
        if (offset + size == 0x210) {
            if (q->written != 0xffff) {
                return MEMTX_DECODE_ERROR;
            }
            memcpy(q->messages[(q->head + q->count) % MAIL_DEPTH],
                   q->pending, 16);
            qemu_log_mask(LOG_UNIMP, "tomacco-vphone SEP mailbox %u -> %u: "
                          "%016" PRIx64 " %016" PRIx64 "\n", e->side,
                          1 - e->side, ldq_le_p(q->pending),
                          ldq_le_p(q->pending + 8));
            q->written = 0;
            q->count++;
            mail_irqs(s);
        }
        return MEMTX_OK;
    }
    if (size != 4) {
        return MEMTX_DECODE_ERROR;
    }
    switch (offset) {
    case 0x10:
        s->control = value & 1;
        if (s->control && !s->started && e->side == 0) {
            s->started = true;
            async_run_on_cpu(CPU(s->cpu), sep_start, RUN_ON_CPU_HOST_PTR(s));
        }
        break;
    case 0x14:
        s->status = value & 0x8000003f;
        break;
    case 0x18:
        e->interrupt_mask = value & 0x30;
        mail_irqs(s);
        break;
    default:
        qemu_log_mask(LOG_UNIMP, "tomacco-vphone SEP mailbox %u: write 0x%"
                      HWADDR_PRIx " value=0x%" PRIx64 " size=%u\n",
                      e->side, offset, value, size);
        return MEMTX_DECODE_ERROR;
    }
    return MEMTX_OK;
}

static const MemoryRegionOps mail_ops = {
    .read_with_attrs = mail_read,
    .write_with_attrs = mail_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 8 },
    .impl = { .min_access_size = 4, .max_access_size = 8 },
};

static void sep_reset(DeviceState *dev)
{
    VPhoneSEP *s = VPHONE_SEP(dev);
    uint8_t *ram = memory_region_get_ram_ptr(&s->ram);
    uint8_t *args = ram + SEP_ARGS_BASE - SEP_RAM_BASE;

    s->started = false;
    s->empty_polls = 0;
    s->poll_yield_logged = false;
    s->control = s->status = 0;
    memset(s->queue, 0, sizeof(s->queue));
    for (unsigned i = 0; i < 2; i++) {
        s->endpoint[i].interrupt_mask = 0;
    }
    mail_irqs(s);
    memset(ram, 0, SEP_RAM_SIZE);
    memcpy(ram + SEP_IMAGE_BASE - SEP_RAM_BASE, s->image, s->image_size);
    if (s->manifest) {
        memcpy(ram + SEP_IMAGE_BASE - SEP_RAM_BASE + s->image_size,
               s->manifest, s->manifest_size);
    }
    /* AVPSEPBooter-6 emits a 0xe0-byte SEPB v1 record. The 23E5207q
     * ELFour consumer copies and clears its first 0xc0 bytes. Keep the
     * declared extent and the entire 64-byte seed, even for direct entry.
     * These offsets are pinned to that producer and consumer.
     * Direct entry does not reproduce the booter's trust policy. */
    stl_le_p(args, 0x53455042);
    stl_le_p(args + 8, 1);
    stl_le_p(args + 0x10, 0xe0);
    stq_le_p(args + 0x18, SEP_SHARED_BASE);
    stl_le_p(args + 0x20, SEP_SHARED_SIZE);
    /* AVPSEPBooter copies the payload tag here (raw 0x1dc4..0x1e20).
     * SEPD query 0x4384 reads it to distinguish restore initialization from
     * normal boot. Neither role asserts image authentication. */
    stl_le_p(args + 0x28, s->restore ? 0x72736570 : 0x73657069); /* rsep/sepi */
    if (s->measure_manifest) {
        /* Optional digest ABI, not an authentication bit: SEPD raw
         * 0x4b5ec..0x4b640 requires +0x30 and returns +0x38..0x67.
         * Publish only the SHA-384 actually computed over the complete IM4M
         * loaded below. The separate policy byte +0x68 stays zero. This
         * opt-in research handoff does not evaluate AVPSEPBooter's policy. */
        memcpy(args + 0x38, s->manifest_digest, sizeof(s->manifest_digest));
        args[0x30] = 1;
    }
    /* SEPOS uses +0x70/+0x78 as the manifest offset/length, relative to the
     * firmware base. The image bytes and signed IM4M are never rewritten. */
    stl_le_p(args + 0x70, s->image_size);
    stl_le_p(args + 0x78, s->manifest_size);
    qemu_guest_getrandom_nofail(args + 0x80, 0x40);
    /* The AP tree describes direct entry into loaded firmware, not a SEP ROM
     * waiting for its image. Start the actual ELFour CPU at reset, before AP
     * boot configuration can be submitted. It waits for that real message. */
    s->started = true;
    s->control = 1;
    sep_start(CPU(s->cpu), RUN_ON_CPU_HOST_PTR(s));
}

static uint8_t *sep_read_file(const char *path, size_t minimum, size_t maximum,
                              size_t alignment, size_t *size, Error **errp)
{
    struct stat st;
    size_t offset = 0;
    int fd = path ? open(path, O_RDONLY | O_CLOEXEC) : -1;
    g_autofree uint8_t *image = NULL;

    if (fd < 0) {
        error_setg_errno(errp, errno, "Cannot open SEP boot input");
        return NULL;
    }
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) ||
        st.st_size < minimum || st.st_size > maximum || st.st_size % alignment) {
        error_setg(errp, "SEP boot input must be a regular file of %zu to %zu bytes, aligned to %zu",
                   minimum, maximum, alignment);
        close(fd);
        return NULL;
    }
    image = g_malloc(st.st_size);
    while (offset < st.st_size) {
        ssize_t count = read(fd, image + offset, st.st_size - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            error_setg(errp, "SEP boot input read failed or file was truncated");
            close(fd);
            return NULL;
        }
        offset += count;
    }
    close(fd);
    *size = st.st_size;
    return g_steal_pointer(&image);
}

uint8_t *vphone_sep_read_image(const char *path, size_t *size, Error **errp)
{
    g_autofree uint8_t *image = sep_read_file(path, 0x20000, 0x1000000,
                                             0x4000, size, errp);
    if (!image) {
        return NULL;
    }
    if (ldl_le_p(image + 0x1008) != 0x64697575 ||
        ldl_le_p(image + 0x100c) != 5 ||
        ldq_le_p(image + 0x1188) != *size) {
        error_setg(errp, "SEP expects an unwrapped vphone600 ELFour image");
        return NULL;
    }
    return g_steal_pointer(&image);
}

/* Validate the bounded DER envelope, not the Apple signature or device binding.
 * Direct entry has no qualified ROM verification path. */
static bool sep_der_item(const uint8_t **cursor, size_t *remaining,
                         uint8_t tag, const uint8_t **data, size_t *size)
{
    const uint8_t *p = *cursor;
    size_t header = 2, length;
    unsigned count;

    if (*remaining < 2 || p[0] != tag) {
        return false;
    }
    length = p[1];
    if (length & 0x80) {
        count = length & 0x7f;
        if (!count || count > 3 || *remaining < header + count || !p[2]) {
            return false;
        }
        length = 0;
        for (unsigned i = 0; i < count; i++) {
            length = (length << 8) | p[header++];
        }
        if (length < 128) {
            return false;
        }
    }
    if (length > *remaining - header) {
        return false;
    }
    *data = p + header;
    *size = length;
    *cursor += header + length;
    *remaining -= header + length;
    return true;
}

static bool sep_manifest_valid(const uint8_t *image, size_t length)
{
    const uint8_t *body, *value;
    size_t size, item_size;

    if (!sep_der_item(&image, &length, 0x30, &body, &size) || length ||
        !sep_der_item(&body, &size, 0x16, &value, &item_size) ||
        item_size != 4 || memcmp(value, "IM4M", 4) ||
        !sep_der_item(&body, &size, 0x02, &value, &item_size) ||
        item_size != 1 || value[0]) {
        return false;
    }
    const uint8_t tags[] = { 0x31, 0x04, 0x30 }; /* body, signature, certificates */
    for (unsigned i = 0; i < G_N_ELEMENTS(tags); i++) {
        if (!sep_der_item(&body, &size, tags[i], &value, &item_size) || !item_size) {
            return false;
        }
    }
    return size == 0;
}

void vphone_sep_set_image(DeviceState *dev, const uint8_t *image, size_t size)
{
    VPhoneSEP *s = VPHONE_SEP(dev);
    assert(!dev->realized && !s->image);
    s->image = g_memdup2(image, size);
    s->image_size = size;
}

static void sep_realize(DeviceState *dev, Error **errp)
{
    VPhoneSEP *s = VPHONE_SEP(dev);
    DeviceState *gic, *config;
    ARMCPU *cpu;

    if (!s->image || !s->ap_ram || !s->boot_measurements ||
        !s->storage_registers || !s->storage_buffer) {
        error_setg(errp, "SEP requires firmware, AP RAM, boot measurements and storage regions");
        return;
    }
    if (s->measure_manifest && !s->manifest_path) {
        error_setg(errp, "SEP manifest measurement requires an explicit IM4M");
        return;
    }
    if (s->manifest_path) {
        s->manifest = sep_read_file(s->manifest_path, 16, 0x10000, 1,
                                     &s->manifest_size, errp);
        if (!s->manifest) {
            return;
        }
        if (!sep_manifest_valid(s->manifest, s->manifest_size)) {
            error_setg(errp, "SEP manifest requires a complete DER IM4M envelope with signature and certificates");
            return;
        }
        fprintf(stderr, "TOMACCO_SEP_MANIFEST_LOADED base=0x%zx bytes=%zu signature_verified_by_host=false\n",
                (size_t)SEP_IMAGE_BASE + s->image_size, s->manifest_size);
        if (s->measure_manifest) {
            uint8_t *digest = s->manifest_digest;
            size_t digest_size = sizeof(s->manifest_digest);
            if (qcrypto_hash_bytes(QCRYPTO_HASH_ALGO_SHA384, s->manifest,
                                   s->manifest_size, &digest, &digest_size,
                                   errp) < 0) {
                return;
            }
            fprintf(stderr, "TOMACCO_SEP_MANIFEST_MEASURED sha384=");
            for (unsigned i = 0; i < digest_size; i++) {
                fprintf(stderr, "%02x", digest[i]);
            }
            fprintf(stderr, " signature_verified_by_host=false bootloader_policy_evaluated=false\n");
        }
    }
    memory_region_init(&s->memory, OBJECT(s), "sep-address-space", UINT64_MAX);
    memory_region_add_subregion(&s->memory, VPHONE_ASMF_BASE,
                                 s->boot_measurements);
    memory_region_add_subregion(&s->memory, VPHONE_VSTR_BASE,
                                 s->storage_registers);
    memory_region_add_subregion(&s->memory, VPHONE_VSTD_BASE,
                                 s->storage_buffer);
    memory_region_init_ram(&s->ram, OBJECT(s), "sep-private-ram", SEP_RAM_SIZE,
                           &error_fatal);
    memory_region_add_subregion(&s->memory, SEP_RAM_BASE, &s->ram);
    /* Only AP DRAM is shared. Its devices and the SEP's private memory are
     * never aliases of each other. Both CPUs retain independent page tables. */
    memory_region_init_alias(&s->shared, OBJECT(s), "sep-ap-dram", s->ap_ram,
                              0, memory_region_size(s->ap_ram));
    memory_region_add_subregion(&s->memory, 0x70000000, &s->shared);
    cpu = ARM_CPU(object_new(ARM_CPU_TYPE_NAME("max")));
    s->cpu = cpu;
    CPU(cpu)->tlb_broadcast_domain = 1; /* Separate from the AP domain. */
    object_property_add_child(OBJECT(s), "cpu", OBJECT(cpu));
    object_property_set_link(OBJECT(cpu), "memory", OBJECT(&s->memory),
                              &error_fatal);
    cpu->has_el2 = false;
    cpu->has_el3 = false;
    unset_feature(&cpu->env, ARM_FEATURE_EL2);
    unset_feature(&cpu->env, ARM_FEATURE_EL3);
    unset_feature(&cpu->env, ARM_FEATURE_CBAR);
    unset_feature(&cpu->env, ARM_FEATURE_CBAR_RO);
    unset_feature(&cpu->env, ARM_FEATURE_PMU);
    object_property_set_bool(OBJECT(cpu), "start-powered-off", true,
                              &error_fatal);
    object_property_set_bool(OBJECT(cpu), "tomacco-strict-sysregs", true,
                              &error_fatal);
    object_property_set_int(OBJECT(cpu), "mp-affinity", 0, &error_fatal);
    cpu->gt_cntfrq_hz = 24000000;
    qdev_realize(DEVICE(cpu), NULL, &error_fatal);
    ARMCPU *gic_cpus[] = { cpu };
    gic = vphone_create_interrupts(gic_cpus, G_N_ELEMENTS(gic_cpus),
                                   &s->memory, OBJECT(s));
    memory_region_add_subregion(&s->memory, 0x30250000, &s->endpoint[1].regs);
    s->endpoint[1].receive_irq = qdev_get_gpio_in(gic, 52 - 32);
    s->endpoint[1].space_irq = qdev_get_gpio_in(gic, 53 - 32);
    config = qdev_new(TYPE_VMAPPLE_CFG);
    object_property_add_child(OBJECT(s), "configuration", OBJECT(config));
    qdev_prop_set_uint32(config, "nr-cpus", 1);
    qdev_prop_set_string(config, "ram-name", "SEP Config");
    qdev_prop_set_uint64(config, "ram-size", SEP_RAM_SIZE);
    qdev_prop_set_uint64(config, "ecid", s->ecid);
    qdev_prop_set_string(config, "model", "Tomacco vphone SEP");
    qdev_prop_set_string(config, "soc_name", "Virtual ARM64 (TCG)");
    sysbus_realize_and_unref(SYS_BUS_DEVICE(config), &error_fatal);
    memory_region_add_subregion(&s->memory, 0x400000,
                                 sysbus_mmio_get_region(SYS_BUS_DEVICE(config), 0));
    memory_region_add_subregion_overlap(&s->memory, 0x400038, &s->random, 1);
}

static void sep_init(Object *obj)
{
    VPhoneSEP *s = VPHONE_SEP(obj);
    memory_region_init_io(&s->random, obj, &sep_random_ops, s,
                           "sep-random", sizeof(uint32_t));
    for (unsigned i = 0; i < 2; i++) {
        MailEndpoint *e = &s->endpoint[i];
        e->parent = s;
        e->side = i;
        memory_region_init_io(&e->regs, obj, &mail_ops, e,
                               i ? "sep-mailbox-sep" : "sep-mailbox-ap", 0x1000);
    }
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->endpoint[0].regs);
    sysbus_init_irq(SYS_BUS_DEVICE(s), &s->endpoint[0].receive_irq);
    sysbus_init_irq(SYS_BUS_DEVICE(s), &s->endpoint[0].space_irq);
}

static const Property sep_properties[] = {
    DEFINE_PROP_STRING("manifest", VPhoneSEP, manifest_path),
    DEFINE_PROP_BOOL("measure-manifest", VPhoneSEP, measure_manifest, false),
    DEFINE_PROP_BOOL("restore", VPhoneSEP, restore, false),
    DEFINE_PROP_UINT64("ecid", VPhoneSEP, ecid, 0),
    DEFINE_PROP_LINK("ap-ram", VPhoneSEP, ap_ram, TYPE_MEMORY_REGION,
                     MemoryRegion *),
    DEFINE_PROP_LINK("boot-measurements", VPhoneSEP, boot_measurements,
                     TYPE_MEMORY_REGION, MemoryRegion *),
    DEFINE_PROP_LINK("storage-registers", VPhoneSEP, storage_registers,
                     TYPE_MEMORY_REGION, MemoryRegion *),
    DEFINE_PROP_LINK("storage-buffer", VPhoneSEP, storage_buffer,
                     TYPE_MEMORY_REGION, MemoryRegion *),
};

static const VMStateDescription sep_vmstate = {
    .name = TYPE_VPHONE_SEP,
    .unmigratable = true, /* No complete SEP migration contract yet. */
};

static void sep_finalize(Object *obj)
{
    g_free(VPHONE_SEP(obj)->image);
    g_free(VPHONE_SEP(obj)->manifest);
}

static void sep_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = sep_realize;
    dc->user_creatable = false;
    dc->vmsd = &sep_vmstate;
    dc->desc = "vphone SEP firmware processor (experimental, no service stubs)";
    device_class_set_legacy_reset(dc, sep_reset);
    device_class_set_props(dc, sep_properties);
}

static const TypeInfo sep_type = {
    .name = TYPE_VPHONE_SEP,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(VPhoneSEP),
    .instance_init = sep_init,
    .instance_finalize = sep_finalize,
    .class_init = sep_class_init,
};

static void register_sep(void)
{
    type_register_static(&sep_type);
}
type_init(register_sep)
