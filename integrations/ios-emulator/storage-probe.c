/* Read-only guest storage diagnostic, built on Linux without an Apple SDK.
 * ABI declarations resolve the actual iOS libSystem/IOKit/CoreFoundation.
 * The raw-device entitlement applies only to this disposable research guest.
 */
extern int open(const char *, int, ...);
extern int close(int);
extern long pread(int, void *, unsigned long, long long);
extern long write(int, const void *, unsigned long);
extern int dprintf(int, const char *, ...);
extern int *__error(void);
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern char *dlerror(void);
extern int sysctlbyname(const char *, void *, unsigned long *, void *, unsigned long);

typedef unsigned io_object_t;
typedef const void *cf_object_t;

static int write_all(int fd, const void *bytes, unsigned long size) {
    const char *p = bytes;
    while (size) {
        long n = write(fd, p, size);
        if (n < 0 && *__error() == 4) continue;
        if (n <= 0) return -1;
        p += n;
        size -= n;
    }
    return 0;
}

static int registry(void) {
    void *cf = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    void *io = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", 2);
    if (!io) io = dlopen("/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit", 2);
    if (!cf || !io) {
        dprintf(2, "TOMACCO_STORAGE registry_library_error=%s\n", dlerror());
        return -1;
    }
#define BIND(handle, type, name) type name = (type)dlsym(handle, #name); \
    if (!name) { dprintf(2, "TOMACCO_STORAGE missing_symbol=%s\n", #name); return -1; }
    typedef cf_object_t (*matching_fn)(const char *);
    typedef int (*services_fn)(unsigned, cf_object_t, io_object_t *);
    typedef io_object_t (*next_fn)(io_object_t);
    typedef int (*release_fn)(io_object_t);
    typedef int (*props_fn)(io_object_t, cf_object_t *, cf_object_t, unsigned);
    typedef int (*path_fn)(io_object_t, const char *, char *);
    typedef cf_object_t (*xml_fn)(cf_object_t, cf_object_t);
    typedef long (*length_fn)(cf_object_t);
    typedef const unsigned char *(*bytes_fn)(cf_object_t);
    typedef void (*cf_release_fn)(cf_object_t);
    BIND(io, matching_fn, IOServiceMatching);
    BIND(io, services_fn, IOServiceGetMatchingServices);
    BIND(io, next_fn, IOIteratorNext);
    BIND(io, release_fn, IOObjectRelease);
    BIND(io, props_fn, IORegistryEntryCreateCFProperties);
    BIND(io, path_fn, IORegistryEntryGetPath);
    BIND(cf, xml_fn, CFPropertyListCreateXMLData);
    BIND(cf, length_fn, CFDataGetLength);
    BIND(cf, bytes_fn, CFDataGetBytePtr);
    BIND(cf, cf_release_fn, CFRelease);
#undef BIND
    io_object_t iterator = 0;
    cf_object_t match = IOServiceMatching("IOMedia");
    if (!match || IOServiceGetMatchingServices(0, match, &iterator)) return -1;
    unsigned count = 0;
    io_object_t entry;
    int failed = 0;
    while (count < 32 && (entry = IOIteratorNext(iterator))) {
        cf_object_t properties = 0;
        char path[512] = {0};
        IORegistryEntryGetPath(entry, "IOService", path);
        dprintf(1, "TOMACCO_IOMEDIA_BEGIN %s\n", path);
        if (IORegistryEntryCreateCFProperties(entry, &properties, 0, 0) || !properties) {
            failed = 1;
        } else {
            cf_object_t xml = CFPropertyListCreateXMLData(0, properties);
            if (xml) {
                long length = CFDataGetLength(xml);
                if (length < 0 || length > 262144 || write_all(1, CFDataGetBytePtr(xml), length)) failed = 1;
                CFRelease(xml);
            } else failed = 1;
            CFRelease(properties);
        }
        dprintf(1, "TOMACCO_IOMEDIA_END\n");
        IOObjectRelease(entry);
        ++count;
    }
    if (count == 32 && (entry = IOIteratorNext(iterator))) {
        IOObjectRelease(entry);
        dprintf(2, "TOMACCO_STORAGE registry_limit_exceeded\n");
        failed = 1;
    }
    IOObjectRelease(iterator);
    dprintf(1, "TOMACCO_STORAGE iomedia_count=%u\n", count);
    return failed ? -1 : 0;
}

int main(void) {
    char bootuuid[128] = {0};
    unsigned long length = sizeof(bootuuid) - 1;
    int result = sysctlbyname("kern.bootuuid", bootuuid, &length, 0, 0);
    dprintf(1, "TOMACCO_STORAGE bootuuid_result=%d bootuuid=%s\n", result, bootuuid);
    int registry_result = registry();
    /* Fixed device and bounded read; this program has no write/mount API. */
    int fd = open("/dev/rdisk0", 0);
    if (fd < 0) {
        dprintf(2, "TOMACCO_STORAGE open_error=%d\n", *__error());
        return 2;
    }
    unsigned char bytes[4096];
    unsigned long total = 0;
    while (total < sizeof(bytes)) {
        long count = pread(fd, bytes + total, sizeof(bytes) - total, total);
        if (count < 0 && *__error() == 4) continue;
        if (count <= 0) { close(fd); return 3; }
        total += count;
    }
    close(fd);
    static const char hex[] = "0123456789abcdef";
    char output[8193];
    for (unsigned i = 0; i < sizeof(bytes); ++i) {
        output[i * 2] = hex[bytes[i] >> 4];
        output[i * 2 + 1] = hex[bytes[i] & 15];
    }
    output[8192] = '\n';
    dprintf(1, "TOMACCO_STORAGE_BLOCK ");
    if (write_all(1, output, sizeof(output))) return 4;
    dprintf(1, "TOMACCO_STORAGE read_bytes=%lu registry_result=%d\n", total, registry_result);
    return registry_result ? 5 : 0;
}
