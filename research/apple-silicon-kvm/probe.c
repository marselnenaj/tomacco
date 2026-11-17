// SPDX-License-Identifier: GPL-2.0-or-later
// kvm-probe: what KVM on an Apple Silicon Linux host does with Apple's own
// EL1 instructions and registers in a guest. Each test runs one instruction
// at EL1 with the MMU off; success stores 0x5a to MMIO+8, a guest exception
// stores ESR_EL1 to MMIO+0 through the vector table.
#include <errno.h>
#include <fcntl.h>
#include <linux/kvm.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stddef.h>

#define GUEST_RAM 0x10000ULL
#define RAM_SIZE  0x10000ULL
#define VECTORS   0x10000ULL
#define CODE      0x11000ULL
#define MMIO      0xA0000000ULL

#define TEST(name, body) \
    __asm__(".pushsection .text\n.global " #name "_start\n.global " #name "_end\n.balign 4\n" \
            #name "_start:\n" body "\n mov x1, #0x5a\n str x1, [x9, #8]\n b .\n" #name "_end:\n.popsection\n"); \
    extern const uint8_t name##_start[], name##_end[];

TEST(t_nop, "nop")
TEST(t_sprr_config, "mrs x0, s3_6_c15_c1_0")
TEST(t_gxf_config, "mrs x0, s3_6_c15_c1_2")
TEST(t_sprr_perm_el1, "mrs x0, s3_6_c15_c1_5")
TEST(t_genter, ".long 0x00201420")
TEST(t_gexit, ".long 0x00201400")
TEST(t_ipi_sr, "mrs x0, s3_5_c15_c1_1")
TEST(t_ipi_rr_local, "msr s3_5_c15_c0_0, xzr")
TEST(t_hid0, "mrs x0, s3_0_c15_c0_0")
TEST(t_actlr, "mrs x0, actlr_el1")
TEST(t_pacia, "mov x1, #0x1234\n pacia x1, sp\n autia x1, sp")
TEST(t_apiakey, "mrs x0, apiakeylo_el1")

// 16 vector slots of 0x80: report ESR_EL1, then park.
__asm__(".pushsection .text\n.global vec_start\n.global vec_end\n.balign 2048\nvec_start:\n"
        ".rept 16\n mrs x1, esr_el1\n mrs x2, elr_el1\n str x1, [x9]\n b .\n .balign 128\n.endr\nvec_end:\n.popsection\n");
extern const uint8_t vec_start[], vec_end[];

static int vm_fd, vcpu_fd;
static struct kvm_run *run;

static uint64_t reg_id_core(size_t off) { return KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM_CORE | (off / 4); }
static void set_reg(uint64_t id, uint64_t value) {
    struct kvm_one_reg r = { .id = id, .addr = (uint64_t)&value };
    if (ioctl(vcpu_fd, KVM_SET_ONE_REG, &r) < 0) { perror("SET_ONE_REG"); exit(1); }
}
static uint64_t get_reg(uint64_t id) {
    uint64_t value = 0; struct kvm_one_reg r = { .id = id, .addr = (uint64_t)&value };
    ioctl(vcpu_fd, KVM_GET_ONE_REG, &r); return value;
}
#define CORE(field) reg_id_core(offsetof(struct kvm_regs, field))
#define SYS(op0, op1, crn, crm, op2) (KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM64_SYSREG | \
    ((uint64_t)(op0) << 14) | ((uint64_t)(op1) << 11) | ((uint64_t)(crn) << 7) | ((uint64_t)(crm) << 3) | (op2))

static void one(const char *name, const uint8_t *start, const uint8_t *end, uint8_t *ram, int kvm) {
    vcpu_fd = ioctl(vm_fd, KVM_CREATE_VCPU, 0);
    if (vcpu_fd < 0) { perror("CREATE_VCPU"); exit(1); }
    size_t size = ioctl(kvm, KVM_GET_VCPU_MMAP_SIZE, 0);
    run = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, vcpu_fd, 0);
    struct kvm_vcpu_init init;
    ioctl(vm_fd, KVM_ARM_PREFERRED_TARGET, &init);
    init.features[0] |= (1 << KVM_ARM_VCPU_PTRAUTH_ADDRESS) | (1 << KVM_ARM_VCPU_PTRAUTH_GENERIC);
    if (ioctl(vcpu_fd, KVM_ARM_VCPU_INIT, &init) < 0) {
        init.features[0] &= ~((1 << KVM_ARM_VCPU_PTRAUTH_ADDRESS) | (1 << KVM_ARM_VCPU_PTRAUTH_GENERIC));
        if (ioctl(vcpu_fd, KVM_ARM_VCPU_INIT, &init) < 0) { perror("VCPU_INIT"); exit(1); }
        printf("  (no guest ptrauth)\n");
    }
    memset(ram + (CODE - GUEST_RAM), 0, 0x1000);
    memcpy(ram + (CODE - GUEST_RAM), start, end - start);
    set_reg(CORE(regs.pc), CODE);
    set_reg(CORE(regs.pstate), 0x3c5);               /* EL1h, DAIF masked */
    set_reg(CORE(regs.regs[9]), MMIO);
    set_reg(CORE(sp_el1), GUEST_RAM + RAM_SIZE - 16);
    set_reg(SYS(3, 0, 12, 0, 0), VECTORS);           /* VBAR_EL1 */
    int r = ioctl(vcpu_fd, KVM_RUN, 0);
    int err = errno;
    printf("%-16s ", name);
    if (r < 0) {
        printf("KVM_RUN failed: %s\n", strerror(err));
    } else if (run->exit_reason == KVM_EXIT_MMIO) {
        uint64_t value = 0; memcpy(&value, run->mmio.data, run->mmio.len > 8 ? 8 : run->mmio.len);
        if (run->mmio.phys_addr == MMIO + 8)
            printf("ran, x0=%#llx\n", (unsigned long long)get_reg(CORE(regs.regs[0])));
        else
            printf("guest exception ESR=%#llx (EC %#llx) at %#llx\n", (unsigned long long)value,
                   (unsigned long long)(value >> 26) & 0x3f, (unsigned long long)get_reg(CORE(regs.regs[2])));
    } else {
        printf("exit %u", run->exit_reason);
        if (run->exit_reason == KVM_EXIT_INTERNAL_ERROR) printf(" internal suberror %u", run->internal.suberror);
        if (run->exit_reason == KVM_EXIT_FAIL_ENTRY) printf(" hw %#llx", (unsigned long long)run->fail_entry.hardware_entry_failure_reason);
        printf(" pc=%#llx\n", (unsigned long long)get_reg(CORE(regs.pc)));
    }
    munmap(run, size); close(vcpu_fd);
}

int main(void) {
    int kvm = open("/dev/kvm", O_RDWR);
    if (kvm < 0) { perror("/dev/kvm"); return 1; }
    struct { const char *n; const uint8_t *s, *e; } tests[] = {
#define T(x) { #x, x##_start, x##_end }
        T(t_nop), T(t_sprr_config), T(t_gxf_config), T(t_sprr_perm_el1), T(t_genter), T(t_gexit),
        T(t_ipi_sr), T(t_ipi_rr_local), T(t_hid0), T(t_actlr), T(t_pacia), T(t_apiakey),
    };
    for (size_t i = 0; i < sizeof tests / sizeof *tests; i++) {
        vm_fd = ioctl(kvm, KVM_CREATE_VM, KVM_VM_TYPE_ARM_IPA_SIZE(36));
        if (vm_fd < 0) { perror("CREATE_VM"); return 1; }
        uint8_t *ram = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
        memcpy(ram, vec_start, vec_end - vec_start);
        struct kvm_userspace_memory_region region = { .slot = 0, .guest_phys_addr = GUEST_RAM,
            .memory_size = RAM_SIZE, .userspace_addr = (uint64_t)ram };
        if (ioctl(vm_fd, KVM_SET_USER_MEMORY_REGION, &region) < 0) { perror("SET_USER_MEMORY_REGION"); return 1; }
        one(tests[i].n, tests[i].s, tests[i].e, ram, kvm);
        munmap(ram, RAM_SIZE); close(vm_fd);
    }
    return 0;
}
