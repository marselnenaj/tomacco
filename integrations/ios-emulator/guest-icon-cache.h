/* Make iconservicesagent render every icon again.
 *
 * The agent keeps each icon it renders in its IconsCache store and serves it
 * again on later boots, so an icon rendered while the renderer drew it wrong
 * stays wrong after the renderer is fixed. Asking the agent to clear the store
 * itself (clearAllCachedItemsWithReply: over IconServices' own connection) was
 * refused: every request was interrupted (NSCocoaErrorDomain 4097) before a
 * reply while the agent kept running.
 *
 * This moves each IconsCache store aside instead, within the directory that
 * holds it and under a name that keeps it, so nothing is deleted and the move
 * can be undone. Stores are looked for only at fixed places: the mobile user's
 * Library/Caches and Library/Caches of each system group container (on iOS
 * 27.0.1 the store is systemgroup.com.apple.lsd.iconscache's). The running
 * agent, which runs as another user and is not signalled, renders an icon
 * again on its next request once the store is gone; SpringBoard is
 * restarted so that it requests every icon again. No preference, launchd job
 * or other file is changed.
 */
#define TOMACCO_ICON_STORE "com.apple.IconsCache"
#define TOMACCO_ICON_GROUPS "/private/var/containers/Shared/SystemGroup"

static int icon_cache_argument(const char *value) {
    return value && !strcmp(value, "clear");
}

/* Move <caches>/com.apple.IconsCache to <caches>/com.apple.IconsCache.<suffix>
 * when it is a real directory. Records the move or the refusal in rows. */
static void icon_cache_move(object moved, object refused, const char *caches, const char *suffix) {
    char from[1024], to[1024];
    if (snprintf(from, sizeof(from), "%s/%s", caches, TOMACCO_ICON_STORE) >= (int)sizeof(from) ||
        snprintf(to, sizeof(to), "%s/%s.%s", caches, TOMACCO_ICON_STORE, suffix) >= (int)sizeof(to))
        return;
    int store = open(from, 0x1100100); /* RDONLY|DIRECTORY|NOFOLLOW|CLOEXEC */
    if (store < 0) {
        unsigned error = (unsigned)*__error();
        if (error == 2) return; /* ENOENT: no store here */
        object row = get(get_class("NSMutableDictionary"), "dictionary");
        put(row, "path", string(from));
        put(row, "errno", number(error));
        ((void (*)(object, object, object))message)(refused, selector("addObject:"), row);
        return;
    }
    close(store);
    object row = get(get_class("NSMutableDictionary"), "dictionary");
    put(row, "path", string(from));
    if (rename(from, to)) {
        put(row, "errno", number((unsigned)*__error()));
        ((void (*)(object, object, object))message)(refused, selector("addObject:"), row);
        return;
    }
    put(row, "moved_to", string(to));
    ((void (*)(object, object, object))message)(moved, selector("addObject:"), row);
}

static int icon_cache(object row, const char *argument) {
    if (!icon_cache_argument(argument)) return failure(row, "invalid_icon_cache_request");
    if (!setup_virtual_device() || getuid() != 501) return failure(row, "virtual_mobile_guest_required");
    put(row, "argument", string(argument));
    void *(*from_fd)(int) = dlsym((void *)-2, "fdopendir");
    struct tomacco_crash_dirent *(*next_entry)(void *) = dlsym((void *)-2, "readdir");
    int (*close_directory)(void *) = dlsym((void *)-2, "closedir");
    if (!from_fd || !next_entry || !close_directory) return failure(row, "icon_cache_directory_api_unavailable");
    char suffix[64];
    snprintf(suffix, sizeof(suffix), "tomacco-moved-%d", getpid());
    object moved = get(get_class("NSMutableArray"), "array");
    object refused = get(get_class("NSMutableArray"), "array");
    put(row, "request_may_have_executed", boolean(1));
    put(row, "automatic_retry_safe", boolean(0));
    icon_cache_move(moved, refused, "/private/var/mobile/Library/Caches", suffix);
    int groups = open(TOMACCO_ICON_GROUPS, 0x1100100);
    unsigned scanned = 0;
    if (groups >= 0) {
        void *directory = from_fd(groups); /* owns groups */
        while (directory && scanned < 4096) {
            struct tomacco_crash_dirent *entry = next_entry(directory);
            if (!entry) break;
            ++scanned;
            unsigned length = entry->name_length;
            if (length >= 256 || entry->name[length] || entry->type != 4 /* DT_DIR */ ||
                !strcmp(entry->name, ".") || !strcmp(entry->name, ".."))
                continue;
            char caches[1024];
            if (snprintf(caches, sizeof(caches), "%s/%s/Library/Caches", TOMACCO_ICON_GROUPS, entry->name)
                    >= (int)sizeof(caches))
                continue;
            icon_cache_move(moved, refused, caches, suffix);
        }
        if (directory) close_directory(directory);
        else close(groups);
    } else {
        put(row, "groups_errno", number((unsigned)*__error()));
    }
    put(row, "groups_scanned", number(scanned));
    put(row, "stores_moved", moved);
    put(row, "stores_refused", refused);
    unsigned moved_count = ((unsigned (*)(object, object))message)(moved, selector("count"));
    if (!moved_count) return failure(row, "icon_cache_store_not_found");
    setup_storage_flush(row);
    int previous = setup_ui_process();
    if (previous <= 0) return failure(row, "ui_process_unavailable");
    put(row, "ui_previous_pid", number((unsigned)previous));
    if (kill(previous, 15)) {
        put(row, "ui_errno", number((unsigned)*__error()));
        return failure(row, "ui_restart_refused");
    }
    int current = 0;
    for (unsigned attempt = 0; attempt < 90; ++attempt) {
        sleep(1);
        current = setup_ui_process();
        if (current > 0 && current != previous) break;
    }
    if (current <= 0 || current == previous) return failure(row, "ui_restart_unconfirmed");
    put(row, "ui_pid", number((unsigned)current));
    /* Icons appear as SpringBoard requests them; the agent renders each once. */
    put(row, "applies_to_new_requests", boolean(1));
    put(row, "application_scene_verified", boolean(0));
    put(row, "ok", boolean(1));
    return emit(row);
}
