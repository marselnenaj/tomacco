/* Bounded, opt-in observation of an owned iOS research guest.
 * ABI references: XNU bsd/sys/{proc_info,stackshot}.h and osfmk/kern/debug.h.
 * Uses Apple's actual stackshot implementation. No synthetic process states,
 * task-control ports, register writes or replacement Apple services.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int dprintf(int, const char *, ...);
extern int snprintf(char *, unsigned long, const char *, ...);
extern int strcmp(const char *, const char *);
extern long write(int, const void *, unsigned long);
extern int *__error(void);
extern int open(const char *, int, ...);
extern int close(int);
extern int usleep(unsigned);

struct tomacco_termios {
    unsigned long input, output, control, local;
    unsigned char characters[20];
    unsigned long input_speed, output_speed;
};
_Static_assert(sizeof(struct tomacco_termios) == 72, "Darwin termios ABI");
extern int tcgetattr(int, struct tomacco_termios *);
extern void cfmakeraw(struct tomacco_termios *);
extern int tcsetattr(int, int, const struct tomacco_termios *);

struct tomacco_task_info {
    unsigned long long virtual_size, resident_size, total_user, total_system;
    unsigned long long threads_user, threads_system;
    int policy, faults, pageins, cow_faults, messages_sent, messages_received;
    int syscalls_mach, syscalls_unix, switches, threads, running, priority;
};
_Static_assert(sizeof(struct tomacco_task_info) == 96, "Darwin proc_taskinfo ABI");

static unsigned emitted_bytes;
static int diagnostic_fd = -1;
static unsigned backpressure_retries;

static int output(const char *bytes, unsigned long size) {
    while (size) {
        long count = write(diagnostic_fd, bytes, size);
        if (count < 0 && *__error() == 4) continue;
        /* Bounded wait for the UART queue, never an unbounded observer stall. */
        if (count < 0 && *__error() == 35 && backpressure_retries++ < 300) {
            usleep(10000);
            continue;
        }
        if (count <= 0) return -1;
        bytes += count;
        size -= count;
    }
    return 0;
}

static int emit_stackshot(unsigned tick, int pid, const unsigned char *data, unsigned size) {
    /* One MiB per capture, two MiB over this observer's whole lifetime. */
    if (!data || size < 16 || size > 1024 * 1024 || size > 2 * 1024 * 1024 - emitted_bytes) {
        dprintf(diagnostic_fd, "TOMACCO_STACKSHOT_REJECTED tick=%u pid=%d bytes=%u reason=budget\n", tick, pid, size);
        return -1;
    }
    emitted_bytes += size;
    unsigned crc = ~0u;
    for (unsigned i = 0; i < size; ++i) {
        crc ^= data[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
    }
    char boundary[160];
    int length = snprintf(boundary, sizeof(boundary), "TOMACCO_STACKSHOT_BEGIN tick=%u pid=%d bytes=%u crc32=%08x\n", tick, pid, size, ~crc);
    if (length < 0 || (unsigned)length >= sizeof(boundary) || output(boundary, length)) return -1;
    static const char hex[] = "0123456789abcdef";
    for (unsigned offset = 0; offset < size; offset += 256) {
        unsigned count = size - offset < 256 ? size - offset : 256;
        char line[768];
        int header = snprintf(line, sizeof(line), "TOMACCO_STACKSHOT_DATA tick=%u pid=%d offset=%u hex=", tick, pid, offset);
        if (header < 0 || header > 200) return -1;
        for (unsigned i = 0; i < count; ++i) {
            line[header + i * 2] = hex[data[offset + i] >> 4];
            line[header + i * 2 + 1] = hex[data[offset + i] & 15];
        }
        line[header + count * 2] = '\n';
        if (output(line, header + count * 2 + 1)) return -1;
    }
    length = snprintf(boundary, sizeof(boundary), "TOMACCO_STACKSHOT_END tick=%u pid=%d bytes=%u crc32=%08x\n", tick, pid, size, ~crc);
    if (length < 0 || (unsigned)length >= sizeof(boundary) || output(boundary, length)) return -1;
    return 0;
}

void tomacco_observe_processes(unsigned tick, int graphics_pid) {
    /* Exactly two scheduled observations; selected processes plus kernel only. */
    if (tick != 60 && tick != 120) return;
    if (diagnostic_fd < 0) {
        /* Dedicated, local UART; the host captures QEMU's second serial port.
         * Raw mode prevents terminal transformations. No TCP listener or daemon.
         * O_RDWR | O_NONBLOCK | O_NOFOLLOW | O_NOCTTY avoids open-time blocking.
         */
        int port = open("/dev/cu.qemuport0", 0x20106);
        struct tomacco_termios settings;
        if (port < 0 || tcgetattr(port, &settings)) {
            dprintf(2, "TOMACCO_PROCESS diagnostic_uart_unavailable errno=%d\n", *__error());
            if (port >= 0) close(port);
            return;
        }
        cfmakeraw(&settings);
        if (tcsetattr(port, 0, &settings)) {
            dprintf(2, "TOMACCO_PROCESS diagnostic_uart_configuration_failed errno=%d\n", *__error());
            close(port);
            return;
        }
        diagnostic_fd = port;
        dprintf(2, "TOMACCO_PROCESS diagnostic_uart=/dev/cu.qemuport0\n");
    }
    backpressure_retries = 0;
    static void *system;
    if (!system) system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    if (!system) { dprintf(diagnostic_fd, "TOMACCO_PROCESS library_unavailable\n"); return; }
    /* This getter only samples the atomic boot stage. kern.exclaves_status
     * would wait for boot completion and deadlock this diagnostic on failure.
     * Resolve dynamically so older guests still produce their stackshots. */
    typedef int (*sysctl_fn)(const char *, void *, unsigned long *, const void *, unsigned long);
    sysctl_fn query = (sysctl_fn)dlsym(system, "sysctlbyname");
    if (query) {
        unsigned stage = 0;
        unsigned long size = sizeof(stage);
        int result = query("kern.exclaves_boot_stage", &stage, &size, 0, 0);
        if (result == 0 && size == sizeof(stage))
            dprintf(diagnostic_fd, "TOMACCO_PROCESS tick=%u exclaves_boot_stage=%u\n", tick, stage);
        else
            dprintf(diagnostic_fd, "TOMACCO_PROCESS tick=%u exclaves_boot_stage_result=%d errno=%d bytes=%lu\n",
                    tick, result, result ? *__error() : 0, size);
    } else {
        dprintf(diagnostic_fd, "TOMACCO_PROCESS tick=%u exclaves_boot_stage_symbol_unavailable\n", tick);
    }
#define BIND(type, name) type name = (type)dlsym(system, #name); \
    if (!name) { dprintf(diagnostic_fd, "TOMACCO_PROCESS missing_symbol=%s\n", #name); return; }
    typedef int (*list_fn)(void *, int);
    typedef int (*path_fn)(int, void *, unsigned);
    typedef int (*info_fn)(int, int, unsigned long long, void *, int);
    typedef void *(*create_fn)(void);
    typedef int (*pid_fn)(void *, int);
    typedef int (*flags_fn)(void *, unsigned long long);
    typedef int (*config_fn)(void *);
    typedef const unsigned char *(*buffer_fn)(void *);
    typedef unsigned (*size_fn)(void *);
    BIND(list_fn, proc_listallpids);
    BIND(path_fn, proc_pidpath);
    BIND(info_fn, proc_pidinfo);
    BIND(create_fn, stackshot_config_create);
    BIND(pid_fn, stackshot_config_set_pid);
    BIND(flags_fn, stackshot_config_set_flags);
    BIND(config_fn, stackshot_capture_with_config);
    BIND(buffer_fn, stackshot_config_get_stackshot_buffer);
    BIND(size_fn, stackshot_config_get_stackshot_size);
    BIND(config_fn, stackshot_config_dealloc);
#undef BIND
    const char *targets[] = {
        "/sbin/launchd", "/tomacco/launchd.apple",
        "/System/Library/CoreServices/SpringBoard.app/SpringBoard",
        "/usr/libexec/backboardd", "/usr/libexec/usermanagerd", "/usr/libexec/runningboardd"
    };
    int pids[1024];
    int count = proc_listallpids(pids, sizeof(pids));
    if (count < 0 || count > 1024) {
        dprintf(diagnostic_fd, "TOMACCO_PROCESS enumeration_failed result=%d errno=%d\n", count, *__error());
        return;
    }
    dprintf(diagnostic_fd, "TOMACCO_PROCESS sample_tick=%u processes=%d possibly_truncated=%d\n", tick, count, count == 1024);
    if (graphics_pid > 0) {
        unsigned present = 0;
        for (int i = 0; i < count; ++i) if (pids[i] == graphics_pid) present = 1;
        dprintf(diagnostic_fd, "TOMACCO_PROCESS owned_probe_tick=%u pid=%d present=%u\n", tick, graphics_pid, present);
    }
    unsigned selected = 0, captured = 0, failed = 0;
    for (int i = -1; i < count && selected < 9; ++i) {
        int pid = i < 0 ? 0 : pids[i];
        char path[4096] = {0};
        if (i < 0) {
            snprintf(path, sizeof(path), "kernel_task");
        } else {
            if (pid <= 0) continue;
            int path_size = proc_pidpath(pid, path, sizeof(path));
            path[sizeof(path) - 1] = 0;
            if (pid == graphics_pid)
                dprintf(diagnostic_fd, "TOMACCO_PROCESS owned_probe_tick=%u pid=%d path_bytes=%d errno=%d path=%s\n",
                        tick, pid, path_size, path_size > 0 ? 0 : *__error(), path);
            if (path_size <= 0) continue;
            unsigned target;
            for (target = 0; target < sizeof(targets) / sizeof(targets[0]); ++target)
                if (!strcmp(path, targets[target])) break;
            if (target == sizeof(targets) / sizeof(targets[0]) &&
#if TOMACCO_APPLICATION_DIAGNOSTICS
                    strcmp(path, "/tomacco/TomaccoProbe.app/TomaccoProbe") &&
#endif
                    !(pid == graphics_pid && !strcmp(path, "/tomacco/graphics-probe"))) continue;
        }
        ++selected;
        struct tomacco_task_info info = {0};
        int info_size = proc_pidinfo(pid, 4, 0, &info, sizeof(info));
        dprintf(diagnostic_fd, "TOMACCO_PROCESS tick=%u pid=%d path=%s info_bytes=%d threads=%d running=%d resident_bytes=%llu\n",
                tick, pid, path, info_size, info.threads, info.running, info.resident_size);
        void *config = stackshot_config_create();
        if (!config) { ++failed; dprintf(diagnostic_fd, "TOMACCO_PROCESS config_allocation_failed\n"); continue; }
        int result = stackshot_config_set_pid(config, pid);
        /* KCData, load addresses, dispatch queues and actual kernel wait owners. */
        if (!result) result = stackshot_config_set_flags(config, 0x1010003ull | (pid == 0 ? 8 : 0));
        if (!result) result = stackshot_capture_with_config(config);
        dprintf(diagnostic_fd, "TOMACCO_PROCESS tick=%u pid=%d stackshot_result=%d\n", tick, pid, result);
        int transfer = 0;
        if (!result)
            transfer = emit_stackshot(tick, pid, stackshot_config_get_stackshot_buffer(config),
                                     stackshot_config_get_stackshot_size(config));
        stackshot_config_dealloc(config);
        if (transfer) {
            dprintf(2, "TOMACCO_PROCESS diagnostic_transfer_failed tick=%u pid=%d\n", tick, pid);
            return;
        }
        if (result) ++failed;
        else ++captured;
    }
    char summary[160];
    int length = snprintf(summary, sizeof(summary), "TOMACCO_PROCESS sample_tick=%u selected=%u captured=%u failed=%u finished=1\n",
                          tick, selected, captured, failed);
    if (length < 0 || (unsigned)length >= sizeof(summary) || output(summary, length))
        dprintf(2, "TOMACCO_PROCESS diagnostic_summary_failed tick=%u\n", tick);
}
