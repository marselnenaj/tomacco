/* Public Darwin ARM64 libproc layouts: XNU bsd/sys/proc_info.h.
 * One definition is shared by the graphics snapshot and launch observation.
 * A PID/start record describes the instant of the read; it is not a retained
 * process lifetime or proof that an application owns a foreground scene. */
#ifndef TOMACCO_GUEST_PROCESS_INFO_H
#define TOMACCO_GUEST_PROCESS_INFO_H

struct graphics_process_info {
    unsigned flags, status, exit_status, pid, parent_pid;
    unsigned uid, gid, real_uid, real_gid, saved_uid, saved_gid, reserved;
    char command[16], name[32];
    unsigned files, process_group, job_control, terminal, terminal_group;
    int nice;
    unsigned long start_seconds, start_microseconds;
};
_Static_assert(sizeof(struct graphics_process_info) == 136, "Darwin ARM64 proc_bsdinfo ABI");
_Static_assert(__builtin_offsetof(struct graphics_process_info, uid) == 20, "effective uid ABI");
_Static_assert(__builtin_offsetof(struct graphics_process_info, name) == 64, "process name ABI");
_Static_assert(__builtin_offsetof(struct graphics_process_info, start_seconds) == 120, "process start ABI");

struct graphics_short_process_info {
    unsigned pid, parent_pid, process_group, status;
    char command[16];
    unsigned flags, uid, gid, real_uid, real_gid, saved_uid, saved_gid, reserved;
};
_Static_assert(sizeof(struct graphics_short_process_info) == 64, "Darwin short BSD info ABI");
_Static_assert(__builtin_offsetof(struct graphics_short_process_info, uid) == 36, "short effective uid ABI");

static int process_start_fields_known(const struct graphics_process_info *value) {
    return value && value->start_seconds > 0 && value->start_microseconds < 1000000;
}

#endif
