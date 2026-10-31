/* Native application control inside an owned iOS development guest.
 * Calls the guest's actual LaunchServices implementation. No replacement
 * application database, activation response, or simulated launch result.
 * The private API boundary is checked against Objective-C method encodings.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int dprintf(int, const char *, ...);
extern int snprintf(char *, unsigned long, const char *, ...);
extern void _exit(int);
extern long write(int, const void *, unsigned long);
extern int strcmp(const char *, const char *);
extern char *strerror(int);
extern unsigned long strlen(const char *);
extern unsigned sleep(unsigned);
extern unsigned alarm(unsigned);
extern unsigned getuid(void);
extern int getpid(void);
extern int *__error(void);
extern int open(const char *, int, ...);
extern int openat(int, const char *, int, ...);
extern void *malloc(unsigned long);
extern void free(void *);
extern int close(int);
extern int fsync(int);
extern int rename(const char *, const char *);
extern int renamex_np(const char *, const char *, unsigned);
extern int unlink(const char *);
extern int mkdir(const char *, unsigned short);
extern long read(int, void *, unsigned long);
extern long long lseek(int, long long, int);
extern int dup(int);
extern int dup2(int, int);
extern int kill(int, int);
extern void (*signal(int, void (*)(int)))(int);
#include "guest-buffer-log.h"
#include "guest-surface-log.h"
#include "guest-development-protocol.h"

/* The prefix of the owned launchd jobs' logs and of this service's own files
 * (build_ios_guest_tools.py GUEST_IDENTIFIER). */
#define TOMACCO_GUEST_LOGS "/private/var/mobile/Library/Logs/io.github.marselnenaj.tomacco."
#define TOMACCO_RESEARCH_APPLICATION "io.github.marselnenaj.tomacco.research-app"

typedef void *object;
static object (*get_class)(const char *);
static object (*selector)(const char *);
static void (*message)(void);
static object (*object_class)(object);
static object (*instance_method)(object, object);
static unsigned (*argument_count)(object);
static void (*return_type)(object, char *, unsigned long);
static void (*argument_type)(object, unsigned, char *, unsigned long);
static const char *request_id;
static const char *installation_identifier;
static int record_mutation;
static int record_result;
/* Darwin's sig_atomic_t is int. A one-way transition before publishing a
 * definitive response prevents a late signal from replacing it with unknown. */
static volatile int terminal_response_started;
static char timeout_receipt[512], cancel_receipt[512];
static unsigned long timeout_length, cancel_length;
static unsigned long installation_progress_sequence;
static int (*copy_package)(const char *, const char *, object, unsigned);
static int (*remove_package)(const char *, object, unsigned);
static const char *progress_file = TOMACCO_GUEST_LOGS "install-progress.json";

static object get(object value, const char *method) {
    return ((object (*)(object, object))message)(value, selector(method));
}

static object with(object value, const char *method, object argument) {
    return ((object (*)(object, object, object))message)(value, selector(method), argument);
}

static object string(const char *value) {
    return ((object (*)(object, object, const char *))message)(
        get_class("NSString"), selector("stringWithUTF8String:"), value);
}

static object number(unsigned long value) {
    return ((object (*)(object, object, unsigned long))message)(
        get_class("NSNumber"), selector("numberWithUnsignedLong:"), value);
}

static object boolean(unsigned char value) {
    return ((object (*)(object, object, unsigned char))message)(
        get_class("NSNumber"), selector("numberWithBool:"), value);
}

static void put(object dictionary, const char *key, object value) {
    ((void (*)(object, object, object, object))message)(dictionary,
        selector("setObject:forKey:"), value ? value : get(get_class("NSNull"), "null"), string(key));
}

static unsigned long count(object value) {
    return ((unsigned long (*)(object, object))message)(value, selector("count"));
}

/* Verify the entire call shape before crossing the private framework ABI.
 * object_class also makes this work for class methods through the metaclass.
 */
#include "guest-objc-contract.h"

static object bounded_text(object value, unsigned long limit) {
    if (!value || !((unsigned char (*)(object, object, object))message)(
            value, selector("isKindOfClass:"), get_class("NSString"))) return 0;
    unsigned long length = ((unsigned long (*)(object, object))message)(value, selector("length"));
    if (length <= limit) return value;
    unsigned short last = ((unsigned short (*)(object, object, unsigned long))message)(
        value, selector("characterAtIndex:"), limit - 1);
    if (last >= 0xd800 && last <= 0xdbff) --limit;
    return ((object (*)(object, object, unsigned long))message)(value, selector("substringToIndex:"), limit);
}

/* Preserve the OS's actual nested failure, without an unbounded userInfo dump. */
static object native_error(object error, unsigned depth) {
    if (!error || depth == 4 || !((unsigned char (*)(object, object, object))message)(
            error, selector("isKindOfClass:"), get_class("NSError"))) return 0;
    object result = get(get_class("NSMutableDictionary"), "dictionary");
    const char *keys[] = {"domain", "message"};
    const char *methods[] = {"domain", "localizedDescription"};
    for (unsigned index = 0; index < 2; ++index) {
        object value = get(error, methods[index]);
        if (!value) value = string("");
        put(result, keys[index], bounded_text(value, 1024));
    }
    long code = ((long (*)(object, object))message)(error, selector("code"));
    put(result, "code", ((object (*)(object, object, long))message)(
        get_class("NSNumber"), selector("numberWithLong:"), code));
    object info = get(error, "userInfo");
    const char *path_keys[] = {"NSFilePath", "NSSourceFilePathErrorKey", "NSDestinationFilePathErrorKey"};
    const char *path_names[] = {"file_path", "source_path", "destination_path"};
    for (unsigned index = 0; index < 3; ++index) {
        object value = bounded_text(with(info, "objectForKey:", string(path_keys[index])), 1024);
        if (value) put(result, path_names[index], value);
    }
    const char *detail_keys[] = {"NSDebugDescription", "NSLocalizedFailureReason", "NSLocalizedRecoverySuggestion", "BSErrorCodeDescription"};
    const char *detail_names[] = {"debug_description", "failure_reason", "recovery_suggestion", "code_description"};
    for (unsigned index = 0; index < 4; ++index) {
        object value = bounded_text(with(info, "objectForKey:", string(detail_keys[index])), 1024);
        if (value) put(result, detail_names[index], value);
    }
    object underlying = native_error(with(info, "objectForKey:", string("NSUnderlyingError")), depth + 1);
    if (underlying) put(result, "underlying", underlying);
    return result;
}

static int write_bytes(int fd, const char *bytes, unsigned long length) {
    while (length) {
        unsigned long chunk = length > 16384 ? 16384 : length;
        long written = write(fd, bytes, chunk);
        if (written < 0 && *__error() == 4) continue;
        if (written <= 0 || (unsigned long)written > chunk) return 74;
        bytes += written;
        length -= (unsigned long)written;
    }
    return 0;
}

static void save_result(const char *bytes, unsigned long length) {
    if (!record_result || !request_id || length > 786432) return;
    const char *temporary = TOMACCO_GUEST_LOGS "last-operation.tmp";
    const char *destination = TOMACCO_GUEST_LOGS "last-operation.jsonl";
    /* Single owned worker. Atomic replacement retains the last complete
     * framework result even when its requesting host has disconnected.
     * Parent status/log/cancel requests never replace this receipt. */
    int file = open(temporary, 0x1000701, 0600); /* WRONLY|CREAT|TRUNC|NOFOLLOW|CLOEXEC */
    if (file < 0) return;
    int failed = write_bytes(file, bytes, length) || write_bytes(file, "\n", 1) || fsync(file);
    if (close(file)) failed = 1;
    if (failed || rename(temporary, destination)) unlink(temporary);
}

static void interrupted_worker(int signum) {
    if (terminal_response_started) _exit(signum == 14 ? 124 : 143);
    /* Pre-encoded before entering Foundation. All calls here are POSIX
     * async-signal-safe; never invoke Objective-C from a signal handler.
     * A second alarm also bounds a blocked receipt/stream write. */
    signal(14, 0);
    alarm(2);
    const char *bytes = signum == 14 ? timeout_receipt : cancel_receipt;
    unsigned long length = signum == 14 ? timeout_length : cancel_length;
    save_result(bytes, length);
    static const char reply_prefix[] = "TOMACCO_APP_CONTROL ";
    write_bytes(1, reply_prefix, sizeof(reply_prefix) - 1);
    write_bytes(1, bytes, length);
    write_bytes(1, "\n", 1);
    _exit(signum == 14 ? 124 : 143);
}

/* Guest-time bound on one framework worker. Host waits remain short and
 * cancellable; these bounds use guest time. First-boot installation may
 * initialize several OS services. SpringBoard answers the screen query on its
 * main thread, which icon regeneration under this machine's emulation can hold
 * for minutes; a query waiting that long would keep the only framework worker
 * and turn every later request into guest_operation_busy, so it ends well
 * inside a host wait and reports a timeout instead. */
static unsigned worker_deadline_seconds(const char *operation) {
    if (!strcmp(operation, "install") || !strcmp(operation, "receive_bundle")) return 600;
    if (!strcmp(operation, "screen_state")) return 20;
    return 120;
}

static void prepare_worker_deadlines(const char *operation) {
    char identity[64] = {0};
    if (request_id) snprintf(identity, sizeof(identity), ",\"request_id\":\"%s\"", request_id);
    char *receipts[] = {timeout_receipt, cancel_receipt};
    const char *errors[] = {"guest_worker_timeout", "guest_worker_cancelled"};
    unsigned long *lengths[] = {&timeout_length, &cancel_length};
    for (unsigned index = 0; index < 2; ++index) {
        int size = snprintf(receipts[index], sizeof(timeout_receipt),
            "{\"format\":1,\"operation\":\"%s\",\"ok\":false,\"error\":\"%s\","
            "\"request_may_have_executed\":true,\"automatic_retry_safe\":%s,"
            "\"pid\":%d,\"uid\":%u%s}", operation, errors[index],
            record_mutation && strcmp(operation, "auto_lock") ? "false" : "true",
            getpid(), getuid(), identity);
        if (size <= 0 || (unsigned long)size >= sizeof(timeout_receipt)) _exit(64);
        *lengths[index] = (unsigned long)size;
    }
    signal(14, interrupted_worker);
    signal(15, interrupted_worker);
    alarm(worker_deadline_seconds(operation));
}

static int emit(object row) {
    object error = 0;
    object data = ((object (*)(object, object, object, unsigned long, object *))message)(
        get_class("NSJSONSerialization"), selector("dataWithJSONObject:options:error:"), row, 0, &error);
    unsigned long length = ((unsigned long (*)(object, object))message)(data, selector("length"));
    const char *bytes = get(data, "bytes");
    if (error || !bytes || !length || length > 1048576) return 74;
    if (record_result) {
        /* A failed/interrupted publication may leave no correlated receipt;
         * the host then still reports unknown. Never overwrite a definitive
         * receipt just because flushing to a disconnected client takes time. */
        terminal_response_started = 1;
        alarm(5);
    }
    save_result(bytes, length);
    if (dprintf(1, "TOMACCO_APP_CONTROL ") < 0) return 74;
    return write_bytes(1, bytes, length) || write_bytes(1, "\n", 1) ? 74 : 0;
}

static object response(const char *operation) {
    object row = get(get_class("NSMutableDictionary"), "dictionary");
    put(row, "format", number(1));
    put(row, "operation", string(operation));
    put(row, "pid", number((unsigned)getpid()));
    put(row, "uid", number(getuid()));
    if (request_id) put(row, "request_id", string(request_id));
    return row;
}

/* The installer delivers one object to its progress block on its serial
 * delegate queue. Our preparation updates happen before entering that API.
 * Progress has its own bounded atomic file: it never replaces a completion
 * receipt and even a native "Complete" status is not an installation result. */
static void installation_progress(const char *phase, object native) {
    if (!request_id || !record_mutation) return;
    object pool = get(get_class("NSAutoreleasePool"), "new");
    object row = response("install");
    put(row, "phase", string(phase));
    put(row, "sequence", number(++installation_progress_sequence));
    if (native) {
        if (((unsigned char (*)(object, object, object))message)(
                native, selector("isKindOfClass:"), get_class("NSDictionary"))) {
            object status = bounded_text(with(native, "objectForKey:", string("Status")), 128);
            if (status) put(row, "native_status", status);
            object percent = with(native, "objectForKey:", string("PercentComplete"));
            if (percent && ((unsigned char (*)(object, object, object))message)(
                    percent, selector("isKindOfClass:"), get_class("NSNumber"))) {
                double value = ((double (*)(object, object))message)(percent, selector("doubleValue"));
                /* Ordered comparisons also reject NaN and infinities. Never
                 * clamp a malformed OS value into plausible progress. */
                if (value >= 0.0 && value <= 100.0) {
                    put(row, "percent_complete", ((object (*)(object, object, double))message)(
                        get_class("NSNumber"), selector("numberWithDouble:"), value));
                } else put(row, "native_progress_error", string("invalid_percentage"));
            } else if (percent) put(row, "native_progress_error", string("invalid_percentage"));
        } else put(row, "native_progress_error", string("invalid_payload"));
    }
    object error = 0;
    object data = ((object (*)(object, object, object, unsigned long, object *))message)(
        get_class("NSJSONSerialization"), selector("dataWithJSONObject:options:error:"), row, 0, &error);
    unsigned long length = ((unsigned long (*)(object, object))message)(data, selector("length"));
    const char *bytes = get(data, "bytes");
    if (!error && bytes && length && length <= 4096) {
        const char *temporary = TOMACCO_GUEST_LOGS "install-progress.tmp";
        int file = open(temporary, 0x1000701, 0600);
        if (file >= 0) {
            /* Progress is observational and need not force a guest disk flush
             * per callback. Readers still see only complete atomic records. */
            int failed = write_bytes(file, bytes, length);
            if (close(file)) failed = 1;
            if (failed || rename(temporary, progress_file)) unlink(temporary);
        }
    }
    ((void (*)(object, object))message)(pool, selector("drain"));
}

static void read_installation_progress(object row) {
    int file = open(progress_file, 0x104); /* RDONLY|NOFOLLOW|NONBLOCK */
    if (file < 0) {
        if (*__error() != 2) put(row, "progress_read_error", string("progress_unavailable"));
        return;
    }
    char bytes[4097];
    unsigned long used = 0;
    int failed = 0;
    while (used < sizeof(bytes)) {
        long size = read(file, bytes + used, sizeof(bytes) - used);
        if (size < 0 && *__error() == 4) continue;
        if (size < 0) { failed = 1; break; }
        if (!size) break;
        used += (unsigned long)size;
    }
    close(file);
    if (failed || !used || used > 4096) {
        put(row, "progress_read_error", string("invalid_progress_record"));
        return;
    }
    object data = ((object (*)(object, object, const void *, unsigned long))message)(
        get_class("NSData"), selector("dataWithBytes:length:"), bytes, used);
    object error = 0;
    object progress = ((object (*)(object, object, object, unsigned long, object *))message)(
        get_class("NSJSONSerialization"), selector("JSONObjectWithData:options:error:"), data, 0, &error);
    if (error || !((unsigned char (*)(object, object, object))message)(
            progress, selector("isKindOfClass:"), get_class("NSDictionary"))) {
        put(row, "progress_read_error", string("invalid_progress_record"));
        return;
    }
    put(row, "installation_progress", progress);
}

/* Report installd's development-signing adapter activation state, read from the
 * tail of the setup helper's log, so a start can wait for the adapter before it
 * installs a staged ad-hoc app (an install sent before activation is rejected
 * as an invalid signature). A plain bounded file read: no framework or XPC
 * dependency, safe in the service process that answers status. */
static void read_development_signing_activation(object row) {
    put(row, "development_signing_activation_supported", boolean(1));
    int file = open(TOMACCO_GUEST_LOGS "development-signing.jsonl", 0x104); /* RDONLY|NOFOLLOW|NONBLOCK */
    if (file < 0) {
        put(row, "development_signing_activation", string(*__error() == 2 ? "absent" : "unreadable"));
        return;
    }
    char bytes[8192];
    long long end = lseek(file, 0, 2); /* SEEK_END */
    if (end < 0) { close(file); put(row, "development_signing_activation", string("unreadable")); return; }
    if (lseek(file, end > (long long)sizeof(bytes) ? end - (long long)sizeof(bytes) : 0, 0) < 0) { /* SEEK_SET */
        close(file); put(row, "development_signing_activation", string("unreadable")); return;
    }
    unsigned long used = 0;
    int failed = 0;
    while (used < sizeof(bytes)) {
        long size = read(file, bytes + used, sizeof(bytes) - used);
        if (size < 0 && *__error() == 4) continue;
        if (size < 0) { failed = 1; break; }
        if (!size) break;
        used += (unsigned long)size;
    }
    close(file);
    if (failed) { put(row, "development_signing_activation", string("unreadable")); return; }
    int state = tomacco_development_activation_scan(bytes, used);
    put(row, "development_signing_activation", string(
        state == TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED ? "activated" :
        state == TOMACCO_DEVELOPMENT_ACTIVATION_FAILED ? "failed" :
        state == TOMACCO_DEVELOPMENT_ACTIVATION_PENDING ? "pending" : "absent"));
}

static int failure(object row, const char *code) {
    put(row, "ok", boolean(0));
    put(row, "error", string(code));
    int result = emit(row);
    return result ? result : 69;
}

static object filesystem_error(int error, const char *source, const char *destination) {
    object row = get(get_class("NSMutableDictionary"), "dictionary");
    put(row, "domain", string("NSPOSIXErrorDomain"));
    put(row, "code", number((unsigned)error));
    put(row, "message", bounded_text(string(strerror(error)), 1024));
    if (source) put(row, "source_path", bounded_text(string(source), 1024));
    if (destination) put(row, "destination_path", bounded_text(string(destination), 1024));
    return row;
}

static void cleanup_staging(object directory, object row) {
    const char *path = get(directory, "UTF8String");
    /* Only the exclusively reserved directory for this request reaches here.
     * REMOVEFILE_RECURSIVE neither follows links nor crosses mount points.
     * No Foundation URL metadata or file-manager logging is needed. */
    int removed = path && !remove_package(path, 0, 1);
    int saved = path ? *__error() : 22;
    /* installd may already have consumed or removed the handoff directory.
     * Check after a failed removal so a concurrent cleanup is idempotent.
     * Only actual ENOENT establishes absence; permission errors do not. */
    int file = !removed && path ? open(path, 0x100104) : -1;
    if (!removed && file < 0 && path && *__error() == 2) {
        put(row, "staging_retained", boolean(0));
        return;
    }
    if (file >= 0) close(file);
    put(row, "staging_retained", boolean(!removed));
    if (!removed) put(row, "staging_cleanup_error", filesystem_error(saved, path, 0));
}

static object stage_package(object source, object row, object *directory) {
    /* installd is sandboxed and cannot read Supplemental Cryptex paths.
     * PublicStaging is the regular AFC/installation-proxy handoff location.
     * Prepare outside that OS-managed directory, then publish a complete
     * package atomically. A boot-time staging cleanup must not see a partial
     * copy. The OS still verifies the copied bundle or archive itself. */
    const char *base = "/private/var/mobile/Media/PublicStaging";
    if (mkdir(base, 0700) && *__error() != 17) {
        put(row, "errno", number((unsigned)*__error()));
        failure(row, "staging_unavailable");
        return 0;
    }
    /* NSFileManager attributesOfItemAtPath initializes CoreServices file
     * metadata and its logging connection. A no-follow directory open checks
     * exactly the filesystem property that the staging transaction needs. */
    int staging = open(base, 0x100104); /* DIRECTORY | NOFOLLOW | NONBLOCK */
    if (staging < 0) {
        int saved = *__error();
        put(row, "native_error", filesystem_error(saved, base, 0));
        failure(row, "invalid_staging_directory");
        return 0;
    }
    close(staging);
    const char *identity = request_id ? request_id :
        get(get(get(get_class("NSUUID"), "UUID"), "UUIDString"), "UTF8String");
    char reserved[256], published[256];
    int length = identity ? snprintf(reserved, sizeof(reserved),
        "/private/var/mobile/Media/.Tomacco-%s", identity) : -1;
    int public_length = identity ? snprintf(published, sizeof(published), "%s/Tomacco-%s", base, identity) : -1;
    if (length <= 0 || (unsigned long)length >= sizeof(reserved) ||
        public_length <= 0 || (unsigned long)public_length >= sizeof(published)) {
        failure(row, "invalid_staging_identity");
        return 0;
    }
    if (mkdir(reserved, 0700)) {
        put(row, "errno", number((unsigned)*__error()));
        failure(row, "staging_reservation_failed");
        return 0;
    }
    *directory = string(reserved);
    unsigned char archive = ((unsigned char (*)(object, object, object))message)(
        source, selector("hasSuffix:"), string(".ipa"));
    object destination = with(*directory, "stringByAppendingPathComponent:",
                              string(archive ? "Package.ipa" : "Package.app"));
    put(row, "staging_path", destination);
    put(row, "staging_retained", boolean(1));
    put(row, "staging_published", boolean(0));
    installation_progress("staging-copy", 0);
    const char *from = get(source, "UTF8String"), *to = get(destination, "UTF8String");
    /* Darwin copyfile.h: ALL | RECURSIVE | EXCL | NOFOLLOW. Preserve data,
     * modes, ACLs, extended attributes and symlinks, never replace a target or
     * move the source. The original installer still validates the package. */
    unsigned flags = 0xf | (1u << 15) | (1u << 17) | (1u << 18) | (1u << 19);
    if (copy_package(from, to, 0, flags)) {
        int saved = *__error();
        put(row, "native_error", filesystem_error(saved, from, to));
        cleanup_staging(*directory, row);
        failure(row, "staging_copy_failed");
        return 0;
    }
    /* Darwin RENAME_EXCL (bsd/sys/stdio.h): never replace an existing path.
     * Media and PublicStaging must share a filesystem; EXDEV is a real refusal,
     * never a reason to expose a partial copy in the public handoff directory. */
    installation_progress("staging-publish", 0);
    if (renamex_np(reserved, published, 4)) {
        put(row, "errno", number((unsigned)*__error()));
        cleanup_staging(*directory, row);
        failure(row, "staging_publish_failed");
        return 0;
    }
    *directory = string(published);
    destination = with(*directory, "stringByAppendingPathComponent:",
                       string(archive ? "Package.ipa" : "Package.app"));
    put(row, "staging_path", destination);
    put(row, "staging_published", boolean(1));
    return with(get_class("NSURL"), "fileURLWithPath:", destination);
}

static int valid_installation_identifier(const char *, const char *);

static object installation_bundle_identifier(object row, const char *path) {
    /* NSBundle's bundleIdentifier initializes the main-bundle loading logger
     * and can synchronously wait on logd during first boot. Installation only
     * needs the package's Info.plist identity. Parse that bounded file with
     * the original plist implementation (binary and XML), without creating
     * a bundle or consulting the registration database. */
    int directory = open(path, 0x100104); /* DIRECTORY | NOFOLLOW | NONBLOCK */
    if (directory < 0) { put(row, "errno", number((unsigned)*__error())); failure(row, "bundle_directory_unavailable"); return 0; }
    int file = openat(directory, "Info.plist", 0x104);
    int saved = *__error(); close(directory);
    if (file < 0) { put(row, "errno", number((unsigned)saved)); failure(row, "bundle_metadata_unavailable"); return 0; }
    const unsigned long limit = 1ul << 20;
    unsigned char *bytes = malloc(limit + 1);
    if (!bytes) { close(file); failure(row, "bundle_metadata_memory"); return 0; }
    unsigned long used = 0;
    saved = 0;
    while (used <= limit) {
        long size = read(file, bytes + used, limit + 1 - used);
        if (size < 0 && *__error() == 4) continue;
        if (size < 0) { saved = *__error(); break; }
        if (!size) break;
        used += (unsigned long)size;
    }
    close(file);
    if (saved || !used || used > limit) {
        free(bytes);
        if (saved) put(row, "errno", number((unsigned)saved));
        failure(row, "invalid_bundle_metadata"); return 0;
    }
    object data = ((object (*)(object, object, const void *, unsigned long))message)(
        get_class("NSData"), selector("dataWithBytes:length:"), bytes, used);
    free(bytes);
    if (!data) { failure(row, "bundle_metadata_memory"); return 0; }
    object error = 0;
    object info = ((object (*)(object, object, object, unsigned long, unsigned long *, object *))message)(
        get_class("NSPropertyListSerialization"), selector("propertyListWithData:options:format:error:"), data, 0, 0, &error);
    if (error || !((unsigned char (*)(object, object, object))message)(info,
        selector("isKindOfClass:"), get_class("NSDictionary"))) {
        if (error) put(row, "native_error", native_error(error, 0));
        failure(row, "invalid_bundle_metadata"); return 0;
    }
    object identifier = with(info, "objectForKey:", string("CFBundleIdentifier"));
    if (!((unsigned char (*)(object, object, object))message)(identifier,
        selector("isKindOfClass:"), get_class("NSString"))) {
        failure(row, "invalid_application_identity"); return 0;
    }
    const char *name = get(identifier, "UTF8String");
    if (!name || !valid_installation_identifier("install", name)) {
        failure(row, "invalid_application_identity"); return 0;
    }
    return identifier;
}

/* installd replaces an app's bundle but leaves a running copy alone; iOS 27's
 * own installers hold a RunningBoard assertion that both ends the app and
 * keeps it from launching until installd releases it. This tool holds no such
 * assertion, so it ends the app once installd has finished instead: not ended,
 * the old process runs on after the upgrade, and ended before the request,
 * FrontBoard relaunches it a minute later into the half-replaced bundle (exit
 * 22: no bundle identifier). Ended after the definitive completion, the next launch is the installed copy. The
 * outcome is recorded and never changes the install's verdict. */
static void end_running_application(object row, object identifier) {
    const char *failure_reason = 0;
    object request = 0;
    unsigned char ended = 0;
    object error = 0;
    /* -execute: is declared (out NSError **): its runtime type carries the
     * `out` qualifier, o^@, and the real API refuses ^@. */
    const char *one[] = {"@"}, *two[] = {"@", "@"}, *out[] = {"o^@"};
    object predicates = 0, contexts = 0, requests = 0, predicate = 0, context = 0;
    if (!dlopen("/System/Library/PrivateFrameworks/RunningBoardServices.framework/RunningBoardServices", 2)) {
        failure_reason = "termination_framework_unavailable";
        goto done;
    }
    predicates = get_class("RBSProcessPredicate");
    contexts = get_class("RBSTerminateContext");
    requests = get_class("RBSTerminateRequest");
    if (!accepts_types(predicates, "predicateMatchingBundleIdentifier:", '@', one, 1) ||
        !accepts_types(contexts, "defaultContextWithExplanation:", '@', one, 1) || !requests) {
        failure_reason = "incompatible_termination_api";
        goto done;
    }
    predicate = with(predicates, "predicateMatchingBundleIdentifier:", identifier);
    context = with(contexts, "defaultContextWithExplanation:", string("Tomacco replaces this app"));
    if (!predicate || !context) {
        failure_reason = "termination_request_unavailable";
        goto done;
    }
    request = get(requests, "alloc");
    if (!accepts_types(request, "initWithPredicate:context:", '@', two, 2) ||
        !accepts_types(request, "execute:", 'B', out, 1)) {
        failure_reason = "incompatible_termination_api";
        goto done; /* An uninitialized allocation is not sent anything else. */
    }
    request = ((object (*)(object, object, object, object))message)(request,
        selector("initWithPredicate:context:"), predicate, context);
    if (!request) {
        failure_reason = "termination_request_unavailable";
        goto done;
    }
    ended = ((unsigned char (*)(object, object, object *))message)(request, selector("execute:"), &error);
    ((void (*)(object, object))message)(request, selector("release"));
done:
    put(row, "running_application_ended", boolean(ended));
    if (error) put(row, "termination_error", native_error(error, 0));
    else if (failure_reason) put(row, "termination_error", string(failure_reason));
}

/* MIInstallerClient tears itself down when its XPC connection to installd
 * ends: the invalidation handler runs -_invalidateObject, which clears
 * `connection` and the progress block. A request sent after that is a message
 * to nil; nothing reaches installd and no completion ever runs. A client kept
 * from the migration round trip is closed that way (MIInstallerErrorDomain 131)
 * while the package is copied, so the install would reach nobody and the worker
 * would wait out its deadline. Each request gets a
 * client made for it, and one without a connection is refused, not awaited. */
static object connected_installer(object klass, const char *factory, void (^progress)(object)) {
    object client = with(klass, factory, (object)progress);
    if (!accepts(client, "connection", '@', 0) || !get(client, "connection")) return 0;
    return client;
}

static int install(object row, const char *argument) {
    installation_progress("preparing", 0);
    /* iOS 27 retains the old LSApplicationWorkspace install selectors as
     * errUnimp stubs. Use the actual MobileInstallation client/XPC protocol. */
    if (!dlopen("/System/Library/PrivateFrameworks/MobileInstallation.framework/MobileInstallation", 2))
        return failure(row, "installer_framework_unavailable");
    object klass = get_class("MIInstallerClient");
    const char *factory = "installerWithProgressBlock:";
    const char *factory_types[] = {"@?"};
    if (!accepts_types(klass, factory, '@', factory_types, 1)) return failure(row, "incompatible_install_api");
    void (^progress)(object) = ^(object native) { installation_progress("installer", native); };
    object client = connected_installer(klass, factory, progress);
    if (!client) return failure(row, "installer_connection_unavailable");
    const char *method = "installURL:identity:targetingDomain:withOptions:returningResultInfo:completion:";
    const char *types[] = {"@", "@", "Q", "@", "B", "@?"};
    if (!accepts_types(client, method, 'v', types, 6)) return failure(row, "incompatible_install_api");
    const char *connection_query = "systemAppMigratorHasCompleted:";
    if (!accepts_types(client, connection_query, 'v', factory_types, 1))
        return failure(row, "incompatible_installer_connection_api");
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    object (*create)(long) = dlsym(system, "dispatch_semaphore_create");
    long (*wait)(object, unsigned long long) = dlsym(system, "dispatch_semaphore_wait");
    long (*notify)(object) = dlsym(system, "dispatch_semaphore_signal");
    void (*release)(object) = dlsym(system, "dispatch_release");
    if (!create || !wait || !notify || !release) return failure(row, "installer_wait_unavailable");
    copy_package = dlsym(system, "copyfile");
    remove_package = dlsym(system, "removefile");
    if (!copy_package || !remove_package) return failure(row, "staging_api_unavailable");
    object options = get(get_class("MIInstallOptions"), "new");
    const char *bool_type[] = {"B"};
    if (!accepts_types(options, "setDeveloperInstall:", 'v', bool_type, 1))
        return failure(row, "incompatible_install_options");
    ((void (*)(object, object, unsigned char))message)(options, selector("setDeveloperInstall:"), 1);
    object value = string(argument);
    if (!value) return failure(row, "invalid_utf8_argument");
    put(row, "argument", value);
    put(row, "installer", string("MobileInstallation"));
    object requested = installation_identifier ? string(installation_identifier) : 0;
    unsigned long argument_length = strlen(argument);
    int archive = argument_length >= 4 && !strcmp(argument + argument_length - 4, ".ipa");
    object identifier = archive ? requested : installation_bundle_identifier(row, argument);
    if (!archive && !identifier) return 69; /* The metadata reader emitted its failure. */
    if (identifier && requested && !((unsigned char (*)(object, object, object))message)(
            identifier, selector("isEqualToString:"), requested))
        return failure(row, "application_identity_mismatch");
    if (!identifier) identifier = requested;
    if (!identifier) return failure(row, "application_identity_required");
    /* This iOS SPI requires the same identity in both arguments. For an IPA
     * the caller supplies its bundle ID; installd validates the actual archive.
     * No archive extraction or package verification is bypassed by this tool. */
    if (!accepts(options, "setBundleIdentifier:", 'v', 1)) return failure(row, "incompatible_install_options");
    ((void (*)(object, object, object))message)(options, selector("setBundleIdentifier:"), identifier);
    object allocation = get(get_class("MIAppIdentity"), "alloc");
    if (!accepts(allocation, "initWithBundleID:", '@', 1)) return failure(row, "incompatible_install_identity");
    object identity = with(allocation, "initWithBundleID:", identifier);
    if (!identity) return failure(row, "invalid_application_identity");
    put(row, "identifier", identifier);
    struct installation { unsigned char accepted; object error; } result = {0, 0};
    struct installation *completion = &result;
    object semaphore = create(0);
    if (!semaphore) return failure(row, "installer_wait_unavailable");
    /* The original daemon cleans its staging area before resuming its XPC
     * listener. Publishing first can lose a complete package during startup,
     * even though the rename succeeded. Establish a real, read-only round
     * trip on this same client before creating or publishing the package.
     * This queries migration state; it neither changes it nor waits for it to
     * become true. Both false and true with no NSError prove a native reply. */
    struct installer_connection { unsigned char migrated; object error; } connection = {0, 0};
    struct installer_connection *connected = &connection;
    void (^connection_reply)(unsigned char, object) = ^(unsigned char migrated, object error) {
        connected->migrated = migrated;
        connected->error = get(error, "retain");
        notify(semaphore);
    };
    installation_progress("installer-connect", 0);
    ((void (*)(object, object, void (^)(unsigned char, object)))message)(
        client, selector(connection_query), connection_reply);
    wait(semaphore, ~0ull);
    if (connection.error) {
        put(row, "native_error", native_error(connection.error, 0));
        ((void (*)(object, object))message)(connection.error, selector("release"));
        ((void (*)(object, object))message)(identity, selector("release"));
        ((void (*)(object, object))message)(options, selector("release"));
        release(semaphore);
        return failure(row, "installer_connection_failed");
    }
    put(row, "installer_connected", boolean(1));
    put(row, "system_app_migration_complete", boolean(connection.migrated));
    object staged_directory = 0;
    object url = stage_package(value, row, &staged_directory);
    if (!url) {
        release(semaphore);
        return 69; /* stage_package already emitted the actual failure. */
    }
    /* The synchronous worker owns this stack until completion. The framework
     * copies the block, and its serial callback retains the NSError for us.
     * The process alarm/cancel bound still applies to a stalled OS operation. */
    void (^finished)(unsigned char, object, object, object) =
        ^(unsigned char accepted, object records, object info, object error) {
            (void)records; (void)info;
            completion->accepted = accepted;
            completion->error = get(error, "retain");
            notify(semaphore);
        };
    /* The round trip's client can lose its connection while the package is
     * copied; the request goes through a client made for it. */
    object installer = connected_installer(klass, factory, progress);
    if (!installer) {
        ((void (*)(object, object))message)(identity, selector("release"));
        ((void (*)(object, object))message)(options, selector("release"));
        release(semaphore);
        cleanup_staging(staged_directory, row); /* iOS was never asked to read it. */
        return failure(row, "installer_connection_unavailable");
    }
    installation_progress("installer", 0);
    ((void (*)(object, object, object, object, unsigned long long, object, unsigned char,
               void (^)(unsigned char, object, object, object)))message)(
        installer, selector(method), url, identity, 1, options, 1, finished);
    wait(semaphore, ~0ull); /* MIInstallationDomainDefault = 1. */
    release(semaphore);
    ((void (*)(object, object))message)(identity, selector("release"));
    ((void (*)(object, object))message)(options, selector("release"));
    if (result.error) {
        put(row, "native_error", native_error(result.error, 0));
        ((void (*)(object, object))message)(result.error, selector("release"));
    }
    /* Only a definitive OS callback makes cleanup safe. Deadline/cancellation
     * handlers deliberately retain staging because iOS may still be reading. */
    cleanup_staging(staged_directory, row);
    if (!result.accepted) return failure(row, "installation_rejected_by_ios");
    end_running_application(row, identifier);
    put(row, "ok", boolean(1));
    put(row, "request_accepted", boolean(1));
    put(row, "application_scene_verified", boolean(0));
    return emit(row);
}

static int bind_api(void) {
    object objc = dlopen("/usr/lib/libobjc.A.dylib", 2);
    if (!objc || !dlopen("/System/Library/Frameworks/Foundation.framework/Foundation", 2)) return 0;
    get_class = dlsym(objc, "objc_getClass");
    selector = dlsym(objc, "sel_registerName");
    message = dlsym(objc, "objc_msgSend");
    object_class = dlsym(objc, "object_getClass");
    instance_method = dlsym(objc, "class_getInstanceMethod");
    argument_count = dlsym(objc, "method_getNumberOfArguments");
    return_type = dlsym(objc, "method_getReturnType");
    argument_type = dlsym(objc, "method_getArgumentType");
    return get_class && selector && message && object_class && instance_method &&
        argument_count && return_type && argument_type;
}

static int inspect(object workspace, object row) {
    const char *methods[] = {"allApplications", "registerApplication:", "openApplicationWithBundleID:"};
    object capabilities = get(get_class("NSMutableDictionary"), "dictionary");
    for (unsigned index = 0; index < 3; ++index)
        put(capabilities, methods[index], boolean(accepts(workspace, methods[index], index ? 'B' : '@', index != 0)));
    put(row, "methods", capabilities);
    if (!accepts(workspace, "allApplications", '@', 0)) return failure(row, "incompatible_workspace_api");
    object applications = get(workspace, "allApplications");
    if (!applications) return failure(row, "application_database_unavailable");
    unsigned long size = count(applications);
    if (size > 4096) return failure(row, "application_inventory_limit");
    object records = get(get_class("NSMutableArray"), "array");
    for (unsigned long index = 0; index < size; ++index) {
        object application = ((object (*)(object, object, unsigned long))message)(
            applications, selector("objectAtIndex:"), index);
        if (!accepts(application, "applicationIdentifier", '@', 0) ||
            !accepts(application, "applicationType", '@', 0) ||
            !accepts(application, "isInstalled", 'B', 0)) return failure(row, "incompatible_application_api");
        object item = get(get_class("NSMutableDictionary"), "dictionary");
        put(item, "identifier", get(application, "applicationIdentifier"));
        put(item, "type", get(application, "applicationType"));
        put(item, "installed", boolean(((unsigned char (*)(object, object))message)(application, selector("isInstalled"))));
        ((void (*)(object, object, object))message)(records, selector("addObject:"), item);
    }
    put(row, "applications", records);
    put(row, "ok", boolean(1));
    return emit(row);
}

/* Supplemental fixed-name metadata logs. Reading keeps every native refusal
 * and UID boundary explicit. A bounded read of a live append-only file is
 * not an atomic snapshot or proof that every constructor was observed. */
static object surface_log_contents(unsigned owner,unsigned limit,unsigned *bytes_read) {
    object row=get(get_class("NSMutableDictionary"),"dictionary");
    *bytes_read=0;
    put(row,"revision",string("native-surface-v4"));
    put(row,"expected_log_owner_uid",number(owner));
    put(row,"atomic_snapshot",boolean(0));
    put(row,"ok",boolean(0));
    char name[TOMACCO_SURFACE_LOG_NAME_LIMIT];
    if (!tomacco_surface_log_name(owner,name)) { put(row,"error",string("unsupported_surface_log_owner")); return row; }
    if (limit>TOMACCO_SURFACE_LOG_VIEW_LIMIT) limit=TOMACCO_SURFACE_LOG_VIEW_LIMIT;
    int directory=tomacco_buffer_log_directory();
    if (directory<0) {
        put(row,"errno",number((unsigned)*__error()));
        put(row,"error",string("surface_log_directory_unavailable")); return row;
    }
    struct tomacco_darwin_stat directory_info;
    if (fstat(directory,&directory_info) || !tomacco_surface_directory_identity(&directory_info)) {
        close(directory);put(row,"error",string("surface_log_directory_identity_unverified"));return row;
    }
    int file=openat(directory,name,0x1000104);
    int saved=*__error();close(directory);
    if (file<0) {
        put(row,"errno",number((unsigned)saved));
        put(row,"error",string("surface_log_unavailable"));return row;
    }
    struct tomacco_darwin_stat info;
    if (fstat(file,&info) || !tomacco_surface_log_identity(&info,owner)) {
        close(file);put(row,"error",string("surface_log_identity_unverified"));return row;
    }
    put(row,"file_name",string(name));
    put(row,"log_owner_uid",number(info.uid));
    put(row,"size_at_open",number((unsigned long long)info.size));
    object data=get(get_class("NSMutableData"),"data");
    char buffer[1024];unsigned total=0;int error=0;
    while (total<limit) {
        unsigned wanted=limit-total;
        if (wanted>sizeof(buffer)) wanted=sizeof(buffer);
        long size=read(file,buffer,wanted);
        if (size<0 && *__error()==4) continue;
        if (size<0) { error=*__error();break; }
        if (!size) break;
        ((void (*)(object,object,const void *,unsigned long))message)(data,
            selector("appendBytes:length:"),buffer,(unsigned long)size);
        total+=(unsigned)size;
    }
    *bytes_read=total;
    struct tomacco_darwin_stat after;
    int identity=!fstat(file,&after) && tomacco_surface_log_identity(&after,owner) && after.size>=info.size;
    close(file);
    if (error || !identity) {
        if (error) put(row,"errno",number((unsigned)error));
        put(row,"error",string(error ? "surface_log_read_failed" : "surface_log_identity_changed"));return row;
    }
    object text=((object (*)(object,object,object,unsigned long))message)(
        get(get_class("NSString"),"alloc"),selector("initWithData:encoding:"),data,4);
    if (!text) { put(row,"error",string("surface_log_invalid_utf8"));return row; }
    put(row,"text",text);
    put(row,"bytes_read",number(total));
    put(row,"truncated",boolean(after.size>(long long)total));
    put(row,"size_at_read_end",number((unsigned long long)after.size));
    put(row,"ok",boolean(1));
    ((void (*)(object,object))message)(text,selector("release"));
    return row;
}
/* Discover only canonical names in the owned diagnostic namespace. Each read
 * independently verifies the root sticky directory and a nofollow, single-link
 * regular file with its actual UID, mode and size. No caller supplies a path. */
static void surface_logs(object row) {
    unsigned owners[TOMACCO_SURFACE_LOG_OWNER_LIMIT]={0,276,501},used=3,matching=0;
    int owner_limited=0;
    object inventory=get(get_class("NSMutableDictionary"),"dictionary");
    object files=0,error=0;
    int directory=tomacco_buffer_log_directory();
    struct tomacco_darwin_stat info;
    if (directory<0) put(inventory,"error",string("surface_log_directory_unavailable"));
    else {
        if (fstat(directory,&info) || !tomacco_surface_directory_identity(&info))
            put(inventory,"error",string("surface_log_directory_identity_unverified"));
        else {
            object manager=get(get_class("NSFileManager"),"defaultManager");
            files=((object (*)(object,object,object,object *))message)(manager,
                selector("contentsOfDirectoryAtPath:error:"),string("/private/var/tmp"),&error);
            if (!files) {
                put(inventory,"error",string("surface_log_inventory_unavailable"));
                if (error) put(inventory,"native_error",get(error,"description"));
            }
        }
        close(directory);
    }
    unsigned long names=files ? count(files) : 0;
    int scan_bounded=names<=4096;
    if (scan_bounded && files) {
        files=((object (*)(object,object,object))message)(files,
            selector("sortedArrayUsingSelector:"),selector("compare:"));
        for (unsigned long k=0;k<names;++k) {
            object name=((object (*)(object,object,unsigned long))message)(files,selector("objectAtIndex:"),k);
            unsigned uid;
            if (!tomacco_surface_log_owner(get(name,"UTF8String"),&uid)) continue;
            ++matching;
            unsigned index=0;
            while (index<used && owners[index]!=uid) ++index;
            if (index==used) {
                if (used<TOMACCO_SURFACE_LOG_OWNER_LIMIT) owners[used++]=uid;
                else owner_limited=1;
            }
        }
    }
    unsigned total=0;
    object records=get(get_class("NSMutableArray"),"array");
    for (unsigned k=0;k<used && total<TOMACCO_SURFACE_LOG_TOTAL_VIEW_LIMIT;++k) {
        unsigned bytes=0;
        object record=surface_log_contents(owners[k],TOMACCO_SURFACE_LOG_TOTAL_VIEW_LIMIT-total,&bytes);
        total+=bytes;
        ((void (*)(object,object,object))message)(records,selector("addObject:"),record);
    }
    put(inventory,"directory_entries",number(names));
    put(inventory,"scan_limit",number(4096));
    put(inventory,"scan_limit_reached",boolean(!scan_bounded));
    put(inventory,"matching_names",number(matching));
    put(inventory,"selected_owners",number(used));
    put(inventory,"owner_limit",number(TOMACCO_SURFACE_LOG_OWNER_LIMIT));
    put(inventory,"owner_limit_reached",boolean(owner_limited));
    put(inventory,"bytes_read",number(total));
    put(inventory,"read_limit_bytes",number(TOMACCO_SURFACE_LOG_TOTAL_VIEW_LIMIT));
    put(inventory,"read_limit_reached",boolean(total==TOMACCO_SURFACE_LOG_TOTAL_VIEW_LIMIT));
    put(inventory,"atomic_snapshot",boolean(0));
    put(row,"surface_log_inventory",inventory);
    put(row,"surface_logs",records);
}
/* The owned log a logs argument names, or null. "quiet" is tomacco-quiet's
 * record (the services it unloaded, its locationd guard's questions, the
 * answers it waited for and the processes it ended), which is what shows whether the guard ended a hung locationd or one that
 * had not finished starting. */
static const char *log_label(const char *name) {
    return !strcmp(name, "application") ? "research-app" :
        !strcmp(name, "platform") ? "platform-inventory" :
        !strcmp(name, "control") ? "application-inventory" :
        !strcmp(name, "development") ? "development-signing" :
        !strcmp(name, "operation") ? "last-operation" :
        !strcmp(name, "quiet") ? "quiet" :
        !strcmp(name, "battery") ? "battery" :
        !strcmp(name, "debugserver") ? "debugserver" :
        !strcmp(name, "buffer_texture") ? "metal-buffer-texture" : 0;
}

static int logs(const char *name, object row) {
    const char *label = log_label(name);
    if (!label) return failure(row, "unknown_log");
    char path[256];
    if (!strcmp(name, "application"))
        snprintf(path, sizeof(path), "/private/var/mobile/Library/Logs/%s.jsonl", TOMACCO_RESEARCH_APPLICATION);
    else snprintf(path, sizeof(path), TOMACCO_GUEST_LOGS "%s.jsonl", label);
    const char *log_path = path;
    if (!strcmp(name, "application")) {
        /* Resolve only our own bundle's container through LaunchServices. This
         * may wait for lsd, so it runs in the bounded framework worker, never
         * in the service process that answers status and cancellation. */
        object klass = get_class("LSApplicationProxy");
        if (!accepts(klass, "applicationProxyForIdentifier:", '@', 1)) return failure(row, "incompatible_application_api");
        object application = with(klass, "applicationProxyForIdentifier:",
            string(TOMACCO_RESEARCH_APPLICATION));
        if (application && !accepts(application, "isInstalled", 'B', 0)) return failure(row, "incompatible_application_api");
        unsigned char installed = application &&
            ((unsigned char (*)(object, object))message)(application, selector("isInstalled"));
        if (installed) {
            if (!accepts(application, "dataContainerURL", '@', 0)) return failure(row, "incompatible_application_api");
            object directory = get(get(application, "dataContainerURL"), "path");
            object value = with(directory, "stringByAppendingPathComponent:",
                string("Library/Logs/" TOMACCO_RESEARCH_APPLICATION ".jsonl"));
            log_path = get(value, "UTF8String");
            if (!log_path || log_path[0] != '/' || strlen(log_path) > 4096)
                return failure(row, "application_container_unavailable");
            put(row, "log_source", string("application-container"));
        } else put(row, "log_source", string("bootstrap-diagnostic"));
    }
    /* Fixed owned log names, no-follow/nonblocking open, bounded memory. */
    int file;
    if (!strcmp(name,"buffer_texture")) {
        surface_logs(row);
        int directory=tomacco_buffer_log_directory();
        if (directory<0) {
            put(row,"errno",number((unsigned)*__error()));
            return failure(row,"buffer_log_directory_unavailable");
        }
        struct tomacco_darwin_stat directory_info;
        if (fstat(directory,&directory_info) || (directory_info.mode&0170000)!=0040000) {
            close(directory);
            return failure(row,"buffer_log_directory_identity_unverified");
        }
        put(row,"log_directory_uid",number(directory_info.uid));
        put(row,"log_directory_gid",number(directory_info.gid));
        put(row,"log_directory_mode",number(directory_info.mode&07777));
        put(row,"expected_log_owner_uid",number(TOMACCO_BUFFER_LOG_UID));
        file=openat(directory,TOMACCO_BUFFER_LOG_NAME,0x1000104);
        int saved=*__error();
        close(directory);
        if (file>=0) {
            struct tomacco_darwin_stat info;
            if (fstat(file,&info) || !tomacco_buffer_log_identity(&info)) {
                close(file);
                return failure(row,"buffer_log_identity_unverified");
            }
            put(row,"log_source",string("iconservices-owned-diagnostic"));
            put(row,"log_path",string(TOMACCO_BUFFER_LOG_PATH));
            put(row,"log_owner_uid",number(info.uid));
        } else *__error()=saved;
    } else file = open(log_path, 0x104);
    if (file < 0) {
        put(row, "errno", number((unsigned)*__error()));
        return failure(row, "log_unavailable");
    }
    object data = get(get_class("NSMutableData"), "data");
    char buffer[4096];
    unsigned total = 0;
    int error = 0, truncated = 0;
    /* Plain logs can expand sixfold. Larger correlated operation receipts
     * are parsed and returned as an object, avoiding a second JSON escaping
     * pass over bounded base64 crash-report content. */
    unsigned limit = !strcmp(name, "operation") ? 786432 : 131072;
    while (total <= limit) {
        long size = read(file, buffer, sizeof(buffer));
        if (size < 0 && *__error() == 4) continue;
        if (size < 0) { error = 1; break; }
        if (!size) break;
        if (total + (unsigned)size > limit) { truncated = 1; break; }
        ((void (*)(object, object, const void *, unsigned long))message)(data,
            selector("appendBytes:length:"), buffer, (unsigned long)size);
        total += (unsigned)size;
    }
    close(file);
    if (error) return failure(row, "log_read_failed");
    if (!strcmp(name, "operation") && total > 65536) {
        if (truncated) return failure(row, "operation_receipt_limit");
        object parse_error = 0;
        object receipt = ((object (*)(object, object, object, unsigned long, object *))message)(
            get_class("NSJSONSerialization"), selector("JSONObjectWithData:options:error:"), data, 0, &parse_error);
        if (!receipt || !((unsigned char (*)(object, object, object))message)(receipt,
                selector("isKindOfClass:"), get_class("NSDictionary")))
            return failure(row, "invalid_operation_receipt");
        put(row, "receipt", receipt);
        put(row, "truncated", boolean(0));
        put(row, "ok", boolean(1));
        return emit(row);
    }
    object text = ((object (*)(object, object, object, unsigned long))message)(
        get(get_class("NSString"), "alloc"), selector("initWithData:encoding:"), data, 4);
    if (!text) return failure(row, "invalid_utf8_log");
    put(row, "text", text);
    put(row, "truncated", boolean(truncated));
    put(row, "ok", boolean(1));
    ((void (*)(object, object))message)(text, selector("release"));
    return emit(row);
}

#include "guest-launch-process.h"

static int open_application(object row, const char *argument) {
    /* LaunchServices' synchronous SpringBoard wrapper replaces the original
     * error with generic code 115. Use that same original FrontBoard service
     * directly, with our own process identity and no launch-policy overrides.
     * Its completion supplies a process handle and the actual NSError. */
    if (!dlopen("/System/Library/PrivateFrameworks/FrontBoardServices.framework/FrontBoardServices", 2))
        return failure(row, "launch_framework_unavailable");
    object klass = get_class("FBSOpenApplicationService");
    if (!accepts(klass, "serviceWithDefaultShellEndpoint", '@', 0))
        return failure(row, "incompatible_launch_api");
    object service = get(klass, "serviceWithDefaultShellEndpoint");
    if (!service) return failure(row, "launch_service_unavailable");
    const char *method = "openApplication:withOptions:completion:";
    const char *types[] = {"@", "@", "@?"};
    if (!accepts_types(service, method, 'v', types, 3)) return failure(row, "incompatible_launch_api");
    klass = get_class("FBSOpenApplicationOptions");
    if (!accepts(klass, "optionsWithDictionary:", '@', 1)) return failure(row, "incompatible_launch_api");
    object options = with(klass, "optionsWithDictionary:", get(get_class("NSDictionary"), "dictionary"));
    if (!options) return failure(row, "launch_options_unavailable");
    object identifier = string(argument);
    if (!identifier) return failure(row, "invalid_utf8_argument");
    put(row, "argument", identifier);
    put(row, "launch_service", string("FrontBoard"));
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    object (*create)(long) = dlsym(system, "dispatch_semaphore_create");
    long (*wait)(object, unsigned long long) = dlsym(system, "dispatch_semaphore_wait");
    long (*notify)(object) = dlsym(system, "dispatch_semaphore_signal");
    void (*release)(object) = dlsym(system, "dispatch_release");
    if (!create || !wait || !notify || !release) return failure(row, "launch_wait_unavailable");
    object semaphore = create(0);
    if (!semaphore) return failure(row, "launch_wait_unavailable");
    /* One framework operation per worker process. Static storage keeps late or
     * duplicate callbacks from referencing a returned stack frame. Only the
     * first callback can retain an error or touch the completion semaphore. */
    static struct launch_completion completion;
    void (^finished)(object, object) = ^(object process, object error) {
        if (launch_completion_publish(&completion, process, error, launch_retain)) notify(semaphore);
    };
    ((void (*)(object, object, object, object, void (^)(object, object)))message)(
        service, selector(method), identifier, options, finished);
    /* The worker's guest-time alarm and cancellation handler remain in force;
     * an interrupted launch stays unknown and must never be replayed blindly. */
    if (wait(semaphore, ~0ull) || __atomic_load_n(&completion.state, __ATOMIC_ACQUIRE) != LAUNCH_READY) {
        put(row, "request_may_have_executed", boolean(1));
        put(row, "automatic_retry_safe", boolean(0));
        return failure(row, "launch_completion_unconfirmed");
    }
    release(semaphore);
    if (completion.error) {
        put(row, "native_error", native_error(completion.error, 0));
    }
    launch_process_observation(row, completion.process, completion.accepted);
    launch_completion_dispose(&completion, launch_release);
    put(row, "request_accepted", boolean(completion.accepted));
    put(row, "application_scene_verified", boolean(0));
    if (!completion.accepted) return failure(row, "request_rejected_by_ios");
    put(row, "ok", boolean(1));
    return emit(row);
}

#include "guest-setup.h"
#include "guest-screen-state.h"
#include "guest-display-state.h"
#include "guest-foreground-state.h"
#include "guest-graphics-state.h"
#include "guest-surface-state.h"
#include "guest-crash-reports.h"
#include "guest-persistence-probe.h"
#include "guest-icon-cache.h"
#include "guest-time-zone.h"
#include "guest-auto-lock.h"
#include "guest-host-channel.h"

static const char *service_executable(void);

static int operate(const char *operation, const char *argument) {
    object row = response(operation);
    /* Keep the long-lived transport service and unrelated framework workers
     * independent of LaunchServices' first-use initialization. */
    if ((!strcmp(operation, "inspect") || !strcmp(operation, "lookup") ||
         !strcmp(operation, "register") || (!strcmp(operation, "logs") && !strcmp(argument, "application"))) &&
        !dlopen("/System/Library/Frameworks/CoreServices.framework/CoreServices", 2))
        return failure(row, "application_framework_unavailable");
    if (!strcmp(operation, "crash_reports")) return crash_reports(row, argument);
    if (!strcmp(operation, "logs")) return logs(argument, row);
    if (!strcmp(operation, "cancel") || !strcmp(operation, "status") || !strcmp(operation, "flush"))
        return failure(row, "service_required");
    if (!strcmp(operation, "install")) return install(row, argument);
    if (!strcmp(operation, "open")) return open_application(row, argument);
    if (!strcmp(operation, "screen_state")) return screen_state(row);
    if (!strcmp(operation, "display_state")) return display_state(row);
    if (!strcmp(operation, "foreground_state")) return foreground_state(row);
    if (!strcmp(operation, "graphics_state")) return graphics_state(row);
    if (!strcmp(operation, "surface_state")) return surface_state(row, argument);
    if (!strcmp(operation, "persistence_probe")) return persistence_probe(row, argument);
    if (!strcmp(operation, "display_refresh")) return display_refresh(row);
    if (!strcmp(operation, "setup") || !strcmp(operation, "setup_finish"))
        return setup_state(row, !strcmp(operation, "setup_finish"));
    if (!strcmp(operation, "restart_ui")) return setup_restart_ui(row);
    if (!strcmp(operation, "effects")) return display_effects(row, argument);
    if (!strcmp(operation, "watchdog_scale")) return launch_watchdog_scale(row, argument);
    if (!strcmp(operation, "icon_cache")) return icon_cache(row, argument);
    if (!strcmp(operation, "time_zone")) return set_time_zone(row, argument);
    if (!strcmp(operation, "auto_lock")) return set_auto_lock(row, argument);
    if (!strcmp(operation, "receive_bundle")) return receive_bundle(row);
    if (!strcmp(operation, "policy_add")) return policy_add(row, argument);
    if (!strcmp(operation, "debug_server")) return debug_server(row, argument, service_executable());
    if (!strcmp(operation, "network_state")) return network_state(row);
    if (!strcmp(operation, "hardware_probe")) return hardware_probe(row, argument);
    if (!strcmp(operation, "gestalt")) return gestalt(row, argument);
    if (!strcmp(operation, "developer_mode")) return developer_mode(row, argument);
    if (!strcmp(operation, "keyboard_layout")) return keyboard_layout(row);
    if (!strcmp(operation, "lookup")) {
        object klass = get_class("LSApplicationProxy");
        if (!accepts(klass, "applicationProxyForIdentifier:", '@', 1)) return failure(row, "incompatible_application_api");
        object application = with(klass, "applicationProxyForIdentifier:", string(argument));
        put(row, "identifier", string(argument));
        if (application && !accepts(application, "isInstalled", 'B', 0)) return failure(row, "incompatible_application_api");
        put(row, "installed", boolean(application &&
            ((unsigned char (*)(object, object))message)(application, selector("isInstalled"))));
        /* The app's extensions as LaunchServices registered them from
         * installd's record: the database PlugInKit (pkd) discovers them in.
         * Read-only; an app without extensions lists none. */
        if (application && accepts(application, "plugInKitPlugins", '@', 0)) {
            object plugins = get(application, "plugInKitPlugins");
            object records = get(get_class("NSMutableArray"), "array");
            unsigned long size = plugins && ((unsigned char (*)(object, object, object))message)(
                plugins, selector("isKindOfClass:"), get_class("NSArray")) ? count(plugins) : 0;
            for (unsigned long index = 0; index < size && index < 64; ++index) {
                object plugin = ((object (*)(object, object, unsigned long))message)(
                    plugins, selector("objectAtIndex:"), index);
                object item = get(get_class("NSMutableDictionary"), "dictionary");
                put(item, "identifier", accepts(plugin, "pluginIdentifier", '@', 0) ?
                    bounded_text(get(plugin, "pluginIdentifier"), 255) : 0);
                put(item, "extension_point", accepts(plugin, "protocol", '@', 0) ?
                    bounded_text(get(plugin, "protocol"), 255) : 0);
                ((void (*)(object, object, object))message)(records, selector("addObject:"), item);
            }
            put(row, "plugins", records);
        }
        put(row, "ok", boolean(1));
        return emit(row);
    }
    object klass = get_class("LSApplicationWorkspace");
    if (!accepts(klass, "defaultWorkspace", '@', 0)) return failure(row, "workspace_unavailable");
    object workspace = get(klass, "defaultWorkspace");
    if (!strcmp(operation, "inspect")) return inspect(workspace, row);
    const char *method = "registerApplication:";
    if (!accepts(workspace, method, 'B', 1)) return failure(row, "incompatible_workspace_api");
    object value = string(argument);
    if (!value) return failure(row, "invalid_utf8_argument");
    put(row, "argument", value);
    if (!strcmp(operation, "register")) {
        value = with(get_class("NSURL"), "fileURLWithPath:", value);
        object bundle = with(get_class("NSBundle"), "bundleWithURL:", value);
        object identifier = get(bundle, "bundleIdentifier");
        object executable = get(bundle, "executableURL");
        if (!bundle || !identifier || !executable) return failure(row, "invalid_application_bundle");
        put(row, "identifier", identifier);
    }
    unsigned char accepted = ((unsigned char (*)(object, object, object))message)(workspace, selector(method), value);
    if (!accepted) return failure(row, "request_rejected_by_ios");
    put(row, "ok", boolean(1));
    put(row, "request_accepted", boolean(1));
    /* Acceptance is not evidence of a connected, visible application scene. */
    put(row, "application_scene_verified", boolean(0));
    return emit(row);
}

static int valid_command(const char *operation, const char *argument) {
    if (!operation) return 0;
    if (!strcmp(operation, "inspect") || !strcmp(operation, "status") || !strcmp(operation, "cancel") ||
        !strcmp(operation, "flush") ||
        !strcmp(operation, "setup") || !strcmp(operation, "setup_finish") || !strcmp(operation, "restart_ui") ||
        !strcmp(operation, "screen_state") || !strcmp(operation, "display_state") ||
        !strcmp(operation, "foreground_state") ||
        !strcmp(operation, "display_refresh") || !strcmp(operation, "graphics_state") ||
        !strcmp(operation, "receive_bundle") || !strcmp(operation, "network_state") ||
        !strcmp(operation, "keyboard_layout"))
        return argument == 0;
    if (!strcmp(operation, "policy_add")) return policy_add_argument(argument, 0);
    if (!strcmp(operation, "debug_server")) return debug_target_valid(argument);
    if (!strcmp(operation, "hardware_probe")) return hardware_class_valid(argument);
    if (!strcmp(operation, "gestalt")) return gestalt_key_valid(argument);
    if (!strcmp(operation, "developer_mode")) return argument && (!strcmp(argument, "status") || !strcmp(argument, "enable"));
    if (!strcmp(operation, "crash_reports")) return crash_process_allowed(argument);
    if (!strcmp(operation, "persistence_probe")) return persistence_probe_argument(argument) != 0;
    if (!strcmp(operation, "surface_state")) {
        unsigned ids[4];
        return surface_state_ids(argument, ids) != 0;
    }
    if (!strcmp(operation, "effects"))
        return argument && (!strcmp(argument, "reduced") || !strcmp(argument, "original"));
    if (!strcmp(operation, "watchdog_scale")) return watchdog_scale_argument(argument) != 0;
    if (!strcmp(operation, "icon_cache")) return icon_cache_argument(argument);
    if (!strcmp(operation, "time_zone")) return time_zone_argument(argument);
    if (!strcmp(operation, "auto_lock")) return auto_lock_argument(argument);
    if (strcmp(operation, "install") && strcmp(operation, "register") && strcmp(operation, "open") && strcmp(operation, "lookup") && strcmp(operation, "logs")) return 0;
    return argument && argument[0] && strlen(argument) <= 4096 &&
        ((strcmp(operation, "register") && strcmp(operation, "install")) || argument[0] == '/');
}

static int worker_records_result(const char *operation, const char *argument) {
    if (!valid_command(operation, argument)) return 0;
    /* Every valid framework worker owns a durable response. Status, flush,
     * cancellation and direct file logs belong to the parent service and must
     * never overwrite that response. Derive this from the worker boundary so
     * adding a query cannot silently omit its late result. */
    return strcmp(operation, "status") && strcmp(operation, "cancel") && strcmp(operation, "flush") &&
        (strcmp(operation, "logs") || !strcmp(argument, "application"));
}

static int valid_request_id(const char *value) {
    if (!value) return 1; /* Older host clients have no recoverable receipt ID. */
    if (strlen(value) != 32) return 0;
    for (unsigned index = 0; index < 32; ++index)
        if (!((value[index] >= '0' && value[index] <= '9') ||
              (value[index] >= 'a' && value[index] <= 'f'))) return 0;
    return 1;
}

static int valid_installation_identifier(const char *operation, const char *value) {
    if (!value) return 1;
    if (!operation || strcmp(operation, "install") || !value[0] || strlen(value) > 255) return 0;
    for (unsigned index = 0; value[index]; ++index) {
        char c = value[index];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')) return 0;
    }
    return 1;
}

static const char *request_string(object request, const char *key) {
    object value = with(request, "objectForKey:", string(key));
    if (!((unsigned char (*)(object, object, object))message)(value,
            selector("isKindOfClass:"), get_class("NSString"))) return 0;
    return get(value, "UTF8String");
}

static void worker_details(object row, int pid, const char *operation, const char *identity) {
    put(row, "worker_running", boolean(pid != 0));
    put(row, "worker_pid", pid ? number((unsigned)pid) : 0);
    put(row, "worker_operation", pid ? string(operation) : 0);
    put(row, "worker_request_id", pid && identity[0] ? string(identity) : 0);
}

/* posix_spawn stops argv at the first null pointer. Keep the optional argument
 * as an empty slot so an inspect worker still receives its request identity. */
static void worker_arguments(char *arguments[6], const char *executable,
                             const char *operation, const char *argument,
                             const char *identity, const char *bundle) {
    arguments[0] = (char *)executable;
    arguments[1] = (char *)operation;
    arguments[2] = (char *)(argument ? argument : "");
    arguments[3] = (char *)(identity ? identity : "");
    arguments[4] = (char *)bundle;
    arguments[5] = 0;
}

static const char *worker_argument(int argc, char **argv) {
    return argc >= 3 && argv[2][0] ? argv[2] : 0;
}

static int serve(void) {
    /* Apple's AF_VSOCK ABI, from XNU bsd/sys/vsock.h. Only the hypervisor's
     * local host CID may connect; no TCP interface or external network exists. */
    struct address { unsigned char length, family; unsigned short reserved; unsigned port, cid; };
    _Static_assert(sizeof(struct address) == 12, "Darwin sockaddr_vm ABI");
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*socket_fn)(int, int, int) = dlsym(system, "socket");
    int (*bind_fn)(int, const void *, unsigned) = dlsym(system, "bind");
    int (*listen_fn)(int, int) = dlsym(system, "listen");
    int (*accept_fn)(int, void *, unsigned *) = dlsym(system, "accept");
    int (*fcntl_fn)(int, int, ...) = dlsym(system, "fcntl");
    int (*spawn_fn)(int *, const char *, const void *, const void *, char *const [], char *const []) = dlsym(system, "posix_spawn");
    int (*wait_fn)(int, int *, int) = dlsym(system, "waitpid");
    char ***(*environment_fn)(void) = dlsym(system, "_NSGetEnviron");
    int (*executable_fn)(char *, unsigned *) = dlsym(system, "_NSGetExecutablePath");
    if (!socket_fn || !bind_fn || !listen_fn || !accept_fn || !fcntl_fn ||
        !spawn_fn || !wait_fn || !environment_fn || !executable_fn) return 69;
    char executable[4096];
    unsigned executable_size = sizeof(executable);
    if (executable_fn(executable, &executable_size)) return 69;
    if (getuid() != 501) return failure(response("serve"), "mobile_user_required");
    signal(13, (void (*)(int))1); /* SIGPIPE must not kill the guest service. */
    int server = socket_fn(40, 1, 0);
    struct address local = {12, 40, 0, 1339, 0xffffffffu};
    if (server < 0 || bind_fn(server, &local, sizeof(local)) || listen_fn(server, 8)) {
        object row = response("serve");
        put(row, "errno", number((unsigned)*__error()));
        if (server >= 0) close(server);
        return failure(row, "vsock_unavailable");
    }
    object ready = response("serve");
    put(ready, "ok", boolean(1));
    put(ready, "port", number(1339));
    emit(ready);
    int saved_output = dup(1), worker = 0;
    const char *worker_operation = 0;
    char worker_request_id[33] = {0};
    if (saved_output < 0) { close(server); return 74; }
    if (fcntl_fn(server, 2, 1) || fcntl_fn(saved_output, 2, 1)) {
        close(saved_output); close(server); return 74;
    }
    for (;;) {
        /* No bound while idle: the host may not call for hours, and a timed
         * out service would leave the phone without control. VM shutdown owns
         * the service's lifetime; launchd restarts it after any exit. */
        alarm(0);
        struct address peer = {0};
        unsigned size = sizeof(peer);
        int client = accept_fn(server, &peer, &size);
        if (client < 0 && *__error() == 4) continue;
        if (client < 0) break;
        if (size != sizeof(peer) || peer.family != 40 || peer.cid != 2) { close(client); continue; }
        if (fcntl_fn(client, 2, 1)) { close(client); continue; }
        alarm(120);
        object pool = get(get_class("NSAutoreleasePool"), "new");
        char buffer[8192];
        unsigned used = 0;
        int complete = 0;
        while (used < sizeof(buffer)) {
            long count = read(client, buffer + used, sizeof(buffer) - used);
            if (count < 0 && *__error() == 4) continue;
            if (count <= 0) break;
            used += (unsigned)count;
            for (unsigned index = 0; index < used; ++index)
                if (buffer[index] == '\n') { complete = index == used - 1 ? 1 : -1; break; }
            if (complete) break;
        }
        if (dup2(client, 1) < 0) { close(client); break; }
        object data = ((object (*)(object, object, const void *, unsigned long))message)(
            get_class("NSData"), selector("dataWithBytes:length:"), buffer, used);
        object error = 0;
        object request = complete == 1 ? ((object (*)(object, object, object, unsigned long, object *))message)(
            get_class("NSJSONSerialization"), selector("JSONObjectWithData:options:error:"), data, 0, &error) : 0;
        const char *operation = 0, *argument = 0;
        request_id = 0;
        installation_identifier = 0;
        if (!error && ((unsigned char (*)(object, object, object))message)(request,
                selector("isKindOfClass:"), get_class("NSDictionary"))) {
            operation = request_string(request, "operation");
            argument = request_string(request, "argument");
            request_id = request_string(request, "request_id");
            installation_identifier = request_string(request, "bundle_id");
        }
        if (!valid_command(operation, argument) || !valid_request_id(request_id) ||
            !valid_installation_identifier(operation, installation_identifier) ||
            (request && with(request, "objectForKey:", string("request_id")) && !request_id) ||
            (request && with(request, "objectForKey:", string("bundle_id")) && !installation_identifier))
            failure(response("request"), "invalid_request");
        else if (!strcmp(operation, "logs") && strcmp(argument, "application")) operate(operation, argument);
        else {
            /* Framework/XPC calls can wait for services during boot. Use a
             * freshly exec'd copy (never fork an initialized ObjC runtime),
             * so diagnostics remain available while that request is pending.
             * Only one such worker may run; it has main()'s guest-time bound.
             * dup2 left stdout inheritable; listening/client fds are CLOEXEC. */
            int status = 0;
            if (worker) {
                int reaped;
                do { reaped = wait_fn(worker, &status, 1); }
                while (reaped < 0 && *__error() == 4);
                if (reaped == worker || (reaped < 0 && *__error() == 10)) worker = 0;
            }
            if (!strcmp(operation, "status")) {
                object row = response(operation);
                worker_details(row, worker, worker_operation, worker_request_id);
                put(row, "recoverable_framework_queries", boolean(1));
                put(row, "launch_error_details_supported", boolean(1));
                put(row, "launch_process_observation_supported", boolean(1));
                put(row, "setup_state_supported", boolean(1));
                put(row, "screen_state_supported", boolean(1));
                put(row, "screen_backlight_state_supported", boolean(1));
                put(row, "foreground_state_supported", boolean(1));
                put(row, "graphics_state_supported", boolean(1));
                put(row, "surface_state_supported", boolean(1));
                put(row, "crash_reports_supported", boolean(1));
                put(row, "display_state_supported", boolean(1));
                put(row, "display_cache_refresh_supported", boolean(1));
                put(row, "ui_restart_supported", boolean(1));
                put(row, "storage_flush_supported", boolean(1));
                put(row, "persistence_probe_supported", boolean(1));
                put(row, "display_effects_supported", boolean(1));
                put(row, "launch_watchdog_scale_supported", boolean(1));
                put(row, "icon_cache_clear_supported", boolean(1));
                put(row, "time_zone_supported", boolean(1));
                put(row, "auto_lock_supported", boolean(1));
                put(row, "crash_report_filter_supported", boolean(1));
                put(row, "installation_progress_supported", boolean(1));
                read_installation_progress(row);
                read_development_signing_activation(row);
                put(row, "ok", boolean(1));
                emit(row);
            }
            else if (!strcmp(operation, "flush")) {
                /* Commit pending Data-volume writes before the host stops the
                 * emulator. POSIX calls only; no framework or XPC dependency. */
                object row = response(operation);
                void (*sync_fn)(void) = dlsym(system, "sync");
                int directory = open("/private/var/mobile/Library/Logs", 0x1100100); /* RDONLY|DIRECTORY|NOFOLLOW|CLOEXEC */
                if (sync_fn) sync_fn();
                int flushed = sync_fn && directory >= 0 && !fcntl_fn(directory, 51); /* F_FULLFSYNC */
                if (directory >= 0) close(directory);
                put(row, "storage_flushed", boolean(flushed));
                if (!flushed) failure(row, "storage_flush_failed");
                else { put(row, "ok", boolean(1)); emit(row); }
            }
            else if (!strcmp(operation, "cancel")) {
                /* Only our tracked, unreaped child can be signalled. A child
                 * PID cannot be reused before waitpid reaps it. Cancellation
                 * never rolls back a LaunchServices mutation already sent. */
                object row = response(operation);
                worker_details(row, worker, worker_operation, worker_request_id);
                put(row, "request_may_have_executed", boolean(worker &&
                    (!strcmp(worker_operation, "install") || !strcmp(worker_operation, "register") || !strcmp(worker_operation, "open") || !strcmp(worker_operation, "display_refresh") || !strcmp(worker_operation, "persistence_probe") || !strcmp(worker_operation, "watchdog_scale") || !strcmp(worker_operation, "icon_cache") || !strcmp(worker_operation, "auto_lock"))));
                if (worker && kill(worker, 15)) failure(row, "worker_cancel_failed");
                else {
                    put(row, "signal_delivered", boolean(worker != 0));
                    put(row, "ok", boolean(1));
                    emit(row);
                }
            }
            else if (worker) {
                object row = response(operation);
                worker_details(row, worker, worker_operation, worker_request_id);
                failure(row, "guest_operation_busy");
            }
            else {
                char *arguments[6];
                worker_arguments(arguments, executable, operation, argument,
                                 request_id, installation_identifier);
                int result = spawn_fn(&worker, executable, 0, 0, arguments, *environment_fn());
                snprintf(worker_request_id, sizeof(worker_request_id), "%s", request_id ? request_id : "");
                worker_operation = !strcmp(operation, "inspect") ? "inspect" :
                    !strcmp(operation, "register") ? "register" : !strcmp(operation, "install") ? "install" :
                    !strcmp(operation, "lookup") ? "lookup" : !strcmp(operation, "logs") ? "logs" :
                    !strcmp(operation, "setup") ? "setup" : !strcmp(operation, "setup_finish") ? "setup_finish" :
                    !strcmp(operation, "restart_ui") ? "restart_ui" :
                    !strcmp(operation, "screen_state") ? "screen_state" :
                    !strcmp(operation, "display_state") ? "display_state" :
                    !strcmp(operation, "foreground_state") ? "foreground_state" :
                    !strcmp(operation, "graphics_state") ? "graphics_state" :
                    !strcmp(operation, "surface_state") ? "surface_state" :
                    !strcmp(operation, "crash_reports") ? "crash_reports" :
                    !strcmp(operation, "persistence_probe") ? "persistence_probe" :
                    !strcmp(operation, "display_refresh") ? "display_refresh" :
                    !strcmp(operation, "effects") ? "effects" :
                    !strcmp(operation, "watchdog_scale") ? "watchdog_scale" :
                    !strcmp(operation, "icon_cache") ? "icon_cache" :
                    !strcmp(operation, "time_zone") ? "time_zone" :
                    !strcmp(operation, "auto_lock") ? "auto_lock" :
                    !strcmp(operation, "receive_bundle") ? "receive_bundle" :
                    !strcmp(operation, "policy_add") ? "policy_add" :
                    !strcmp(operation, "debug_server") ? "debug_server" :
                    !strcmp(operation, "network_state") ? "network_state" :
                    !strcmp(operation, "hardware_probe") ? "hardware_probe" :
                    !strcmp(operation, "gestalt") ? "gestalt" :
                    !strcmp(operation, "developer_mode") ? "developer_mode" :
                    !strcmp(operation, "keyboard_layout") ? "keyboard_layout" : "open";
                if (result) {
                    worker = 0;
                    object row = response(operation);
                    put(row, "errno", number((unsigned)result));
                    failure(row, "worker_start_failed");
                }
            }
        }
        if (dup2(saved_output, 1) < 0) { close(client); break; }
        close(client);
        request_id = 0;
        ((void (*)(object, object))message)(pool, selector("drain"));
    }
    close(saved_output);
    close(server);
    return 74;
}

static const char *service_executable(void) {
    static char path[4096];
    unsigned size = sizeof(path);
    int (*executable_fn)(char *, unsigned *) = dlsym(dlopen("/usr/lib/libSystem.B.dylib", 2), "_NSGetExecutablePath");
    return executable_fn && !executable_fn(path, &size) ? path : "";
}

int main(int argc, char **argv) {
    /* The debug helper needs no framework and owns no receipt. */
    if (argc == 4 && !strcmp(argv[1], "debug-accept")) return debug_accept(argv[2], argv[3]);
    int server = argc == 2 && !strcmp(argv[1], "serve");
    const char *argument = worker_argument(argc, argv);
    request_id = argc >= 4 && argv[3][0] ? argv[3] : 0;
    installation_identifier = argc == 5 ? argv[4] : 0;
    if (!server && (argc < 2 || argc > 5 || !valid_command(argv[1], argument) ||
                    !valid_request_id(request_id) ||
                    !valid_installation_identifier(argv[1], installation_identifier))) {
        dprintf(2, "Usage: tomacco-app-control serve | inspect | status | cancel | flush | setup | screen_state | display_state | foreground_state | display_refresh | surface_state id[,id,id,id] | persistence_probe create:<64 lowercase hex digits>/read:<same challenge> | setup_finish | restart_ui | crash_reports <process name the reports start with> | effects reduced/original | watchdog_scale original/2..30 | icon_cache clear | time_zone Area/City | auto_lock never/original | lookup bundle.identifier | install /path/to/App.app-or.ipa | register /path/to/App.app | open bundle.identifier | receive_bundle | policy_add <cdhash>:<identifier> | debug_server pid-or-name | network_state | hardware_probe class | gestalt key | developer_mode status/enable | keyboard_layout | logs application/platform/control/operation/development/quiet/buffer_texture\n");
        return 64;
    }
    /* A stuck framework/XPC call must not keep a launchd job alive indefinitely. */
    alarm(120);
    record_mutation = !server && (!strcmp(argv[1], "install") || !strcmp(argv[1], "register") || !strcmp(argv[1], "open") || !strcmp(argv[1], "setup_finish") || !strcmp(argv[1], "restart_ui") || !strcmp(argv[1], "effects") || !strcmp(argv[1], "watchdog_scale") || !strcmp(argv[1], "icon_cache") || !strcmp(argv[1], "time_zone") || !strcmp(argv[1], "auto_lock") || !strcmp(argv[1], "display_refresh") || !strcmp(argv[1], "receive_bundle") || !strcmp(argv[1], "policy_add") || !strcmp(argv[1], "debug_server") || !strcmp(argv[1], "developer_mode") || (!strcmp(argv[1], "persistence_probe") && persistence_probe_argument(argument) == 7));
    record_result = !server && worker_records_result(argv[1], worker_argument(argc, argv));
    if (!server) prepare_worker_deadlines(argv[1]);
    if (!bind_api()) {
        dprintf(1, "TOMACCO_APP_CONTROL {\"format\":1,\"ok\":false,\"error\":\"framework_unavailable\"}\n");
        return 69;
    }
    object pool = get(get_class("NSAutoreleasePool"), "new");
    int result = server ? serve() : operate(argv[1], argument);
    /* A fresh exec owns each framework request. Its explicit filesystem
     * cleanup and native completion precede emit(); process teardown must not
     * keep the single worker reservation alive through Foundation/atexit. */
    if (!server) _exit(result);
    ((void (*)(object, object))message)(pool, selector("drain"));
    return result;
}
