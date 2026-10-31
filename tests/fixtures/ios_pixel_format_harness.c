/* Synthetic Foundation dispatch; exercises the shipped projection and lifetime
 * code, not native format semantics. The trampoline preserves both integer and
 * floating-point Objective-C return ABIs on the Linux x86_64 test host. */
#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void *object;
enum kind { CLASS, DICTIONARY, ARRAY, STRING, NUMBER, OTHER };
struct value {
    enum kind kind;
    const char *text;
    double numeric;
    unsigned long count;
    object keys[64], values[64];
    unsigned releases, retains;
};
static struct value pool[2048];
static unsigned used, create_calls, release_calls, lookups;
static object descriptions[3];
static object exported_keys[20];
static int symbols_available = 1;
static struct value *new_value(enum kind kind) {
    assert(used < sizeof(pool) / sizeof(pool[0]));
    struct value *value = &pool[used++];
    value->kind = kind;
    value->retains = 1;
    return value;
}
static object string(const char *text) {
    struct value *value = new_value(STRING);
    value->text = text;
    value->count = strlen(text);
    return value;
}
static object number(unsigned long n) {
    struct value *value = new_value(NUMBER);
    value->numeric = (double)n;
    return value;
}
static object boolean(int b) { return number((unsigned long)!!b); }
static object get_class(const char *name) {
    struct value *value = new_value(CLASS);
    value->text = name;
    return value;
}
static object selector(const char *name) { return (object)name; }
static object get(object receiver, const char *name) {
    (void)receiver;
    assert(!strcmp(name, "dictionary") || !strcmp(name, "array"));
    return new_value(!strcmp(name, "array") ? ARRAY : DICTIONARY);
}
static object with(object receiver, const char *name, object key) {
    assert(!strcmp(name, "objectForKey:"));
    struct value *dictionary = receiver, *needle = key;
    assert(dictionary->kind == DICTIONARY && needle->kind == STRING);
    lookups++;
    for (unsigned long i = 0; i < dictionary->count; ++i)
        if (!strcmp(((struct value *)dictionary->keys[i])->text, needle->text))
            return dictionary->values[i];
    return 0;
}
static void put(object receiver, const char *key, object item) {
    struct value *dictionary = receiver;
    assert(dictionary->kind == DICTIONARY && dictionary->count < 64 && item);
    dictionary->keys[dictionary->count] = string(key);
    dictionary->values[dictionary->count++] = item;
    ((struct value *)item)->retains++;
}
static object bounded_text(object value, unsigned long limit) {
    assert(((struct value *)value)->kind == STRING && ((struct value *)value)->count <= limit);
    return value;
}
static unsigned char is_class(object item, object method, object klass) {
    (void)method;
    enum kind kind = ((struct value *)item)->kind;
    const char *name = ((struct value *)klass)->text;
    return (kind == DICTIONARY && !strcmp(name, "NSDictionary")) ||
           (kind == ARRAY && !strcmp(name, "NSArray")) ||
           (kind == STRING && !strcmp(name, "NSString")) ||
           (kind == NUMBER && !strcmp(name, "NSNumber"));
}
static unsigned long size_value(object item, object method) {
    (void)method;
    return ((struct value *)item)->count;
}
static double double_value(object item, object method) {
    (void)method;
    return ((struct value *)item)->numeric;
}
static object array_at(object item, object method, unsigned long index) {
    (void)method;
    assert(index < ((struct value *)item)->count);
    return ((struct value *)item)->values[index];
}
static void array_add(object item, object method, object value) {
    (void)method;
    struct value *array = item;
    assert(array->kind == ARRAY && array->count < 64);
    array->values[array->count++] = value;
    ((struct value *)value)->retains++;
}
__attribute__((used, noinline)) static object dispatch_target(object receiver, object method) {
    (void)receiver;
    const char *name = method;
    if (!strcmp(name, "isKindOfClass:")) return (object)is_class;
    if (!strcmp(name, "count") || !strcmp(name, "length")) return (object)size_value;
    if (!strcmp(name, "doubleValue")) return (object)double_value;
    if (!strcmp(name, "objectAtIndex:")) return (object)array_at;
    assert(!strcmp(name, "addObject:"));
    return (object)array_add;
}
__attribute__((naked)) static void mock_message(void) {
    __asm__("push %rdi\n push %rsi\n push %rdx\n push %rcx\n push %r8\n push %r9\n"
            "sub $8, %rsp\n call dispatch_target\n add $8, %rsp\n"
            "pop %r9\n pop %r8\n pop %rcx\n pop %rdx\n pop %rsi\n pop %rdi\n jmp *%rax\n");
}
static object message = (object)mock_message;
static object mock_dlopen(const char *path, int flags) { (void)path; (void)flags; return 0; }
static object mock_dlsym(object library, const char *name);
#define dlopen mock_dlopen
#define dlsym mock_dlsym
#include "../../integrations/ios-emulator/guest-pixel-format-state.h"
#undef dlopen
#undef dlsym
static object mock_dlsym(object library, const char *name) {
    assert(library);
    if (!symbols_available) return 0;
    for (unsigned i = 0; i < sizeof(pixel_format_keys) / sizeof(pixel_format_keys[0]); ++i)
        if (!strcmp(name, pixel_format_keys[i].symbol)) return &exported_keys[i];
    if (!strcmp(name, "kCVPixelFormatPlanes")) return &exported_keys[19];
    assert(0);
    return 0;
}
static object create_description(const void *allocator, unsigned format) {
    const unsigned expected[] = {0x5f524234u, 0x42475241u, 0x34323076u};
    assert(!allocator && create_calls < 3 && format == expected[create_calls]);
    return descriptions[create_calls++];
}
static void release_description(const void *description) {
    struct value *value = (struct value *)description;
    assert(value && !value->releases);
    value->releases++;
    release_calls++;
}
static object field(object dictionary, const char *key) {
    return with(dictionary, "objectForKey:", string(key));
}
static void text_is(object value, const char *expected) {
    assert(value && ((struct value *)value)->kind == STRING &&
           !strcmp(((struct value *)value)->text, expected));
}
static void reset(void) {
    memset(pool, 0, sizeof(pool));
    used = create_calls = release_calls = lookups = 0;
    memset(descriptions, 0, sizeof(descriptions));
    symbols_available = 1;
    for (unsigned i = 0; i < 19; ++i) exported_keys[i] = string(pixel_format_keys[i].symbol);
    exported_keys[19] = string("kCVPixelFormatPlanes");
}
static object query(void) {
    object row = new_value(DICTIONARY);
    pixel_format_query_with_api(row, (object)1, create_description, release_description);
    assert(create_calls == 3);
    return row;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    reset();
    if (!strcmp(argv[1], "missing")) {
        for (unsigned missing = 0; missing < 3; ++missing) {
            object row = new_value(DICTIONARY);
            pixel_format_query_with_api(row, missing == 0 ? 0 : (object)1,
                missing == 1 ? 0 : create_description, missing == 2 ? 0 : release_description);
            assert(!create_calls && !release_calls);
            assert(!((struct value *)field(row, "corevideo_format_query_supported"))->numeric);
        }
        pixel_format_state(new_value(DICTIONARY));
        assert(!create_calls && !release_calls);
    } else if (!strcmp(argv[1], "null")) {
        object row = query();
        struct value *items = field(row, "corevideo_pixel_formats");
        assert(items->count == 3 && !release_calls);
        for (unsigned i = 0; i < 3; ++i)
            text_is(field(items->values[i], "state"), "description_unavailable_unknown");
        assert(!((struct value *)field(row, "corevideo_renderer_contract_verified"))->numeric);
    } else if (!strcmp(argv[1], "ownership")) {
        descriptions[0] = new_value(OTHER);
        descriptions[1] = new_value(DICTIONARY);
        ((struct value *)descriptions[1])->count = 65;
        descriptions[2] = new_value(DICTIONARY);
        query();
        assert(release_calls == 3 && lookups == 20);
        for (unsigned i = 0; i < 3; ++i) assert(((struct value *)descriptions[i])->releases == 1);
    } else if (!strcmp(argv[1], "fields")) {
        object native = new_value(DICTIONARY), numeric = number(123), nan = number(0);
        ((struct value *)nan)->numeric = NAN;
        put(native, "kCVPixelFormatName", string("native control"));
        put(native, "kCVPixelFormatConstant", numeric);
        put(native, "kCVPixelFormatFourCC", nan);
        put(native, "kCVPixelFormatContainsAlpha", new_value(OTHER));
        object long_text = string("length stub");
        ((struct value *)long_text)->count = 257;
        put(native, "kCVPixelFormatComponentRange", long_text);
        descriptions[0] = native;
        struct value *items = field(query(), "corevideo_pixel_formats");
        object projection = field(items->values[0], "description");
        object fields = field(projection, "fields"), states = field(projection, "field_states");
        text_is(field(fields, "name"), "native control");
        assert(field(fields, "constant") == numeric && ((struct value *)numeric)->retains >= 3);
        text_is(field(states, "constant"), "number");
        text_is(field(states, "fourcc"), "nonfinite_number");
        text_is(field(states, "contains_alpha"), "value_type_unhandled");
        text_is(field(states, "component_range"), "string_limit");
        text_is(field(states, "block_width"), "not_present");
        assert(!field(fields, "fourcc") && !field(fields, "component_range") && release_calls == 1);
    } else if (!strcmp(argv[1], "symbols")) {
        symbols_available = 0;
        descriptions[0] = new_value(DICTIONARY);
        struct value *items = field(query(), "corevideo_pixel_formats");
        object projection = field(items->values[0], "description");
        text_is(field(field(projection, "field_states"), "name"), "key_symbol_unavailable");
        assert(!((struct value *)field(projection, "planes_key_supported"))->numeric && release_calls == 1);
    } else if (!strcmp(argv[1], "planes")) {
        object native = new_value(DICTIONARY), planes = new_value(ARRAY), nested = new_value(DICTIONARY);
        put(nested, "kCVPixelFormatPlanes", planes);
        array_add(planes, 0, nested); /* A cycle must stop at the one-level limit. */
        array_add(planes, 0, new_value(OTHER));
        put(native, "kCVPixelFormatPlanes", planes);
        descriptions[0] = native;
        struct value *items = field(query(), "corevideo_pixel_formats");
        object projection = field(items->values[0], "description");
        struct value *projected_planes = field(projection, "planes");
        assert(projected_planes->count == 2 && release_calls == 1);
        text_is(field(projected_planes->values[0], "planes_state"), "nested_plane_limit");
        assert(!((struct value *)field(projected_planes->values[1], "dictionary_known"))->numeric);
    } else {
        assert(!strcmp(argv[1], "limits"));
        for (unsigned i = 0; i < 2; ++i) {
            descriptions[i] = new_value(DICTIONARY);
            object planes = new_value(i == 0 ? ARRAY : OTHER);
            ((struct value *)planes)->count = 9;
            put(descriptions[i], "kCVPixelFormatPlanes", planes);
        }
        struct value *items = field(query(), "corevideo_pixel_formats");
        text_is(field(field(items->values[0], "description"), "planes_state"), "plane_count_limit");
        text_is(field(field(items->values[1], "description"), "planes_state"), "planes_value_type_unhandled");
        assert(release_calls == 2);
    }
    puts("synthetic pixel-format owner checks passed");
    return 0;
}
