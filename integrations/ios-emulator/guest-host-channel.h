/* Operations that use a second guest channel, or look at the guest's hardware
 * and network, for the control service (guest-app-control.c).
 *
 * receive_bundle   takes an .app directory from the host over vsock port 1342,
 *                  so an app can be installed on a running phone.
 * policy_add       names one more (cdhash, identifier) in the adapter's boot's
 *                  table. Nothing is trusted by this alone: the executable
 *                  still needs the trust-cache entry only the host can write.
 * debug_server     listens on vsock port 1343 and hands the host's connection
 *                  to the system's debugserver, attaching to one process.
 * network_state    the interfaces' IPv4 addresses, and one name lookup.
 * hardware_probe   whether IOKit has a service of one class.
 * gestalt          one MobileGestalt answer, by its public key name.
 * developer_mode   status, or enable: arms iOS's Developer Mode, which takes
 *                  effect at the next boot. debugserver needs it.
 * keyboard_layout  the guest's language, which decides how iOS reads a
 *                  hardware keyboard's keys.
 *
 * Only the hypervisor's own CID may connect to either port (host_cid 2).
 */
#ifndef TOMACCO_GUEST_HOST_CHANNEL_H
#define TOMACCO_GUEST_HOST_CHANNEL_H

/* Darwin's AF_VSOCK ABI, from XNU bsd/sys/vsock.h. */
struct tomacco_vsock_address { unsigned char length, family; unsigned short reserved; unsigned port, cid; };
_Static_assert(sizeof(struct tomacco_vsock_address) == 12, "Darwin sockaddr_vm ABI");
enum { TOMACCO_BUNDLE_PORT = 1342, TOMACCO_DEBUG_PORT = 1343, TOMACCO_HOST_CID = 2,
       TOMACCO_BUNDLE_MAX_FILES = 65536, TOMACCO_BUNDLE_PATH_BYTES = 1024 };
#define TOMACCO_BUNDLE_ROOT "/private/var/mobile/Media/.TomaccoHot"
#define TOMACCO_DEBUGSERVER "/usr/libexec/debugserver"
#define TOMACCO_DEBUG_LOG "/private/var/mobile/Library/Logs/io.github.marselnenaj.tomacco.debugserver.jsonl"

/* A relative path inside the bundle: components of ordinary names, no "." or
 * "..", no empty component, and the first one is the bundle itself. */
static int bundle_path_valid(const char *path, unsigned long length) {
    if (!length || length >= TOMACCO_BUNDLE_PATH_BYTES || path[0] == '/') return 0;
    unsigned long start = 0;
    for (unsigned long index = 0; index <= length; ++index) {
        if (index < length && !path[index]) return 0;
        if (index < length && path[index] != '/') continue;
        unsigned long size = index - start;
        if (!size || size > 255) return 0;
        if (path[start] == '.' && (size == 1 || (size == 2 && path[start + 1] == '.'))) return 0;
        start = index + 1;
    }
    unsigned long first = 0;
    while (first < length && path[first] != '/') ++first;
    return first > 4 && path[first - 4] == '.' && path[first - 3] == 'a' && path[first - 2] == 'p' && path[first - 1] == 'p';
}

static int bundle_read_all(int file, void *buffer, unsigned long size) {
    unsigned char *bytes = buffer;
    unsigned long used = 0;
    while (used < size) {
        long count = read(file, bytes + used, size - used);
        if (count < 0 && *__error() == 4) continue;
        if (count <= 0) return 0;
        used += (unsigned long)count;
    }
    return 1;
}

static int bundle_make_parents(char *path) {
    for (char *cursor = path + 1; *cursor; ++cursor) {
        if (*cursor != '/') continue;
        *cursor = 0;
        int made = mkdir(path, 0755);
        int saved = *__error();
        *cursor = '/';
        if (made && saved != 17) return 0;
    }
    return 1;
}

/* One listening socket on this port, for the hypervisor's connections.
 * Returns the descriptor or a negative errno. */
static int vsock_listener(unsigned port) {
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*socket_fn)(int, int, int) = dlsym(system, "socket");
    int (*bind_fn)(int, const void *, unsigned) = dlsym(system, "bind");
    int (*listen_fn)(int, int) = dlsym(system, "listen");
    if (!socket_fn || !bind_fn || !listen_fn) return -78;
    int server = socket_fn(40, 1, 0);
    if (server < 0) return -*__error();
    struct tomacco_vsock_address local = {12, 40, 0, port, 0xffffffffu};
    if (bind_fn(server, &local, sizeof(local)) || listen_fn(server, 1)) {
        int saved = *__error();
        close(server);
        return -saved;
    }
    return server;
}

static int vsock_accept_host(int server) {
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*accept_fn)(int, void *, unsigned *) = dlsym(system, "accept");
    if (!accept_fn) return -78;
    for (;;) {
        struct tomacco_vsock_address peer = {0};
        unsigned size = sizeof(peer);
        int client = accept_fn(server, &peer, &size);
        if (client < 0 && *__error() == 4) continue;
        if (client < 0) return -*__error();
        if (size == sizeof(peer) && peer.family == 40 && peer.cid == TOMACCO_HOST_CID) return client;
        close(client);
    }
}

/* Files, each: u32 path bytes, u32 mode, u64 size (little-endian), the path,
 * then the content; a path length of 0 ends the bundle. The earlier bundle is
 * removed first, so one copy exists at most and nothing piles up. */
static int receive_bundle(object row) {
    remove_package = dlsym(dlopen("/usr/lib/libSystem.B.dylib", 2), "removefile");
    if (!remove_package) return failure(row, "staging_api_unavailable");
    if (remove_package(TOMACCO_BUNDLE_ROOT, 0, 1) && *__error() != 2) {
        put(row, "native_error", filesystem_error(*__error(), TOMACCO_BUNDLE_ROOT, 0));
        return failure(row, "bundle_cleanup_failed");
    }
    if (mkdir(TOMACCO_BUNDLE_ROOT, 0700)) {
        put(row, "native_error", filesystem_error(*__error(), TOMACCO_BUNDLE_ROOT, 0));
        return failure(row, "bundle_directory_failed");
    }
    int server = vsock_listener(TOMACCO_BUNDLE_PORT);
    if (server < 0) { put(row, "errno", number((unsigned)-server)); return failure(row, "vsock_unavailable"); }
    int client = vsock_accept_host(server);
    close(server);
    if (client < 0) { put(row, "errno", number((unsigned)-client)); return failure(row, "vsock_unavailable"); }
    char name[TOMACCO_BUNDLE_PATH_BYTES], target[TOMACCO_BUNDLE_PATH_BYTES + sizeof(TOMACCO_BUNDLE_ROOT) + 1];
    char first[256] = {0};
    unsigned long files = 0, bytes = 0;
    static unsigned char block[1u << 18];
    const char *refused = 0;
    for (;;) {
        unsigned char header[16];
        if (!bundle_read_all(client, header, sizeof(header))) { refused = "bundle_stream_ended"; break; }
        unsigned length = header[0] | header[1] << 8 | header[2] << 16 | (unsigned)header[3] << 24;
        unsigned mode = header[4] | header[5] << 8 | header[6] << 16 | (unsigned)header[7] << 24;
        unsigned long size = 0;
        for (unsigned index = 0; index < 8; ++index) size |= (unsigned long)header[8 + index] << (8 * index);
        if (!length) break;
        if (length >= sizeof(name) || ++files > TOMACCO_BUNDLE_MAX_FILES || (mode != 0644 && mode != 0755) ||
            !bundle_read_all(client, name, length) || !bundle_path_valid(name, length)) {
            refused = "bundle_entry_invalid"; break;
        }
        name[length] = 0;
        if (!first[0]) {
            unsigned end = 0;
            while (name[end] && name[end] != '/') ++end;
            for (unsigned index = 0; index < end; ++index) first[index] = name[index];
        }
        int size_written = snprintf(target, sizeof(target), "%s/%s", TOMACCO_BUNDLE_ROOT, name);
        if (size_written <= 0 || (unsigned long)size_written >= sizeof(target) || !bundle_make_parents(target)) {
            refused = "bundle_directory_failed"; break;
        }
        int file = open(target, 0xb01, mode); /* WRONLY|CREAT|EXCL|NOFOLLOW */
        if (file < 0) { put(row, "native_error", filesystem_error(*__error(), target, 0)); refused = "bundle_write_failed"; break; }
        unsigned long left = size;
        while (left && !refused) {
            unsigned long part = left < sizeof(block) ? left : sizeof(block);
            if (!bundle_read_all(client, block, part)) { refused = "bundle_stream_ended"; break; }
            for (unsigned long used = 0; used < part;) {
                long count = write(file, block + used, part - used);
                if (count < 0 && *__error() == 4) continue;
                if (count <= 0) { put(row, "native_error", filesystem_error(*__error(), target, 0)); refused = "bundle_write_failed"; break; }
                used += (unsigned long)count;
            }
            left -= part;
        }
        bytes += size - left;
        close(file);
        if (refused) break;
    }
    close(client);
    put(row, "files", number(files));
    put(row, "bytes", number(bytes));
    if (!refused && !first[0]) refused = "bundle_empty";
    if (refused) {
        remove_package(TOMACCO_BUNDLE_ROOT, 0, 1);
        return failure(row, refused);
    }
    snprintf(target, sizeof(target), "%s/%s", TOMACCO_BUNDLE_ROOT, first);
    put(row, "path", string(target));
    put(row, "ok", boolean(1));
    return emit(row);
}

/* <40 lowercase hex digits>:<bundle identifier> */
static int policy_add_argument(const char *argument, unsigned char *cdhash) {
    if (!argument) return 0;
    for (unsigned index = 0; index < 40; ++index) {
        char c = argument[index];
        unsigned value = c >= '0' && c <= '9' ? (unsigned)(c - '0') : c >= 'a' && c <= 'f' ? (unsigned)(c - 'a') + 10 : 16;
        if (value == 16) return 0;
        if (cdhash) {
            if (index % 2) cdhash[index / 2] |= (unsigned char)value;
            else cdhash[index / 2] = (unsigned char)(value << 4);
        }
    }
    if (argument[40] != ':') return 0;
    const char *identifier = argument + 41;
    unsigned length = 0;
    while (identifier[length]) {
        if (!tomacco_policy_identifier_character((unsigned char)identifier[length])) return 0;
        if (++length >= TOMACCO_POLICY_IDENTIFIER_BYTES) return 0;
    }
    return length && identifier[0] != '.' && identifier[0] != '-';
}

static int policy_add(object row, const char *argument) {
    unsigned char cdhash[TOMACCO_POLICY_CDHASH_BYTES];
    if (!policy_add_argument(argument, cdhash)) return failure(row, "invalid_policy_entry");
    static unsigned char table[TOMACCO_POLICY_TABLE_BYTES];
    /* The setup helper creates the file for the mobile user; it lies where
     * installd's sandbox maps the adapter from. */
    int file = open(TOMACCO_HOT_POLICY, 0x102); /* RDWR|NOFOLLOW */
    if (file < 0) { put(row, "native_error", filesystem_error(*__error(), TOMACCO_HOT_POLICY, 0)); return failure(row, "hot_policy_unavailable"); }
    unsigned long used = 0;
    for (;;) {
        long count = read(file, table + used, sizeof(table) - used);
        if (count < 0 && *__error() == 4) continue;
        if (count <= 0) break;
        used += (unsigned long)count;
    }
    unsigned entries = used ? tomacco_policy_count(table, used) : 0;
    if (used && !entries) { close(file); return failure(row, "hot_policy_invalid"); }
    unsigned long total = tomacco_policy_insert(table, entries, cdhash, argument + 41);
    if (!total) { close(file); return failure(row, "hot_policy_full"); }
    int failed = lseek(file, 0, 0) < 0;
    for (unsigned long written = 0; !failed && written < total;) {
        long count = write(file, table + written, total - written);
        if (count < 0 && *__error() == 4) continue;
        if (count <= 0) failed = 1;
        else written += (unsigned long)count;
    }
    if (failed || fsync(file)) {
        int saved = *__error();
        close(file);
        put(row, "native_error", filesystem_error(saved, TOMACCO_HOT_POLICY, 0));
        return failure(row, "hot_policy_write_failed");
    }
    close(file);
    put(row, "entries", number(tomacco_policy_count(table, total)));
    put(row, "ok", boolean(1));
    return emit(row);
}

/* A process to attach to: its decimal PID or its name (the executable's). */
static int debug_target_valid(const char *value) {
    if (!value || !value[0]) return 0;
    unsigned length = 0;
    for (; value[length]; ++length) {
        char c = value[length];
        if (length >= 64 || !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                              c == '.' || c == '_' || c == '-')) return 0;
    }
    return value[0] != '.' && value[0] != '-';
}

/* The helper's part, a fresh exec of this tool: the listener is inherited.
 * It waits two minutes for the host's connection, then becomes debugserver on
 * that socket. debugserver takes its gdb-remote stream from --fd. */
static int debug_accept(const char *listener, const char *target) {
    int server = 0;
    for (const char *cursor = listener; *cursor; ++cursor) {
        if (*cursor < '0' || *cursor > '9' || server > 4096) return 64;
        server = server * 10 + (*cursor - '0');
    }
    if (!debug_target_valid(target)) return 64;
    alarm(120);
    int log = open(TOMACCO_DEBUG_LOG, 0x309, 0644); /* WRONLY|CREAT|APPEND|NOFOLLOW */
    if (log >= 0) { dup2(log, 1); dup2(log, 2); close(log); }
    int client = vsock_accept_host(server);
    close(server);
    if (client < 0) { dprintf(2, "{\"event\":\"debug-accept\",\"errno\":%d}\n", -client); return 74; }
    alarm(0);
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*execute)(const char *, char *const [], char *const []) = dlsym(system, "execve");
    char ***(*environment)(void) = dlsym(system, "_NSGetEnviron");
    if (!execute || !environment) return 69;
    char descriptor[32], attach[96];
    snprintf(descriptor, sizeof(descriptor), "--fd=%d", client);
    snprintf(attach, sizeof(attach), "--attach=%s", target);
    char *arguments[] = {"debugserver", descriptor, attach, 0};
    dprintf(2, "{\"event\":\"debug-accept\",\"target\":\"%s\",\"fd\":%d}\n", target, client);
    execute(TOMACCO_DEBUGSERVER, arguments, *environment());
    dprintf(2, "{\"event\":\"debug-accept\",\"exec_errno\":%d}\n", *__error());
    return 69;
}

static int debug_server(object row, const char *target, const char *executable) {
    int (*access_fn)(const char *, int) = dlsym(dlopen("/usr/lib/libSystem.B.dylib", 2), "access");
    if (access_fn && access_fn(TOMACCO_DEBUGSERVER, 1)) {
        put(row, "native_error", filesystem_error(*__error(), TOMACCO_DEBUGSERVER, 0));
        return failure(row, "debugserver_unavailable");
    }
    int server = vsock_listener(TOMACCO_DEBUG_PORT);
    if (server < 0) {
        put(row, "errno", number((unsigned)-server));
        return failure(row, -server == 48 ? "debug_port_busy" : "vsock_unavailable");
    }
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*spawn_fn)(int *, const char *, const void *, const void *, char *const [], char *const []) = dlsym(system, "posix_spawn");
    char ***(*environment)(void) = dlsym(system, "_NSGetEnviron");
    if (!spawn_fn || !environment) { close(server); return failure(row, "debug_spawn_failed"); }
    char descriptor[32];
    snprintf(descriptor, sizeof(descriptor), "%d", server);
    char *arguments[] = {(char *)executable, "debug-accept", descriptor, (char *)target, 0};
    int pid = 0;
    int result = spawn_fn(&pid, executable, 0, 0, arguments, *environment());
    close(server);
    if (result) { put(row, "errno", number((unsigned)result)); return failure(row, "debug_spawn_failed"); }
    put(row, "port", number(TOMACCO_DEBUG_PORT));
    put(row, "target", string(target));
    put(row, "helper_pid", number((unsigned)pid));
    put(row, "ok", boolean(1));
    return emit(row);
}

/* Darwin netinet/in.h and sys/socket.h: AF_INET is 2; struct ifaddrs starts
 * with next, name, flags, then the address. */
struct tomacco_ifaddrs {
    struct tomacco_ifaddrs *next; char *name; unsigned flags;
    unsigned char *address, *netmask, *destination; void *data;
};

static int network_state(object row) {
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*interfaces)(struct tomacco_ifaddrs **) = dlsym(system, "getifaddrs");
    void (*release)(struct tomacco_ifaddrs *) = dlsym(system, "freeifaddrs");
    int (*lookup)(const char *, const char *, const void *, void **) = dlsym(system, "getaddrinfo");
    void (*release_lookup)(void *) = dlsym(system, "freeaddrinfo");
    if (!interfaces || !release || !lookup || !release_lookup) return failure(row, "network_api_unavailable");
    struct tomacco_ifaddrs *list = 0;
    if (interfaces(&list)) { put(row, "errno", number((unsigned)*__error())); return failure(row, "network_api_unavailable"); }
    object rows = get(get_class("NSMutableArray"), "array");
    for (struct tomacco_ifaddrs *entry = list; entry; entry = entry->next) {
        /* A link-level or IPv6 row names the interface as well; only AF_INET
         * rows carry the address this reports. */
        if (!entry->address || entry->address[1] != 2) continue;
        char text[32];
        snprintf(text, sizeof(text), "%u.%u.%u.%u", entry->address[4], entry->address[5], entry->address[6], entry->address[7]);
        object item = get(get_class("NSMutableDictionary"), "dictionary");
        put(item, "name", string(entry->name));
        put(item, "ipv4", string(text));
        put(item, "up", boolean((entry->flags & 1) != 0));
        ((void (*)(object, object, object))message)(rows, selector("addObject:"), item);
    }
    release(list);
    put(row, "interfaces", rows);
    void *found = 0;
    int resolved = lookup("example.com", "443", 0, &found);
    put(row, "lookup_host", string("example.com"));
    put(row, "lookup_error", number((unsigned)resolved));
    put(row, "lookup_resolved", boolean(!resolved && found));
    if (found) release_lookup(found);
    put(row, "ok", boolean(1));
    return emit(row);
}

/* An IOKit class name. */
static int hardware_class_valid(const char *value) {
    if (!value || !value[0]) return 0;
    unsigned length = 0;
    for (; value[length]; ++length) {
        char c = value[length];
        if (length >= 96 || !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_')) return 0;
    }
    return 1;
}

static int hardware_probe(object row, const char *name) {
    object iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", 2);
    object (*matching)(const char *) = iokit ? dlsym(iokit, "IOServiceMatching") : 0;
    int (*services)(unsigned, object, unsigned *) = iokit ? dlsym(iokit, "IOServiceGetMatchingServices") : 0;
    unsigned (*next)(unsigned) = iokit ? dlsym(iokit, "IOIteratorNext") : 0;
    int (*release)(unsigned) = iokit ? dlsym(iokit, "IOObjectRelease") : 0;
    if (!matching || !services || !next || !release) return failure(row, "iokit_unavailable");
    unsigned iterator = 0, count = 0, entry;
    int result = services(0, matching(name), &iterator);
    if (result) { put(row, "kern_return", number((unsigned)result)); return failure(row, "iokit_query_failed"); }
    while ((entry = next(iterator))) { release(entry); ++count; }
    release(iterator);
    put(row, "class", string(name));
    put(row, "services", number(count));
    put(row, "ok", boolean(1));
    return emit(row);
}

/* A MobileGestalt key: letters, digits and '-'. */
static int gestalt_key_valid(const char *value) {
    if (!value || !value[0]) return 0;
    unsigned length = 0;
    for (; value[length]; ++length) {
        char c = value[length];
        if (length >= 96 || !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '-')) return 0;
    }
    return 1;
}

static int gestalt(object row, const char *key) {
    object library = dlopen("/usr/lib/libMobileGestalt.dylib", 2);
    object (*answer)(object, object) = library ? dlsym(library, "MGCopyAnswer") : 0;
    if (!answer) return failure(row, "gestalt_unavailable");
    object value = answer(string(key), 0);
    put(row, "key", string(key));
    put(row, "known", boolean(value != 0));
    if (value) put(row, "description", bounded_text(get(value, "description"), 1024));
    put(row, "ok", boolean(1));
    return emit(row);
}

/* com.apple.amfi.xpc, as TrollStore's and vphone's Developer Mode switch use it:
 * a CF dictionary with "action" 0 (arm) or 2 (status); the reply's "cfreply" holds
 * "success", "status" and "armed". Arming is what Settings' switch does: iOS asks
 * for confirmation at the next boot. Needs the entitlement
 * com.apple.private.amfi.developer-mode-control. */
static int developer_mode(object row, const char *argument) {
    object xpc = dlopen("/usr/lib/system/libxpc.dylib", 2);
    object foundation = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    object (*connect_service)(const char *, object, unsigned long long) = xpc ? dlsym(xpc, "xpc_connection_create_mach_service") : 0;
    void (*event_handler)(object, void (^)(object)) = xpc ? dlsym(xpc, "xpc_connection_set_event_handler") : 0;
    void (*resume)(object) = xpc ? dlsym(xpc, "xpc_connection_resume") : 0;
    object (*send_sync)(object, object) = xpc ? dlsym(xpc, "xpc_connection_send_message_with_reply_sync") : 0;
    object (*value)(object, const char *) = xpc ? dlsym(xpc, "xpc_dictionary_get_value") : 0;
    object (*to_xpc)(object) = foundation ? dlsym(foundation, "_CFXPCCreateXPCMessageWithCFObject") : 0;
    object (*from_xpc)(object) = foundation ? dlsym(foundation, "_CFXPCCreateCFObjectFromXPCMessage") : 0;
    if (!connect_service || !event_handler || !resume || !send_sync || !value || !to_xpc || !from_xpc)
        return failure(row, "developer_mode_api_unavailable");
    object connection = connect_service("com.apple.amfi.xpc", 0, 0);
    if (!connection) return failure(row, "developer_mode_service_unavailable");
    event_handler(connection, ^(object event) { (void)event; });
    resume(connection);
    object request = get(get_class("NSMutableDictionary"), "dictionary");
    int status_only = !strcmp(argument, "status");
    put(request, "action", number(status_only ? 2 : 0));
    object reply = send_sync(connection, to_xpc(request));
    object body = reply ? value(reply, "cfreply") : 0;
    object answer = body ? from_xpc(body) : 0;
    if (!answer || !((unsigned char (*)(object, object, object))message)(answer, selector("isKindOfClass:"), get_class("NSDictionary")))
        return failure(row, "developer_mode_no_answer");
    const char *keys[] = {"success", "status", "armed", "error"};
    for (unsigned index = 0; index < 4; ++index) {
        object field = with(answer, "objectForKey:", string(keys[index]));
        if (field) put(row, keys[index], field);
    }
    int (*sysctl_by_name)(const char *, void *, unsigned long *, void *, unsigned long) =
        dlsym(dlopen("/usr/lib/libSystem.B.dylib", 2), "sysctlbyname");
    int state = 0;
    unsigned long size = sizeof(state);
    if (sysctl_by_name && !sysctl_by_name("security.mac.amfi.developer_mode_status", &state, &size, 0, 0))
        put(row, "kernel_status", boolean(state != 0));
    put(row, "ok", boolean(1));
    return emit(row);
}

/* The guest's first language: iOS reads a hardware keyboard's keys with its
 * layout. Setting HardwareKeyboardLastSeenLayout in its
 * preferences changed nothing there, so the host types for this layout instead. */
static int keyboard_layout(object row) {
    object languages = with(get(get_class("NSUserDefaults"), "standardUserDefaults"), "objectForKey:", string("AppleLanguages"));
    if (!languages || !count(languages)) return failure(row, "keyboard_layout_unknown");
    object first = get(languages, "firstObject");
    const char *text = first ? get(first, "UTF8String") : 0;
    if (!text || !text[0] || !text[1]) return failure(row, "keyboard_layout_unknown");
    char language[3] = {text[0], text[1], 0};
    put(row, "language", string(language));
    put(row, "preferred_language", string(text));
    put(row, "ok", boolean(1));
    return emit(row);
}

#endif
