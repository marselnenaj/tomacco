/* Research-only volume discovery through the real iOS frameworks. No SDK.
 * A successful formatter exit alone is insufficient: select the actual Data
 * role in the actual boot container before attempting to mount it.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern char *dlerror(void);
extern int dprintf(int, const char *, ...);
extern int strcmp(const char *, const char *);
extern int snprintf(char *, unsigned long, const char *, ...);

typedef unsigned io_object_t;
typedef const void *cf_object_t;

int tomacco_data_volume(char *device, unsigned long size, char *container_device, unsigned long container_size) {
    void *cf = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    void *io = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", 2);
    if (!io) io = dlopen("/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit", 2);
    void *apfs = dlopen("/System/Library/PrivateFrameworks/APFS.framework/APFS", 2);
    if (!cf || !io || !apfs) {
        dprintf(2, "TOMACCO_INIT apfs_library_error=%s\n", dlerror());
        return -1;
    }
#define BIND(handle, type, name) type name = (type)dlsym(handle, #name); \
    if (!name) { dprintf(2, "TOMACCO_INIT missing_symbol=%s\n", #name); return -1; }
    typedef cf_object_t (*matching_fn)(const char *);
    typedef int (*services_fn)(unsigned, cf_object_t, io_object_t *);
    typedef io_object_t (*next_fn)(io_object_t);
    typedef int (*release_fn)(io_object_t);
    typedef int (*props_fn)(io_object_t, cf_object_t *, cf_object_t, unsigned);
    typedef int (*name_fn)(io_object_t, char *);
    typedef cf_object_t (*get_fn)(cf_object_t, cf_object_t);
    typedef cf_object_t (*string_fn)(cf_object_t, const char *, unsigned);
    typedef unsigned char (*cstring_fn)(cf_object_t, char *, long, unsigned);
    typedef unsigned char (*number_fn)(cf_object_t, int, void *);
    typedef unsigned long (*type_fn)(cf_object_t);
    typedef unsigned long (*type_id_fn)(void);
    typedef void (*cf_release_fn)(cf_object_t);
    typedef int (*boot_fn)(cf_object_t *);
    BIND(io, matching_fn, IOServiceMatching);
    BIND(io, services_fn, IOServiceGetMatchingServices);
    BIND(io, next_fn, IOIteratorNext);
    BIND(io, release_fn, IOObjectRelease);
    BIND(io, props_fn, IORegistryEntryCreateCFProperties);
    BIND(io, name_fn, IORegistryEntryGetName);
    BIND(cf, get_fn, CFDictionaryGetValue);
    BIND(cf, string_fn, CFStringCreateWithCString);
    BIND(cf, cstring_fn, CFStringGetCString);
    BIND(cf, number_fn, CFNumberGetValue);
    BIND(cf, type_fn, CFGetTypeID);
    BIND(cf, type_id_fn, CFNumberGetTypeID);
    BIND(cf, type_id_fn, CFStringGetTypeID);
    BIND(cf, cf_release_fn, CFRelease);
    BIND(apfs, boot_fn, APFSContainerGetBootDevice);
#undef BIND
    cf_object_t boot = 0;
    int result = APFSContainerGetBootDevice(&boot);
    char container[64] = {0};
    if (result || !boot) {
        dprintf(2, "TOMACCO_INIT boot_container_error=%d\n", result);
        if (boot) CFRelease(boot);
        return -1;
    }
    int valid = CFGetTypeID(boot) == CFStringGetTypeID() &&
        CFStringGetCString(boot, container, sizeof(container), 0x08000100);
    CFRelease(boot);
    if (!valid) return -1;
    unsigned container_length = 0;
    while (container[container_length]) ++container_length;
    if (container_length < 5 || container[0] != 'd' || container[1] != 'i' ||
        container[2] != 's' || container[3] != 'k') return -1;
    for (unsigned i = 4; i < container_length; ++i)
        if (container[i] < '0' || container[i] > '9') return -1;
    int length = snprintf(container_device, container_size, "/dev/%s", container);
    if (length < 0 || (unsigned long)length >= container_size) return -1;
    dprintf(2, "TOMACCO_INIT boot_container=%s\n", container);
    cf_object_t role_key = CFStringCreateWithCString(0, "RoleValue", 0x08000100);
    cf_object_t bsd_key = CFStringCreateWithCString(0, "BSD Name", 0x08000100);
    if (!role_key || !bsd_key) {
        if (role_key) CFRelease(role_key);
        if (bsd_key) CFRelease(bsd_key);
        return -1;
    }
    io_object_t iterator = 0, entry;
    cf_object_t match = IOServiceMatching("AppleAPFSVolume");
    int failed = !match || IOServiceGetMatchingServices(0, match, &iterator);
    unsigned found = 0, count = 0;
    while (!failed && count < 100 && (entry = IOIteratorNext(iterator))) {
        ++count;
        cf_object_t properties = 0;
        char name[128] = {0}, bsd[128] = {0};
        int role = -1;
        if (IORegistryEntryCreateCFProperties(entry, &properties, 0, 0) || !properties ||
            IORegistryEntryGetName(entry, name)) {
            failed = 1;
        } else {
            cf_object_t value = CFDictionaryGetValue(properties, role_key);
            if (value && CFGetTypeID(value) == CFNumberGetTypeID()) CFNumberGetValue(value, 9, &role);
            value = CFDictionaryGetValue(properties, bsd_key);
            if (value && CFGetTypeID(value) == CFStringGetTypeID())
                CFStringGetCString(value, bsd, sizeof(bsd), 0x08000100);
            dprintf(2, "TOMACCO_INIT apfs_volume name=%s role=%d bsd=%s\n", name, role, bsd);
            unsigned i = 0;
            while (i < container_length && bsd[i] == container[i]) ++i;
            if (i == container_length && bsd[i] == 's' && bsd[i+1] >= '0' &&
                bsd[i+1] <= '9' && role == 64 && !strcmp(name, "Data")) {
                for (++i; bsd[i]; ++i)
                    if (bsd[i] < '0' || bsd[i] > '9') failed = 1;
                length = snprintf(device, size, "/dev/%s", bsd);
                if (length < 0 || (unsigned long)length >= size) failed = 1;
                ++found;
            }
        }
        if (properties) CFRelease(properties);
        IOObjectRelease(entry);
    }
    if (count == 100 && (entry = IOIteratorNext(iterator))) {
        IOObjectRelease(entry);
        failed = 1;
    }
    if (iterator) IOObjectRelease(iterator);
    CFRelease(role_key);
    CFRelease(bsd_key);
    dprintf(2, "TOMACCO_INIT data_volumes=%u registry_failed=%d\n", found, failed);
    return failed || found > 1 ? -1 : (int)found;
}
