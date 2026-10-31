/* Public CoreVideo description queries in this disposable worker only.
 * Create Rule ownership is balanced even for malformed/oversized descriptions.
 * No pixel buffer, IOSurface, texture or GPU command is created. Registrations
 * are process-local evidence; missing entries do not establish unsupported
 * native storage or a renderer contract. */
typedef object (*pixel_format_create)(const void *, unsigned);
typedef void (*pixel_format_release)(const void *);
_Static_assert(sizeof(unsigned) == 4, "CoreVideo OSType ABI");

struct pixel_format_key {
    const char *symbol;
    const char *field;
};

static const struct pixel_format_key pixel_format_keys[] = {
    {"kCVPixelFormatName", "name"},
    {"kCVPixelFormatConstant", "constant"},
    {"kCVPixelFormatFourCC", "fourcc"},
    {"kCVPixelFormatContainsAlpha", "contains_alpha"},
    {"kCVPixelFormatContainsRGB", "contains_rgb"},
    {"kCVPixelFormatContainsYCbCr", "contains_ycbcr"},
    {"kCVPixelFormatComponentRange", "component_range"},
    {"kCVPixelFormatBlockWidth", "block_width"},
    {"kCVPixelFormatBlockHeight", "block_height"},
    {"kCVPixelFormatBitsPerBlock", "bits_per_block"},
    {"kCVPixelFormatBlockHorizontalAlignment", "block_horizontal_alignment"},
    {"kCVPixelFormatBlockVerticalAlignment", "block_vertical_alignment"},
    {"kCVPixelFormatHorizontalSubsampling", "horizontal_subsampling"},
    {"kCVPixelFormatVerticalSubsampling", "vertical_subsampling"},
    {"kCVPixelFormatOpenGLFormat", "opengl_format"},
    {"kCVPixelFormatOpenGLType", "opengl_type"},
    {"kCVPixelFormatOpenGLInternalFormat", "opengl_internal_format"},
    {"kCVPixelFormatCGBitmapContextCompatibility", "cg_bitmap_context_compatibility"},
    {"kCVPixelFormatCGImageCompatibility", "cg_image_compatibility"},
};

static int pixel_format_is_class(object value, const char *name) {
    return value && ((unsigned char (*)(object, object, object))message)(
        value, selector("isKindOfClass:"), get_class(name));
}

/* Project selected public keys only. Ignore no unknown value silently: its
 * field receives an explicit state. Never stringify native callback/pointer
 * values or arbitrary recursive property dictionaries. */
static object pixel_format_description_fields(object value, object library,
                                               unsigned plane_depth) {
    object result = get(get_class("NSMutableDictionary"), "dictionary");
    put(result, "dictionary_known", boolean(pixel_format_is_class(value, "NSDictionary")));
    put(result, "complete_dictionary_dump", boolean(0));
    if (!pixel_format_is_class(value, "NSDictionary")) return result;
    unsigned long count = ((unsigned long (*)(object, object))message)(value, selector("count"));
    put(result, "native_key_count", number(count));
    if (count > 64) {
        put(result, "state", string("description_key_limit"));
        return result;
    }
    object fields = get(get_class("NSMutableDictionary"), "dictionary");
    object states = get(get_class("NSMutableDictionary"), "dictionary");
    for (unsigned i = 0; i < sizeof(pixel_format_keys) / sizeof(pixel_format_keys[0]); ++i) {
        const struct pixel_format_key *key = &pixel_format_keys[i];
        object *exported = dlsym(library, key->symbol);
        if (!exported || !*exported) {
            put(states, key->field, string("key_symbol_unavailable"));
            continue;
        }
        object item = with(value, "objectForKey:", *exported);
        if (!item) {
            put(states, key->field, string("not_present"));
        } else if (pixel_format_is_class(item, "NSString")) {
            unsigned long length = ((unsigned long (*)(object, object))message)(item, selector("length"));
            if (length > 256) {
                put(states, key->field, string("string_limit"));
            } else {
                put(fields, key->field, bounded_text(item, 256));
                put(states, key->field, string("string"));
            }
        } else if (pixel_format_is_class(item, "NSNumber")) {
            double numeric = ((double (*)(object, object))message)(item, selector("doubleValue"));
            if (__builtin_isfinite(numeric)) {
                /* Keep the native NSNumber (including its integer/Boolean
                 * representation), not a lossy conversion through double. */
                put(fields, key->field, item);
                put(states, key->field, string("number"));
            } else {
                put(states, key->field, string("nonfinite_number"));
            }
        } else {
            put(states, key->field, string("value_type_unhandled"));
        }
    }
    put(result, "fields", fields);
    put(result, "field_states", states);
    object *planes_key = dlsym(library, "kCVPixelFormatPlanes");
    object planes = planes_key && *planes_key ? with(value, "objectForKey:", *planes_key) : 0;
    put(result, "planes_key_supported", boolean(planes_key && *planes_key));
    put(result, "planes_present", boolean(planes != 0));
    if (planes) {
        if (!pixel_format_is_class(planes, "NSArray")) {
            put(result, "planes_state", string("planes_value_type_unhandled"));
        } else if (plane_depth != 0) {
            put(result, "planes_state", string("nested_plane_limit"));
        } else {
            unsigned long size = ((unsigned long (*)(object, object))message)(planes, selector("count"));
            put(result, "native_plane_count", number(size));
            if (size > 8) {
                put(result, "planes_state", string("plane_count_limit"));
            } else {
                object items = get(get_class("NSMutableArray"), "array");
                for (unsigned long index = 0; index < size; ++index) {
                    object plane = ((object (*)(object, object, unsigned long))message)(
                        planes, selector("objectAtIndex:"), index);
                    object item = pixel_format_description_fields(plane, library, 1);
                    put(item, "index", number(index));
                    ((void (*)(object, object, object))message)(items, selector("addObject:"), item);
                }
                put(result, "planes", items);
                put(result, "planes_state", string("selected_fields_projected"));
            }
        }
    }
    return result;
}

static void pixel_format_query_with_api(object row, object library,
                                       pixel_format_create create,
                                       pixel_format_release release) {
    put(row, "corevideo_format_query_supported", boolean(library && create && release));
    put(row, "corevideo_registration_scope", string("this-disposable-worker"));
    put(row, "corevideo_renderer_contract_verified", boolean(0));
    if (!library || !create || !release) return;
    const unsigned formats[] = {0x5f524234u, 0x42475241u, 0x34323076u};
    const char *roles[] = {"target-rb4", "control-bgra", "control-420v"};
    object items = get(get_class("NSMutableArray"), "array");
    for (unsigned i = 0; i < sizeof(formats) / sizeof(formats[0]); ++i) {
        object item = get(get_class("NSMutableDictionary"), "dictionary");
        put(item, "role", string(roles[i]));
        put(item, "fourcc", number(formats[i]));
        object description = create(0, formats[i]);
        put(item, "native_description_returned", boolean(description != 0));
        if (description) {
            put(item, "description", pixel_format_description_fields(description, library, 0));
            release(description);
        } else {
            put(item, "state", string("description_unavailable_unknown"));
        }
        ((void (*)(object, object, object))message)(items, selector("addObject:"), item);
    }
    put(row, "corevideo_pixel_formats", items);
}

static void pixel_format_state(object row) {
    object library = dlopen("/System/Library/Frameworks/CoreVideo.framework/CoreVideo", 2);
    object foundation = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    pixel_format_create create = library ? dlsym(library, "CVPixelFormatDescriptionCreateWithPixelFormatType") : 0;
    pixel_format_release release = foundation ? dlsym(foundation, "CFRelease") : 0;
    pixel_format_query_with_api(row, library, create, release);
}
