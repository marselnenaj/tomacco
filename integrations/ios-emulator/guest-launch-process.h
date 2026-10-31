/* FrontBoard completion supplies a BSProcessHandle, not a worker PID.
 * Apple WebKit's FrontBoardServicesSPI.h establishes the callback object type.
 * Every optional Objective-C getter retains the full runtime ABI guard. */
#ifndef TOMACCO_GUEST_LAUNCH_PROCESS_H
#define TOMACCO_GUEST_LAUNCH_PROCESS_H

#include "guest-process-info.h"

enum launch_completion_state { LAUNCH_PENDING, LAUNCH_PUBLISHING, LAUNCH_READY, LAUNCH_DISPOSED };
struct launch_completion {
    unsigned state;
    unsigned char accepted;
    object process, error;
};

/* The first callback owns both retained objects. Duplicate callbacks cannot
 * retain objects, overwrite the identity or touch a released semaphore. */
static int launch_completion_publish(struct launch_completion *value, object process,
                                     object error, object (*retain)(object)) {
    unsigned expected = LAUNCH_PENDING;
    if (!__atomic_compare_exchange_n(&value->state, &expected, LAUNCH_PUBLISHING, 0,
                                     __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return 0;
    value->accepted = process && !error;
    value->process = process ? retain(process) : 0;
    value->error = error ? retain(error) : 0;
    __atomic_store_n(&value->state, LAUNCH_READY, __ATOMIC_RELEASE);
    return 1;
}

static void launch_completion_dispose(struct launch_completion *value, void (*release)(object)) {
    unsigned expected = LAUNCH_READY;
    if (!__atomic_compare_exchange_n(&value->state, &expected, LAUNCH_DISPOSED, 0,
                                     __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return;
    object process = value->process, error = value->error;
    value->process = 0;
    value->error = 0;
    if (process) release(process);
    if (error) release(error);
}

static object launch_retain(object value) { return get(value, "retain"); }
static void launch_release(object value) {
    ((void (*)(object, object))message)(value, selector("release"));
}

static void launch_process_observation(object row, object process, int accepted) {
    put(row, "worker_pid", number((unsigned)getpid()));
    put(row, "pid_role", string("request-worker"));
    put(row, "launch_process_handle_present", boolean(process != 0));
    put(row, "launch_process_pid_known", boolean(0));
    put(row, "launch_process_start_snapshot_known", boolean(0));
    put(row, "launch_process_lifetime_bound", boolean(0));
    put(row, "launch_process_snapshot_atomic_with_handle", boolean(0));
    if (!accepted || !process) {
        put(row, "launch_process_observation_state", string("launch-not-accepted"));
        return;
    }
    object runtime = dlopen("/usr/lib/libobjc.A.dylib", 2);
    const char *(*class_name)(object) = runtime ? dlsym(runtime, "class_getName") : 0;
    if (class_name) {
        const char *name = class_name(object_class(process));
        if (name && strlen(name) <= 128) put(row, "launch_process_handle_class", string(name));
    }
    object expected = get_class("BSProcessHandle");
    int class_abi = expected && accepts_class(process, "isKindOfClass:", 'B');
    put(row, "launch_process_class_abi_qualified", boolean(class_abi));
    if (!class_abi) {
        put(row, "launch_process_observation_state", string("handle-class-api-unqualified"));
        return;
    }
    if (!((unsigned char (*)(object, object, object))message)(process,
                selector("isKindOfClass:"), expected)) {
        put(row, "launch_process_observation_state", string("unexpected-handle-class"));
        return;
    }
    int pid_abi = accepts(process, "pid", 'i', 0);
    put(row, "launch_process_pid_abi_qualified", boolean(pid_abi));
    if (!pid_abi) {
        put(row, "launch_process_observation_state", string("handle-pid-api-unqualified"));
        return;
    }
    int pid = ((int (*)(object, object))message)(process, selector("pid"));
    if (pid <= 1 || pid == getpid()) {
        put(row, "launch_process_observation_state", string("handle-pid-invalid"));
        return;
    }
    put(row, "launch_process_pid", number((unsigned)pid));
    put(row, "launch_process_pid_known", boolean(1));
    put(row, "launch_process_pid_source", string("FrontBoard-completion-BSProcessHandle.pid"));
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*info)(int, int, unsigned long, void *, int) = system ? dlsym(system, "proc_pidinfo") : 0;
    if (!info) {
        put(row, "launch_process_observation_state", string("process-info-api-unavailable"));
        return;
    }
    struct graphics_process_info value = {0};
    *__error() = 0;
    int bytes = info(pid, 3, 0, &value, sizeof(value));
    int error = *__error();
    put(row, "launch_process_snapshot_bytes", number(bytes > 0 ? (unsigned)bytes : 0));
    if (error) put(row, "launch_process_snapshot_errno", number((unsigned)error));
    if (bytes != sizeof(value) || value.pid != (unsigned)pid) {
        put(row, "launch_process_observation_state", string("process-info-unavailable"));
        return;
    }
    if (!process_start_fields_known(&value)) {
        put(row, "launch_process_observation_state", string("process-start-invalid"));
        return;
    }
    object snapshot = get(get_class("NSMutableDictionary"), "dictionary");
    put(snapshot, "source", string("PROC_PIDTBSDINFO"));
    put(snapshot, "pid", number(value.pid));
    put(snapshot, "start_seconds", number(value.start_seconds));
    put(snapshot, "start_microseconds", number(value.start_microseconds));
    put(snapshot, "effective_uid", number(value.uid));
    put(snapshot, "process_status", number(value.status));
    put(snapshot, "single_native_record", boolean(1));
    const char *name = value.name[0] ? value.name : value.command;
    unsigned limit = value.name[0] ? sizeof(value.name) : sizeof(value.command);
    unsigned length = 0;
    while (length < limit && name[length]) ++length;
    if (length && length < limit) put(snapshot, "process_name", string(name));
    put(row, "launch_process_start_snapshot", snapshot);
    put(row, "launch_process_start_snapshot_known", boolean(1));
    put(row, "launch_process_observation_state", string("pid-and-start-snapshot"));
}

#endif
