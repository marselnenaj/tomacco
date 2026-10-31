/* Public Objective-C metadata queries only. Their results describe this
 * disposable worker, never another process's loaded images.
 * ABI: apple-oss-distributions/objc4/runtime/runtime.h. */
typedef void (*graphics_imp)(void);
struct graphics_dl_info {
    const char *image;
    void *base;
    const char *symbol;
    void *symbol_address;
};
_Static_assert(sizeof(struct graphics_dl_info) == 32, "Darwin ARM64 Dl_info ABI");
/* Use the public dladdr boundary; no guessed pointer mask or code reads.
 * Apple dyld strips pointer authentication in its own address lookup. */
static void graphics_method_image(object item, object method,
                                 graphics_imp (*implementation)(object),
                                 int (*lookup)(const void *, struct graphics_dl_info *)) {
    put(item, "method_implementation_image_known", boolean(0));
    if (!method || !implementation || !lookup) return;
    graphics_imp address = implementation(method);
    struct graphics_dl_info info = {0};
    if (!address || !lookup((const void *)address, &info) || !info.image) return;
    put(item, "method_implementation_image", bounded_text(string(info.image), 4096));
    put(item, "method_implementation_image_known", boolean(1));
}

static void graphics_class_metadata(object row, object device) {
    put(row, "metal_runtime_metadata_supported", boolean(0));
    put(row, "buffer_texture_hook_verified", boolean(0));
    object runtime = dlopen("/usr/lib/libobjc.A.dylib", 2);
    const char *(*class_name)(object) = runtime ? dlsym(runtime, "class_getName") : 0;
    const char *(*image_name)(object) = runtime ? dlsym(runtime, "class_getImageName") : 0;
    const char *(*encoding)(object) = runtime ? dlsym(runtime, "method_getTypeEncoding") : 0;
    graphics_imp (*implementation)(object) = runtime ? dlsym(runtime, "method_getImplementation") : 0;
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*lookup)(const void *, struct graphics_dl_info *) = system ? dlsym(system, "dladdr") : 0;
    put(row, "metal_implementation_metadata_supported", boolean(implementation && lookup));
    if (!class_name || !image_name || !encoding) return;
    put(row, "metal_runtime_metadata_supported", boolean(1));
    object classes = get(get_class("NSMutableArray"), "array");
    const char *names[] = {"AppleParavirtDevice", "AppleParavirtTexture"};
    for (unsigned index = 0; index < 3; ++index) {
        object cls = index == 0 ? (device ? object_class(device) : 0) : get_class(names[index - 1]);
        object item = get(get_class("NSMutableDictionary"), "dictionary");
        put(item, "role", string(index == 0 ? "default-device" : names[index - 1]));
        put(item, "class_present", boolean(cls != 0));
        if (cls) {
            const char *name = class_name(cls), *image = image_name(cls);
            put(item, "name", name ? bounded_text(string(name), 128) : 0);
            put(item, "image", image ? bounded_text(string(image), 4096) : 0);
            if (index == 2) {
                object method = instance_method(cls, selector("initWithBuffer:descriptor:offset:bytesPerRow:"));
                const char *types = method ? encoding(method) : 0;
                put(item, "buffer_initializer_present", boolean(method != 0));
                put(item, "buffer_initializer_encoding", types ? bounded_text(string(types), 128) : 0);
                put(item, "observed_method", string("initWithBuffer:descriptor:offset:bytesPerRow:"));
                graphics_method_image(item, method, implementation, lookup);
            } else if (index == 1) {
                object method = instance_method(cls, selector("newTextureWithDescriptor:"));
                put(item, "observed_method", string("newTextureWithDescriptor:"));
                graphics_method_image(item, method, implementation, lookup);
            }
        }
        ((void (*)(object, object, object))message)(classes, selector("addObject:"), item);
    }
    put(row, "metal_runtime_classes", classes);
}

/* Public proc_pidinfo(PROC_PIDTBSDINFO=3) ABI from XNU bsd/sys/proc_info.h.
 * uid is effective, ruid is real; neither is inferred from a crash report.
 * Only a complete record for the requested PID is accepted. No process memory,
 * arguments, environment, credentials changes or debugging permission needed. */
#include "guest-process-info.h"
/* PROC_PIDT_SHORTBSDINFO=13 is permitted across users by XNU's public
 * proc_info policy. Its command is only 16 bytes; identify a full basename
 * through proc_pidpath, never by treating a truncated command as a name. */

static int graphics_terminated(const char *text, unsigned size) {
    for (unsigned index = 0; index < size; ++index) if (!text[index]) return 1;
    return 0;
}

static void graphics_process_snapshot(object row) {
    put(row, "native_process_snapshot_known", boolean(0));
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*list)(void *, int) = system ? dlsym(system, "proc_listallpids") : 0;
    int (*info)(int, int, unsigned long, void *, int) = system ? dlsym(system, "proc_pidinfo") : 0;
    int (*path)(int, void *, unsigned) = system ? dlsym(system, "proc_pidpath") : 0;
    put(row, "native_process_credentials_supported", boolean(info != 0));
    put(row, "native_process_short_credentials_supported", boolean(info && path));
    if (!list || !info) return;
    int pids[4096];
    int size = list(pids, sizeof(pids));
    /* A full array may have been truncated. Zero also represents libproc's
     * syscall-error return, so it cannot establish an empty process list. */
    if (size <= 0 || size >= 4096) return;
    object processes = get(get_class("NSMutableArray"), "array");
    unsigned failures = 0, last_error = 0, full_denials = 0, short_records = 0;
    for (int index = 0; index < size; ++index) {
        if (pids[index] <= 1) continue;
        struct graphics_process_info value = {0};
        *__error() = 0;
        int bytes = info(pids[index], 3, 0, &value, sizeof(value));
        int error = *__error();
        const char *text = value.name[0] ? value.name : value.command;
        unsigned limit = value.name[0] ? sizeof(value.name) : sizeof(value.command);
        int full = bytes == sizeof(value) && value.pid == (unsigned)pids[index];
        char executable[4096] = {0};
        const char *source = value.name[0] ? "pbi_name" : "pbi_comm";
        if (!full && bytes == 0 && error == 1 && path) {
            ++full_denials;
            struct graphics_short_process_info short_value = {0};
            *__error() = 0;
            int short_bytes = info(pids[index], 13, 0, &short_value, sizeof(short_value));
            error = *__error();
            if (short_bytes == sizeof(short_value) && short_value.pid == (unsigned)pids[index]) {
                /* proc_pidpath is also exempt from the same-user check.
                 * MACF can still refuse it; that stays incomplete coverage. */
                *__error() = 0;
                int path_bytes = path(pids[index], executable, sizeof(executable));
                error = *__error();
                if (path_bytes > 0 && path_bytes < (int)sizeof(executable) &&
                        graphics_terminated(executable, sizeof(executable)) &&
                        strlen(executable) == (unsigned)path_bytes && executable[0] == '/') {
                    text = executable;
                    for (const char *cursor = executable; *cursor; ++cursor)
                        if (*cursor == '/') text = cursor + 1;
                    limit = sizeof(executable) - (unsigned)(text - executable);
                    value.pid = short_value.pid;
                    value.uid = short_value.uid; value.gid = short_value.gid;
                    value.real_uid = short_value.real_uid; value.real_gid = short_value.real_gid;
                    value.saved_uid = short_value.saved_uid; value.saved_gid = short_value.saved_gid;
                    bytes = sizeof(value); /* Normalized, complete short record and path. */
                    source = "proc_pidpath_basename";
                    ++short_records;
                }
            }
        }
        if (bytes != sizeof(value) || value.pid != (unsigned)pids[index] ||
                !graphics_terminated(text, limit) || !text[0]) {
            ++failures;
            last_error = error > 0 ? (unsigned)error : 0;
            continue; /* Exited, denied, malformed or unknown; never absent. */
        }
        if (strcmp(text, "SpringBoard") && strcmp(text, "Preferences") &&
                strcmp(text, "iconservicesagent") && strcmp(text, "locationd")) continue;
        object item = get(get_class("NSMutableDictionary"), "dictionary");
        put(item, "name", string(text));
        put(item, "name_source", string(source));
        if (!full) put(item, "executable_path", string(executable));
        put(item, "pid", number(value.pid));
        put(item, "credentials_known", boolean(1));
        put(item, "credentials_source", string(full ? "PROC_PIDTBSDINFO" : "PROC_PIDT_SHORTBSDINFO"));
        put(item, "name_and_credentials_single_record", boolean(full));
        put(item, "effective_uid", number(value.uid));
        put(item, "real_uid", number(value.real_uid));
        put(item, "saved_uid", number(value.saved_uid));
        put(item, "effective_gid", number(value.gid));
        put(item, "real_gid", number(value.real_gid));
        put(item, "saved_gid", number(value.saved_gid));
        int start_known = full && process_start_fields_known(&value);
        put(item, "process_start_known", boolean(start_known));
        if (start_known) {
            put(item, "process_start_seconds", number(value.start_seconds));
            put(item, "process_start_microseconds", number(value.start_microseconds));
        }
        ((void (*)(object, object, object))message)(processes, selector("addObject:"), item);
    }
    put(row, "matching_native_processes", processes);
    put(row, "native_process_snapshot_known", boolean(1));
    put(row, "native_process_names_complete", boolean(failures == 0));
    put(row, "native_process_credentials_complete", boolean(failures == 0));
    put(row, "native_process_query_failures", number(failures));
    put(row, "native_process_query_last_errno", number(last_error));
    put(row, "native_process_full_record_denials", number(full_denials));
    put(row, "native_process_short_records", number(short_records));
}

#include "guest-pixel-format-state.h"
#include "guest-metal-capabilities.h"

/* Read the native Metal factory in a disposable framework worker. A returned
 * object establishes only this worker's default device, not a successful draw
 * or a visible app scene. No queue, buffer, texture or command is submitted.
 * The worker owns any factory object until process exit; no guessed ownership
 * convention or pointer is transferred to the long-lived service. */
static int graphics_state(object row) {
    int contract = display_contract(row);
    if (contract) return contract;
    put(row, "metal_default_device_known", boolean(0));
    object library = dlopen("/System/Library/Frameworks/Metal.framework/Metal", 2);
    object (*create_device)(void) = library ? dlsym(library, "MTLCreateSystemDefaultDevice") : 0;
    if (!create_device) return failure(row, "graphics_query_api_unavailable");
    object device = create_device();
    put(row, "metal_default_device_known", boolean(1));
    put(row, "metal_default_device_present", boolean(device != 0));
    if (device && accepts(device, "name", '@', 0)) {
        object name = get(device, "name");
        if (name && ((unsigned char (*)(object, object, object))message)(
                name, selector("isKindOfClass:"), get_class("NSString")))
            put(row, "metal_default_device_name", name);
    }
    if (device && accepts(device, "registryID", 'Q', 0))
        put(row, "metal_default_device_registry_id", number(
            ((unsigned long (*)(object, object))message)(device, selector("registryID"))));
    graphics_class_metadata(row, device);
    metal_capability_state(row, device);
    pixel_format_state(row);

    /* A bounded process snapshot distinguishes a named live process from an
     * accepted launch. It does not establish foreground scene or longevity. */
    graphics_process_snapshot(row);
    put(row, "application_scene_verified", boolean(0));
    put(row, "graphics_execution_verified", boolean(0));
    put(row, "ok", boolean(1));
    return emit(row);
}
