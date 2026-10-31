/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * tomacco-tcg-profile: which guest code the paravirtual iPhone's TCG vCPUs
 * spend their time in.
 *
 * A QEMU TCG plugin (API version 7, the engine's include/plugins/qemu-plugin.h).
 * At translation time every translated block (TB) is keyed by its guest
 * virtual start address and a hash of its instruction bytes, its instructions
 * are classified once, and one inline per-vCPU counter increment is added at
 * its start. Nothing runs per executed instruction and no callback runs per
 * executed block: the cost while the guest runs is a handful of host
 * instructions per TB entry. Interrupt/exception entries (the discontinuity
 * hook) and idle periods are counted from callbacks QEMU makes anyway.
 *
 * Reports are JSON, written atomically (temporary file, then rename):
 *   out=PATH                 the cumulative report, written at exit
 *   PATH-stem.window-NNNN.json  the change since the previous window, written
 *                            when PATH-stem.mark appears (its first line is
 *                            the window's label; the file is removed) or
 *                            every interval=SECONDS
 * Executed instructions are estimated as TB entries x the TB's instruction
 * count: a TB left early by an exception is counted whole.
 *
 * Options (QEMU -plugin file=...,key=value,...):
 *   out=PATH (required) top=N (300) pages=N (300) page_bits=B (12)
 *   interval=SECONDS (0: none) mark=on|off (on) exceptions=on|off (on)
 *   code=on|off (off: hot blocks' instruction words stay out of reports)
 *   max_blocks=N (4000000; later blocks share one overflow counter)
 *
 * The instruction classes are a static decode of the A64 encoding, written
 * for this profile; they need no disassembler. Decoding is approximate where
 * the report says so (simd_fp is a heuristic).
 */
#include <errno.h>
#include <glib.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

#define MAX_VCPUS 64
#define MAX_TB_INSNS 512
/* Counter slots per scoreboard chunk. QEMU allocates every scoreboard for
 * at least 16 vCPUs, so one chunk holds 16 x 8 KiB slots x 8 bytes. */
#define CHUNK_SLOTS 8192
#define EXC_SLOTS 4096
#define EXC_PROBE 32

enum {
    C_INT, C_BRANCH, C_LOAD, C_STORE, C_VEC_LDST, C_ATOMIC, C_EXCL,
    C_ORDERED, C_MOPS, C_FP, C_SIMD, C_SIMD_FP, C_CRYPTO, C_SVE_SME, C_PAUTH,
    C_SYSREG, C_SYSOP, C_BARRIER, C_HINT, C_WFX, C_EXC, C_ERET, C_GXF,
    C_OTHER, NCLASS
};

static const char *const class_names[NCLASS] = {
    "int", "branch", "load", "store", "vec_ldst", "atomic", "exclusive",
    "ordered", "mops", "fp", "simd", "simd_fp", "crypto", "sve_sme", "pauth",
    "sysreg", "sysop", "barrier", "hint", "wfx", "exception", "eret", "gxf",
    "other",
};

typedef struct Block {
    uint64_t pc;
    uint32_t hash;
    uint16_t insns;
    uint16_t nspecial;
    uint32_t bytes;
    uint32_t translations;
    uint32_t slot;
    uint16_t cls[NCLASS];
    uint32_t *special;
    uint32_t *code;
} Block;

typedef struct ExcEntry {
    uint64_t pc;      /* from_pc | 1 while used (from_pc is 4-aligned) */
    uint64_t count;
    uint32_t slot;    /* vector offset of to_pc and the discontinuity type */
} ExcEntry;

typedef struct Vcpu {
    uint64_t idle_ns, idle_entries, idle_since;
    uint64_t discon[3][16];
    uint64_t exc_overflow;
    ExcEntry exc[EXC_SLOTS];
} Vcpu;

/* Window baselines: per-vCPU values of the previous window. */
typedef struct VcpuBase {
    uint64_t idle_ns, idle_entries;
    uint64_t discon[3][16];
    uint64_t exc_overflow;
    uint64_t *exc;
} VcpuBase;

static GMutex lock;          /* blocks, chunks */
static GMutex report_lock;   /* one report at a time; window baselines */
static GHashTable *blocks;
static GPtrArray *order;     /* Block *, by slot */
static GPtrArray *chunks;    /* struct qemu_plugin_scoreboard *, by slot / CHUNK_SLOTS */
static struct qemu_plugin_scoreboard *overflow;  /* [0] TB entries, [1] instructions */
static Vcpu *vcpus[MAX_VCPUS];
static VcpuBase bases[MAX_VCPUS];
static uint64_t *base_counts;  /* [slot * base_vcpus + v] */
static size_t base_blocks, base_vcpus;
static uint64_t base_overflow[2];

static char *out_path, *stem, *mark_path;
static unsigned top_n = 300, page_n = 300, page_bits = 12;
static double interval;
static bool use_mark = true, use_exceptions = true, keep_code;
static uint64_t max_blocks = 4000000;
static uint64_t started_ns, window_started_ns;
static unsigned window_index;
static volatile bool stopping;
static pthread_t watcher;
static bool watcher_running;

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* ---- A64 decode -------------------------------------------------------- */

static bool simd_fp(uint32_t w)
{
    if ((w & 0x9f200400) == 0x0e200400 || (w & 0xdf200400) == 0x5e200400) {
        return ((w >> 11) & 0x1f) >= 0x18;            /* three same */
    }
    if ((w & 0x9f60c400) == 0x0e400400 || (w & 0xdf60c400) == 0x5e400400) {
        return true;                                  /* three same FP16 */
    }
    if ((w & 0x9f3e0c00) == 0x0e200800 || (w & 0xdf3e0c00) == 0x5e200800) {
        unsigned op = (w >> 12) & 0x1f;               /* two-register misc */
        return (op >= 0x0c && op <= 0x0f && (w >> 23 & 1)) || op >= 0x16;
    }
    if ((w & 0x9f000400) == 0x0f000000 || (w & 0xdf000400) == 0x5f000000) {
        unsigned op = (w >> 12) & 0xf;                /* by element */
        return op == 1 || op == 5 || op == 9;
    }
    if ((w & 0x9f3e0c00) == 0x0e300800 || (w & 0xdf3e0c00) == 0x5e300800) {
        return ((w >> 12) & 0x1f) >= 0x0c;            /* across lanes / pairwise FP */
    }
    return false;
}

static int classify_ldst(uint32_t w)
{
    bool vector = (w >> 26) & 1;
    if ((w & 0x3f000000) == 0x08000000 && !vector) {
        bool o2 = (w >> 23) & 1, o1 = (w >> 21) & 1;
        if (o1 && ((w >> 10) & 0x1f) == 0x1f) {
            return C_ATOMIC;                          /* CAS, CASP */
        }
        return o2 ? C_ORDERED : C_EXCL;               /* LDAR/STLR : LDXR/STXR */
    }
    if ((w & 0x3b200c00) == 0x38200000 && !vector) {
        bool o3 = (w >> 15) & 1;
        unsigned opc = (w >> 12) & 7;
        return o3 && opc == 4 ? C_ORDERED : C_ATOMIC; /* LDAPR : LDADD, SWP, ... */
    }
    if ((w & 0x3f200c00) == 0x19000000) {
        return C_ORDERED;                             /* LDAPUR, STLUR */
    }
    if ((w & 0x39200c00) == 0x19000400) {
        return C_MOPS;                                /* CPY*, SET* */
    }
    if (vector) {
        return C_VEC_LDST;
    }
    if ((w & 0xff200400) == 0xf8200400) {
        return C_LOAD;                                /* LDRAA, LDRAB */
    }
    if ((w & 0xff200000) == 0xd9200000) {             /* MTE tags: LDG, STG, ST2G, STZG, ... */
        return ((w >> 22) & 3) == 1 && ((w >> 10) & 3) == 0 ? C_LOAD : C_STORE;
    }
    switch ((w >> 28) & 3) {
    case 3:
        return ((w >> 22) & 3) ? C_LOAD : C_STORE;
    case 2:
        return ((w >> 22) & 1) ? C_LOAD : C_STORE;
    case 1:
        return C_LOAD;                                /* literal */
    default:
        return ((w >> 22) & 1) ? C_LOAD : C_STORE;
    }
}

static int classify_system(uint32_t w)
{
    unsigned op0 = (w >> 19) & 3, op1 = (w >> 16) & 7;
    unsigned crn = (w >> 12) & 0xf, crm = (w >> 8) & 0xf, op2 = (w >> 5) & 7;
    if (op0 >= 2) {
        return C_SYSREG;
    }
    if (op0 == 1) {
        return C_SYSOP;
    }
    if (crn == 2 && op1 == 3) {
        if (crm == 0 && (op2 == 1 || op2 == 2 || op2 == 3)) {
            return C_WFX;                             /* YIELD, WFE, WFI */
        }
        if (crm == 1 || crm == 3 || (crm == 0 && op2 == 7)) {
            return C_PAUTH;                           /* PACIASP, AUTIASP, XPACLRI, ... */
        }
        return C_HINT;
    }
    if (crn == 1 && op1 == 3 && crm == 0 && op2 <= 1) {
        return C_WFX;                                 /* WFET, WFIT */
    }
    if (crn == 3) {
        return C_BARRIER;
    }
    return C_SYSREG;                                  /* MSR (immediate), PSTATE */
}

static int classify(uint32_t w)
{
    unsigned op0 = (w >> 25) & 0xf;
    if ((w & 0xffffffe0) == 0x00201420 || w == 0x00201400) {
        return C_GXF;                                 /* Apple GENTER, GEXIT */
    }
    if ((op0 & 0xe) == 0x8) {
        return C_INT;                                 /* data processing, immediate */
    }
    if ((op0 & 0xe) == 0xa) {
        if ((w & 0xff000000) == 0xd4000000) {
            return C_EXC;
        }
        if ((w & 0xffc00000) == 0xd5000000) {
            return classify_system(w);
        }
        if ((w & 0xfffff3ff) == 0xd69f03e0 || (w & 0xfffffbff) == 0xd69f0bff) {
            return C_ERET;                            /* ERET, ERETAA, ERETAB */
        }
        if ((w & 0x7c000000) == 0x14000000 || (w & 0xff000000) == 0x54000000 ||
            (w & 0x7e000000) == 0x34000000 || (w & 0x7e000000) == 0x36000000 ||
            (w & 0xfe000000) == 0xd6000000) {
            return C_BRANCH;
        }
        return C_OTHER;
    }
    if ((op0 & 0x5) == 0x4) {
        return classify_ldst(w);
    }
    if ((op0 & 0x7) == 0x5) {
        if ((w & 0xffffc000) == 0xdac10000 || (w & 0xffe0fc00) == 0x9ac03000) {
            return C_PAUTH;                           /* PACIA..., AUTDZB, XPAC*, PACGA */
        }
        return C_INT;
    }
    if ((op0 & 0x7) == 0x7) {
        if ((w & 0xff3e0c00) == 0x4e280800 || (w & 0xff208c00) == 0x5e000000 ||
            (w & 0xff3e0c00) == 0x5e280800 || (w & 0xff000000) == 0xce000000) {
            return C_CRYPTO;                          /* AES, SHA1/2, SHA512, SHA3, SM3/4 */
        }
        if ((w & 0x5f000000) == 0x1e000000) {
            if ((w & 0x5f20fc00) == 0x1e200000 && ((w >> 16) & 6) == 6) {
                return C_SIMD;                        /* FMOV between general and FP */
            }
            return C_FP;
        }
        if ((w & 0x5f000000) == 0x1f000000) {
            return C_FP;                              /* FMADD, FMSUB, ... */
        }
        return simd_fp(w) ? C_SIMD_FP : C_SIMD;
    }
    if (op0 == 2 || (op0 == 0 && (w >> 31))) {
        return C_SVE_SME;
    }
    return C_OTHER;
}

static bool is_special(int cls)
{
    return cls == C_SYSREG || cls == C_SYSOP || cls == C_BARRIER || cls == C_WFX ||
           cls == C_EXC || cls == C_ERET || cls == C_GXF || cls == C_MOPS;
}

typedef struct Named {
    uint8_t op0, op1, crn, crm, op2;
    const char *name;
} Named;

static const Named sysregs[] = {
    {3, 3, 14, 0, 0, "CNTFRQ_EL0"}, {3, 3, 14, 0, 1, "CNTPCT_EL0"},
    {3, 3, 14, 0, 2, "CNTVCT_EL0"}, {3, 3, 14, 0, 5, "CNTPCTSS_EL0"},
    {3, 3, 14, 0, 6, "CNTVCTSS_EL0"}, {3, 3, 14, 2, 0, "CNTP_TVAL_EL0"},
    {3, 3, 14, 2, 1, "CNTP_CTL_EL0"}, {3, 3, 14, 2, 2, "CNTP_CVAL_EL0"},
    {3, 3, 14, 3, 0, "CNTV_TVAL_EL0"}, {3, 3, 14, 3, 1, "CNTV_CTL_EL0"},
    {3, 3, 14, 3, 2, "CNTV_CVAL_EL0"}, {3, 0, 14, 1, 0, "CNTKCTL_EL1"},
    {3, 3, 13, 0, 2, "TPIDR_EL0"}, {3, 3, 13, 0, 3, "TPIDRRO_EL0"},
    {3, 0, 13, 0, 4, "TPIDR_EL1"}, {3, 0, 13, 0, 1, "CONTEXTIDR_EL1"},
    {3, 3, 4, 2, 0, "NZCV"}, {3, 3, 4, 2, 1, "DAIF"}, {3, 3, 4, 4, 0, "FPCR"},
    {3, 3, 4, 4, 1, "FPSR"}, {3, 0, 4, 2, 2, "CurrentEL"}, {3, 0, 4, 2, 0, "SPSel"},
    {3, 0, 4, 2, 3, "PAN"}, {3, 0, 4, 2, 4, "UAO"}, {3, 3, 4, 2, 5, "DIT"},
    {3, 3, 4, 2, 6, "SSBS"}, {3, 3, 4, 2, 7, "TCO"}, {3, 0, 4, 1, 0, "SP_EL0"},
    {3, 0, 4, 0, 0, "SPSR_EL1"}, {3, 0, 4, 0, 1, "ELR_EL1"}, {3, 0, 5, 2, 0, "ESR_EL1"},
    {3, 0, 6, 0, 0, "FAR_EL1"}, {3, 0, 12, 0, 0, "VBAR_EL1"}, {3, 0, 2, 0, 0, "TTBR0_EL1"},
    {3, 0, 2, 0, 1, "TTBR1_EL1"}, {3, 0, 2, 0, 2, "TCR_EL1"}, {3, 0, 1, 0, 0, "SCTLR_EL1"},
    {3, 0, 1, 0, 2, "CPACR_EL1"}, {3, 0, 10, 2, 0, "MAIR_EL1"}, {3, 0, 7, 4, 0, "PAR_EL1"},
    {2, 0, 0, 2, 2, "MDSCR_EL1"}, {3, 0, 0, 0, 0, "MIDR_EL1"}, {3, 0, 0, 0, 5, "MPIDR_EL1"},
    {3, 3, 0, 0, 1, "CTR_EL0"}, {3, 3, 0, 0, 7, "DCZID_EL0"}, {3, 0, 2, 1, 0, "APIAKeyLo_EL1"},
    {3, 0, 2, 1, 1, "APIAKeyHi_EL1"}, {3, 0, 2, 1, 2, "APIBKeyLo_EL1"},
    {3, 0, 2, 1, 3, "APIBKeyHi_EL1"}, {3, 0, 2, 2, 0, "APDAKeyLo_EL1"},
    {3, 0, 2, 2, 1, "APDAKeyHi_EL1"}, {3, 0, 2, 2, 2, "APDBKeyLo_EL1"},
    {3, 0, 2, 2, 3, "APDBKeyHi_EL1"}, {3, 0, 2, 3, 0, "APGAKeyLo_EL1"},
    {3, 0, 2, 3, 1, "APGAKeyHi_EL1"}, {3, 0, 12, 12, 0, "ICC_IAR1_EL1"},
    {3, 0, 12, 12, 1, "ICC_EOIR1_EL1"}, {3, 0, 4, 6, 0, "ICC_PMR_EL1"},
    {3, 0, 12, 11, 5, "ICC_SGI1R_EL1"}, {3, 0, 12, 12, 3, "ICC_BPR1_EL1"},
    {3, 0, 12, 12, 7, "ICC_IGRPEN1_EL1"}, {3, 0, 12, 11, 3, "ICC_RPR_EL1"},
    {3, 3, 9, 12, 0, "PMCR_EL0"}, {3, 3, 9, 13, 0, "PMCCNTR_EL0"},
};

static const Named sysops[] = {
    {1, 3, 7, 4, 1, "DC ZVA"}, {1, 3, 7, 10, 1, "DC CVAC"}, {1, 3, 7, 11, 1, "DC CVAU"},
    {1, 3, 7, 14, 1, "DC CIVAC"}, {1, 3, 7, 12, 1, "DC CVAP"}, {1, 3, 7, 13, 1, "DC CVADP"},
    {1, 0, 7, 6, 1, "DC IVAC"}, {1, 0, 7, 6, 2, "DC ISW"}, {1, 0, 7, 10, 2, "DC CSW"},
    {1, 0, 7, 14, 2, "DC CISW"}, {1, 3, 7, 4, 3, "DC GVA"}, {1, 3, 7, 4, 4, "DC GZVA"},
    {1, 0, 7, 1, 0, "IC IALLUIS"}, {1, 0, 7, 5, 0, "IC IALLU"}, {1, 3, 7, 5, 1, "IC IVAU"},
    {1, 0, 7, 8, 0, "AT S1E1R"}, {1, 0, 7, 8, 1, "AT S1E1W"}, {1, 0, 7, 8, 2, "AT S1E0R"},
    {1, 0, 7, 8, 3, "AT S1E0W"}, {1, 0, 7, 9, 0, "AT S1E1RP"}, {1, 0, 7, 9, 1, "AT S1E1WP"},
    {1, 0, 8, 3, 0, "TLBI VMALLE1IS"}, {1, 0, 8, 3, 1, "TLBI VAE1IS"},
    {1, 0, 8, 3, 2, "TLBI ASIDE1IS"}, {1, 0, 8, 3, 3, "TLBI VAAE1IS"},
    {1, 0, 8, 3, 5, "TLBI VALE1IS"}, {1, 0, 8, 3, 7, "TLBI VAALE1IS"},
    {1, 0, 8, 7, 0, "TLBI VMALLE1"}, {1, 0, 8, 7, 1, "TLBI VAE1"}, {1, 0, 8, 7, 2, "TLBI ASIDE1"},
    {1, 0, 8, 7, 3, "TLBI VAAE1"}, {1, 0, 8, 7, 5, "TLBI VALE1"}, {1, 0, 8, 7, 7, "TLBI VAALE1"},
    {1, 0, 8, 2, 1, "TLBI RVAE1IS"}, {1, 0, 8, 2, 3, "TLBI RVAAE1IS"},
    {1, 0, 8, 2, 5, "TLBI RVALE1IS"}, {1, 0, 8, 2, 7, "TLBI RVAALE1IS"},
    {1, 0, 8, 6, 1, "TLBI RVAE1"}, {1, 0, 8, 6, 5, "TLBI RVALE1"},
    {1, 0, 8, 1, 0, "TLBI VMALLE1OS"}, {1, 0, 8, 1, 1, "TLBI VAE1OS"},
    {1, 0, 8, 1, 2, "TLBI ASIDE1OS"}, {1, 0, 8, 1, 5, "TLBI VALE1OS"},
};

static const char *lookup(const Named *table, size_t n, unsigned op0, unsigned op1,
                          unsigned crn, unsigned crm, unsigned op2)
{
    for (size_t i = 0; i < n; i++) {
        if (table[i].op0 == op0 && table[i].op1 == op1 && table[i].crn == crn &&
            table[i].crm == crm && table[i].op2 == op2) {
            return table[i].name;
        }
    }
    return NULL;
}

/* A stable, register-free name for a special instruction. */
static void special_name(uint32_t w, char *buf, size_t size)
{
    int cls = classify(w);
    unsigned op0 = (w >> 19) & 3, op1 = (w >> 16) & 7;
    unsigned crn = (w >> 12) & 0xf, crm = (w >> 8) & 0xf, op2 = (w >> 5) & 7;
    static const char *const dsb_opt[16] = {
        "#0", "OSHLD", "OSHST", "OSH", "#4", "NSHLD", "NSHST", "NSH",
        "#8", "ISHLD", "ISHST", "ISH", "#12", "LD", "ST", "SY",
    };
    switch (cls) {
    case C_GXF:
        snprintf(buf, size, w == 0x00201400 ? "GEXIT" : "GENTER #%u", w & 0x1f);
        return;
    case C_MOPS:
        snprintf(buf, size, ((w >> 24) & 3) == 3 ? "MOPS SET*" : "MOPS CPY*");
        return;
    case C_ERET:
        snprintf(buf, size, (w & 0x800) ? "ERETA%c" : "ERET", (w & 0x400) ? 'B' : 'A');
        return;
    case C_EXC: {
        static const char *const names[8] = {"SVC", "HVC", "SMC", "?"};
        unsigned opc = (w >> 21) & 7, ll = w & 3, imm = (w >> 5) & 0xffff;
        if (opc == 0 && ll >= 1) {
            snprintf(buf, size, "%s #0x%x", names[ll - 1], imm);
        } else if (opc == 1) {
            snprintf(buf, size, "BRK #0x%x", imm);
        } else if (opc == 2) {
            snprintf(buf, size, "HLT #0x%x", imm);
        } else {
            snprintf(buf, size, "EXC 0x%08x", w);
        }
        return;
    }
    case C_WFX:
        if (crn == 1) {
            snprintf(buf, size, op2 ? "WFIT" : "WFET");
        } else {
            snprintf(buf, size, op2 == 1 ? "YIELD" : op2 == 2 ? "WFE" : "WFI");
        }
        return;
    case C_BARRIER:
        switch (op2) {
        case 2: snprintf(buf, size, "CLREX"); return;
        case 4: snprintf(buf, size, "DSB %s", dsb_opt[crm]); return;
        case 5: snprintf(buf, size, "DMB %s", dsb_opt[crm]); return;
        case 6: snprintf(buf, size, "ISB"); return;
        case 7: snprintf(buf, size, "SB"); return;
        default: snprintf(buf, size, "BARRIER op2=%u CRm=%u", op2, crm); return;
        }
    case C_SYSOP: {
        const char *name = lookup(sysops, G_N_ELEMENTS(sysops), op0, op1, crn, crm, op2);
        if (name) {
            snprintf(buf, size, "%s", name);
        } else {
            snprintf(buf, size, "%s S1_%u_C%u_C%u_%u",
                     crn == 8 || crn == 9 ? "TLBI" : (w >> 21 & 1) ? "SYSL" : "SYS",
                     op1, crn, crm, op2);
        }
        return;
    }
    case C_SYSREG:
        if (op0 == 0) {
            static const char *const fields[8][8] = {
                [0][3] = "UAO", [0][4] = "PAN", [0][5] = "SPSel",
                [3][1] = "SSBS", [3][2] = "DIT", [3][4] = "TCO",
                [3][6] = "DAIFSet", [3][7] = "DAIFClr",
            };
            const char *field = fields[op1][op2];
            if (field) {
                snprintf(buf, size, "MSR %s, #imm", field);
            } else {
                snprintf(buf, size, "MSR PSTATE op1=%u op2=%u", op1, op2);
            }
        } else {
            const char *name = lookup(sysregs, G_N_ELEMENTS(sysregs), op0, op1, crn, crm, op2);
            bool read = (w >> 21) & 1;
            if (name) {
                snprintf(buf, size, "%s %s", read ? "MRS" : "MSR", name);
            } else {
                snprintf(buf, size, "%s S%u_%u_C%u_C%u_%u%s", read ? "MRS" : "MSR",
                         op0, op1, crn, crm, op2,
                         op0 == 3 && (crn == 11 || crn == 15) ? " (IMPDEF)" : "");
            }
        }
        return;
    default:
        snprintf(buf, size, "0x%08x", w);
    }
}

/* ---- translation ------------------------------------------------------- */

static guint block_hash(gconstpointer p)
{
    const Block *b = p;
    return (guint)(b->pc ^ (b->pc >> 32) ^ (b->hash * 0x9e3779b1u) ^ b->insns);
}

static gboolean block_equal(gconstpointer a, gconstpointer b)
{
    const Block *x = a, *y = b;
    return x->pc == y->pc && x->hash == y->hash && x->insns == y->insns &&
           x->bytes == y->bytes;
}

static void vcpu_tb_trans(struct qemu_plugin_tb *tb, void *userdata)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    uint32_t words[MAX_TB_INSNS], special[MAX_TB_INSNS];
    Block key = {.pc = qemu_plugin_tb_vaddr(tb)};
    uint16_t cls[NCLASS] = {0};
    unsigned nspecial = 0;
    uint32_t hash = 2166136261u;
    if (n > MAX_TB_INSNS) {
        n = MAX_TB_INSNS;
    }
    for (size_t i = 0; i < n; i++) {
        const struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint8_t bytes[16] = {0};
        size_t size = qemu_plugin_insn_size(insn);
        size_t got = qemu_plugin_insn_data(insn, bytes, size < sizeof(bytes) ? size : sizeof(bytes));
        for (size_t j = 0; j < got; j++) {
            hash = (hash ^ bytes[j]) * 16777619u;
        }
        key.bytes += size;
        uint32_t w = (uint32_t)bytes[0] | (uint32_t)bytes[1] << 8 |
                     (uint32_t)bytes[2] << 16 | (uint32_t)bytes[3] << 24;
        words[i] = w;
        int c = size == 4 ? classify(w) : C_OTHER;
        cls[c]++;
        if (size == 4 && is_special(c)) {
            special[nspecial++] = w;
        }
    }
    key.hash = hash;
    key.insns = (uint16_t)n;

    g_mutex_lock(&lock);
    Block *b = g_hash_table_lookup(blocks, &key);
    if (!b && order->len < max_blocks) {
        b = g_new0(Block, 1);
        *b = key;
        memcpy(b->cls, cls, sizeof(cls));
        b->nspecial = (uint16_t)nspecial;
        if (nspecial) {
            b->special = g_memdup2(special, nspecial * sizeof(uint32_t));
        }
        if (keep_code) {
            b->code = g_memdup2(words, n * sizeof(uint32_t));
        }
        b->slot = order->len;
        if (b->slot % CHUNK_SLOTS == 0) {
            g_ptr_array_add(chunks, qemu_plugin_scoreboard_new(CHUNK_SLOTS * sizeof(uint64_t)));
        }
        g_ptr_array_add(order, b);
        g_hash_table_add(blocks, b);
    }
    if (b) {
        b->translations++;
    }
    g_mutex_unlock(&lock);

    if (b) {
        struct qemu_plugin_scoreboard *chunk = g_ptr_array_index(chunks, b->slot / CHUNK_SLOTS);
        qemu_plugin_u64 entry = {chunk, (b->slot % CHUNK_SLOTS) * sizeof(uint64_t)};
        qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(tb, QEMU_PLUGIN_INLINE_ADD_U64, entry, 1);
    } else {
        qemu_plugin_u64 entries = {overflow, 0}, insns = {overflow, sizeof(uint64_t)};
        qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(tb, QEMU_PLUGIN_INLINE_ADD_U64, entries, 1);
        qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(tb, QEMU_PLUGIN_INLINE_ADD_U64, insns, n);
    }
}

/* ---- vCPU events ------------------------------------------------------- */

static void vcpu_init(unsigned int index, void *userdata)
{
    if (index < MAX_VCPUS && !vcpus[index]) {
        vcpus[index] = g_new0(Vcpu, 1);
    }
}

static void vcpu_idle(unsigned int index, void *userdata)
{
    if (index < MAX_VCPUS && vcpus[index]) {
        __atomic_store_n(&vcpus[index]->idle_since, now_ns(), __ATOMIC_RELAXED);
        __atomic_fetch_add(&vcpus[index]->idle_entries, 1, __ATOMIC_RELAXED);
    }
}

static void vcpu_resume(unsigned int index, void *userdata)
{
    if (index < MAX_VCPUS && vcpus[index]) {
        Vcpu *v = vcpus[index];
        uint64_t since = __atomic_exchange_n(&v->idle_since, 0, __ATOMIC_RELAXED);
        if (since) {
            __atomic_fetch_add(&v->idle_ns, now_ns() - since, __ATOMIC_RELAXED);
        }
    }
}

static void vcpu_discon(unsigned int index, enum qemu_plugin_discon_type type,
                        uint64_t from_pc, uint64_t to_pc, void *userdata)
{
    if (index >= MAX_VCPUS || !vcpus[index]) {
        return;
    }
    Vcpu *v = vcpus[index];
    unsigned t = type == QEMU_PLUGIN_DISCON_INTERRUPT ? 0 :
                 type == QEMU_PLUGIN_DISCON_EXCEPTION ? 1 : 2;
    unsigned vector = (to_pc >> 7) & 0xf;
    __atomic_fetch_add(&v->discon[t][vector], 1, __ATOMIC_RELAXED);
    if (t != 1) {
        return;
    }
    /* Synchronous exceptions by the PC they came from. Only this vCPU's
     * thread writes its table; a report reads it with relaxed loads. */
    uint64_t key = (from_pc & ~3ull) | 1;
    uint32_t slot = (t << 4) | vector;
    uint64_t h = (from_pc >> 2) * 0x9e3779b97f4a7c15ull;
    for (unsigned probe = 0; probe < EXC_PROBE; probe++) {
        ExcEntry *e = &v->exc[(h + probe) % EXC_SLOTS];
        uint64_t pc = __atomic_load_n(&e->pc, __ATOMIC_RELAXED);
        if (pc == key && e->slot == slot) {
            __atomic_store_n(&e->count, e->count + 1, __ATOMIC_RELAXED);
            return;
        }
        if (pc == 0) {
            e->slot = slot;
            __atomic_store_n(&e->count, 1, __ATOMIC_RELAXED);
            __atomic_store_n(&e->pc, key, __ATOMIC_RELEASE);
            return;
        }
    }
    __atomic_fetch_add(&v->exc_overflow, 1, __ATOMIC_RELAXED);
}

/* ---- reports ----------------------------------------------------------- */

typedef struct Ranked {
    uint32_t slot;
    uint64_t execs;
    uint64_t insns;
} Ranked;

typedef struct PageAgg {
    uint64_t page, insns, execs, blocks;
} PageAgg;

typedef struct SpecAgg {
    char *name;
    uint64_t count, kernel, user;
    uint64_t top_pc[3], top_count[3];
} SpecAgg;

typedef struct ExcAgg {
    uint64_t pc;
    uint32_t slot;
    uint64_t count;
} ExcAgg;

static int by_insns(const void *a, const void *b)
{
    const Ranked *x = a, *y = b;
    if (x->insns != y->insns) {
        return x->insns > y->insns ? -1 : 1;
    }
    return x->slot < y->slot ? -1 : x->slot > y->slot;
}

static int by_page_insns(const void *a, const void *b)
{
    const PageAgg *x = *(PageAgg *const *)a, *y = *(PageAgg *const *)b;
    if (x->insns != y->insns) {
        return x->insns > y->insns ? -1 : 1;
    }
    return x->page < y->page ? -1 : x->page > y->page;
}

static int by_spec_count(const void *a, const void *b)
{
    const SpecAgg *x = *(SpecAgg *const *)a, *y = *(SpecAgg *const *)b;
    if (x->count != y->count) {
        return x->count > y->count ? -1 : 1;
    }
    return strcmp(x->name, y->name);
}

static guint exc_hash(gconstpointer p)
{
    const ExcAgg *e = p;
    return (guint)((e->pc >> 2) ^ (e->pc >> 34) ^ (e->slot * 0x9e3779b1u));
}

static gboolean exc_equal(gconstpointer a, gconstpointer b)
{
    const ExcAgg *x = a, *y = b;
    return x->pc == y->pc && x->slot == y->slot;
}

static int by_exc_count(const void *a, const void *b)
{
    const ExcAgg *x = a, *y = b;
    if (x->count != y->count) {
        return x->count > y->count ? -1 : 1;
    }
    return x->pc < y->pc ? -1 : x->pc > y->pc;
}

static void spec_free(gpointer p)
{
    SpecAgg *s = p;
    g_free(s->name);
    g_free(s);
}

static const char *space_of(uint64_t pc)
{
    return (pc >> 63) ? "kernel" : "user";
}

static const char *const vector_names[16] = {
    "current-sp0-sync", "current-sp0-irq", "current-sp0-fiq", "current-sp0-serror",
    "current-spx-sync", "current-spx-irq", "current-spx-fiq", "current-spx-serror",
    "lower-a64-sync", "lower-a64-irq", "lower-a64-fiq", "lower-a64-serror",
    "lower-a32-sync", "lower-a32-irq", "lower-a32-fiq", "lower-a32-serror",
};

static void json_string(GString *s, const char *text)
{
    g_string_append_c(s, '"');
    for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
        if (*p == '"' || *p == '\\') {
            g_string_append_c(s, '\\');
            g_string_append_c(s, *p);
        } else if (*p < 0x20 || *p >= 0x7f) {
            g_string_append_printf(s, "\\u%04x", *p);
        } else {
            g_string_append_c(s, *p);
        }
    }
    g_string_append_c(s, '"');
}

static void append_mix(GString *s, const uint16_t *cls)
{
    bool first = true;
    g_string_append_c(s, '{');
    for (int c = 0; c < NCLASS; c++) {
        if (cls[c]) {
            g_string_append_printf(s, "%s\"%s\":%u", first ? "" : ",", class_names[c], cls[c]);
            first = false;
        }
    }
    g_string_append_c(s, '}');
}

static bool write_atomically(const char *path, GString *s)
{
    g_autofree char *temporary = g_strdup_printf("%s.tmp", path);
    FILE *f = fopen(temporary, "w");
    if (!f) {
        return false;
    }
    bool ok = fwrite(s->str, 1, s->len, f) == s->len;
    ok = fclose(f) == 0 && ok;
    if (!ok || rename(temporary, path) != 0) {
        unlink(temporary);
        return false;
    }
    return true;
}

/* Writes a report: since the start (delta false) or since the previous
 * window (delta true, which then becomes the new baseline). */
static void report(const char *path, const char *kind, const char *label, bool delta)
{
    g_mutex_lock(&report_lock);
    uint64_t end_ns = now_ns();
    uint64_t start_ns = delta ? window_started_ns : started_ns;
    int nv = qemu_plugin_num_vcpus();
    if (nv > MAX_VCPUS) {
        nv = MAX_VCPUS;
    }
    if (nv < 0) {
        nv = 0;
    }

    g_mutex_lock(&lock);
    size_t n = order->len;
    Block **list = g_new(Block *, n ? n : 1);
    memcpy(list, order->pdata, n * sizeof(Block *));
    size_t nchunks = chunks->len;
    struct qemu_plugin_scoreboard **chunk = g_new(struct qemu_plugin_scoreboard *, nchunks ? nchunks : 1);
    memcpy(chunk, chunks->pdata, nchunks * sizeof(*chunk));
    g_mutex_unlock(&lock);

    /* Per-vCPU counts of every block, then the baseline for the next window. */
    uint64_t *counts = g_new0(uint64_t, (n ? n : 1) * (nv ? nv : 1));
    for (int v = 0; v < nv; v++) {
        for (size_t c = 0; c < nchunks; c++) {
            const uint64_t *base = qemu_plugin_scoreboard_find(chunk[c], v);
            size_t first = c * CHUNK_SLOTS, last = MIN(n, first + CHUNK_SLOTS);
            for (size_t i = first; i < last; i++) {
                counts[i * nv + v] = __atomic_load_n(&base[i - first], __ATOMIC_RELAXED);
            }
        }
    }
    uint64_t overflow_now[2] = {0, 0};
    for (int v = 0; v < nv; v++) {
        const uint64_t *o = qemu_plugin_scoreboard_find(overflow, v);
        overflow_now[0] += __atomic_load_n(&o[0], __ATOMIC_RELAXED);
        overflow_now[1] += __atomic_load_n(&o[1], __ATOMIC_RELAXED);
    }
    uint64_t overflow_execs = overflow_now[0], overflow_insns = overflow_now[1];
    if (delta) {
        /* The absolute counts become the next window's baseline. */
        uint64_t *absolute = g_memdup2(counts, (n ? n : 1) * (nv ? nv : 1) * sizeof(uint64_t));
        size_t nb = MIN(base_blocks, n);
        int bv = MIN((int)base_vcpus, nv);
        for (size_t i = 0; i < nb; i++) {
            for (int v = 0; v < bv; v++) {
                counts[i * nv + v] -= MIN(counts[i * nv + v], base_counts[i * base_vcpus + v]);
            }
        }
        overflow_execs -= MIN(overflow_execs, base_overflow[0]);
        overflow_insns -= MIN(overflow_insns, base_overflow[1]);
        g_free(base_counts);
        base_counts = absolute;
        base_blocks = n;
        base_vcpus = nv;
        memcpy(base_overflow, overflow_now, sizeof(base_overflow));
    }
    g_free(chunk);

    /* Aggregates. */
    uint64_t total_execs = 0, total_insns = 0, space_insns[2] = {0, 0};
    uint64_t class_insns[2][NCLASS] = {{0}};
    uint64_t *vcpu_execs = g_new0(uint64_t, nv ? nv : 1);
    uint64_t *vcpu_insns = g_new0(uint64_t, nv ? nv : 1);
    uint64_t *vcpu_kernel = g_new0(uint64_t, nv ? nv : 1);
    Ranked *ranked = g_new(Ranked, n ? n : 1);
    size_t nranked = 0;
    GHashTable *pages = g_hash_table_new_full(g_int64_hash, g_int64_equal, NULL, g_free);
    GHashTable *specials = g_hash_table_new_full(g_str_hash, g_str_equal, NULL, spec_free);
    for (size_t i = 0; i < n; i++) {
        const Block *b = list[i];
        uint64_t execs = 0;
        bool kernel = b->pc >> 63;
        for (int v = 0; v < nv; v++) {
            uint64_t e = counts[i * nv + v];
            execs += e;
            vcpu_execs[v] += e;
            vcpu_insns[v] += e * b->insns;
            if (kernel) {
                vcpu_kernel[v] += e * b->insns;
            }
        }
        if (!execs) {
            continue;
        }
        uint64_t insns = execs * b->insns;
        total_execs += execs;
        total_insns += insns;
        space_insns[kernel] += insns;
        for (int c = 0; c < NCLASS; c++) {
            class_insns[kernel][c] += execs * b->cls[c];
        }
        ranked[nranked++] = (Ranked){b->slot, execs, insns};
        uint64_t page = b->pc >> page_bits;
        PageAgg *p = g_hash_table_lookup(pages, &page);
        if (!p) {
            p = g_new0(PageAgg, 1);
            p->page = page;
            g_hash_table_insert(pages, &p->page, p);
        }
        p->insns += insns;
        p->execs += execs;
        p->blocks++;
        for (unsigned k = 0; k < b->nspecial; k++) {
            char name[96];
            special_name(b->special[k], name, sizeof(name));
            SpecAgg *s = g_hash_table_lookup(specials, name);
            if (!s) {
                s = g_new0(SpecAgg, 1);
                s->name = g_strdup(name);
                g_hash_table_insert(specials, s->name, s);
            }
            s->count += execs;
            if (kernel) {
                s->kernel += execs;
            } else {
                s->user += execs;
            }
            for (int t = 0; t < 3; t++) {
                if (s->top_pc[t] == b->pc) {
                    s->top_count[t] += execs;
                    goto placed;
                }
            }
            for (int t = 0; t < 3; t++) {
                if (execs > s->top_count[t]) {
                    for (int u = 2; u > t; u--) {
                        s->top_pc[u] = s->top_pc[u - 1];
                        s->top_count[u] = s->top_count[u - 1];
                    }
                    s->top_pc[t] = b->pc;
                    s->top_count[t] = execs;
                    break;
                }
            }
        placed:;
        }
    }
    qsort(ranked, nranked, sizeof(Ranked), by_insns);

    GString *s = g_string_sized_new(1 << 20);
    double seconds = (end_ns - start_ns) / 1e9;
    g_string_append_printf(s, "{\"format\":\"tomacco-tcg-profile\",\"version\":1,\"kind\":\"%s\",", kind);
    if (delta) {
        g_string_append_printf(s, "\"window\":%u,", window_index);
    }
    g_string_append(s, "\"label\":");
    json_string(s, label ? label : "");
    g_string_append_printf(s, ",\"start_seconds\":%.3f,\"end_seconds\":%.3f,\"seconds\":%.3f,",
                           (start_ns - started_ns) / 1e9, (end_ns - started_ns) / 1e9, seconds);
    g_string_append_printf(s, "\"vcpus\":%d,\"blocks\":%zu,\"executed_blocks\":%zu,\"page_bits\":%u,"
                           "\"code_included\":%s,", nv, n, nranked, page_bits,
                           keep_code ? "true" : "false");
    g_string_append(s, "\"estimate\":\"instructions = TB entries x instructions in the TB; "
                       "a TB left early by an exception is counted whole; simd_fp is a heuristic\",");
    g_string_append_printf(s, "\"totals\":{\"tb_execs\":%" PRIu64 ",\"insns\":%" PRIu64
                           ",\"kernel_insns\":%" PRIu64 ",\"user_insns\":%" PRIu64
                           ",\"overflow_tb_execs\":%" PRIu64 ",\"overflow_insns\":%" PRIu64 "},\n",
                           total_execs, total_insns, space_insns[1], space_insns[0],
                           overflow_execs, overflow_insns);

    /* vCPUs */
    g_string_append(s, "\"per_vcpu\":[\n");
    for (int v = 0; v < nv; v++) {
        Vcpu *cpu = vcpus[v];
        uint64_t idle_ns = 0, idle_entries = 0, kinds[3] = {0, 0, 0}, vectors[3][16];
        memset(vectors, 0, sizeof(vectors));
        if (cpu) {
            idle_ns = __atomic_load_n(&cpu->idle_ns, __ATOMIC_RELAXED);
            uint64_t since = __atomic_load_n(&cpu->idle_since, __ATOMIC_RELAXED);
            if (since && end_ns > since) {
                idle_ns += end_ns - since;
            }
            idle_entries = __atomic_load_n(&cpu->idle_entries, __ATOMIC_RELAXED);
            for (int t = 0; t < 3; t++) {
                for (int x = 0; x < 16; x++) {
                    vectors[t][x] = __atomic_load_n(&cpu->discon[t][x], __ATOMIC_RELAXED);
                }
            }
        }
        if (delta) {
            uint64_t abs_idle = idle_ns, abs_entries = idle_entries, abs_vectors[3][16];
            memcpy(abs_vectors, vectors, sizeof(vectors));
            idle_ns -= MIN(idle_ns, bases[v].idle_ns);
            idle_entries -= MIN(idle_entries, bases[v].idle_entries);
            for (int t = 0; t < 3; t++) {
                for (int x = 0; x < 16; x++) {
                    vectors[t][x] -= MIN(vectors[t][x], bases[v].discon[t][x]);
                }
            }
            bases[v].idle_ns = abs_idle;
            bases[v].idle_entries = abs_entries;
            memcpy(bases[v].discon, abs_vectors, sizeof(abs_vectors));
        }
        for (int t = 0; t < 3; t++) {
            for (int x = 0; x < 16; x++) {
                kinds[t] += vectors[t][x];
            }
        }
        g_string_append_printf(s, "{\"vcpu\":%d,\"tb_execs\":%" PRIu64 ",\"insns\":%" PRIu64
                               ",\"kernel_insns\":%" PRIu64 ",\"user_insns\":%" PRIu64
                               ",\"idle_seconds\":%.3f,\"idle_entries\":%" PRIu64
                               ",\"interrupts\":%" PRIu64 ",\"exceptions\":%" PRIu64
                               ",\"hostcalls\":%" PRIu64 ",\"vectors\":{",
                               v, vcpu_execs[v], vcpu_insns[v], vcpu_kernel[v],
                               vcpu_insns[v] - vcpu_kernel[v], idle_ns / 1e9, idle_entries,
                               kinds[0], kinds[1], kinds[2]);
        bool first = true;
        static const char *const type_names[3] = {"interrupt", "exception", "hostcall"};
        for (int t = 0; t < 2; t++) {
            for (int x = 0; x < 16; x++) {
                if (vectors[t][x]) {
                    g_string_append_printf(s, "%s\"%s:%s\":%" PRIu64, first ? "" : ",",
                                           type_names[t], vector_names[x], vectors[t][x]);
                    first = false;
                }
            }
        }
        /* A host call (PSCI, semihosting) takes no vector. */
        if (kinds[2]) {
            g_string_append_printf(s, "%s\"%s\":%" PRIu64, first ? "" : ",", type_names[2], kinds[2]);
        }
        g_string_append_printf(s, "}}%s\n", v + 1 < nv ? "," : "");
    }
    g_string_append(s, "],\n");

    /* Classes */
    g_string_append(s, "\"classes\":{");
    for (int k = 1; k >= 0; k--) {
        g_string_append_printf(s, "\"%s\":{", k ? "kernel" : "user");
        bool first = true;
        for (int c = 0; c < NCLASS; c++) {
            if (class_insns[k][c]) {
                g_string_append_printf(s, "%s\"%s\":%" PRIu64, first ? "" : ",",
                                       class_names[c], class_insns[k][c]);
                first = false;
            }
        }
        g_string_append_printf(s, "}%s", k ? "," : "");
    }
    g_string_append(s, "},\n");

    /* Special instructions */
    GPtrArray *spec_list = g_hash_table_get_values_as_ptr_array(specials);
    qsort(spec_list->pdata, spec_list->len, sizeof(gpointer), by_spec_count);
    g_string_append(s, "\"special\":[\n");
    for (guint i = 0; i < spec_list->len; i++) {
        const SpecAgg *sp = g_ptr_array_index(spec_list, i);
        g_string_append(s, "{\"name\":");
        json_string(s, sp->name);
        g_string_append_printf(s, ",\"count\":%" PRIu64 ",\"kernel\":%" PRIu64 ",\"user\":%" PRIu64
                               ",\"top\":[", sp->count, sp->kernel, sp->user);
        for (int t = 0; t < 3 && sp->top_count[t]; t++) {
            g_string_append_printf(s, "%s{\"pc\":\"0x%016" PRIx64 "\",\"count\":%" PRIu64 "}",
                                   t ? "," : "", sp->top_pc[t], sp->top_count[t]);
        }
        g_string_append_printf(s, "]}%s\n", i + 1 < spec_list->len ? "," : "");
    }
    g_string_append(s, "],\n");
    g_ptr_array_free(spec_list, TRUE);

    /* Exceptions by the PC they were taken from */
    GHashTable *merged = g_hash_table_new_full(exc_hash, exc_equal, g_free, NULL);
    uint64_t exc_overflow = 0;
    for (int v = 0; v < nv; v++) {
        Vcpu *cpu = vcpus[v];
        if (!cpu || !use_exceptions) {
            continue;
        }
        if (delta && !bases[v].exc) {
            bases[v].exc = g_new0(uint64_t, EXC_SLOTS);
        }
        uint64_t over = __atomic_load_n(&cpu->exc_overflow, __ATOMIC_RELAXED);
        if (delta) {
            uint64_t abs = over;
            over -= MIN(over, bases[v].exc_overflow);
            bases[v].exc_overflow = abs;
        }
        exc_overflow += over;
        for (unsigned i = 0; i < EXC_SLOTS; i++) {
            ExcEntry *e = &cpu->exc[i];
            uint64_t pc = __atomic_load_n(&e->pc, __ATOMIC_ACQUIRE);
            if (!pc) {
                continue;
            }
            uint64_t count = __atomic_load_n(&e->count, __ATOMIC_RELAXED);
            if (delta) {
                uint64_t abs = count;
                count -= MIN(count, bases[v].exc[i]);
                bases[v].exc[i] = abs;
            }
            if (!count) {
                continue;
            }
            ExcAgg a = {pc & ~3ull, e->slot, count};
            ExcAgg *x = g_hash_table_lookup(merged, &a);
            if (x) {
                x->count += count;
            } else {
                x = g_memdup2(&a, sizeof(a));
                g_hash_table_add(merged, x);
            }
        }
    }
    /* Merged across vCPUs, by origin and vector. */
    GArray *excs = g_array_sized_new(FALSE, FALSE, sizeof(ExcAgg), g_hash_table_size(merged));
    GHashTableIter iter;
    gpointer item;
    g_hash_table_iter_init(&iter, merged);
    while (g_hash_table_iter_next(&iter, &item, NULL)) {
        g_array_append_val(excs, *(ExcAgg *)item);
    }
    g_hash_table_destroy(merged);
    qsort(excs->data, excs->len, sizeof(ExcAgg), by_exc_count);
    g_string_append_printf(s, "\"exception_pcs_overflow\":%" PRIu64 ",\"exception_pcs\":[\n", exc_overflow);
    for (guint i = 0; i < excs->len && i < top_n; i++) {
        const ExcAgg *a = &g_array_index(excs, ExcAgg, i);
        g_string_append_printf(s, "{\"from_pc\":\"0x%016" PRIx64 "\",\"space\":\"%s\",\"vector\":\"%s\","
                               "\"count\":%" PRIu64 "}%s\n", a->pc, space_of(a->pc),
                               vector_names[a->slot & 0xf], a->count,
                               i + 1 < MIN(excs->len, top_n) ? "," : "");
    }
    g_string_append(s, "],\n");
    g_array_free(excs, TRUE);

    /* Pages */
    GPtrArray *page_list = g_hash_table_get_values_as_ptr_array(pages);
    qsort(page_list->pdata, page_list->len, sizeof(gpointer), by_page_insns);
    g_string_append_printf(s, "\"pages_total\":%u,\"pages\":[\n", page_list->len);
    for (guint i = 0; i < page_list->len && i < page_n; i++) {
        const PageAgg *p = g_ptr_array_index(page_list, i);
        uint64_t address = p->page << page_bits;
        g_string_append_printf(s, "{\"page\":\"0x%016" PRIx64 "\",\"space\":\"%s\",\"insns\":%" PRIu64
                               ",\"share\":%.6f,\"tb_execs\":%" PRIu64 ",\"blocks\":%" PRIu64 "}%s\n",
                               address, space_of(address), p->insns,
                               total_insns ? (double)p->insns / total_insns : 0.0, p->execs, p->blocks,
                               i + 1 < MIN(page_list->len, page_n) ? "," : "");
    }
    g_string_append(s, "],\n");
    g_ptr_array_free(page_list, TRUE);

    /* Hot blocks */
    g_string_append(s, "\"top_blocks\":[\n");
    uint64_t cumulative = 0;
    for (size_t r = 0; r < nranked && r < top_n; r++) {
        const Ranked *k = &ranked[r];
        const Block *b = list[k->slot];
        cumulative += k->insns;
        g_string_append_printf(s, "{\"rank\":%zu,\"pc\":\"0x%016" PRIx64 "\",\"space\":\"%s\","
                               "\"tb_execs\":%" PRIu64 ",\"insns_per_tb\":%u,\"insns\":%" PRIu64
                               ",\"share\":%.6f,\"cumulative\":%.6f,\"bytes\":%u,\"hash\":\"0x%08x\","
                               "\"translations\":%u,\"per_vcpu\":[",
                               r + 1, b->pc, space_of(b->pc), k->execs, b->insns, k->insns,
                               total_insns ? (double)k->insns / total_insns : 0.0,
                               total_insns ? (double)cumulative / total_insns : 0.0,
                               b->bytes, b->hash, b->translations);
        for (int v = 0; v < nv; v++) {
            g_string_append_printf(s, "%s%" PRIu64, v ? "," : "", counts[k->slot * nv + v]);
        }
        g_string_append(s, "],\"mix\":");
        append_mix(s, b->cls);
        g_string_append(s, ",\"special\":[");
        for (unsigned j = 0; j < b->nspecial; j++) {
            char name[96];
            special_name(b->special[j], name, sizeof(name));
            if (j) {
                g_string_append_c(s, ',');
            }
            json_string(s, name);
        }
        g_string_append_c(s, ']');
        if (b->code) {
            g_string_append(s, ",\"code\":\"");
            for (unsigned j = 0; j < b->insns; j++) {
                g_string_append_printf(s, "%s%08x", j ? " " : "", b->code[j]);
            }
            g_string_append_c(s, '"');
        }
        g_string_append_printf(s, "}%s\n", r + 1 < MIN(nranked, top_n) ? "," : "");
    }
    g_string_append(s, "]}\n");

    if (!write_atomically(path, s)) {
        /* stderr: qemu_plugin_outs() is silent without -d plugin. */
        fprintf(stderr, "tomacco-tcg-profile: cannot write %s: %s\n", path, strerror(errno));
    }
    if (delta) {
        window_started_ns = end_ns;
    }
    g_string_free(s, TRUE);
    g_hash_table_destroy(pages);
    g_hash_table_destroy(specials);
    g_free(ranked);
    g_free(vcpu_execs);
    g_free(vcpu_insns);
    g_free(vcpu_kernel);
    g_free(counts);
    g_free(list);
    g_mutex_unlock(&report_lock);
}

static void write_window(const char *label)
{
    window_index++;
    g_autofree char *path = g_strdup_printf("%s.window-%04u.json", stem, window_index);
    report(path, "window", label, true);
}

static void *watch(void *unused)
{
    uint64_t next = interval > 0 ? now_ns() + (uint64_t)(interval * 1e9) : 0;
    while (!stopping) {
        struct timespec pause = {0, 100 * 1000 * 1000};
        nanosleep(&pause, NULL);
        if (stopping) {
            break;
        }
        if (use_mark && access(mark_path, F_OK) == 0) {
            char label[128] = "";
            FILE *f = fopen(mark_path, "r");
            if (f) {
                if (!fgets(label, sizeof(label), f)) {
                    label[0] = 0;
                }
                fclose(f);
            }
            label[strcspn(label, "\r\n")] = 0;
            unlink(mark_path);
            write_window(label);
        }
        if (next && now_ns() >= next) {
            write_window("interval");
            next += (uint64_t)(interval * 1e9);
        }
    }
    return NULL;
}

static void plugin_exit(void *userdata)
{
    stopping = true;
    if (watcher_running) {
        pthread_join(watcher, NULL);
        watcher_running = false;
    }
    report(out_path, "final", "", false);
}

static bool parse_uint(const char *text, uint64_t lo, uint64_t hi, uint64_t *value)
{
    char *end = NULL;
    if (!text || !*text) {
        return false;
    }
    errno = 0;
    unsigned long long v = strtoull(text, &end, 10);
    if (errno || *end || v < lo || v > hi) {
        return false;
    }
    *value = v;
    return true;
}

QEMU_PLUGIN_EXPORT
int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info, int argc, char **argv)
{
    if (strcmp(info->target_name, "aarch64") != 0) {
        fprintf(stderr, "tomacco-tcg-profile: aarch64 guests only, not %s\n", info->target_name);
        return -1;
    }
    for (int i = 0; i < argc; i++) {
        g_auto(GStrv) kv = g_strsplit(argv[i], "=", 2);
        const char *k = kv[0], *v = kv[1];
        uint64_t number = 0;
        bool ok = v != NULL;
        if (ok && strcmp(k, "out") == 0) {
            g_free(out_path);
            out_path = g_strdup(v);
            ok = *v != 0;
        } else if (ok && strcmp(k, "top") == 0) {
            ok = parse_uint(v, 1, 100000, &number);
            top_n = (unsigned)number;
        } else if (ok && strcmp(k, "pages") == 0) {
            ok = parse_uint(v, 1, 100000, &number);
            page_n = (unsigned)number;
        } else if (ok && strcmp(k, "page_bits") == 0) {
            ok = parse_uint(v, 10, 30, &number);
            page_bits = (unsigned)number;
        } else if (ok && strcmp(k, "max_blocks") == 0) {
            ok = parse_uint(v, 1, 1u << 30, &max_blocks);
        } else if (ok && strcmp(k, "interval") == 0) {
            char *end = NULL;
            interval = g_ascii_strtod(v, &end);
            ok = end && !*end && interval >= 0 && interval <= 86400;
        } else if (ok && strcmp(k, "mark") == 0) {
            ok = qemu_plugin_bool_parse(k, v, &use_mark);
        } else if (ok && strcmp(k, "exceptions") == 0) {
            ok = qemu_plugin_bool_parse(k, v, &use_exceptions);
        } else if (ok && strcmp(k, "code") == 0) {
            ok = qemu_plugin_bool_parse(k, v, &keep_code);
        } else {
            ok = false;
        }
        if (!ok) {
            fprintf(stderr, "tomacco-tcg-profile: invalid option %s\n", argv[i]);
            return -1;
        }
    }
    if (!out_path) {
        fprintf(stderr, "tomacco-tcg-profile: out=PATH is required\n");
        return -1;
    }
    stem = g_str_has_suffix(out_path, ".json")
        ? g_strndup(out_path, strlen(out_path) - 5) : g_strdup(out_path);
    mark_path = g_strdup_printf("%s.mark", stem);

    blocks = g_hash_table_new(block_hash, block_equal);
    order = g_ptr_array_new();
    chunks = g_ptr_array_new();
    overflow = qemu_plugin_scoreboard_new(2 * sizeof(uint64_t));
    started_ns = window_started_ns = now_ns();

    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, vcpu_tb_trans, NULL);
    qemu_plugin_register_vcpu_idle_cb(id, vcpu_idle, NULL);
    qemu_plugin_register_vcpu_resume_cb(id, vcpu_resume, NULL);
    if (use_exceptions) {
        qemu_plugin_register_vcpu_discon_cb(id, QEMU_PLUGIN_DISCON_ALL, vcpu_discon, NULL);
    }
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    if (use_mark || interval > 0) {
        watcher_running = pthread_create(&watcher, NULL, watch, NULL) == 0;
    }
    g_autofree char *message = g_strdup_printf(
        "tomacco-tcg-profile: report %s, windows %s.window-NNNN.json%s%s\n", out_path, stem,
        use_mark ? " on " : "", use_mark ? mark_path : "");
    qemu_plugin_outs(message);
    return 0;
}
