/* Experimental full-system bootstrap. Used only in a copied research image.
 * Calls the real guest libSystem and Apple mount utility; no emulated success.
 * Guest ABI declarations avoid an Apple SDK dependency for this small program.
 */
extern int open(const char *, int, ...);
extern int close(int);
extern int dup2(int, int);
extern int dprintf(int, const char *, ...);
extern int fork(void);
extern int execve(const char *, char *const *, char *const *);
extern int waitpid(int, int *, int);
extern int kill(int, int);
extern unsigned sleep(unsigned);
extern void _exit(int);
extern int mkdir(const char *, unsigned short);
extern int chmod(const char *, unsigned short);
extern int chown(const char *, unsigned, unsigned);
extern int *__error(void);
extern long write(int, const void *, unsigned long);
extern long read(int, void *, unsigned long);
extern int fsync(int);
extern int unlink(const char *);
#if TOMACCO_DATA_APFS
extern int unmount(const char *, int);
#endif
extern int symlink(const char *, const char *);
extern int strcmp(const char *, const char *);
extern int snprintf(char *, unsigned long, const char *, ...);
extern long long lseek(int, long long, int);
extern void *opendir(const char *);
extern int closedir(void *);

/* Darwin's 64-bit inode dirent ABI (bsd/sys/dirent.h). */
struct darwin_dirent {
    unsigned long long inode, seekoff;
    unsigned short reclen, namlen;
    unsigned char type;
    char name[1024];
};
extern struct darwin_dirent *readdir(void *);

struct tomacco_seed {
    const char *path;
    unsigned kind, mode, uid, gid;
    const unsigned char *data;
    unsigned long size;
};
/* Generated from this firmware's original data template, never distributed. */
#include "research-data-seed.h"
#if TOMACCO_PROCESS_DIAGNOSTICS
extern void tomacco_observe_processes(unsigned, int);
#endif
#if TOMACCO_DATA_APFS
extern int tomacco_data_volume(char *, unsigned long, char *, unsigned long);
#if TOMACCO_RESTART_PROBE
static int data_reused;
#endif
#endif

static int write_all(int fd, const void *bytes, unsigned long size) {
    const char *cursor = bytes;
    while (size) {
        long count = write(fd, cursor, size);
        if (count < 0 && *__error() == 4) continue;
        if (count <= 0) return -1;
        cursor += count;
        size -= count;
    }
    return 0;
}

static int directory(const char *path, unsigned short mode, unsigned owner) {
    int result = mkdir(path, mode);
    int error = result ? *__error() : 0;
    int ownership = chown(path, owner, owner);
    int permissions = chmod(path, mode);
    dprintf(2, "TOMACCO_INIT directory=%s mkdir=%d errno=%d chown=%d chmod=%d\n",
            path, result, error, ownership, permissions);
    return (result && error != 17) || ownership || permissions ? -1 : 0;
}

static int seed_data(void) {
    unsigned failed = 0;
    for (unsigned i = 0; i < sizeof(tomacco_seeds) / sizeof(tomacco_seeds[0]); ++i) {
        const struct tomacco_seed *entry = &tomacco_seeds[i];
        int result = -1;
        if (entry->kind == 1) {
            result = mkdir(entry->path, entry->mode);
            if (result && *__error() == 17) result = 0;
        } else if (entry->kind == 2) {
            int fd = open(entry->path, 0x601, entry->mode);
            if (fd >= 0) {
                result = write_all(fd, entry->data, entry->size);
                if (close(fd)) result = -1;
            }
        } else if (entry->kind == 3) {
            result = symlink((const char *)entry->data, entry->path);
        }
        if (!result && entry->kind != 3) {
            result = chown(entry->path, entry->uid, entry->gid);
            if (!result) result = chmod(entry->path, entry->mode);
        }
        if (result) {
            ++failed;
            dprintf(2, "TOMACCO_INIT seed_failed=%s errno=%d\n", entry->path, *__error());
        }
    }
    dprintf(2, "TOMACCO_INIT data_seed_entries=%lu failed=%u\n",
            sizeof(tomacco_seeds) / sizeof(tomacco_seeds[0]), failed);
    return failed ? -1 : 0;
}

struct observed_file { char path[2048]; long long offset; };
static struct observed_file observed[128];
static unsigned observed_count;

static void dump_file(const char *path) {
    unsigned index = 0;
    while (index < observed_count && strcmp(observed[index].path, path)) ++index;
    if (index == 128) return;
    /* O_NOFOLLOW prevents logs from redirecting this root observer elsewhere. */
    int fd = open(path, 0x100);
    if (fd < 0) return;
    if (index == observed_count) {
        snprintf(observed[index].path, sizeof(observed[index].path), "%s", path);
        ++observed_count;
    }
    if (lseek(fd, observed[index].offset, 0) < 0) { close(fd); return; }
    char buffer[2048];
    unsigned total = 0;
    while (total < 65536) {
        long size = read(fd, buffer, sizeof(buffer));
        if (size <= 0) break;
        if (!total) dprintf(2, "\nTOMACCO_GUEST_FILE_BEGIN %s offset=%lld\n", path, observed[index].offset);
        if (write_all(2, buffer, size)) break;
        observed[index].offset += size;
        total += size;
    }
    if (total) dprintf(2, "\nTOMACCO_GUEST_FILE_END %s bytes=%u\n", path, total);
    close(fd);
}

static void dump_directory(const char *path, unsigned depth) {
    if (depth > 3) return;
    void *dir = opendir(path);
    if (!dir) return;
    struct darwin_dirent *entry;
    unsigned count = 0;
    while (count++ < 256 && (entry = readdir(dir))) {
        if (!strcmp(entry->name, ".") || !strcmp(entry->name, "..")) continue;
        char file[2048];
        int size = snprintf(file, sizeof(file), "%s/%s", path, entry->name);
        if (size < 0 || (unsigned)size >= sizeof(file)) continue;
        if (entry->type == 4) dump_directory(file, depth + 1);
        else if (entry->type == 8) dump_file(file);
    }
    closedir(dir);
}

static void observe(void) {
    long long previous_persona_size = -1;
    for (unsigned i = 0; i < 30; ++i) {
        sleep(5);
        dprintf(2, "TOMACCO_OBSERVER tick=%u\n", (i + 1) * 5);
        dump_file("/private/var/logs/usermanagerd_init.log");
        dump_file("/private/var/logs/usermanagerd.log");
        dump_file("/private/var/logs/tomacco-usermanagerd.log");
        dump_file("/private/var/mobile/tmp/tomacco-backboardd.log");
        dump_file("/private/var/mobile/tmp/tomacco-SpringBoard.log");
        dump_directory("/private/var/mobile/Library/Logs/CrashReporter", 0);
        dump_directory("/private/var/root/Library/Logs/CrashReporter", 0);
        /* Observe Apple's own saved manifest without printing its contents.
         * Existence/size alone does not qualify a successful persona reload.
         */
        int fd = open("/private/var/keybags/persona.kb", 0x100);
        if (fd >= 0) {
            long long size = lseek(fd, 0, 2);
            if (size != previous_persona_size) {
                dprintf(2, "TOMACCO_OBSERVER persona_manifest_bytes=%lld\n", size);
                previous_persona_size = size;
            }
            close(fd);
        }
    }
    _exit(0);
}

static int run_setup_tool(const char *stage, char **args, char **envp) {
    int child = fork();
    if (child == 0) {
        execve(args[0], args, envp);
        dprintf(2, "TOMACCO_INIT %s exec errno=%d\n", stage, *__error());
        _exit(127);
    }
    if (child < 0) {
        dprintf(2, "TOMACCO_INIT %s fork errno=%d\n", stage, *__error());
        return -1;
    }
    int status = 0, complete = 0;
    for (unsigned second = 0; second < 30; ++second) {
        int result = waitpid(child, &status, 1);
        if (result == child) { complete = 1; break; }
        if (result < 0 && *__error() != 4) break;
        sleep(1);
    }
    if (!complete) { kill(child, 9); waitpid(child, &status, 0); }
    dprintf(2, "TOMACCO_INIT %s complete=%d status=%d\n", stage, complete, status);
    return complete && status == 0 ? 0 : -1;
}

#if TOMACCO_DATA_APFS
static int mount_data(char **envp) {
    char device[160] = {0}, container[80] = {0};
    if (tomacco_data_volume(device, sizeof(device), container, sizeof(container)) != 1) return -1;
    char *args[] = {"/sbin/mount_apfs", "-o", "rw", device, "/private/var", 0};
    return run_setup_tool("mount", args, envp);
}
#endif

static int prepare_data(char **envp) {
#if TOMACCO_DATA_APFS
    /* Only for a disposable full-system research image. Refuse to overwrite
     * an existing Data volume; discover its BSD device through actual IOKit.
     * Apple newfs creates the real Data role; no raw metadata writes here.
     * The container is RAM-backed, so this does not provide persistence.
     */
    char device[160] = {0}, container[80] = {0};
    int existing = tomacco_data_volume(device, sizeof(device), container, sizeof(container));
#if TOMACCO_RESTART_PROBE
    /* The isolated storage probe may reopen its own Data volume. It must then
     * read the previous boot's marker, never replace or recreate it. */
    if (existing == 1) {
        data_reused = 1;
        return mount_data(envp);
    }
#endif
    if (existing != 0) {
        dprintf(2, "TOMACCO_INIT data_volume_preflight=%d\n", existing);
        return -1;
    }
    /* iOS newfs uses lowercase d for Data (0x40); uppercase D is 0x280. */
    char *create_args[] = {"/sbin/newfs_apfs", "-A", "-v", "Data", "-R", "d", container, 0};
    if (run_setup_tool("create_data", create_args, envp)) return -1;
    return mount_data(envp);
#else
    char *mount_args[] = {"/sbin/mount_tmpfs", "-s", "2147483648", "/private/var", 0};
    return run_setup_tool("mount", mount_args, envp);
#endif
}

/* Require real binary I/O before starting Apple's launchd. In APFS mode,
 * unmount/remount forces the second read through a fresh filesystem mount.
 * This qualifies only the disposable RAM-backed volume, never persistence
 * across an emulator restart or the security of an actual iPhone.
 */
static int check_data_bytes(const char *path, const unsigned char *expected, unsigned long size) {
    unsigned char bytes[4097];
    int fd = open(path, 0x100); /* O_RDONLY | O_NOFOLLOW */
    if (fd < 0) return -1;
    unsigned long total = 0;
    int failed = 0;
    while (total < sizeof(bytes)) {
        long count = read(fd, bytes + total, sizeof(bytes) - total);
        if (count < 0 && *__error() == 4) continue;
        if (count < 0) { failed = 1; break; }
        if (!count) break;
        total += count;
    }
    if (close(fd)) failed = 1;
    if (total != size) failed = 1;
    if (!failed)
        for (unsigned long i = 0; i < size; ++i)
            if (bytes[i] != expected[i]) { failed = 1; break; }
    dprintf(2, "TOMACCO_INIT data_readback bytes=%lu expected=%lu matched=%d\n", total, size, !failed);
    return failed ? -1 : 0;
}

static int write_data_probe(const char *path, const unsigned char *bytes, unsigned long size) {
    /* O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW: never overwrite a file. */
    int fd = open(path, 0xb01, 0600);
    if (fd < 0) {
        dprintf(2, "TOMACCO_INIT data_probe_open errno=%d\n", *__error());
        return -1;
    }
    int failed = write_all(fd, bytes, size);
    int synced;
    do { synced = fsync(fd); } while (synced < 0 && *__error() == 4);
    int closed = close(fd);
    if (synced || closed) failed = -1;
    dprintf(2, "TOMACCO_INIT data_write bytes=%lu fsync=%d complete=%d\n", size, synced, !failed);
    return failed;
}

static int verify_data(char **envp) {
    (void)envp;
    unsigned char bytes[4096];
    for (unsigned long i = 0; i < sizeof(bytes); ++i) bytes[i] = (i * 37 + i / 256 + 17) & 255;
#if TOMACCO_RESTART_PROBE
    const char *path = "/private/var/.tomacco-restart-probe";
    if (!data_reused && write_data_probe(path, bytes, sizeof(bytes))) return -1;
#else
    const char *path = "/private/var/.tomacco-data-probe";
    if (write_data_probe(path, bytes, sizeof(bytes))) return -1;
#endif
    if (check_data_bytes(path, bytes, sizeof(bytes))) return -1;
#if TOMACCO_DATA_APFS
    int result = unmount("/private/var", 0);
    dprintf(2, "TOMACCO_INIT data_unmount result=%d errno=%d\n", result, result ? *__error() : 0);
    if (result || mount_data(envp) || check_data_bytes(path, bytes, sizeof(bytes))) return -1;
    dprintf(2, "TOMACCO_INIT data_remount_readback_verified bytes=%lu\n", sizeof(bytes));
#endif
#if TOMACCO_RESTART_PROBE
    /* Leave the marker on a cleanly unmounted filesystem. The host must stop
     * this engine and start a different process before claiming persistence. */
    int final_unmount = unmount("/private/var", 0);
    dprintf(2, "TOMACCO_INIT data_final_unmount result=%d errno=%d\n",
            final_unmount, final_unmount ? *__error() : 0);
    if (final_unmount) return -1;
    dprintf(2, "TOMACCO_INIT restart_probe_complete reused=%d bytes=%lu\n", data_reused, sizeof(bytes));
#else
    if (unlink(path)) return -1;
    dprintf(2, "TOMACCO_INIT data_io_verified bytes=%lu persistent=0\n", sizeof(bytes));
#endif
    return 0;
}

int main(int argc, char **argv, char **envp) {
    (void)argc;
    int console = open("/dev/console", 2);
    if (console >= 0) {
        for (int fd = 0; fd < 3; ++fd) dup2(console, fd);
        if (console > 2) close(console);
    }
    dprintf(2, "TOMACCO_INIT begin\n");
    int setup_failed = prepare_data(envp) || verify_data(envp);
#if TOMACCO_RESTART_PROBE
    if (!setup_failed) for (;;) sleep(60);
#endif
    if (setup_failed || directory("/private/var/keybags", 0700, 0) ||
            directory("/private/var/logs", 0755, 0)) {
        dprintf(2, "TOMACCO_INIT data_setup_failed; launchd not started\n");
        /* Exiting PID 1 obscures this diagnostic with another kernel panic.
         * The host research harness owns the deadline and tears down the VM.
         */
        for (;;) sleep(60);
    }
    seed_data();
    directory("/private/var/mobile/tmp", 0700, 501);
#if TOMACCO_APPLICATION_DIAGNOSTICS
    /* Only the dedicated research UART becomes accessible to the mobile job.
     * Starting the app before launchd would inherit no bootstrap namespace.
     */
    int app_uart = chown("/dev/cu.qemuport1", 501, 501);
    if (!app_uart) app_uart = chmod("/dev/cu.qemuport1", 0600);
    dprintf(2, "TOMACCO_INIT application_uart=%d errno=%d\n", app_uart, app_uart ? *__error() : 0);
#endif
#if TOMACCO_GRAPHICS_DIAGNOSTICS
    int graphics_observer = fork();
    if (graphics_observer == 0) {
        /* CoreFoundation/IOKit setup can start threads. Never load Objective-C
         * frameworks in that fork's inherited runtime: execute a fresh process.
         */
        char *graphics_args[] = {"/tomacco/graphics-probe", 0};
        execve(graphics_args[0], graphics_args, envp);
        dprintf(2, "TOMACCO_INIT graphics_exec_failed errno=%d\n", *__error());
        _exit(127);
    }
    dprintf(2, "TOMACCO_INIT graphics_observer=%d\n", graphics_observer);
#elif TOMACCO_PROCESS_DIAGNOSTICS
    const int graphics_observer = -1;
#endif
#if TOMACCO_PROCESS_DIAGNOSTICS
    /* A noisy console/crash report must not postpone process sampling.
     * Like the log observer, this bounded child is reaped by the original PID 1.
     */
    int process_observer = fork();
    if (process_observer == 0) {
        for (unsigned tick = 60; tick <= 120; tick += 60) {
            unsigned remaining = 60;
            while (remaining) remaining = sleep(remaining);
            tomacco_observe_processes(tick, graphics_observer);
        }
        _exit(0);
    }
    dprintf(2, "TOMACCO_INIT process_observer=%d errno=%d\n", process_observer,
            process_observer < 0 ? *__error() : 0);
#endif
    int observer = fork();
    if (observer == 0) observe();
    dprintf(2, "TOMACCO_INIT observer=%d\n", observer);
    dprintf(2, "TOMACCO_INIT exec original launchd\n");
    argv[0] = "/sbin/launchd";
    execve("/tomacco/launchd.apple", argv, envp);
    dprintf(2, "TOMACCO_INIT launchd exec errno=%d\n", *__error());
    for (;;) sleep(60);
}
