/* Synthetic runtime records and integer dispatch exercise the actual shipped
 * capability owner and metadata guard. They establish no native GPU support. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void *object;
enum kind { DICTIONARY, ARRAY, STRING, NUMBER };
struct value {
    enum kind kind;
    const char *text;
    unsigned long numeric, count;
    object keys[64], values[64];
};
static struct value pool[4096];
static unsigned used;
static object new_value(enum kind kind) {
    assert(used < sizeof(pool) / sizeof(pool[0]));
    struct value *value = &pool[used++];
    value->kind = kind;
    return value;
}
static object string(const char *text) { struct value *v = new_value(STRING); v->text = text; return v; }
static object number(unsigned long n) { struct value *v = new_value(NUMBER); v->numeric = n; return v; }
static object boolean(int b) { return number(!!b); }
static object bounded_text(object value, unsigned long limit) {
    assert(strlen(((struct value *)value)->text) <= limit);
    return value;
}
static object field(object dictionary, const char *key) {
    struct value *v = dictionary;
    assert(v && v->kind == DICTIONARY);
    for (unsigned long i = 0; i < v->count; ++i)
        if (!strcmp(((struct value *)v->keys[i])->text, key)) return v->values[i];
    return 0;
}
static void put(object dictionary, const char *key, object value) {
    struct value *v = dictionary;
    assert(v && v->kind == DICTIONARY && value);
    for (unsigned long i = 0; i < v->count; ++i)
        if (!strcmp(((struct value *)v->keys[i])->text, key)) { v->values[i] = value; return; }
    assert(v->count < 64);
    v->keys[v->count] = string(key);
    v->values[v->count++] = value;
}
static object selector(const char *name) { return (object)name; }
static object get_class(const char *name) { return (object)name; }
static unsigned char device, queries;
struct method_shape {
    const char *name, *result, *self_type, *selector_type;
    unsigned argc, present, calls;
    unsigned long value;
    object receiver;
    const char *value_type;
};
static struct method_shape methods[24];
static unsigned getter_calls, image_calls;
static unsigned family_count;
static long observed_families[7];
static const long expected_families[] = {1001, 1002, 1003, 1004, 1005, 1007, 1009};
static unsigned char family_values[7] = {1, 0, 1, 0, 1, 0, 1};
static object query_result = &queries;
static object object_class(object receiver) {
    assert(receiver == &device || receiver == &queries);
    return receiver;
}
static object instance_method(object cls, object sel) {
    for (unsigned i = 0; i < sizeof(methods) / sizeof(methods[0]); ++i)
        if (methods[i].receiver == cls && methods[i].name && !strcmp(methods[i].name, sel))
            return methods[i].present ? &methods[i] : 0;
    return 0;
}
static unsigned argument_count(object method) { return ((struct method_shape *)method)->argc; }
static void return_type(object method, char *out, unsigned long size) {
    snprintf(out, size, "%s", ((struct method_shape *)method)->result);
}
static void argument_type(object method, unsigned index, char *out, unsigned long size) {
    struct method_shape *m = method;
    snprintf(out, size, "%s", index == 0 ? m->self_type : index == 1 ? m->selector_type : m->value_type);
}
#include "../../integrations/ios-emulator/guest-objc-contract.h"

static object get(object receiver, const char *name) {
    if (!strcmp(name, "dictionary")) return new_value(DICTIONARY);
    if (!strcmp(name, "array")) return new_value(ARRAY);
    assert(receiver == &device && !strcmp(name, "featureQueries"));
    struct method_shape *method = instance_method(receiver, (object)name);
    assert(method && accepts(receiver, name, '@', 0));
    method->calls++;
    getter_calls++;
    return query_result;
}
static void array_add(object receiver, object sel, object item) {
    (void)sel;
    struct value *v = receiver;
    assert(v->kind == ARRAY && v->count < 64);
    v->values[v->count++] = item;
}
static unsigned char native_boolean(object receiver, object sel) {
    struct method_shape *method = instance_method(receiver, sel);
    assert(method && accepts(receiver, sel, 'B', 0));
    method->calls++;
    return (unsigned char)method->value;
}
static unsigned long long native_profile(object receiver, object sel) {
    struct method_shape *method = instance_method(receiver, sel);
    assert(method && receiver == &device && accepts(receiver, sel, 'Q', 0));
    method->calls++;
    return method->value;
}
static unsigned char native_family(object receiver, object sel, long family) {
    struct method_shape *method = instance_method(receiver, sel);
    const char *arguments[] = {"q"};
    assert(method && receiver == &device && accepts_types(receiver, sel, 'B', arguments, 1));
    assert(family_count < 7 && family == expected_families[family_count]);
    observed_families[family_count] = family;
    method->calls++;
    return family_values[family_count++];
}
__attribute__((used, noinline)) static object dispatch_target(object receiver, object sel) {
    if (!strcmp(sel, "addObject:")) return (object)array_add;
    assert(receiver == &device || receiver == &queries);
    if (!strcmp(sel, "featureProfile")) return (object)native_profile;
    if (!strcmp(sel, "supportsFamily:")) return (object)native_family;
    return (object)native_boolean;
}
__attribute__((naked)) static void mock_message(void) {
    __asm__("push %rdi\n push %rsi\n push %rdx\n push %rcx\n push %r8\n push %r9\n"
            "sub $8, %rsp\n call dispatch_target\n add $8, %rsp\n"
            "pop %r9\n pop %r8\n pop %rcx\n pop %rdx\n pop %rsi\n pop %rdi\n jmp *%rax\n");
}
static object message = (object)mock_message;
typedef void (*graphics_imp)(void);
struct graphics_dl_info { const char *image; void *base; const char *symbol; void *symbol_address; };
static unsigned virtual_calls, virtual_lookup_calls, virtual_lookup_mode;
static unsigned char virtual_value;
static unsigned char native_virtual_device(object key) {
    struct value *v = key;
    assert(v && v->kind == STRING && !strcmp(v->text, "IsVirtualDevice"));
    ++virtual_calls;
    return virtual_value;
}
static int virtual_lookup(const void *address, struct graphics_dl_info *info) {
    assert(address == (const void *)native_virtual_device);
    ++virtual_lookup_calls;
    if (virtual_lookup_mode == 1) return 0;
    if (virtual_lookup_mode != 2) info->image = "/usr/lib/libMobileGestalt.dylib";
    return 1;
}
/* Existing image metadata boundary is stubbed; the actual capability owner
 * must pass the selected method and preserve unavailable metadata as unknown. */
static void graphics_method_image(object item, object method,
                                 graphics_imp (*implementation)(object),
                                 int (*lookup)(const void *, struct graphics_dl_info *)) {
    (void)implementation; (void)lookup;
    assert(method || !implementation);
    image_calls++;
    put(item, "method_implementation_image_known", boolean(0));
}
static const char *class_name(object cls) {
    assert(cls == &device || cls == &queries);
    return cls == &device ? "NativeDevice" : "NativeFeatureQueries";
}
static object mock_dlopen(const char *path, int flags) { (void)path; (void)flags; return 0; }
static object mock_dlsym(object library, const char *name) { (void)library; (void)name; assert(0); return 0; }
#define dlopen mock_dlopen
#define dlsym mock_dlsym
#include "../../integrations/ios-emulator/guest-metal-capabilities.h"
#undef dlopen
#undef dlsym

static const char *names[] = {
    "supportsLimitedYUVFormats", "supportsExtendedYUVFormats", "supportsAlphaYUVFormats",
    "supportsPublicXR10Formats", "supportsExtendedXR10Formats", "supportsCorrectExtendedXR10Formats",
    "supportsYCBCRFormatsXR", "supportsYCBCRPackedFormatsXR", "familySupportsLimitedYUVFormats",
    "familySupportsExtendedYUVFormats", "familySupportsPublicXR10Formats", "familySupportsExtendedXR10Formats"
};
static void initialize(void) {
    for (unsigned i = 0; i < 20; ++i) {
        unsigned index = i < 8 ? i : i - 8;
        methods[i] = (struct method_shape){names[index], "B", "@", ":", 2, 1, 0,
                                          i < 8 ? 1 : 0, i < 8 ? (object)&device : &queries, "@"};
    }
    methods[20] = (struct method_shape){"featureProfile", "Q", "@", ":", 2, 1, 0, 4, &device, "@"};
    methods[21] = (struct method_shape){"featureQueries", "@", "@", ":", 2, 1, 0, 0, &device, "@"};
    methods[22] = (struct method_shape){"supportsCorrectTextureUsageBits", "B", "@", ":", 2, 1, 0, 1, &device, "@"};
    methods[23] = (struct method_shape){"supportsFamily:", "B", "@", ":", 3, 1, 0, 0, &device, "q"};
    assert(!accepts_class(&device, "supportsLimitedYUVFormats", 'B'));
}
static unsigned calls(void) {
    unsigned n = 0;
    for (unsigned i = 0; i < 24; ++i) n += methods[i].calls;
    return n;
}
static void text_is(object value, const char *expected) {
    assert(value && ((struct value *)value)->kind == STRING && !strcmp(((struct value *)value)->text, expected));
}
static unsigned long scalar(object value) { assert(value); return ((struct value *)value)->numeric; }
static void families_are(object snapshot, int known, const char *state) {
    struct value *answers = field(snapshot, "compositor_family_support");
    assert(answers && answers->count == 7);
    for (unsigned i = 0; i < 7; ++i) {
        object item = answers->values[i];
        text_is(field(item, "selector"), "supportsFamily:");
        text_is(field(item, "expected_argument_encoding"), "q");
        text_is(field(item, "state"), state);
        assert(scalar(field(item, "family")) == (unsigned long)expected_families[i]);
        assert(scalar(field(item, "known")) == (unsigned)known);
        if (known) {
            assert(scalar(field(item, "value")) == family_values[i]);
            assert(observed_families[i] == expected_families[i]);
        } else assert(!field(item, "value"));
    }
    assert(family_count == (known ? 7u : 0u));
}
static void answers_are(object snapshot, const char *key, int known, int value, const char *state) {
    struct value *items = field(field(snapshot, key), "answers");
    assert(items && items->count == (!strcmp(key, "default_device") ? 8 : 12));
    for (unsigned long i = 0; i < items->count; ++i) {
        object item = items->values[i];
        assert(scalar(field(item, "known")) == (unsigned)known);
        if (known) assert(scalar(field(item, "value")) == (unsigned)value);
        else assert(!field(item, "value"));
        text_is(field(item, "state"), state);
    }
}
static object query(object receiver) {
    struct metal_capability_metadata api = {.class_name = class_name};
    object row = new_value(DICTIONARY);
    metal_capability_state_with_api(row, receiver, &api);
    object snapshot = field(row, "metal_capability_snapshot");
    text_is(field(snapshot, "scope"), "this-disposable-worker");
    assert(!scalar(field(snapshot, "renderer_support_verified")));
    assert(!scalar(field(snapshot, "pixel_layout_verified")));
    assert(!scalar(field(snapshot, "compositor_process_value_observed")));
    object virtual_device = field(snapshot, "virtual_device_query");
    text_is(field(virtual_device, "state"), "api_unavailable");
    assert(!scalar(field(virtual_device, "known")) && !field(virtual_device, "value"));
    assert(!scalar(field(virtual_device, "property_presence_verified")));
    return snapshot;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    initialize();
    if (!strcmp(argv[1], "missing-device")) {
        object snapshot = query(0);
        answers_are(snapshot, "default_device", 0, 0, "receiver_unavailable");
        answers_are(snapshot, "feature_queries", 0, 0, "receiver_unavailable");
        families_are(snapshot, 0, "receiver_unavailable");
        assert(!calls() && !image_calls);
    } else if (!strcmp(argv[1], "missing-methods")) {
        for (unsigned i = 0; i < 24; ++i) methods[i].present = 0;
        object snapshot = query(&device);
        answers_are(snapshot, "default_device", 0, 0, "method_unavailable");
        answers_are(snapshot, "feature_queries", 0, 0, "receiver_unavailable");
        families_are(snapshot, 0, "method_unavailable");
        assert(!calls());
    } else if (!strcmp(argv[1], "incompatible")) {
        for (unsigned i = 1; i < 24; ++i) methods[i].present = 0;
        for (unsigned variant = 0; variant < 6; ++variant) {
            methods[0].result = variant == 0 ? "c" : variant == 1 ? "Bextra" : "B";
            methods[0].argc = variant == 2 ? 3 : 2;
            methods[0].self_type = variant == 3 ? "#" : variant == 5 ? "r@" : "@";
            methods[0].selector_type = variant == 4 ? "@" : ":";
            struct metal_capability_metadata api = {0};
            int compatible = 1;
            object item = metal_capability_method(&device, names[0], 'B', &api, &compatible);
            assert(!compatible && !calls() && !field(item, "value"));
            text_is(field(item, "state"), "signature_incompatible");
        }
    } else if (!strcmp(argv[1], "false")) {
        for (unsigned i = 0; i < 20; ++i) methods[i].value = 0;
        methods[22].value = 0;
        memset(family_values, 0, sizeof(family_values));
        object snapshot = query(&device);
        answers_are(snapshot, "default_device", 1, 0, "observed");
        answers_are(snapshot, "feature_queries", 1, 0, "observed");
        families_are(snapshot, 1, "observed");
        assert(!scalar(field(field(snapshot, "correct_texture_usage_bits"), "value")));
        assert(calls() == 30 && getter_calls == 1);
    } else if (!strcmp(argv[1], "distinct")) {
        object snapshot = query(&device);
        answers_are(snapshot, "default_device", 1, 1, "observed");
        answers_are(snapshot, "feature_queries", 1, 0, "observed");
        text_is(field(field(snapshot, "default_device"), "receiver_class"), "NativeDevice");
        text_is(field(field(snapshot, "feature_queries"), "receiver_class"), "NativeFeatureQueries");
        families_are(snapshot, 1, "observed");
        assert(scalar(field(field(snapshot, "correct_texture_usage_bits"), "value")));
        assert(scalar(field(field(snapshot, "feature_profile"), "value")) == 4 && calls() == 30);
        for (unsigned i = 0; i < 23; ++i) assert(methods[i].calls == 1);
        assert(methods[23].calls == 7);
    } else if (!strcmp(argv[1], "nil")) {
        query_result = 0;
        object snapshot = query(&device);
        text_is(field(field(snapshot, "feature_queries_getter"), "state"), "returned_nil");
        assert(scalar(field(field(snapshot, "feature_queries_getter"), "known")));
        answers_are(snapshot, "feature_queries", 0, 0, "receiver_unavailable");
        assert(calls() == 18 && getter_calls == 1);
    } else if (!strcmp(argv[1], "getter-guard")) {
        methods[20].result = "q";
        methods[21].argc = 3;
        object snapshot = query(&device);
        text_is(field(field(snapshot, "feature_profile"), "state"), "signature_incompatible");
        text_is(field(field(snapshot, "feature_queries_getter"), "state"), "signature_incompatible");
        assert(!field(field(snapshot, "feature_profile"), "value") && calls() == 16 && !getter_calls);
        answers_are(snapshot, "feature_queries", 0, 0, "receiver_unavailable");
        metal_capability_state(new_value(DICTIONARY), 0); /* Missing optional image metadata. */
        assert(calls() == 16);
    } else if (!strcmp(argv[1], "family-guard")) {
        const char *bad_types[] = {"Q", "i", "qextra", "@", "rq"};
        for (unsigned i = 0; i < sizeof(bad_types) / sizeof(bad_types[0]); ++i) {
            methods[23].value_type = bad_types[i];
            struct metal_capability_metadata api = {0};
            object snapshot = new_value(DICTIONARY);
            put(snapshot, "compositor_family_support", metal_compositor_families(&device, &api));
            families_are(snapshot, 0, "signature_incompatible");
            assert(!calls());
        }
    } else if (!strcmp(argv[1], "family-method-guard")) {
        for (unsigned variant = 0; variant < 5; ++variant) {
            methods[23].result = variant == 0 ? "c" : "B";
            methods[23].argc = variant == 1 ? 2 : variant == 2 ? 4 : 3;
            methods[23].self_type = variant == 3 ? "#" : "@";
            methods[23].selector_type = variant == 4 ? "@" : ":";
            struct metal_capability_metadata api = {0};
            object snapshot = new_value(DICTIONARY);
            put(snapshot, "compositor_family_support", metal_compositor_families(&device, &api));
            families_are(snapshot, 0, "signature_incompatible");
            assert(!calls());
        }
    } else if (!strcmp(argv[1], "usage-independent")) {
        methods[5].value = 0;
        object snapshot = query(&device);
        struct value *answers = field(field(snapshot, "default_device"), "answers");
        assert(!scalar(field(answers->values[5], "value")));
        object usage = field(snapshot, "correct_texture_usage_bits");
        text_is(field(usage, "selector"), "supportsCorrectTextureUsageBits");
        assert(scalar(field(usage, "known")) && scalar(field(usage, "value")));
        assert(methods[22].calls == 1 && methods[5].calls == 1);
    } else if (!strcmp(argv[1], "virtual-unavailable")) {
        struct metal_capability_metadata api = {.lookup = virtual_lookup};
        object item = metal_virtual_device_answer(&api);
        text_is(field(item, "state"), "api_unavailable");
        assert(!scalar(field(item, "known")) && !field(item, "value") && !field(item, "raw_answer"));
        assert(!virtual_calls && !virtual_lookup_calls);
    } else if (!strcmp(argv[1], "virtual-answers")) {
        struct metal_capability_metadata api = {.virtual_device_answer = native_virtual_device, .lookup = virtual_lookup};
        const unsigned char values[] = {0, 1, 7};
        for (unsigned i = 0; i < sizeof(values); ++i) {
            virtual_value = values[i];
            object item = metal_virtual_device_answer(&api);
            text_is(field(item, "api"), "MGGetBoolAnswer");
            text_is(field(item, "property"), "IsVirtualDevice");
            text_is(field(item, "state"), "observed_api_answer");
            assert(scalar(field(item, "known")) && scalar(field(item, "raw_answer")) == values[i]);
            assert(scalar(field(item, "value")) == !!values[i]);
            assert(!scalar(field(item, "property_presence_verified")));
            assert(!scalar(field(item, "compositor_process_value_observed")));
            assert(scalar(field(item, "implementation_image_known")));
            text_is(field(item, "implementation_image"), "/usr/lib/libMobileGestalt.dylib");
            assert(virtual_calls == i + 1 && virtual_lookup_calls == i + 1);
        }
    } else if (!strcmp(argv[1], "virtual-metadata")) {
        struct metal_capability_metadata api = {.virtual_device_answer = native_virtual_device, .lookup = virtual_lookup};
        virtual_value = 1;
        for (unsigned i = 1; i <= 3; ++i) {
            virtual_lookup_mode = i;
            if (i == 3) api.lookup = 0;
            object item = metal_virtual_device_answer(&api);
            assert(scalar(field(item, "known")) && scalar(field(item, "value")));
            assert(!scalar(field(item, "implementation_image_known")) && !field(item, "implementation_image"));
            assert(virtual_calls == i);
        }
        assert(virtual_lookup_calls == 2);
    } else assert(0);
    return 0;
}
