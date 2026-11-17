# SPDX-License-Identifier: GPL-2.0-only
# UNTESTED v0: never built or booted. Run from the root of an arm64 Linux source tree
# (written against an Asahi-based 7.1.x tree); it adds arch/arm64/include/asm/apple_gxf.h
# and hooks arch/arm64/kvm/hyp/vhe/sysreg-sr.c. Usage: python3 apply-gxf-v0.py
import pathlib
hdr = pathlib.Path("arch/arm64/include/asm/apple_gxf.h")
hdr.write_text("""/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Apple's guarded execution (GXF) and shadow permission (SPRR) configuration
 * registers, as KVM switches them for a guest that uses them in hardware.
 * Encodings from the Asahi Linux reverse-engineering notes.
 */
#ifndef __ASM_APPLE_GXF_H
#define __ASM_APPLE_GXF_H

#include <linux/bits.h>
#include <asm/cputype.h>
#include <asm/sysreg.h>

#define SYS_APL_SPRR_CONFIG_EL1		sys_reg(3, 6, 15, 1, 0)
#define APL_SPRR_CONFIG_EN		BIT(0)
#define SYS_APL_GXF_CONFIG_EL1		sys_reg(3, 6, 15, 1, 2)
#define APL_GXF_CONFIG_EN		BIT(0)

/* AIDR_EL1 bit 16: the core implements GXF (and with it SPRR). */
#define APL_AIDR_GXF			BIT(16)

static inline bool apple_cpu_has_gxf(void)
{
	return read_cpuid_implementor() == ARM_CPU_IMP_APPLE &&
	       (read_sysreg(aidr_el1) & APL_AIDR_GXF);
}

#endif /* __ASM_APPLE_GXF_H */
""")
p = pathlib.Path("arch/arm64/kvm/hyp/vhe/sysreg-sr.c"); s = p.read_text()
inc = "#include <asm/kvm_hyp.h>\n"
assert s.count(inc) == 1, "include anchor"
s = s.replace(inc, inc + "#include <asm/apple_gxf.h>\n", 1)
anchor = "/**\n * __vcpu_load_switch_sysregs"
helpers = """/*
 * Apple GXF/SPRR for the guest (opt-in with kvm-arm.apple_gxf=1, on cores
 * that have them): enabled for the guest's EL1&0 while its registers are on
 * the CPU, the host's value back when they leave. SPRR first: GXF_CONFIG is
 * UNDEF while SPRR is off.
 */
bool kvm_apple_gxf __read_mostly;

static int __init early_kvm_apple_gxf(char *arg)
{
	return kstrtobool(arg, &kvm_apple_gxf);
}
early_param("kvm-arm.apple_gxf", early_kvm_apple_gxf);

static DEFINE_PER_CPU(u64, host_apl_sprr_config);

static void __apple_gxf_load(void)
{
	if (!kvm_apple_gxf || !apple_cpu_has_gxf())
		return;
	__this_cpu_write(host_apl_sprr_config, read_sysreg_s(SYS_APL_SPRR_CONFIG_EL1));
	write_sysreg_s(APL_SPRR_CONFIG_EN, SYS_APL_SPRR_CONFIG_EL1);
	isb();
	write_sysreg_s(APL_GXF_CONFIG_EN, SYS_APL_GXF_CONFIG_EL1);
	isb();
}

static void __apple_gxf_put(void)
{
	if (!kvm_apple_gxf || !apple_cpu_has_gxf())
		return;
	write_sysreg_s(0, SYS_APL_GXF_CONFIG_EL1);
	isb();
	write_sysreg_s(__this_cpu_read(host_apl_sprr_config), SYS_APL_SPRR_CONFIG_EL1);
	isb();
}

"""
assert s.count(anchor) == 1, "load anchor"
s = s.replace(anchor, helpers + anchor, 1)
old = "\t\t__sysreg_restore_el1_state(guest_ctxt, midr, mpidr);\n\t}\n\n\tvcpu_set_flag(vcpu, SYSREGS_ON_CPU);"
assert s.count(old) == 1, "load hook"
s = s.replace(old, "\t\t__sysreg_restore_el1_state(guest_ctxt, midr, mpidr);\n\t}\n\n\t__apple_gxf_load();\n\tvcpu_set_flag(vcpu, SYSREGS_ON_CPU);", 1)
old = "\tif (unlikely(is_hyp_ctxt(vcpu)))\n\t\t__sysreg_save_vel2_state(vcpu);\n\telse\n\t\t__sysreg_save_el1_state(guest_ctxt);\n"
assert s.count(old) == 1, "put hook"
s = s.replace(old, "\t__apple_gxf_put();\n\n" + old, 1)
p.write_text(s)
print("patched")
