/* Read the original SpringBoard MIG reply in the owned virtual device.
 * Recovered from the pinned iOS 27.0.1 SpringBoardServices implementation:
 * _SBSGetScreenLockStatus(port, byte *locked, byte *passcode) returns the
 * kern_return_t and writes both bytes only after validating a successful
 * reply. SBSGetScreenLockStatus's convenience wrapper discards that status;
 * using its zero-initialized outputs would mistake an RPC failure for unlock.
 */
/* Original 27.0.1 MIG client writes its float only after successful transport,
 * reply validation and server RetCode. The server maps backlight states 2/3
 * to exactly 1.0f/0.05f; screenIsOn recognizes those same two states. Keep
 * failed status and unknown factor independent of a valid lock-status reply.
 * This says nothing about whether the compositor drew a visible scene. */
static void screen_backlight_fields(object row, object framework, unsigned port) {
    put(row, "screen_awake_known", boolean(0));
    int (*query)(unsigned, float *) = dlsym(framework, "_SBSGetCurrentBacklightFactor");
    put(row, "backlight_query_available", boolean(query != 0));
    if (!query) return;
    float factor = __builtin_nanf("");
    int status = query(port, &factor);
    put(row, "backlight_native_status", ((object (*)(object, object, int))message)(
        get_class("NSNumber"), selector("numberWithInt:"), status));
    if (status || !__builtin_isfinite(factor)) return;
    put(row, "native_backlight_factor", ((object (*)(object, object, double))message)(
        get_class("NSNumber"), selector("numberWithDouble:"), (double)factor));
    if (factor != 0.0f && factor != 1.0f && factor != 0.05f) return;
    put(row, "screen_awake", boolean(factor != 0.0f));
    put(row, "screen_awake_known", boolean(1));
}

static int screen_state(object row) {
    put(row, "screen_state_known", boolean(0));
    put(row, "screen_awake_known", boolean(0));
    if (!setup_virtual_device() || getuid() != 501)
        return failure(row, "virtual_screen_query_required");
    object version = with(get_class("NSDictionary"), "dictionaryWithContentsOfFile:",
        string("/System/Library/CoreServices/SystemVersion.plist"));
    object product = with(version, "objectForKey:", string("ProductVersion"));
    if (!product || !((unsigned char (*)(object, object, object))message)(product,
            selector("isKindOfClass:"), get_class("NSString")))
        return failure(row, "screen_query_version_unavailable");
    const char *text = get(product, "UTF8String");
    if (!text || strcmp(text, "27.0.1"))
        return failure(row, "screen_query_contract_unqualified");
    object framework = dlopen("/System/Library/PrivateFrameworks/SpringBoardServices.framework/SpringBoardServices", 2);
    if (!framework) return failure(row, "screen_query_framework_unavailable");
    unsigned (*server_port)(void) = dlsym(framework, "SBSSpringBoardServerPort");
    int (*query)(unsigned, unsigned char *, unsigned char *) =
        dlsym(framework, "_SBSGetScreenLockStatus");
    if (!server_port || !query) return failure(row, "screen_query_api_unavailable");
    /* This cached send right belongs to SpringBoardServices. Do not release it. */
    unsigned port = server_port();
    if (!port || port == ~0u) return failure(row, "screen_query_service_unavailable");
    unsigned char locked = 0xff, passcode = 0xff;
    int status = query(port, &locked, &passcode);
    put(row, "native_status", ((object (*)(object, object, int))message)(
        get_class("NSNumber"), selector("numberWithInt:"), status));
    if (status) return failure(row, "screen_query_rpc_failed");
    if (locked > 1 || passcode > 1) return failure(row, "screen_query_invalid_reply");
    put(row, "screen_locked", boolean(locked));
    put(row, "passcode_enabled", boolean(passcode));
    put(row, "screen_state_known", boolean(1));
    screen_backlight_fields(row, framework, port);
    put(row, "application_scene_verified", boolean(0));
    put(row, "ok", boolean(1));
    return emit(row);
}
