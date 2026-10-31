/* The guest's Auto-Lock (Settings, Display & Brightness, Auto-Lock), set
 * through the same ManagedConfiguration call Settings makes.
 *
 * After the idle timeout iOS turns the display off and locks; the emulator's
 * renderer then has nothing new to draw, so the desktop shows the last frame
 * as if it were live, and every tap first has to wake and unlock the phone.
 *
 * Recovered from the pinned iOS 27.0.1 dyld cache (Objective-C metadata and
 * constants only): Settings' Auto-Lock list (DisplayAndBrightnessSettings,
 * -[DBSSettingsController setAutoLock:specifier:]) calls
 * -[[MCProfileConnection sharedConnection] setAutoLockTime:], a category of
 * that framework, which stores the choice as
 * -[MCProfileConnection setValue:forSetting:] under MCFeatureAutoLockTime
 * (the string "maxInactivity", seconds). "Never" is the list's -1
 * (-_isNever:), stored as INT_MAX (0x7fffffff); -autoLockTime reads
 * -effectiveValueForSetting: and shows an INT_MAX ("unset", -_isUnset:) as
 * Never. ManagedConfiguration sends the value to profiled over its private
 * service com.apple.managedconfiguration.profiled, whose clients carry the
 * entitlement com.apple.managedconfiguration.profiled-access (recorded grant
 * auto_lock_control). SpringBoard's idle timer follows the effective value
 * as it changes (SBIdleTimerGlobalStateMonitor's auto-lock monitor, built
 * with the profile connection); the value lives on the Data volume.
 *
 * Should profiled not take the value (the user value reads back unchanged),
 * SpringBoard's own switch is used instead: the Boolean SBDontLockEver in
 * the mobile user's com.apple.springboard preferences (SpringBoardFoundation
 * SBSecurityDefaults.dontLockEver, which the same monitor also follows).
 *
 * "never" sets Never; "original" sets ManagedConfiguration's default for the
 * setting (-defaultValueForSetting:) and removes SBDontLockEver. Both read
 * the result back; either may be asked again. Nothing else is configured. */
#define TOMACCO_AUTO_LOCK_NEVER 0x7fffffff

static int auto_lock_argument(const char *value) {
    return value && (!strcmp(value, "never") || !strcmp(value, "original"));
}

/* The setting's value as whole seconds; 0 when it is no number. */
static int auto_lock_seconds(object value, int *seconds) {
    if (!value || !((unsigned char (*)(object, object, object))message)(
            value, selector("isKindOfClass:"), get_class("NSNumber"))) return 0;
    *seconds = ((int (*)(object, object))message)(value, selector("intValue"));
    return 1;
}

/* Give a notification-driven cache a chance to see profiled's change. */
static void auto_lock_wait(void) {
    void *core = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    int (*run)(object, double, unsigned char) = core ? dlsym(core, "CFRunLoopRunInMode") : 0;
    const object *mode = core ? dlsym(core, "kCFRunLoopDefaultMode") : 0;
    /* kCFRunLoopRunFinished: no source to wait on, so wait plainly. */
    if (!run || !mode || !*mode || run(*mode, 1.0, 0) == 1) sleep(1);
}

/* Read ManagedConfiguration's user and effective values until both are
 * `target`, for a bounded time. 1 when they are. */
static int auto_lock_read_back(object row, object connection, object feature, int target) {
    int user = 0, effective = 0, user_known = 0, effective_known = 0;
    for (unsigned attempt = 0; attempt < 10; ++attempt) {
        if (attempt) auto_lock_wait();
        object user_value = with(connection, "userValueForSetting:", feature);
        object effective_value = with(connection, "effectiveValueForSetting:", feature);
        user_known = auto_lock_seconds(user_value, &user);
        effective_known = auto_lock_seconds(effective_value, &effective);
        put(row, "user_max_inactivity", user_known ? user_value : 0);
        put(row, "max_inactivity", effective_known ? effective_value : 0);
        if (user_known && user == target && effective_known && effective == target) return 1;
    }
    return 0;
}

/* SpringBoard's own switch: 1 sets SBDontLockEver, 0 removes it. 1 when it
 * reads back so; the stored value lands in `field`. */
static int auto_lock_springboard(object row, int never, const char *previous_field, const char *field) {
    void *core = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    const object *any_host = core ? dlsym(core, "kCFPreferencesAnyHost") : 0;
    object (*copy)(object, object, object, object) = core ? dlsym(core, "CFPreferencesCopyValue") : 0;
    void (*set)(object, object, object, object, object) = core ? dlsym(core, "CFPreferencesSetValue") : 0;
    unsigned char (*sync)(object, object, object) = core ? dlsym(core, "CFPreferencesSynchronize") : 0;
    if (!any_host || !*any_host || !copy || !set || !sync) return 0;
    object key = string("SBDontLockEver"), domain = string("com.apple.springboard"), user = string("mobile");
    if (!sync(domain, user, *any_host)) return 0;
    object previous = copy(key, domain, user, *any_host);
    if (previous) get(previous, "autorelease");
    if (previous_field) put(row, previous_field, previous);
    set(key, never ? boolean(1) : 0, domain, user, *any_host);
    if (!sync(domain, user, *any_host)) return 0;
    object current = copy(key, domain, user, *any_host);
    if (current) get(current, "autorelease");
    put(row, field, current);
    if (!never) return current == 0;
    return current && ((unsigned char (*)(object, object, object))message)(
               current, selector("isKindOfClass:"), get_class("NSNumber")) &&
           ((unsigned char (*)(object, object))message)(current, selector("boolValue"));
}

static int set_auto_lock(object row, const char *argument) {
    if (!auto_lock_argument(argument)) return failure(row, "invalid_auto_lock");
    if (!setup_virtual_device() || getuid() != 501) return failure(row, "virtual_setup_required");
    put(row, "argument", string(argument));
    void *framework = dlopen("/System/Library/PrivateFrameworks/ManagedConfiguration.framework/ManagedConfiguration", 2);
    if (!framework) return failure(row, "auto_lock_framework_unavailable");
    const object *feature = dlsym(framework, "MCFeatureAutoLockTime");
    if (!feature || !*feature || !((unsigned char (*)(object, object, object))message)(
            *feature, selector("isEqual:"), string("maxInactivity")))
        return failure(row, "incompatible_auto_lock_api");
    object klass = get_class("MCProfileConnection");
    if (!accepts(klass, "sharedConnection", '@', 0)) return failure(row, "incompatible_auto_lock_api");
    object connection = get(klass, "sharedConnection");
    if (!connection) return failure(row, "auto_lock_service_unavailable");
    const char *pair[] = {"@", "@"};
    if (!accepts_types(connection, "setValue:forSetting:", 'v', pair, 2) ||
        !accepts(connection, "effectiveValueForSetting:", '@', 1) ||
        !accepts(connection, "userValueForSetting:", '@', 1) ||
        !accepts(connection, "defaultValueForSetting:", '@', 1))
        return failure(row, "incompatible_auto_lock_api");
    int never = !strcmp(argument, "never"), seconds = 0, target = TOMACCO_AUTO_LOCK_NEVER;
    object previous = with(connection, "effectiveValueForSetting:", *feature);
    put(row, "previous_max_inactivity", auto_lock_seconds(previous, &seconds) ? previous : 0);
    object fallback = with(connection, "defaultValueForSetting:", *feature);
    put(row, "default_max_inactivity", auto_lock_seconds(fallback, &seconds) ? fallback : 0);
    if (!never) {
        if (!auto_lock_seconds(fallback, &target)) return failure(row, "auto_lock_default_unknown");
        if (!auto_lock_springboard(row, 0, "previous_springboard_dont_lock_ever", "springboard_dont_lock_ever"))
            return failure(row, "auto_lock_not_applied");
    }
    object value = never ? ((object (*)(object, object, int))message)(
        get_class("NSNumber"), selector("numberWithInt:"), TOMACCO_AUTO_LOCK_NEVER) : fallback;
    /* Setting the same value again is harmless: retrying is safe. */
    put(row, "request_may_have_executed", boolean(1));
    put(row, "automatic_retry_safe", boolean(1));
    ((void (*)(object, object, object, object))message)(connection, selector("setValue:forSetting:"), value, *feature);
    int applied = auto_lock_read_back(row, connection, *feature, target);
    put(row, "managed_configuration_applied", boolean(applied));
    const char *method = "managed_configuration";
    if (!applied && never) {
        int user = 0;
        if (auto_lock_seconds(with(connection, "userValueForSetting:", *feature), &user) && user == target)
            return failure(row, "auto_lock_restricted"); /* a profile caps it below Never */
        if (!auto_lock_springboard(row, 1, "previous_springboard_dont_lock_ever", "springboard_dont_lock_ever"))
            return failure(row, "auto_lock_not_applied");
        method = "springboard_defaults";
    } else if (!applied) {
        return failure(row, "auto_lock_not_applied");
    }
    /* profiled and cfprefsd write without a device flush; an emulator stop
     * must not discard an acknowledged setting. */
    void *system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    void (*sync_fn)(void) = system ? dlsym(system, "sync") : 0;
    if (sync_fn) sync_fn();
    put(row, "method", string(method));
    put(row, "auto_lock", string(argument));
    put(row, "ok", boolean(1));
    return emit(row);
}
