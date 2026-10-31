/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef TOMACCO_VPHONE_FAULT_H
#define TOMACCO_VPHONE_FAULT_H

#include "cpu.h"
#include "internals.h"
#include "trace.h"

/* Observe restored architectural state before the guest's exception handler
 * overwrites it. No guest memory reads, register writes or exception changes.
 * In particular, a secondary SEP panic must not replace the original fault. */
static inline void tomacco_trace_fault_context(CPUARMState *env, uint64_t pc,
                                              const char *kind)
{
    if (!env->aarch64 ||
        !trace_event_get_state_backends(TRACE_TOMACCO_ARM_FAULT_CONTEXT)) {
        return;
    }
    unsigned cpu = env_cpu(env)->cpu_index;
    trace_tomacco_arm_fault_context(cpu, pc, kind, pstate_read(env),
                                   arm_current_el(env), env->cp15.tcr_el[1],
                                   env->cp15.ttbr0_el[1], env->cp15.ttbr1_el[1]);
    /* xregs[31] holds the active stack pointer, not the zero register. */
    for (unsigned first = 0; first < 32; first += 4) {
        trace_tomacco_arm_fault_registers(cpu, pc, first,
                                         env->xregs[first], env->xregs[first + 1],
                                         env->xregs[first + 2], env->xregs[first + 3]);
    }
}

#endif
