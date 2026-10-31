/* Native first-run state for an owned virtual development device.
 * Activation is queried through the original daemon, never manufactured.
 * Setup preferences are written only for an already activated vphone guest.
 */
static int setup_virtual_device(void) {
    object io = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", 2);
    unsigned (*entry)(unsigned, const char *) = dlsym(io, "IORegistryEntryFromPath");
    object (*property)(unsigned, object, object, unsigned) = dlsym(io, "IORegistryEntryCreateCFProperty");
    int (*release)(unsigned) = dlsym(io, "IOObjectRelease");
    if (!io || !entry || !property || !release) return 0;
    const char *paths[] = {"IODeviceTree:/", "IODeviceTree:/defaults"};
    const char *keys[] = {"target-sub-type", "vmm-present"};
    const unsigned char expected[][12] = {"VPHONE600AP", {1, 0, 0, 0}};
    const unsigned long sizes[] = {12, 4};
    for (unsigned index = 0; index < 2; ++index) {
        unsigned handle = entry(0, paths[index]);
        if (!handle) return 0;
        object value = property(handle, string(keys[index]), 0, 0);
        release(handle);
        int valid = value && ((unsigned char (*)(object, object, object))message)(
            value, selector("isKindOfClass:"), get_class("NSData"));
        if (valid) {
            unsigned long size = ((unsigned long (*)(object, object))message)(value, selector("length"));
            const unsigned char *bytes = get(value, "bytes");
            valid = bytes && size == sizes[index];
            for (unsigned long offset = 0; valid && offset < size; ++offset)
                if (bytes[offset] != expected[index][offset]) valid = 0;
        }
        if (value) ((void (*)(object, object))message)(value, selector("release"));
        if (!valid) return 0;
    }
    return 1;
}

/* cfprefsd writes its plist without a device flush. Commit the Data volume
 * so an emulator stop cannot discard an acknowledged preference write. */
static void setup_storage_flush(object row) {
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    void (*sync_fn)(void) = dlsym(system, "sync");
    int (*fcntl_fn)(int, int, ...) = dlsym(system, "fcntl");
    int file = -1, flushed = 0;
    for (unsigned attempt = 0; attempt < 10 && file < 0; ++attempt) {
        file = open("/private/var/mobile/Library/Preferences/com.apple.purplebuddy.plist", 0x1000100); /* RDONLY|NOFOLLOW|CLOEXEC */
        if (file < 0) sleep(1);
    }
    put(row, "preference_file_present", boolean(file >= 0));
    if (sync_fn) sync_fn();
    if (file >= 0) {
        flushed = sync_fn && fcntl_fn && !fcntl_fn(file, 51); /* F_FULLFSYNC */
        close(file);
    }
    put(row, "storage_flushed", boolean(flushed));
}

static int setup_ui_process(void) {
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*list)(void *, int) = dlsym(system, "proc_listallpids");
    int (*name)(int, void *, unsigned) = dlsym(system, "proc_name");
    static int pids[4096];
    if (!list || !name) return -1;
    int count = list(pids, sizeof(pids));
    if (count < 0 || count > 4096) return -1;
    for (int index = 0; index < count; ++index) {
        char text[64] = {0};
        if (pids[index] > 1 && name(pids[index], text, sizeof(text)) > 0 && !strcmp(text, "SpringBoard"))
            return pids[index];
    }
    return 0;
}

/* SpringBoard reads first-run state at startup. launchd owns its restart;
 * this neither activates the device nor changes any preference. */
static int setup_restart_ui(object row) {
    if (!setup_virtual_device() || getuid() != 501) return failure(row, "virtual_setup_required");
    if (!request_id) return failure(row, "setup_request_identity_required");
    setup_storage_flush(row);
    int previous = setup_ui_process();
    if (previous <= 0) return failure(row, "ui_process_unavailable");
    put(row, "previous_pid", number((unsigned)previous));
    if (kill(previous, 15)) {
        put(row, "errno", number((unsigned)*__error()));
        return failure(row, "ui_restart_refused");
    }
    put(row, "request_may_have_executed", boolean(1));
    put(row, "automatic_retry_safe", boolean(0));
    int current = 0;
    for (unsigned attempt = 0; attempt < 90; ++attempt) {
        sleep(1);
        current = setup_ui_process();
        if (current > 0 && current != previous) break;
    }
    if (current <= 0 || current == previous) return failure(row, "ui_restart_unconfirmed");
    put(row, "ui_pid", number((unsigned)current));
    put(row, "request_accepted", boolean(1));
    put(row, "application_scene_verified", boolean(0));
    put(row, "ok", boolean(1));
    return emit(row);
}

/* iOS draws its glass materials, blurs and transitions with effects that this
 * machine's renderer cannot draw correctly and its processor cannot afford:
 * on the Home screen about half of the processor was found in the software
 * renderer's filters for them. iOS has its own switches for plainer surfaces
 * and fewer animations (Settings, Accessibility: Reduce Transparency, Reduce
 * Motion). This sets or clears exactly those two, through the system's own
 * setters, which store the user's preference and notify running processes,
 * and reads them back. Nothing else is configured. */
static int display_effects(object row, const char *argument) {
    object library = dlopen("/usr/lib/libAccessibility.dylib", 2);
    unsigned char (*transparency)(void) = library ? dlsym(library, "_AXSEnhanceBackgroundContrastEnabled") : 0;
    void (*set_transparency)(unsigned char) = library ? dlsym(library, "_AXSSetEnhanceBackgroundContrastEnabled") : 0;
    unsigned char (*motion)(void) = library ? dlsym(library, "_AXSReduceMotionEnabled") : 0;
    void (*set_motion)(unsigned char) = library ? dlsym(library, "_AXSSetReduceMotionEnabled") : 0;
    if (!transparency || !set_transparency || !motion || !set_motion)
        return failure(row, "incompatible_accessibility_api");
    if (!setup_virtual_device() || getuid() != 501) return failure(row, "virtual_setup_required");
    unsigned char reduced = !strcmp(argument, "reduced");
    put(row, "argument", string(argument));
    put(row, "previous_reduce_transparency", boolean(transparency() != 0));
    put(row, "previous_reduce_motion", boolean(motion() != 0));
    set_transparency(reduced);
    set_motion(reduced);
    unsigned char now_transparency = transparency() != 0, now_motion = motion() != 0;
    put(row, "reduce_transparency", boolean(now_transparency));
    put(row, "reduce_motion", boolean(now_motion));
    if (now_transparency != reduced || now_motion != reduced) return failure(row, "effects_not_applied");
    put(row, "ok", boolean(1));
    return emit(row);
}

/* "original" or a whole factor 2..30; zero means invalid. */
static unsigned watchdog_scale_argument(const char *argument) {
    if (!argument) return 0;
    if (!strcmp(argument, "original")) return 1;
    unsigned value = 0;
    for (const char *digit = argument; *digit; ++digit) {
        if (*digit < '0' || *digit > '9' || digit - argument >= 2) return 0;
        value = value * 10 + (unsigned)(*digit - '0');
    }
    return argument[0] != '0' && value >= 2 && value <= 30 ? value : 0;
}

/* FrontBoard bounds app launch, resume and scene creation with wall-clock
 * watchdogs sized for Apple hardware. Under this machine's processor
 * emulation Settings, the widget renderer and the lock screen's wallpaper
 * poster exceed the ten-second scene-create allowance and are terminated with
 * 0x8badf00d. Recovered from the pinned iOS 27.0.1 FrontBoard:
 * FBApplicationProcessWatchdogPolicy reads two factors from SpringBoard's
 * preferences, also on customer installs. FBLaunchWatchdogScale
 * (+_defaultLaunchScale) multiplies the launch allowance;
 * FBLaunchWatchdogResumeScale (+_defaultResumeScale, otherwise the
 * DeviceSceneUpdateTimeLimitScale gestalt answer) multiplies resume and every
 * foreground scene-create and scene-update allowance, through
 * _queue_watchdogScalingFactorForAppInfo:isResume:YES. Setting only the first
 * left the scene allowances at their ten and thirty seconds. No factor reaches
 * the move to the background: watchdogPolicyForProcess:eventContext: gives it a
 * fixed ten seconds, which a slow guest exceeds, so nearly every app that left
 * the foreground was ended and its next open was a cold
 * launch. BKNoWatchdogs (-[FBProcess _shouldWatchdogWithDeclineReason:]) is the
 * only global answer: the watchdogs still fire, but FrontBoard declines the
 * termination, so a truly hung app stays until it is ended by other means. This
 * sets the two factors to one whole factor and BKNoWatchdogs, or removes all
 * three ("original"), through the system's own preference setter, and reads them
 * back. SpringBoard applies the values when it next loads its watchdog policy,
 * at the latest after restart_ui or a reboot. */
static int set_scale_key(object row, object domain, object user, object host, const char *name,
                         const char *previous_field, const char *field, unsigned value) {
    object core = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    object (*copy)(object, object, object, object) = core ? dlsym(core, "CFPreferencesCopyValue") : 0;
    void (*set)(object, object, object, object, object) = core ? dlsym(core, "CFPreferencesSetValue") : 0;
    unsigned char (*sync)(object, object, object) = core ? dlsym(core, "CFPreferencesSynchronize") : 0;
    if (!copy || !set || !sync) return failure(row, "incompatible_preferences_api");
    object key = string(name);
    object previous = copy(key, domain, user, host);
    if (previous) get(previous, "autorelease");
    if (previous && !((unsigned char (*)(object, object, object))message)(
            previous, selector("isKindOfClass:"), get_class("NSNumber")))
        return failure(row, "invalid_watchdog_preferences");
    put(row, previous_field, previous);
    set(key, value ? number(value) : 0, domain, user, host);
    if (!sync(domain, user, host)) return failure(row, "watchdog_preferences_unavailable");
    object current = copy(key, domain, user, host);
    if (current) get(current, "autorelease");
    unsigned stored = current && ((unsigned char (*)(object, object, object))message)(
        current, selector("isKindOfClass:"), get_class("NSNumber"))
        ? ((unsigned (*)(object, object))message)(current, selector("unsignedIntValue")) : 0;
    put(row, field, current);
    if (value ? stored != value : current != 0) return failure(row, "watchdog_scale_not_applied");
    return 0;
}

static int launch_watchdog_scale(object row, const char *argument) {
    object core = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    const object *any_host = core ? dlsym(core, "kCFPreferencesAnyHost") : 0;
    unsigned char (*sync)(object, object, object) = core ? dlsym(core, "CFPreferencesSynchronize") : 0;
    if (!any_host || !*any_host || !sync) return failure(row, "incompatible_preferences_api");
    if (!setup_virtual_device() || getuid() != 501) return failure(row, "virtual_setup_required");
    unsigned scale = watchdog_scale_argument(argument);
    if (!scale) return failure(row, "invalid_watchdog_scale");
    object domain = string("com.apple.springboard"), user = string("mobile");
    put(row, "argument", string(argument));
    if (!sync(domain, user, *any_host)) return failure(row, "watchdog_preferences_unavailable");
    int refused = set_scale_key(row, domain, user, *any_host, "FBLaunchWatchdogScale",
                                "previous_scale", "scale", scale == 1 ? 0 : scale);
    if (refused) return refused;
    refused = set_scale_key(row, domain, user, *any_host, "FBLaunchWatchdogResumeScale",
                            "previous_resume_scale", "resume_scale", scale == 1 ? 0 : scale);
    if (refused) return refused;
    refused = set_scale_key(row, domain, user, *any_host, "BKNoWatchdogs",
                            "previous_no_watchdogs", "no_watchdogs", scale != 1);
    if (refused) return refused;
    put(row, "applies_after_ui_restart", boolean(1));
    put(row, "application_scene_verified", boolean(0));
    put(row, "ok", boolean(1));
    return emit(row);
}

static int setup_state(object row, int finish) {
    object framework = dlopen("/System/Library/PrivateFrameworks/SetupAssistant.framework/SetupAssistant", 2);
    object core = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    if (!framework || !core) return failure(row, "setup_framework_unavailable");
    const int *version = dlsym(framework, "BYBuddyIOSCurrentVersion");
    const object *done = dlsym(framework, "BYBuddyDoneKey");
    const object *finished = dlsym(framework, "BYBuddyFinishedInitialRunKey");
    const object *version_key = dlsym(framework, "BYBuddyIOSVersionKey");
    const object *any_host = dlsym(core, "kCFPreferencesAnyHost");
    object (*copy)(object, object, object, object) = dlsym(core, "CFPreferencesCopyValue");
    void (*set_multiple)(object, object, object, object, object) = dlsym(core, "CFPreferencesSetMultiple");
    unsigned char (*sync)(object, object, object) = dlsym(core, "CFPreferencesSynchronize");
    unsigned char (*needs_setup)(void) = dlsym(framework, "BYSetupAssistantNeedsToRun");
    if (!version || *version < 1 || *version > 1000 || !done || !finished || !version_key ||
        !any_host || !*any_host || !copy || !set_multiple || !sync || !needs_setup)
        return failure(row, "incompatible_setup_api");
    object keys[] = {*done, *finished, *version_key};
    const char *names[] = {"SetupDone", "SetupFinishedAllSteps", "SetupVersion"};
    for (unsigned index = 0; index < 3; ++index)
        if (!keys[index] || !((unsigned char (*)(object, object, object))message)(
            keys[index], selector("isEqual:"), string(names[index])))
            return failure(row, "incompatible_setup_api");
    object domain = string("com.apple.purplebuddy"), user = string("mobile");
    if (!sync(domain, user, *any_host)) return failure(row, "setup_preferences_unavailable");
    object before = get(get_class("NSMutableDictionary"), "dictionary");
    long stored_version = 0;
    for (unsigned index = 0; index < 3; ++index) {
        object value = copy(keys[index], domain, user, *any_host);
        if (value) get(value, "autorelease");
        if (value && !((unsigned char (*)(object, object, object))message)(
                value, selector("isKindOfClass:"), get_class("NSNumber")))
            return failure(row, "invalid_setup_preferences");
        put(before, names[index], value);
        if (index == 2 && value)
            stored_version = ((long (*)(object, object))message)(value, selector("longValue"));
    }
    if (stored_version < 0 || stored_version > 1000) return failure(row, "invalid_setup_preferences");
    put(row, "preferences", before);
    put(row, "current_version", number((unsigned)*version));
    put(row, "native_setup_required", boolean(needs_setup()));
    put(row, "virtual_device_verified", boolean(setup_virtual_device()));
    put(row, "application_scene_verified", boolean(0));

    if (!dlopen("/System/Library/PrivateFrameworks/MobileActivation.framework/MobileActivation", 2))
        return failure(row, "activation_framework_unavailable");
    object gate = get(get_class("MadGate"), "new");
    const char *types[] = {"^@"};
    if (!accepts_types(gate, "getActivationState:", '@', types, 1))
        return failure(row, "incompatible_activation_query_api");
    object error = 0;
    object result = ((object (*)(object, object, object *))message)(gate, selector("getActivationState:"), &error);
    if (error) {
        put(row, "native_error", native_error(error, 0));
        ((void (*)(object, object))message)(gate, selector("release"));
        return failure(row, "activation_state_unavailable");
    }
    /* The daemon replies with a dictionary, but MadGate's native callback
     * already extracts and validates ActivationState before returning it. */
    object state = bounded_text(result, 128);
    if (!state) return failure(row, "invalid_activation_response");
    put(row, "activation_state", state);
    ((void (*)(object, object))message)(gate, selector("release"));
    if (!finish) { put(row, "ok", boolean(1)); return emit(row); }
    /* This operation configures only this VM's first-run preferences. It
     * cannot activate a device, dismiss ownership locks, or run on hardware. */
    if (!setup_virtual_device() || getuid() != 501) return failure(row, "virtual_setup_required");
    const char *activation = get(state, "UTF8String");
    if (!activation || (strcmp(activation, "Activated") && strcmp(activation, "FactoryActivated")))
        return failure(row, "device_not_activated");
    if (!request_id) return failure(row, "setup_request_identity_required");
    object desired = get(get_class("NSMutableDictionary"), "dictionary");
    put(desired, names[0], boolean(1));
    put(desired, names[1], boolean(1));
    put(desired, names[2], number((unsigned long)(stored_version > *version ? stored_version : *version)));
    /* Save the precise previous values before any write, including absent
     * keys. A single request owns this immutable local recovery artifact. */
    char path[256];
    snprintf(path, sizeof(path), "/private/var/mobile/Library/Logs/io.github.marselnenaj.tomacco.setup-%s.json", request_id);
    object recovery = get(get_class("NSMutableDictionary"), "dictionary");
    put(recovery, "format", number(1));
    put(recovery, "request_id", string(request_id));
    put(recovery, "domain", domain);
    put(recovery, "user", user);
    put(recovery, "previous_values", before);
    put(recovery, "desired_values", desired);
    /* JSON preserves absent keys as null; a plist would lose that distinction. */
    error = 0;
    object backup = ((object (*)(object, object, object, unsigned long, object *))message)(
        get_class("NSJSONSerialization"), selector("dataWithJSONObject:options:error:"), recovery, 0, &error);
    if (!backup || error) return failure(row, "setup_backup_unavailable");
    unsigned long size = ((unsigned long (*)(object, object))message)(backup, selector("length"));
    if (!size || size > 4096) return failure(row, "invalid_setup_backup");
    int file = open(path, 0x1000b01, 0600); /* WRONLY|CREAT|EXCL|NOFOLLOW|CLOEXEC */
    if (file < 0) return failure(row, "setup_backup_unavailable");
    int failed = write_bytes(file, get(backup, "bytes"), size) || fsync(file);
    if (close(file)) failed = 1;
    if (failed) { unlink(path); return failure(row, "setup_backup_unavailable"); }
    put(row, "backup_path", string(path));
    set_multiple(desired, 0, domain, user, *any_host);
    if (!sync(domain, user, *any_host)) {
        put(row, "request_may_have_executed", boolean(1));
        put(row, "automatic_retry_safe", boolean(0));
        return failure(row, "setup_write_unconfirmed");
    }
    object after = get(get_class("NSMutableDictionary"), "dictionary");
    int matched = 1;
    for (unsigned index = 0; index < 3; ++index) {
        object value = copy(keys[index], domain, user, *any_host);
        if (value) get(value, "autorelease");
        put(after, names[index], value);
        if (!value || !((unsigned char (*)(object, object, object))message)(
                value, selector("isEqual:"), with(desired, "objectForKey:", keys[index]))) matched = 0;
    }
    put(row, "previous_preferences", before);
    put(row, "preferences", after);
    if (!matched) {
        put(row, "request_may_have_executed", boolean(1));
        put(row, "automatic_retry_safe", boolean(0));
        return failure(row, "setup_write_unconfirmed");
    }
    put(row, "preferences_configured", boolean(1));
    setup_storage_flush(row);
    put(row, "restart_required", boolean(1));
    put(row, "request_accepted", boolean(1));
    put(row, "ok", boolean(1));
    return emit(row);
}
