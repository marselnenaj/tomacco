/* An opt-in guest bootstrap. Configure each invocation of
 * com.apple.mobile.installd of this boot through launchd's service
 * configuration protocol: it stays the service's controller, because installd
 * exits when idle and an app can be installed at any time of a boot.
 * Never change the global environment, original job, sealed OS or shared cache.
 */
#include "guest-development-library.h"
#include "guest-development-policy.h"
#include "guest-development-protocol.h"
#include "guest-service-state.h"
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int dprintf(int, const char *, ...);
extern int snprintf(char *, unsigned long, const char *, ...);
extern int open(const char *, int, ...), openat(int, const char *, int, ...);
extern int mkdirat(int, const char *, unsigned short), close(int), fsync(int);
extern int unlinkat(int, const char *, int);
extern long read(int, void *, unsigned long), write(int, const void *, unsigned long);
extern long long lseek(int, long long, int);
extern char *dlerror(void);
extern int *__error(void);
extern unsigned getuid(void), sleep(unsigned), alarm(unsigned);
extern int getpid(void);
extern void *malloc(unsigned long);
extern void free(void *);
typedef void *object;
enum { XPC_SERVICE_NOT_FOUND = 113 };
/* Accessed by main and the native service notification queue. */
enum { WAITING, ARMED, CONFIGURING, SUBMITTED, FAILED };
static unsigned configuration_state;
static int first_instance_pid;

static int fail(const char *phase, long error) {
    dprintf(1, "{\"event\":\"development-setup\",\"state\":\"failed\",\"phase\":\"%s\",\"error\":%ld}\n", phase, error);
    return 69;
}

struct service_query_api {
    object (*dictionary)(const char *const *, const object *, unsigned long);
    void (*u64)(object, const char *, unsigned long long);
    void (*string)(object, const char *, const char *);
    void (*boolean)(object, const char *, unsigned char);
    void (*set)(object, const char *, object);
    object (*get)(object, const char *);
    object (*type)(object);
    void (*release)(object);
    object (*shmem)(void *, unsigned long);
    unsigned long long (*get_u64)(object, const char *);
    long long (*get_error)(object, const char *);
    int (*routine)(unsigned long long, object, object *);
    void *(*map)(void *, unsigned long, int, int, int, long long);
    int (*unmap)(void *, unsigned long);
    object dictionary_type, integer_type, unsigned_type;
};

static int service_state(const struct service_query_api *api, unsigned long long domain,
                         unsigned long long handle, const char *specifier,
                         struct tomacco_service_state *state) {
    /* The original service-print routine writes to a caller-owned shared
     * mapping and reports bytes-written in its XPC reply. No output pipe can
     * fill while the serial controller queue waits for that reply. */
    const unsigned long capacity = 1ul << 20;
    void *buffer = api->map(0, capacity, 3, 0x1001, -1, 0); /* ANON | SHARED */
    if (buffer == (void *)-1) return *__error();
    object memory = api->shmem(buffer, capacity);
    object request = api->dictionary(0, 0, 0), reply = 0;
    int result = 12;
    if (memory && request) {
        api->u64(request, "type", domain); api->u64(request, "handle", handle);
        api->string(request, "name", "com.apple.mobile.installd");
        api->boolean(request, "unidomain", 1);
        api->set(request, "shmem", memory);
        result = api->routine(0x2c4, request, &reply);
        if (!result) {
            result = 100; /* Darwin EPROTO: absent, mistyped or incomplete reply. */
            if (reply && api->type(reply) == api->dictionary_type) {
                object error = api->get(reply, "error"), written = api->get(reply, "bytes-written");
                if ((!error || (api->type(error) == api->integer_type && !api->get_error(reply, "error"))) &&
                    written && api->type(written) == api->unsigned_type) {
                    unsigned long long count = api->get_u64(reply, "bytes-written");
                    if (count && count < capacity && tomacco_service_state_read(buffer, (unsigned long)count,
                                                                              specifier, state)) result = 0;
                }
            }
        }
    }
    if (reply) api->release(reply);
    if (request) api->release(request);
    if (memory) api->release(memory);
    api->unmap(buffer, capacity);
    return result;
}

/* The running phone's own policy table (guest-development-protocol.h): empty at
 * each boot, because the trust-cache entries it goes with are the boot's too.
 * The control service runs as mobile and writes it. */
static int create_hot_policy(void) {
    extern int chown(const char *, unsigned, unsigned), chmod(const char *, unsigned short);
    int file = open(TOMACCO_HOT_POLICY, 0x701, 0644); /* WRONLY|CREAT|TRUNC|NOFOLLOW */
    if (file < 0) return fail("hot-policy", *__error());
    close(file);
    if (chown(TOMACCO_HOT_POLICY, 501, 501) || chmod(TOMACCO_HOT_POLICY, 0644)) return fail("hot-policy-owner", *__error());
    return 0;
}

static int copy_library(void) {
    /* installd's file-system sandbox maps an executable from installd's own
     * cache but not from the Cryptex supplemental volume,
     * so the adapter is copied into that cache and inserted from there. Walk every
     * destination component through a no-follow directory FD; only the final owned
     * directory and content-addressed library are created. */
    int parent = open("/", 0x100104); /* O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK */
    if (parent < 0) return fail("root", *__error());
    const char *parts[] = {"private", "var", "installd", "Library", "Caches", "Tomacco"};
    for (unsigned i = 0; i < sizeof(parts) / sizeof(parts[0]); ++i) {
        if (i == 5 && mkdirat(parent, parts[i], 0755) && *__error() != 17) {
            int error = *__error(); close(parent); return fail("library-directory", error);
        }
        int next = openat(parent, parts[i], 0x100104);
        int error = *__error();
        close(parent);
        if (next < 0) return fail("library-parent", error);
        parent = next;
    }
    int source = open(TOMACCO_LIBRARY_SOURCE, 0x104);
    if (source < 0) { int error = *__error(); close(parent); return fail("library-source", error); }
    unsigned char *bytes = malloc(TOMACCO_LIBRARY_BYTES + 1);
    if (!bytes) { close(source); close(parent); return fail("memory", 12); }
    unsigned long used = 0;
    while (used <= TOMACCO_LIBRARY_BYTES) {
        long size = read(source, bytes + used, TOMACCO_LIBRARY_BYTES + 1 - used);
        if (size < 0 && *__error() == 4) continue;
        if (size < 0) { used = 0; break; }
        if (!size) break;
        used += (unsigned long)size;
    }
    close(source);
    unsigned char hash[32];
    object crypto = dlopen("/usr/lib/system/libcommonCrypto.dylib", 2);
    unsigned char *(*sha)(const void *, unsigned, unsigned char *) = dlsym(crypto, "CC_SHA256");
    int valid = used == TOMACCO_LIBRARY_BYTES && sha && sha(bytes, (unsigned)used, hash);
    if (valid) for (unsigned i = 0; i < sizeof(hash); ++i) if (hash[i] != tomacco_library_sha256[i]) valid = 0;
    if (!valid) { free(bytes); close(parent); return fail("library-integrity", 0); }
    int existing = openat(parent, TOMACCO_LIBRARY_NAME, 0x104);
    if (existing >= 0) {
        unsigned long offset = 0; unsigned char buffer[4096]; int equal = 1;
        for (;;) {
            long n = read(existing, buffer, sizeof(buffer));
            if (n < 0 && *__error() == 4) continue;
            if (n < 0 || (n > 0 && (unsigned long)n > used - offset)) { equal = 0; break; }
            if (!n) break;
            for (long i = 0; i < n; ++i) if (buffer[i] != bytes[offset + (unsigned long)i]) equal = 0;
            offset += (unsigned long)n;
        }
        close(existing); free(bytes); close(parent);
        return equal && offset == used ? 0 : fail("existing-library-integrity", 0);
    }
    if (*__error() != 2) { int error = *__error(); free(bytes); close(parent); return fail("existing-library", error); }
    int target = openat(parent, TOMACCO_LIBRARY_NAME ".new", 0xb05, 0644); /* EXCL, CREAT, NOFOLLOW */
    if (target < 0) { int error = *__error(); free(bytes); close(parent); return fail("library-create", error); }
    unsigned long offset = 0;
    int saved = 0;
    while (offset < used) {
        long n = write(target, bytes + offset, used - offset);
        if (n < 0 && *__error() == 4) continue;
        if (n <= 0) { saved = n < 0 ? *__error() : 5; break; }
        offset += (unsigned long)n;
    }
    free(bytes);
    if (offset == used && fsync(target)) saved = *__error();
    if (offset != used || saved) {
        close(target); unlinkat(parent, TOMACCO_LIBRARY_NAME ".new", 0); close(parent);
        return fail("library-write", saved);
    }
    close(target);
    /* The content-addressed destination is not replaced. linkat publishes the
     * completed file exclusively even if a second creator appears. */
    int (*link_file)(int, const char *, int, const char *, int) = dlsym(dlopen("/usr/lib/libSystem.B.dylib", 2), "linkat");
    int result = link_file ? link_file(parent, TOMACCO_LIBRARY_NAME ".new", parent, TOMACCO_LIBRARY_NAME, 0) : -1;
    saved = link_file ? *__error() : 78;
    unlinkat(parent, TOMACCO_LIBRARY_NAME ".new", 0); close(parent);
    return result ? fail("library-publish", saved) : 0;
}

#include "guest-development-probe.h"
#ifdef TOMACCO_PROBE_LEGACY_SOURCE
/* Scratch experiment only (built with --probe-legacy-library): load self-tests
 * that compare the current adapter with 367's, each from its Cryptex path and
 * from a fresh copy in installd's cache, before installd is configured. Every
 * dlopen runs in its own forked child: the two adapters share an install name,
 * and a second dlopen in one process could return the first image. The full
 * dlerror is logged in escaped chunks, so the reason at its end is never cut. */
extern int fork(void), waitpid(int, int *, int), unlink(const char *);
extern void _exit(int);
static void probe_log_text(const char *which, const char *text) {
    unsigned length = 0;
    while (text[length]) ++length;
    for (unsigned at = 0; at < length; at += 160) {
        char chunk[161 * 2];
        unsigned used = 0;
        for (unsigned i = at; i < length && i < at + 160; ++i) {
            char c = text[i];
            if (c == '"' || c == '\\') { chunk[used++] = '\\'; chunk[used++] = c; }
            else chunk[used++] = (c >= 0x20 && c < 0x7f) ? c : '?';
        }
        chunk[used] = 0;
        dprintf(1, "{\"event\":\"development-setup\",\"state\":\"load-probe-error\",\"which\":\"%s\",\"offset\":%u,\"text\":\"%s\"}\n",
                which, at, chunk);
    }
}
static void probe_load(const char *which, const char *path) {
    int child = fork();
    if (child == 0) {
        int file = open(path, 0x104); /* RDONLY|NOFOLLOW|NONBLOCK */
        long long size = file >= 0 ? lseek(file, 0, 2) : -1;
        if (file >= 0) close(file);
        void *image = dlopen(path, 2); /* RTLD_NOW */
        const char *error = image ? "" : dlerror();
        dprintf(1, "{\"event\":\"development-setup\",\"state\":\"load-probe\",\"which\":\"%s\",\"path\":\"%s\",\"size\":%lld,\"dlopen\":%s}\n",
                which, path, size, image ? "true" : "false");
        if (!image && error) probe_log_text(which, error);
        _exit(0);
    }
    int status = 0;
    if (child > 0) waitpid(child, &status, 0);
    else dprintf(1, "{\"event\":\"development-setup\",\"state\":\"load-probe\",\"which\":\"%s\",\"fork_errno\":%d}\n", which, *__error());
}
static void probe_copy(const char *from, const char *to) {
    /* A fresh file each boot: remove an earlier experiment's copy first. */
    unlink(to);
    int source = open(from, 0x104), target = open(to, 0xb01, 0644); /* WRONLY|CREAT|EXCL|NOFOLLOW */
    unsigned char buffer[16384];
    long n = 0, total = 0;
    while (source >= 0 && target >= 0 && (n = read(source, buffer, sizeof(buffer))) > 0)
        if (write(target, buffer, (unsigned long)n) == n) total += n; else break;
    if (target >= 0) { fsync(target); close(target); }
    if (source >= 0) close(source);
    dprintf(1, "{\"event\":\"development-setup\",\"state\":\"load-probe-copy\",\"to\":\"%s\",\"bytes\":%ld,\"errno\":%d}\n",
            to, total, source < 0 || target < 0 ? *__error() : 0);
}
#endif

int main(int argc, char **argv) {
    (void)argv;
    if (argc != 1 || getuid()) return fail("root-required", 1);
    alarm(660);
    dprintf(1, "{\"event\":\"development-setup\",\"state\":\"starting\",\"phase\":\"library-integrity\"}\n");
    if (copy_library() || create_hot_policy()) return 69;
#ifdef TOMACCO_PROBE_LEGACY_SOURCE
    probe_load("table-cryptex", TOMACCO_LIBRARY_SOURCE);
    probe_load("legacy-cryptex", TOMACCO_PROBE_LEGACY_SOURCE);
    probe_load("table-cache-installd-copy", TOMACCO_LIBRARY_DESTINATION);
    probe_copy(TOMACCO_LIBRARY_SOURCE, TOMACCO_PROBE_TABLE_COPY);
    probe_load("table-cache-fresh", TOMACCO_PROBE_TABLE_COPY);
    probe_copy(TOMACCO_PROBE_LEGACY_SOURCE, TOMACCO_PROBE_LEGACY_COPY);
    probe_load("legacy-cache-fresh", TOMACCO_PROBE_LEGACY_COPY);
#endif
    object library = dlopen("/usr/lib/system/libxpc.dylib", 2);
    object (*dictionary)(const char *const *, const object *, unsigned long) = dlsym(library, "xpc_dictionary_create");
    void (*u64)(object, const char *, unsigned long long) = dlsym(library, "xpc_dictionary_set_uint64");
    void (*string)(object, const char *, const char *) = dlsym(library, "xpc_dictionary_set_string");
    void (*boolean)(object, const char *, unsigned char) = dlsym(library, "xpc_dictionary_set_bool");
    int (*routine)(unsigned long long, object, object *) = dlsym(library, "_xpc_service_routine");
    long long (*error)(object, const char *) = dlsym(library, "xpc_dictionary_get_int64");
    object (*get)(object, const char *) = dlsym(library, "xpc_dictionary_get_value");
    object (*type)(object) = dlsym(library, "xpc_get_type");
    object dictionary_type = dlsym(library, "_xpc_type_dictionary");
    object integer_type = dlsym(library, "_xpc_type_int64");
    void (*release)(object) = dlsym(library, "xpc_release");
    object (*service_create)(const char *, object) = dlsym(library, "xpc_service_create_from_specifier");
    void (*attach_handler)(object, void (^)(object)) = dlsym(library, "xpc_service_set_attach_handler");
    int (*instance_pid)(object) = dlsym(library, "xpc_service_instance_get_pid");
    unsigned char (*configurable)(object) = dlsym(library, "xpc_service_instance_is_configurable");
    void (*instance_environment)(object, object) = dlsym(library, "xpc_service_instance_set_environment");
    void (*instance_dup2)(object, int, int) = dlsym(library, "xpc_service_instance_dup2");
    void (*instance_run)(object) = dlsym(library, "xpc_service_instance_run");
    object dispatch = dlopen("/usr/lib/system/libdispatch.dylib", 2);
    object (*queue_create)(const char *, object) = dlsym(dispatch, "dispatch_queue_create");
    void (*queue_sync)(object, void (^)(void)) = dlsym(dispatch, "dispatch_sync");
    if (!dictionary || !u64 || !string || !boolean || !routine || !error ||
        !get || !type || !dictionary_type || !integer_type || !release || !service_create ||
        !attach_handler || !instance_pid || !configurable || !instance_environment ||
        !instance_dup2 || !instance_run || !queue_create || !queue_sync)
        return fail("service-api-unavailable", 0);
    object request = dictionary(0, 0, 0), environment = dictionary(0, 0, 0);
    if (!request || !environment) return fail("memory", 12);
    string(request, "name", "com.apple.mobile.installd");
    unsigned long long domain = 1, handle = 0;
    /* Modern iOS moves services into the foreground user's domain. Resolve
     * that owner through read-only existence requests before configuring the
     * service; the helper's root UID does not identify the service's domain. */
    for (unsigned attempt = 0; attempt < 2; ++attempt) {
        u64(request, "type", domain); u64(request, "handle", handle);
        object probe = 0;
        int result = routine(0x2c8, request, &probe);
        int valid = probe && type(probe) == dictionary_type;
        object field = valid ? get(probe, "error") : 0;
        if (field && type(field) != integer_type) valid = 0;
        long long status = result ? result : valid ? error(probe, "error") : 0;
        if (probe) release(probe);
        if (!result && !valid) return fail("service-query-response", 0);
        if (!status) break;
        if (status != XPC_SERVICE_NOT_FOUND || attempt) return fail("service-query", status);
        unsigned char (*sessions_enabled)(void) = dlsym(library, "xpc_user_sessions_enabled");
        unsigned (*foreground_uid)(int *) = dlsym(library, "xpc_user_sessions_get_foreground_uid");
        if (!sessions_enabled || !foreground_uid || !sessions_enabled())
            return fail("service-domain-unavailable", status);
        int uid_error = 0;
        unsigned uid = foreground_uid(&uid_error);
        if (uid_error || uid == ~0u) return fail("service-domain-uid", uid_error);
        domain = 2; handle = uid;
    }
    dprintf(1, "{\"event\":\"development-setup\",\"state\":\"starting\",\"phase\":\"service-domain\",\"type\":%llu,\"handle\":%llu}\n", domain, handle);
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    const struct service_query_api query = {
        dictionary, u64, string, boolean, dlsym(library, "xpc_dictionary_set_value"), get, type, release,
        dlsym(library, "xpc_shmem_create"), dlsym(library, "xpc_dictionary_get_uint64"), error, routine,
        dlsym(system, "mmap"), dlsym(system, "munmap"), dictionary_type, integer_type,
        dlsym(library, "_xpc_type_uint64")
    };
    if (!query.set || !query.shmem || !query.get_u64 || !query.map || !query.unmap || !query.unsigned_type)
        return fail("service-state-api-unavailable", 0);
    int (*socket_pair)(int, int, int, int *) = dlsym(system, "socketpair");
    long (*receive)(int, void *, unsigned long, int) = dlsym(system, "recv");
    int acknowledgement[2];
    if (!socket_pair || !receive) return fail("activation-channel-api", 0);
    if (socket_pair(1, 2, 0, acknowledgement)) return fail("activation-channel", *__error());
    string(environment, "DYLD_INSERT_LIBRARIES", TOMACCO_LIBRARY_DESTINATION);
    const int adapter_socket = acknowledgement[1];
    char specifier[128];
    if (domain == 1) snprintf(specifier, sizeof(specifier), "system/com.apple.mobile.installd");
    else snprintf(specifier, sizeof(specifier), "user/%llu/com.apple.mobile.installd", handle);
    const char *service_specifier = specifier;
    object queue = queue_create("io.github.marselnenaj.tomacco.development-controller", 0);
    if (!queue) return fail("controller-queue", 12);
    object service = service_create(specifier, queue);
    if (!service) return fail("service-controller-create", 0);
    dprintf(1, "{\"event\":\"development-setup\",\"state\":\"starting\",\"phase\":\"service-controller\"}\n");
    /* libxpc owns the controller's receive right, send-once registration
     * reply and death notification. Configuration is authorized by this
     * controller's PID and exec generation, not by a standalone 0x2c1 call.
     * Registration does not kickstart or stop the service. */
    attach_handler(service, ^(object instance) {
        int pid = instance_pid(instance);
        int can_configure = configurable(instance) != 0;
        dprintf(1, "{\"event\":\"development-setup\",\"state\":\"controller-attached\",\"pid\":%d,\"configurable\":%s}\n", pid, can_configure ? "true" : "false");
        unsigned state = __atomic_load_n(&configuration_state, __ATOMIC_ACQUIRE);
        /* PID zero acknowledges a dormant job, not a configurable instance.
         * Do not manufacture a request for it. The caller can now issue its
         * explicit install; launchd holds the real instance for this callback. */
        if (!pid && !can_configure) {
            if (state == WAITING) {
                __atomic_store_n(&configuration_state, ARMED, __ATOMIC_RELEASE);
                dprintf(1, "{\"event\":\"development-setup\",\"state\":\"armed\",\"service\":\"com.apple.mobile.installd\"}\n");
            }
            return;
        }
        if (pid > 0 && first_instance_pid == pid) return;
        if (first_instance_pid && (pid <= 0 || !can_configure)) return;
        if (pid <= 0 || !can_configure) {
            fail("service-already-running", pid);
            __atomic_store_n(&configuration_state, FAILED, __ATOMIC_RELEASE);
            return;
        }
        if (state == FAILED) {
            /* Consuming the native instance without overrides releases its
             * configuration hold. */
            instance_run(instance);
            return;
        }
        if (first_instance_pid) {
            /* installd exited when idle and launchd starts it again. The next
             * install then needs the adapter as well. The acknowledgement
             * belongs to the first instance only. */
            instance_environment(instance, environment);
            instance_dup2(instance, 1, 2);
            instance_run(instance);
            dprintf(1, "{\"event\":\"development-setup\",\"state\":\"configuration-repeated\",\"pid\":%d}\n", pid);
            return;
        }
        first_instance_pid = pid;
        __atomic_store_n(&configuration_state, CONFIGURING, __ATOMIC_RELEASE);
        dprintf(1, "{\"event\":\"development-setup\",\"state\":\"starting\",\"phase\":\"service-configuration\"}\n");
        instance_environment(instance, environment);
        instance_dup2(instance, 1, 2);
        instance_dup2(instance, adapter_socket, TOMACCO_DEVELOPMENT_ACK_FD);
        /* Record exactly what this instance is launched with, so a silent adapter
         * can be told apart from a mis-set injection: the DYLD_INSERT_LIBRARIES
         * path, the adapter's own fd 2 (mapped to this helper's stdout) and the
         * acknowledgement fd it should answer on. */
        dprintf(1, "{\"event\":\"development-setup\",\"state\":\"configuration-detail\",\"pid\":%d,"
                   "\"insert_library\":\"%s\",\"adapter_stderr_fd\":2,\"ack_fd\":%d}\n",
                pid, TOMACCO_LIBRARY_DESTINATION, TOMACCO_DEVELOPMENT_ACK_FD);
        /* This consumes libxpc's PID-bound request and retained descriptors.
         * The API returns void; returning from it is not an acceptance receipt. */
        instance_run(instance);
        __atomic_store_n(&configuration_state, SUBMITTED, __ATOMIC_RELEASE);
        dprintf(1, "{\"event\":\"development-setup\",\"state\":\"configuration-submitted\",\"pid\":%d,\"scope\":\"next-invocation\",\"policy\":\"owned-cdhash\"}\n", pid);
    });
    /* Wait for the constructor's receipt over the inherited private socket:
     * xpcproxy consumed the startup configuration and the adapter is mapped.
     * Human-readable log lines are not synchronization or activation proof. */
    int activated = 0;
    for (unsigned i = 0; i < 600; ++i) {
        if (__atomic_load_n(&configuration_state, __ATOMIC_ACQUIRE) == FAILED) break;
        if (!activated) {
            unsigned char packet[TOMACCO_DEVELOPMENT_ACK_BYTES + 1];
            long size = receive(acknowledgement[0], packet, sizeof(packet), 0x80); /* MSG_DONTWAIT */
            if (size >= 0) {
                activated = tomacco_development_ack_decode(packet, (unsigned long)size, tomacco_policy_digest);
                if (activated != 1) { fail("library-activation-receipt", activated); break; }
            } else if (*__error() != 35 && *__error() != 4) {
                fail("activation-channel-read", *__error()); break;
            }
        }
        /* Retain an early constructor receipt until the callback finishes. */
        if (activated == 1 && __atomic_load_n(&configuration_state, __ATOMIC_ACQUIRE) == SUBMITTED) break;
        sleep(1);
    }
    close(acknowledgement[0]);
    close(acknowledgement[1]);
    unsigned state = __atomic_load_n(&configuration_state, __ATOMIC_ACQUIRE);
    if (state == FAILED) return 69;
    if (state != SUBMITTED) return fail("service-controller-timeout", 60);
    if (activated != 1) return fail("library-activation-unconfirmed", 60);
    /* Native evidence that this process is what launchd holds as the controller. */
    struct tomacco_service_state observed = {0};
    int observed_error = service_state(&query, domain, handle, service_specifier, &observed);
    dprintf(1, "{\"event\":\"development-setup\",\"state\":\"activated\",\"controller_released\":false,\"resident\":true,"
               "\"observed_error\":%d,\"observed_controller\":%d,\"controller\":%d}\n",
            observed_error, observed.controller_pid, getpid());
    /* The rest of the boot: libxpc's queue answers each new instance. */
    alarm(0);
    for (;;) sleep(86400);
}
