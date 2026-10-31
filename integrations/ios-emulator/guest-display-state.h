static void display_tuple_field(object details, object value) {
    if (value && ((unsigned char (*)(object, object, object))message)(
            value, selector("isKindOfClass:"), get_class("NSData")) &&
            ((unsigned long (*)(object, object))message)(value, selector("length")) == 24) {
        const unsigned char *bytes = get(value, "bytes");
        if (bytes) {
            char hex[49];
            for (unsigned index = 0; index < 24; ++index)
                snprintf(hex + index * 2, 3, "%02x", (unsigned)bytes[index]);
            put(details, "original_display_tuple_hex", string(hex));
        }
    }
}

/* Keep the boot value and the original MobileGestalt answer separate. The
 * pinned native answer reads scale from byte 12 of its 24-byte display tuple;
 * a cached answer can differ from the device tree supplied to a later boot.
 * These fixed, read-only queries run in the disposable framework worker. */
static void screen_display_fields(object details) {
    put(details, "main_screen_scale_known", boolean(0));
    put(details, "chosen_display_scale_known", boolean(0));
    object io = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", 2);
    unsigned (*entry)(unsigned, const char *) = io ? dlsym(io, "IORegistryEntryFromPath") : 0;
    object (*property)(unsigned, object, object, unsigned) = io ? dlsym(io, "IORegistryEntryCreateCFProperty") : 0;
    int (*release)(unsigned) = io ? dlsym(io, "IOObjectRelease") : 0;
    if (entry && property && release) {
        unsigned handle = entry(0, "IODeviceTree:/chosen");
        object value = handle ? property(handle, string("display-scale"), 0, 0) : 0;
        if (handle) release(handle);
        if (value && ((unsigned char (*)(object, object, object))message)(
                value, selector("isKindOfClass:"), get_class("NSData")) &&
                ((unsigned long (*)(object, object))message)(value, selector("length")) == 4) {
            const unsigned char *bytes = get(value, "bytes");
            if (bytes) {
                unsigned scale = (unsigned)bytes[0] | (unsigned)bytes[1] << 8 |
                    (unsigned)bytes[2] << 16 | (unsigned)bytes[3] << 24;
                put(details, "chosen_display_scale", number(scale));
                put(details, "chosen_display_scale_known", boolean(1));
            }
        }
        if (value) ((void (*)(object, object))message)(value, selector("release"));
    }
    object library = dlopen("/usr/lib/libMobileGestalt.dylib", 2);
    object (*answer)(object, object) = library ? dlsym(library, "MGCopyAnswer") : 0;
    if (!answer) return;
    /* The pinned getter calls MGCopyAnswer with a null second argument. */
    object value = answer(string("main-screen-scale"), 0);
    if (value && ((unsigned char (*)(object, object, object))message)(
            value, selector("isKindOfClass:"), get_class("NSNumber"))) {
        double scale = ((double (*)(object, object))message)(value, selector("doubleValue"));
        if (__builtin_isfinite(scale)) {
            put(details, "main_screen_scale", ((object (*)(object, object, double))message)(
                get_class("NSNumber"), selector("numberWithDouble:"), scale));
            put(details, "main_screen_scale_known", boolean(1));
            put(details, "main_screen_scale_positive", boolean(scale > 0));
        }
    }
    if (value) ((void (*)(object, object))message)(value, selector("release"));
    value = answer(string("oBbtJ8x+s1q0OkaiocPuog"), 0);
    display_tuple_field(details, value);
    if (value) ((void (*)(object, object))message)(value, selector("release"));
}

/* Clear only this diagnostic worker's in-process cache through the original
 * native entry point. The persistent cache and other processes are untouched;
 * a process clear does not clear the original system helper's cache. */
static int display_contract(object row) {
    if (!setup_virtual_device() || getuid() != 501)
        return failure(row, "virtual_display_query_required");
    object version = with(get_class("NSDictionary"), "dictionaryWithContentsOfFile:",
        string("/System/Library/CoreServices/SystemVersion.plist"));
    object product = with(version, "objectForKey:", string("ProductVersion"));
    if (!product || !((unsigned char (*)(object, object, object))message)(product,
            selector("isKindOfClass:"), get_class("NSString")))
        return failure(row, "display_query_version_unavailable");
    const char *text = get(product, "UTF8String");
    if (!text || strcmp(text, "27.0.1"))
        return failure(row, "display_query_contract_unqualified");
    return 0;
}

static int display_state(object row) {
    int contract = display_contract(row);
    if (contract) return contract;
    object cached = get(get_class("NSMutableDictionary"), "dictionary");
    put(row, "native_display", cached);
    screen_display_fields(cached);
    object library = dlopen("/usr/lib/libMobileGestalt.dylib", 2);
    void (*clear_process_cache)(void) = library ? dlsym(library, "_MGClearInProcessCache") : 0;
    put(row, "persistent_cache_modified", boolean(0));
    put(row, "process_cache_cleared", boolean(clear_process_cache != 0));
    if (clear_process_cache) {
        clear_process_cache();
        object fresh = get(get_class("NSMutableDictionary"), "dictionary");
        put(row, "native_display_after_process_cache_clear", fresh);
        screen_display_fields(fresh);
    }
    /* Native constructor: path, initial keys, values, count, loader. All
     * null creates an empty, memory-only cache with no persistent loader.
     * Query the tuple directly: its scale consumer otherwise recursively
     * consults the original global cache again. */
    object (*create_cache)(const char *, const object *, const object *, long, object) =
        library ? dlsym(library, "MGCreateInMemoryCache") : 0;
    object (*cache_answer)(object, object, object, int *) =
        library ? dlsym(library, "MGCopyAnswerWithCache") : 0;
    void (*destroy_cache)(object) = library ? dlsym(library, "MGCacheDestroy") : 0;
    put(row, "memory_only_cache_created", boolean(0));
    if (create_cache && cache_answer && destroy_cache) {
        object cache = create_cache(0, 0, 0, 0, 0);
        if (cache) {
            put(row, "memory_only_cache_created", boolean(1));
            object details = get(get_class("NSMutableDictionary"), "dictionary");
            put(row, "native_display_from_memory_only_cache", details);
            object value = cache_answer(string("oBbtJ8x+s1q0OkaiocPuog"), 0, cache, 0);
            display_tuple_field(details, value);
            if (value) ((void (*)(object, object))message)(value, selector("release"));
            destroy_cache(cache);
        }
    }
    /* Original 27.0.1 _MGSetServer sets only this worker's local dispatch
     * flag. Without it an empty cache still queries the system helper via
     * libMobileGestaltExtensions. A second empty cache now reaches the
     * original tuple producer and its IORegistry reader, with no persistent
     * loader or substituted answer. This disposable worker exits afterward. */
    void (*set_local_dispatch)(void) = library ? dlsym(library, "_MGSetServer") : 0;
    put(row, "local_native_dispatch_available", boolean(set_local_dispatch != 0));
    if (create_cache && cache_answer && destroy_cache && set_local_dispatch) {
        object cache = create_cache(0, 0, 0, 0, 0);
        if (cache) {
            set_local_dispatch();
            object details = get(get_class("NSMutableDictionary"), "dictionary");
            put(row, "native_display_from_local_memory_only_cache", details);
            object value = cache_answer(string("oBbtJ8x+s1q0OkaiocPuog"), 0, cache, 0);
            display_tuple_field(details, value);
            if (value) ((void (*)(object, object))message)(value, selector("release"));
            destroy_cache(cache);
        }
    }
    put(row, "application_scene_verified", boolean(0));
    put(row, "ok", boolean(1));
    return emit(row);
}

/* Ask the original helper to rebuild its own cache only after a local native
 * computation agrees with the boot scale. The mobile worker cannot recover
 * valid framebuffer geometry, so its tuple must never seed the global cache.
 * The helper's bool reports native cache validity, not a verified rebuild.
 * A failed unlink can return an already-valid cache without replacing it.
 * A new display_state worker must confirm scale before UI restart. */
static int display_refresh(object row) {
    int contract = display_contract(row);
    if (contract) return contract;
    if (!request_id) return failure(row, "display_refresh_identity_required");
    object before = get(get_class("NSMutableDictionary"), "dictionary");
    put(row, "native_display_before", before);
    screen_display_fields(before);
    object known = with(before, "objectForKey:", string("chosen_display_scale_known"));
    object chosen = with(before, "objectForKey:", string("chosen_display_scale"));
    unsigned expected = chosen ? ((unsigned (*)(object, object))message)(chosen, selector("unsignedIntValue")) : 0;
    if (!known || !((unsigned char (*)(object, object))message)(known, selector("boolValue")) ||
            !expected || expected > 4)
        return failure(row, "display_refresh_boot_scale_unqualified");
    object library = dlopen("/usr/lib/libMobileGestalt.dylib", 2);
    object (*create_cache)(const char *, const object *, const object *, long, object) =
        library ? dlsym(library, "MGCreateInMemoryCache") : 0;
    object (*cache_answer)(object, object, object, int *) =
        library ? dlsym(library, "MGCopyAnswerWithCache") : 0;
    void (*destroy_cache)(object) = library ? dlsym(library, "MGCacheDestroy") : 0;
    void (*set_local_dispatch)(void) = library ? dlsym(library, "_MGSetServer") : 0;
    /* Pinned original extension dispatches proxyRebuildCache, whose native
     * ObjC method and reply callback both return a bool. No cache or tuple
     * argument is passed to the helper. */
    unsigned char (*rebuild_on_helper)(void) = library ? dlsym(library, "_MGWriteCacheOnHelper") : 0;
    if (!create_cache || !cache_answer || !destroy_cache ||
            !set_local_dispatch || !rebuild_on_helper)
        return failure(row, "display_refresh_api_unavailable");
    object current = with(before, "objectForKey:", string("main_screen_scale"));
    if (current && ((double (*)(object, object))message)(current, selector("doubleValue")) == (double)expected) {
        put(row, "persistent_cache_rebuild_required", boolean(0));
        put(row, "restart_required", boolean(0));
        put(row, "ok", boolean(1));
        return emit(row);
    }
    object cache = create_cache(0, 0, 0, 0, 0);
    if (!cache) return failure(row, "display_refresh_memory_cache_unavailable");
    set_local_dispatch();
    object tuple = cache_answer(string("oBbtJ8x+s1q0OkaiocPuog"), 0, cache, 0);
    object fresh = get(get_class("NSMutableDictionary"), "dictionary");
    put(row, "native_display_from_local_memory_only_cache", fresh);
    display_tuple_field(fresh, tuple);
    int valid = 0;
    if (tuple && ((unsigned char (*)(object, object, object))message)(
            tuple, selector("isKindOfClass:"), get_class("NSData")) &&
            ((unsigned long (*)(object, object))message)(tuple, selector("length")) == 24) {
        const unsigned char *bytes = get(tuple, "bytes");
        if (bytes) {
            union { unsigned bits; float value; } scale;
            scale.bits = (unsigned)bytes[12] | (unsigned)bytes[13] << 8 |
                (unsigned)bytes[14] << 16 | (unsigned)bytes[15] << 24;
            valid = __builtin_isfinite(scale.value) && scale.value == (float)expected;
        }
    }
    if (tuple) ((void (*)(object, object))message)(tuple, selector("release"));
    destroy_cache(cache);
    if (!valid) return failure(row, "display_refresh_native_tuple_mismatch");
    put(row, "request_may_have_executed", boolean(1));
    put(row, "automatic_retry_safe", boolean(0));
    put(row, "persistent_cache_rebuild_required", boolean(1));
    unsigned char acknowledged = rebuild_on_helper();
    put(row, "native_cache_rebuild_acknowledged", boolean(acknowledged != 0));
    put(row, "cache_rebuild_effect_verified", boolean(0));
    put(row, "display_scale_readback_verified", boolean(0));
    put(row, "independent_readback_required", boolean(1));
    /* A negative reply can follow a partial mutation; it is not a refusal
     * before execution and never permits an automatic retry. */
    if (!acknowledged) return failure(row, "display_refresh_helper_not_acknowledged");
    put(row, "restart_required", boolean(1));
    put(row, "application_scene_verified", boolean(0));
    put(row, "ok", boolean(1));
    return emit(row);
}
