/* The guest's system time zone, set through iOS's own link daemon.
 *
 * iOS keeps the system zone as the link /var/db/timezone/localtime, which
 * points at a file under /var/db/timezone/zoneinfo/ (CoreFoundation reads
 * the link unless TZFILE or TZ is set, and resets every process's system
 * zone on the "com.apple.system.timezone" notification). Settings changes
 * it through libutil's tzlink(): a request to the privileged service
 * com.apple.tzlink (tzlinkd) carrying the name under "tz", answered with an
 * "error" number; the caller needs the entitlement com.apple.tzlink.allow.
 * A guest restored from the image keeps the image's zone (America/Los_
 * Angeles on this phone, eight hours behind a host in Europe/Vienna), since
 * the development setup finishes Setup without the zone step. The link is
 * on the Data volume, so a zone set once stays set for the runs started
 * from that disk; no reboot is needed, the running processes follow the
 * notification. Nothing else is configured.
 *
 * The argument names a zone file: one to three path components, each of 1
 * to 32 letters, digits, '_', '-' or '+', so "UTC", "Europe/Vienna",
 * "America/Argentina/Buenos_Aires" and "Etc/GMT+1"; never a dot or a
 * leading slash, so never a path outside the zoneinfo tree. A name without
 * a zone file is refused before anything is asked of the daemon.
 */
static int time_zone_argument(const char *name) {
    if (!name || !name[0]) return 0;
    unsigned components = 1, length = 0;
    for (unsigned long index = 0; name[index]; ++index) {
        char c = name[index];
        if (c == '/') {
            if (!length || ++components > 3) return 0;
            length = 0;
            continue;
        }
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              c == '_' || c == '-' || c == '+') || ++length > 32) return 0;
    }
    return length > 0;
}

static object time_zone_link(object row, const char *field) {
    void *system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    long (*read_link)(const char *, char *, unsigned long) = system ? dlsym(system, "readlink") : 0;
    char target[1024] = {0};
    long length = read_link ? read_link("/var/db/timezone/localtime", target, sizeof(target) - 1) : -1;
    if (length <= 0 || length >= (long)sizeof(target) - 1) {
        put(row, field, 0);
        return 0;
    }
    target[length] = 0;
    object value = string(target);
    put(row, field, value);
    return value;
}

static int set_time_zone(object row, const char *name) {
    if (!time_zone_argument(name)) return failure(row, "invalid_time_zone");
    if (!setup_virtual_device() || getuid() != 501) return failure(row, "virtual_setup_required");
    put(row, "argument", string(name));
    char path[192];
    snprintf(path, sizeof(path), "/var/db/timezone/zoneinfo/%s", name);
    /* The zoneinfo tree is reached through links; the zone file itself must exist. */
    int file = open(path, 0x1000004); /* RDONLY|NONBLOCK|CLOEXEC */
    if (file < 0) {
        put(row, "errno", number((unsigned)*__error()));
        return failure(row, "unknown_time_zone");
    }
    close(file);
    void *util = dlopen("/usr/lib/libutil.dylib", 2);
    int (*link)(const char *) = util ? dlsym(util, "tzlink") : 0;
    if (!link) return failure(row, "time_zone_api_unavailable");
    time_zone_link(row, "previous_link");
    int error = link(name);
    put(row, "link_error", number((unsigned)error));
    if (error) {
        put(row, "request_may_have_executed", boolean(1));
        put(row, "automatic_retry_safe", boolean(1));
        /* EPERM: tzlinkd found no com.apple.tzlink.allow entitlement on this program. */
        return failure(row, error == 1 ? "time_zone_not_permitted" : "time_zone_refused");
    }
    object current = time_zone_link(row, "link");
    const char *target = current ? get(current, "UTF8String") : 0;
    unsigned long target_length = target ? strlen(target) : 0, name_length = strlen(name);
    int applied = target && target_length > name_length && target[target_length - name_length - 1] == '/' &&
        !strcmp(target + target_length - name_length, name);
    if (!applied) return failure(row, "time_zone_not_applied");
    put(row, "time_zone", string(name));
    put(row, "ok", boolean(1));
    return emit(row);
}
