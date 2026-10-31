/* Public retained IOSurface lookup; raw metadata only. Basic ABI and component
 * getter widths are bound to the original getters in observer75/Source79.
 * IDs identify this lookup, not a historical surface generation or mapper ref.
 * Every nonnull lookup is released once; no texture or pixel is accessed. */
#ifndef TOMACCO_GUEST_SURFACE_STATE_H
#define TOMACCO_GUEST_SURFACE_STATE_H

_Static_assert(sizeof(unsigned) == 4, "native IOSurface ID ABI");
_Static_assert(sizeof(unsigned long) == 8, "Darwin size_t ABI");

static unsigned surface_state_ids(const char *text, unsigned ids[4]) {
    if (!text || !ids || !text[0] || strlen(text) > 43) return 0;
    unsigned values[4], size = 0;
    const char *cursor = text;
    while (*cursor) {
        if (size == 4 || *cursor < '0' || *cursor > '9') return 0;
        if (*cursor == '0' && cursor[1] >= '0' && cursor[1] <= '9') return 0;
        unsigned value = 0;
        do {
            unsigned digit = (unsigned)(*cursor++ - '0');
            if (value > (~0u - digit) / 10) return 0;
            value = value * 10 + digit;
        } while (*cursor >= '0' && *cursor <= '9');
        for (unsigned index = 0; index < size; ++index)
            if (values[index] == value) return 0;
        values[size++] = value;
        if (!*cursor) break;
        if (*cursor++ != ',' || !*cursor) return 0;
    }
    for (unsigned index = 0; index < size; ++index) ids[index] = values[index];
    return size;
}

struct surface_state_api {
    void *(*lookup)(unsigned);
    void (*release)(const void *);
    unsigned (*id)(void *), (*format)(void *);
    unsigned long (*planes)(void *), (*width)(void *), (*height)(void *), (*allocation)(void *);
    unsigned long (*stride)(void *, unsigned long), (*element)(void *, unsigned long);
    unsigned long (*components)(void *, unsigned long);
    unsigned (*name)(void *, unsigned long, unsigned long);
    unsigned (*type)(void *, unsigned long, unsigned long);
    unsigned (*range)(void *, unsigned long, unsigned long);
    unsigned long (*depth)(void *, unsigned long, unsigned long);
    unsigned long (*offset)(void *, unsigned long, unsigned long);
};

static void surface_state_add(object array, object item) {
    ((void (*)(object, object, object))message)(array, selector("addObject:"), item);
}

static void surface_state_query(object row, const unsigned *ids, unsigned size,
                                const struct surface_state_api *api) {
    put(row, "native_surface_query_supported", boolean(0));
    put(row, "surface_generation_bound", boolean(0));
    put(row, "pixel_contract_qualified", boolean(0));
    if (!ids || !size || size > 4 || !api || !api->lookup || !api->release ||
        !api->id || !api->format || !api->planes || !api->width || !api->height || !api->allocation) {
        put(row, "native_surface_query_state", string("api-unavailable"));
        return;
    }
    int component_api = api->stride && api->element && api->components && api->name &&
                        api->type && api->range && api->depth && api->offset;
    put(row, "native_surface_query_supported", boolean(1));
    put(row, "component_query_supported", boolean(component_api));
    put(row, "id_namespace", string("native-iosurface-public-id"));
    object results = get(get_class("NSMutableArray"), "array");
    for (unsigned index = 0; index < size; ++index) {
        object item = get(get_class("NSMutableDictionary"), "dictionary");
        put(item, "requested_native_id", number(ids[index]));
        put(item, "generation_bound", boolean(0));
        void *surface = api->lookup(ids[index]);
        put(item, "lookup_nonnull", boolean(surface != 0));
        if (!surface) {
            /* Missing, denied, retired or unknown; never proof of absence. */
            put(item, "state", string("lookup-unavailable"));
            surface_state_add(results, item);
            continue;
        }
        unsigned actual_id = api->id(surface);
        put(item, "actual_native_id", number(actual_id));
        put(item, "native_id_matches", boolean(actual_id == ids[index]));
        if (actual_id == ids[index]) {
            unsigned long planes = api->planes(surface);
            put(item, "pixel_format_raw", number(api->format(surface)));
            put(item, "width", number(api->width(surface)));
            put(item, "height", number(api->height(surface)));
            put(item, "allocation_bytes", number(api->allocation(surface)));
            put(item, "plane_count", number(planes));
            put(item, "component_planes_bounded", boolean(planes <= 7));
            object rows = get(get_class("NSMutableArray"), "array");
            if (component_api && planes <= 7) {
                for (unsigned long plane = 0; plane < planes; ++plane) {
                    object part = get(get_class("NSMutableDictionary"), "dictionary");
                    unsigned long components = api->components(surface, plane);
                    put(part, "plane", number(plane));
                    put(part, "bytes_per_row", number(api->stride(surface, plane)));
                    put(part, "bytes_per_element", number(api->element(surface, plane)));
                    put(part, "component_count", number(components));
                    put(part, "components_bounded", boolean(components <= 4));
                    object fields = get(get_class("NSMutableArray"), "array");
                    if (components <= 4) {
                        for (unsigned long component = 0; component < components; ++component) {
                            object field = get(get_class("NSMutableDictionary"), "dictionary");
                            put(field, "component", number(component));
                            put(field, "name_raw", number(api->name(surface, plane, component)));
                            put(field, "type_raw", number(api->type(surface, plane, component)));
                            put(field, "range_raw", number(api->range(surface, plane, component)));
                            put(field, "bit_depth", number(api->depth(surface, plane, component)));
                            put(field, "bit_offset", number(api->offset(surface, plane, component)));
                            surface_state_add(fields, field);
                        }
                    }
                    put(part, "components", fields);
                    surface_state_add(rows, part);
                }
            }
            put(item, "planes", rows);
            put(item, "state", string("native-metadata"));
        } else {
            put(item, "state", string("native-id-mismatch"));
        }
        api->release(surface);
        surface_state_add(results, item);
    }
    put(row, "surfaces", results);
    put(row, "native_surface_query_state", string("completed"));
}

static int surface_state(object row, const char *argument) {
    unsigned ids[4];
    unsigned size = surface_state_ids(argument, ids);
    if (!size) return failure(row, "invalid_native_surface_ids");
    object library = dlopen("/System/Library/Frameworks/IOSurface.framework/IOSurface", 2);
    object core = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    struct surface_state_api api = {0};
    if (library) {
#define SURFACE_STATE_SYMBOL(field, name) api.field = dlsym(library, name)
        SURFACE_STATE_SYMBOL(lookup, "IOSurfaceLookup");
        SURFACE_STATE_SYMBOL(id, "IOSurfaceGetID");
        SURFACE_STATE_SYMBOL(format, "IOSurfaceGetPixelFormat");
        SURFACE_STATE_SYMBOL(planes, "IOSurfaceGetPlaneCount");
        SURFACE_STATE_SYMBOL(width, "IOSurfaceGetWidth");
        SURFACE_STATE_SYMBOL(height, "IOSurfaceGetHeight");
        SURFACE_STATE_SYMBOL(allocation, "IOSurfaceGetAllocSize");
        SURFACE_STATE_SYMBOL(stride, "IOSurfaceGetBytesPerRowOfPlane");
        SURFACE_STATE_SYMBOL(element, "IOSurfaceGetBytesPerElementOfPlane");
        SURFACE_STATE_SYMBOL(components, "IOSurfaceGetNumberOfComponentsOfPlane");
        SURFACE_STATE_SYMBOL(name, "IOSurfaceGetNameOfComponentOfPlane");
        SURFACE_STATE_SYMBOL(type, "IOSurfaceGetTypeOfComponentOfPlane");
        SURFACE_STATE_SYMBOL(range, "IOSurfaceGetRangeOfComponentOfPlane");
        SURFACE_STATE_SYMBOL(depth, "IOSurfaceGetBitDepthOfComponentOfPlane");
        SURFACE_STATE_SYMBOL(offset, "IOSurfaceGetBitOffsetOfComponentOfPlane");
#undef SURFACE_STATE_SYMBOL
    }
    if (core) api.release = dlsym(core, "CFRelease");
    surface_state_query(row, ids, size, &api);
    put(row, "ok", boolean(1));
    return emit(row);
}
#endif
