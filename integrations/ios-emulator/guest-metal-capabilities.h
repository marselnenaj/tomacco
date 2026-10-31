/* Observe the current worker's native Metal capability answers. A compatible
 * runtime method signature is required before every call. These queries create
 * no GPU resources and establish neither pixel layout nor renderer support.
 * The default device remains alive for this synchronous borrowed getter use. */
#ifndef TOMACCO_GUEST_METAL_CAPABILITIES_H
#define TOMACCO_GUEST_METAL_CAPABILITIES_H

struct metal_capability_metadata {
    const char *(*class_name)(object);
    graphics_imp (*implementation)(object);
    int (*lookup)(const void *, struct graphics_dl_info *);
    unsigned char (*virtual_device_answer)(object);
};

static object metal_capability_method_with_arguments(
        object receiver, const char *name, char result,
        const char *const *arguments, unsigned count,
        const struct metal_capability_metadata *api, int *compatible) {
    object item = get(get_class("NSMutableDictionary"), "dictionary");
    *compatible = 0;
    put(item, "selector", string(name));
    put(item, "known", boolean(0));
    put(item, "method_present", boolean(0));
    put(item, "signature_compatible", boolean(0));
    if (!receiver) {
        put(item, "state", string("receiver_unavailable"));
        return item;
    }
    object method = instance_method(object_class(receiver), selector(name));
    put(item, "method_present", boolean(method != 0));
    graphics_method_image(item, method, api->implementation, api->lookup);
    if (!method) {
        put(item, "state", string("method_unavailable"));
        return item;
    }
    if (!accepts_types(receiver, name, result, arguments, count)) {
        put(item, "state", string("signature_incompatible"));
        return item;
    }
    put(item, "signature_compatible", boolean(1));
    *compatible = 1;
    return item;
}

static object metal_capability_method(object receiver, const char *name, char result,
                                      const struct metal_capability_metadata *api,
                                      int *compatible) {
    return metal_capability_method_with_arguments(receiver, name, result, 0, 0, api, compatible);
}

/* These seven ordinals are the calls recovered from the original QuartzCore
 * new_metal_context. Observe the actual device; never synthesize
 * a compositor context bit or disable a family to avoid an unsupported format.
 * MTLGPUFamily's signed Darwin NSInteger argument must match runtime metadata. */
static object metal_compositor_families(object device,
                                        const struct metal_capability_metadata *api) {
    const char *arguments[] = {"q"};
    const long families[] = {1001, 1002, 1003, 1004, 1005, 1007, 1009};
    object answers = get(get_class("NSMutableArray"), "array");
    _Static_assert(sizeof(long) == 8, "Darwin ARM64 NSInteger ABI");
    for (unsigned index = 0; index < sizeof(families) / sizeof(families[0]); ++index) {
        int compatible = 0;
        object answer = metal_capability_method_with_arguments(
            device, "supportsFamily:", 'B', arguments, 1, api, &compatible);
        put(answer, "family", number((unsigned long)families[index]));
        put(answer, "expected_argument_encoding", string("q"));
        if (compatible) {
            unsigned char value = ((unsigned char (*)(object, object, long))message)(
                device, selector("supportsFamily:"), families[index]);
            put(answer, "value", boolean(value));
            put(answer, "known", boolean(1));
            put(answer, "state", string("observed"));
        }
        ((void (*)(object, object, object))message)(answers, selector("addObject:"), answer);
    }
    return answers;
}

/* Source107 binds QuartzCore's original call to MGGetBoolAnswer with the
 * CFString IsVirtualDevice and the low-byte result stored by its once block.
 * An NSString is toll-free bridged at that CFString boundary. This observes
 * the same query in this worker, not another process's cached predicate.
 * MG's Boolean API supplies no separate key-presence/error result. */
static object metal_virtual_device_answer(const struct metal_capability_metadata *api) {
    object answer = get(get_class("NSMutableDictionary"), "dictionary");
    put(answer, "api", string("MGGetBoolAnswer"));
    put(answer, "property", string("IsVirtualDevice"));
    put(answer, "known", boolean(0));
    put(answer, "property_presence_verified", boolean(0));
    put(answer, "compositor_process_value_observed", boolean(0));
    put(answer, "implementation_image_known", boolean(0));
    put(answer, "state", string("api_unavailable"));
    if (!api->virtual_device_answer) return answer;
    if (api->lookup) {
        struct graphics_dl_info info = {0};
        if (api->lookup((const void *)api->virtual_device_answer, &info) && info.image) {
            put(answer, "implementation_image", bounded_text(string(info.image), 4096));
            put(answer, "implementation_image_known", boolean(1));
        }
    }
    unsigned char value = api->virtual_device_answer(string("IsVirtualDevice"));
    put(answer, "raw_answer", number(value));
    put(answer, "value", boolean(value));
    put(answer, "known", boolean(1));
    put(answer, "state", string("observed_api_answer"));
    return answer;
}

static object metal_capability_receiver(object receiver, const char *role,
                                        const struct metal_capability_metadata *api,
                                        int family_queries) {
    object item = get(get_class("NSMutableDictionary"), "dictionary");
    put(item, "role", string(role));
    put(item, "receiver_present", boolean(receiver != 0));
    const char *name = receiver && api->class_name ? api->class_name(object_class(receiver)) : 0;
    put(item, "receiver_class_known", boolean(name != 0));
    if (name) put(item, "receiver_class", bounded_text(string(name), 256));
    const char *methods[] = {
        "supportsLimitedYUVFormats", "supportsExtendedYUVFormats", "supportsAlphaYUVFormats",
        "supportsPublicXR10Formats", "supportsExtendedXR10Formats",
        "supportsCorrectExtendedXR10Formats", "supportsYCBCRFormatsXR",
        "supportsYCBCRPackedFormatsXR", "familySupportsLimitedYUVFormats",
        "familySupportsExtendedYUVFormats", "familySupportsPublicXR10Formats",
        "familySupportsExtendedXR10Formats"
    };
    object answers = get(get_class("NSMutableArray"), "array");
    unsigned count = family_queries ? sizeof(methods) / sizeof(methods[0]) : 8;
    for (unsigned index = 0; index < count; ++index) {
        int compatible = 0;
        object answer = metal_capability_method(receiver, methods[index], 'B', api, &compatible);
        if (compatible) {
            unsigned char value = ((unsigned char (*)(object, object))message)(
                receiver, selector(methods[index]));
            put(answer, "value", boolean(value));
            put(answer, "known", boolean(1));
            put(answer, "state", string("observed"));
        }
        ((void (*)(object, object, object))message)(answers, selector("addObject:"), answer);
    }
    put(item, "answers", answers);
    return item;
}

static void metal_capability_state_with_api(object row, object device,
                                            const struct metal_capability_metadata *api) {
    object snapshot = get(get_class("NSMutableDictionary"), "dictionary");
    put(snapshot, "scope", string("this-disposable-worker"));
    put(snapshot, "renderer_support_verified", boolean(0));
    put(snapshot, "pixel_layout_verified", boolean(0));
    put(snapshot, "compositor_process_value_observed", boolean(0));
    put(snapshot, "virtual_device_query", metal_virtual_device_answer(api));
    put(snapshot, "default_device", metal_capability_receiver(device, "default-device", api, 0));
    put(snapshot, "compositor_family_support", metal_compositor_families(device, api));

    int compatible = 0;
    object usage = metal_capability_method(device, "supportsCorrectTextureUsageBits", 'B', api, &compatible);
    if (compatible) {
        unsigned char value = ((unsigned char (*)(object, object))message)(
            device, selector("supportsCorrectTextureUsageBits"));
        put(usage, "value", boolean(value));
        put(usage, "known", boolean(1));
        put(usage, "state", string("observed"));
    }
    put(snapshot, "correct_texture_usage_bits", usage);

    object profile = metal_capability_method(device, "featureProfile", 'Q', api, &compatible);
    if (compatible) {
        unsigned long long value = ((unsigned long long (*)(object, object))message)(
            device, selector("featureProfile"));
        _Static_assert(sizeof(unsigned long) == sizeof(unsigned long long), "Darwin ARM64 scalar ABI");
        put(profile, "value", number((unsigned long)value));
        put(profile, "known", boolean(1));
        put(profile, "state", string("observed"));
    }
    put(snapshot, "feature_profile", profile);

    object getter = metal_capability_method(device, "featureQueries", '@', api, &compatible);
    object queries = 0;
    if (compatible) {
        queries = get(device, "featureQueries");
        put(getter, "known", boolean(1));
        put(getter, "returned_object_present", boolean(queries != 0));
        put(getter, "state", string(queries ? "observed" : "returned_nil"));
    }
    put(snapshot, "feature_queries_getter", getter);
    put(snapshot, "feature_queries", metal_capability_receiver(queries, "feature-queries", api, 1));
    put(row, "metal_capability_snapshot", snapshot);
}

static void metal_capability_state(object row, object device) {
    object runtime = dlopen("/usr/lib/libobjc.A.dylib", 2);
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    object gestalt = dlopen("/usr/lib/libMobileGestalt.dylib", 2);
    struct metal_capability_metadata api = {
        .class_name = runtime ? dlsym(runtime, "class_getName") : 0,
        .implementation = runtime ? dlsym(runtime, "method_getImplementation") : 0,
        .lookup = system ? dlsym(system, "dladdr") : 0,
        .virtual_device_answer = gestalt ? dlsym(gestalt, "MGGetBoolAnswer") : 0,
    };
    metal_capability_state_with_api(row, device, &api);
}

#endif
