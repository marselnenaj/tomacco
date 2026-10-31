/* Read-only IOKit inventory in an owned iOS research guest.
 * ABI: apple-oss-distributions/IOKitUser/IOKitLib.h. No service opens,
 * registry writes, driver injection or fabricated readiness responses.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int open(const char *, int, ...);
extern int close(int);
extern long write(int, const void *, unsigned long);
extern int snprintf(char *, unsigned long, const char *, ...);
extern int dprintf(int, const char *, ...);
extern int strcmp(const char *, const char *);
extern int *__error(void);
extern int usleep(unsigned);
extern unsigned sleep(unsigned);
extern unsigned alarm(unsigned);
extern unsigned getuid(void);
extern int getpid(void);

struct tomacco_termios {
    unsigned long input, output, control, local;
    unsigned char characters[20];
    unsigned long input_speed, output_speed;
};
_Static_assert(sizeof(struct tomacco_termios) == 72, "Darwin termios ABI");
extern int tcgetattr(int, struct tomacco_termios *);
extern void cfmakeraw(struct tomacco_termios *);
extern int tcsetattr(int, int, const struct tomacco_termios *);

#include "research-platform-contract.h"
typedef unsigned io_object_t;
typedef const void *cf_object_t;
static cf_object_t (*matching)(const char *);
static int (*services)(unsigned, cf_object_t, io_object_t *);
static io_object_t (*next)(io_object_t);
static int (*release)(io_object_t);
static int (*object_class)(io_object_t, char *);
static int (*object_name)(io_object_t, char *);
static int (*object_path)(io_object_t, const char *, char *);
static int (*object_id)(io_object_t, unsigned long long *);
static int (*object_busy)(io_object_t, unsigned *);
static int (*global_busy)(unsigned, unsigned *);
static io_object_t (*from_path)(unsigned, const char *);
static cf_object_t (*property)(io_object_t, cf_object_t, cf_object_t, unsigned);
static cf_object_t (*cf_string)(cf_object_t, const char *, unsigned);
static unsigned long (*cf_type)(cf_object_t);
static unsigned long (*data_type)(void);
static unsigned long (*string_type)(void);
static long (*data_length)(cf_object_t);
static const unsigned char *(*data_bytes)(cf_object_t);
static unsigned char (*string_bytes)(cf_object_t, char *, long, unsigned);
static void (*cf_release)(cf_object_t);
static int output_fd = -1;
static unsigned sequence, bytes_emitted, retries;

static int emit(const char *body) {
    char line[8192];
    int length = snprintf(line, sizeof(line), "TOMACCO_PLATFORM {\"seq\":%u,%s}\n", sequence, body);
    if (length < 0 || (unsigned)length >= sizeof(line) ||
        (unsigned)length > TOMACCO_PLATFORM_MAX_BYTES - bytes_emitted) return -1;
    const char *cursor = line;
    unsigned remaining = (unsigned)length;
    while (remaining) {
        long count = write(output_fd, cursor, remaining);
        if (count < 0 && *__error() == 4) continue;
        if (count < 0 && *__error() == 35 && retries++ < 300) { usleep(10000); continue; }
        if (count <= 0) return -1;
        cursor += count;
        remaining -= count;
    }
    bytes_emitted += (unsigned)length;
    ++sequence;
    return 0;
}

static int failure(const char *stage) {
    char line[256];
    snprintf(line, sizeof(line), "\"event\":\"error\",\"stage\":\"%s\"", stage);
    emit(line);
    return 1;
}

/* Callers reserve six output bytes per input byte, plus quotes and NUL. */
static void quote(const char *input, char *output) {
    static const char hex[] = "0123456789abcdef";
    *output++ = '"';
    while (*input) {
        unsigned char c = (unsigned char)*input++;
        if (c < 32) {
            *output++ = '\\'; *output++ = 'u'; *output++ = '0'; *output++ = '0';
            *output++ = hex[c >> 4]; *output++ = hex[c & 15];
        } else {
            if (c == '"' || c == '\\') *output++ = '\\';
            *output++ = (char)c;
        }
    }
    *output++ = '"'; *output = 0;
}

static int bind_api(void) {
    void *io = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", 2);
    void *cf = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    if (!io || !cf) return failure("load-libraries");
#define BIND(handle, target, name) target = (__typeof__(target))dlsym(handle, #name); \
    if (!target) return failure(#name)
    BIND(io, matching, IOServiceMatching);
    BIND(io, services, IOServiceGetMatchingServices);
    BIND(io, next, IOIteratorNext);
    BIND(io, release, IOObjectRelease);
    BIND(io, object_class, IOObjectGetClass);
    BIND(io, object_name, IORegistryEntryGetName);
    BIND(io, object_path, IORegistryEntryGetPath);
    BIND(io, object_id, IORegistryEntryGetRegistryEntryID);
    BIND(io, object_busy, IOServiceGetBusyState);
    BIND(io, global_busy, IOKitGetBusyState);
    BIND(io, from_path, IORegistryEntryFromPath);
    BIND(io, property, IORegistryEntryCreateCFProperty);
    BIND(cf, cf_string, CFStringCreateWithCString);
    BIND(cf, cf_type, CFGetTypeID);
    BIND(cf, data_type, CFDataGetTypeID);
    BIND(cf, string_type, CFStringGetTypeID);
    BIND(cf, data_length, CFDataGetLength);
    BIND(cf, data_bytes, CFDataGetBytePtr);
    BIND(cf, string_bytes, CFStringGetCString);
    BIND(cf, cf_release, CFRelease);
#undef BIND
    return 0;
}

static int describe_service(unsigned tick, const char *query, io_object_t entry) {
    char name[128] = {0}, klass[128] = {0}, path[512] = {0};
    unsigned long long identity = 0;
    unsigned busy = 0;
    int rc_name = object_name(entry, name), rc_class = object_class(entry, klass);
    int rc_path = object_path(entry, "IOService", path), rc_id = object_id(entry, &identity);
    int rc_busy = object_busy(entry, &busy);
    name[127] = 0; klass[127] = 0; path[511] = 0;
    char qname[771], qclass[771], qpath[3075], line[6144];
    quote(name, qname); quote(klass, qclass); quote(path, qpath);
    snprintf(line, sizeof(line), "\"event\":\"service\",\"tick\":%u,\"query\":\"%s\","
             "\"name\":%s,\"class\":%s,\"path\":%s,\"registry_id\":\"0x%016llx\",\"busy\":%u,"
             "\"errors\":{\"name\":%d,\"class\":%d,\"path\":%d,\"id\":%d,\"busy\":%d}",
             tick, query, qname, qclass, qpath, identity, busy, rc_name, rc_class, rc_path, rc_id, rc_busy);
    return emit(line);
}

static int describe_node(unsigned tick, const char *path, cf_object_t key) {
    io_object_t entry = from_path(0, path);
    char hex[TOMACCO_PLATFORM_COMPAT_BYTES * 2 + 1] = {0};
    char klass[128] = {0}, service_path[512] = {0};
    int class_error = -1, plane_error = -1;
    const char *kind = "absent";
    int error = 0;
    if (entry) {
        class_error = object_class(entry, klass);
        plane_error = object_path(entry, "IOService", service_path);
        klass[127] = 0; service_path[511] = 0;
        cf_object_t value = property(entry, key, 0, 0);
        if (value) {
            const unsigned char *bytes = 0;
            long length = 0;
            char string[TOMACCO_PLATFORM_COMPAT_BYTES];
            if (cf_type(value) == data_type()) {
                kind = "data"; bytes = data_bytes(value); length = data_length(value);
            } else if (cf_type(value) == string_type()) {
                kind = "string";
                if (!string_bytes(value, string, sizeof(string), 0x08000100)) error = 1;
                else { bytes = (const unsigned char *)string; while (string[length]) ++length; }
            } else { kind = "unsupported"; error = 1; }
            if (length < 0 || length > TOMACCO_PLATFORM_COMPAT_BYTES) error = 1;
            if (!error && bytes) {
                static const char digits[] = "0123456789abcdef";
                for (long i = 0; i < length; ++i) {
                    hex[i * 2] = digits[bytes[i] >> 4]; hex[i * 2 + 1] = digits[bytes[i] & 15];
                }
            }
            cf_release(value);
        }
        release(entry);
    }
    char line[6144], qclass[771], qpath[3075];
    quote(klass, qclass); quote(service_path, qpath);
    snprintf(line, sizeof(line), "\"event\":\"node\",\"tick\":%u,\"path\":\"%s\",\"found\":%s,"
             "\"compatible_type\":\"%s\",\"compatible_hex\":\"%s\",\"property_error\":%d,"
             "\"class\":%s,\"class_error\":%d,\"service_path\":%s,\"service_path_error\":%d",
             tick, path, entry ? "true" : "false", kind, hex, error, qclass, class_error, qpath, plane_error);
    return emit(line);
}

static int snapshot(unsigned tick) {
    unsigned busy = 0;
    int result = global_busy(0, &busy);
    char line[512];
    snprintf(line, sizeof(line), "\"event\":\"snapshot\",\"tick\":%u,\"busy\":%u,\"busy_error\":%d", tick, busy, result);
    if (emit(line)) return -1;
    unsigned total = 0;
    for (unsigned index = 0; index < sizeof(platform_classes) / sizeof(platform_classes[0]); ++index) {
        const char *query = platform_classes[index];
        io_object_t iterator = 0;
        cf_object_t match = matching(query);
        result = match ? services(0, match, &iterator) : -1;
        unsigned count = 0;
        int truncated = 0;
        if (!result) {
            io_object_t entry;
            while ((entry = next(iterator))) {
                if (count == TOMACCO_PLATFORM_CLASS_LIMIT) { truncated = 1; release(entry); break; }
                int error = describe_service(tick, query, entry);
                release(entry);
                if (error) { release(iterator); return -1; }
                ++count; ++total;
            }
        }
        if (iterator) release(iterator);
        snprintf(line, sizeof(line), "\"event\":\"class\",\"tick\":%u,\"query\":\"%s\","
                 "\"result\":%d,\"count\":%u,\"truncated\":%s", tick, query, result, count, truncated ? "true" : "false");
        if (emit(line)) return -1;
    }
    cf_object_t key = cf_string(0, "compatible", 0x08000100);
    if (!key) return failure("compatible-key");
    for (unsigned index = 0; index < sizeof(platform_nodes) / sizeof(platform_nodes[0]); ++index) {
        if (describe_node(tick, platform_nodes[index], key)) { cf_release(key); return -1; }
    }
    cf_release(key);
    snprintf(line, sizeof(line), "\"event\":\"snapshot-end\",\"tick\":%u,\"services\":%u", tick, total);
    return emit(line);
}

int main(int argc, char **argv) {
    int stdio = argc == 2 && !strcmp(argv[1], "--stdio");
    if (argc != 1 && !stdio) return 64;
    alarm(120);
    if (stdio) output_fd = 1;
    else {
        output_fd = open("/dev/cu.qemuport2", 0x20106);
        struct tomacco_termios settings;
        if (output_fd < 0 || tcgetattr(output_fd, &settings)) {
            dprintf(2, "TOMACCO_PLATFORM uart_unavailable errno=%d\n", *__error());
            if (output_fd >= 0) close(output_fd);
            return 2;
        }
        cfmakeraw(&settings);
        if (tcsetattr(output_fd, 0, &settings)) { close(output_fd); return 2; }
    }
    char line[256];
    snprintf(line, sizeof(line), "\"event\":\"begin\",\"format\":2,\"uid\":%u,\"pid\":%d,\"mode\":\"%s\"",
             getuid(), getpid(), stdio ? "stdio" : "uart");
    if (emit(line) || bind_api()) return 3;
    unsigned rounds = stdio ? 1 : 2;
    for (unsigned round = 0; round < rounds; ++round) {
        unsigned delay = stdio ? 0 : round ? 60 : 30;
        while (delay) delay = sleep(delay);
        if (snapshot(stdio ? 0 : round ? 90 : 30)) return 4;
    }
    snprintf(line, sizeof(line), "\"event\":\"end\",\"snapshots\":%u", rounds);
    int result = emit(line);
    if (!stdio) close(output_fd);
    return result ? 5 : 0;
}
