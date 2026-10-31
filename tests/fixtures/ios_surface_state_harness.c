#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void *object;
enum kind { DICTIONARY, ARRAY, STRING, NUMBER };
struct value { enum kind kind; const char *text; unsigned long numeric, count;
               const char *keys[32]; object values[32]; };
static struct value pool[8192];
static unsigned used, lookup_calls, release_calls, metadata_calls, dlopen_calls;
static struct value *new_value(enum kind kind) {
    assert(used < sizeof(pool) / sizeof(pool[0]));
    struct value *value = &pool[used++]; value->kind = kind; return value;
}
static object string(const char *text) { struct value *v = new_value(STRING); v->text = text; return v; }
static object number(unsigned long n) { struct value *v = new_value(NUMBER); v->numeric = n; return v; }
static object boolean(int b) { return number((unsigned)!!b); }
static object get_class(const char *name) { return (object)name; }
static object get(object value, const char *name) {
    (void)value;
    assert(!strcmp(name, "dictionary") || !strcmp(name, "array"));
    return new_value(!strcmp(name, "array") ? ARRAY : DICTIONARY);
}
static object selector(const char *name) { return (object)name; }
static void array_add(object receiver, object method, object item) {
    assert(!strcmp(method, "addObject:")); struct value *array = receiver;
    assert(array->kind == ARRAY && array->count < 32); array->values[array->count++] = item;
}
static void (*message)(void);
static void put(object receiver, const char *key, object item) {
    struct value *dictionary = receiver; assert(dictionary->kind == DICTIONARY && item);
    unsigned long index;
    for (index = 0; index < dictionary->count; ++index)
        if (!strcmp(dictionary->keys[index], key)) break;
    assert(index < 32); dictionary->keys[index] = key; dictionary->values[index] = item;
    if (index == dictionary->count) dictionary->count++;
}
static object field(object receiver, const char *key) {
    struct value *dictionary = receiver;
    for (unsigned long i = 0; i < dictionary->count; ++i)
        if (!strcmp(dictionary->keys[i], key)) return dictionary->values[i];
    return 0;
}
static unsigned long numeric(object item) { assert(item && ((struct value *)item)->kind == NUMBER); return ((struct value *)item)->numeric; }
static void text_is(object item, const char *expected) { assert(item && !strcmp(((struct value *)item)->text, expected)); }
static int failure(object row, const char *reason) { (void)row; assert(!strcmp(reason, "invalid_native_surface_ids")); return 7; }
static int emit(object row) { (void)row; return 0; }
static object mock_dlopen(const char *path, int flags) { (void)path; assert(flags == 2); dlopen_calls++; return 0; }
static object mock_dlsym(object library, const char *name) { (void)library; (void)name; assert(0); return 0; }
#define dlopen mock_dlopen
#define dlsym mock_dlsym
#include "../../integrations/ios-emulator/guest-surface-state.h"
#undef dlopen
#undef dlsym

struct native_surface { unsigned requested, actual, released; };
static struct native_surface native[4];
static unsigned null_mask, mismatch_mask;
static unsigned long plane_count = 2, component_count = 4;
static void *lookup(unsigned id) {
    unsigned index = lookup_calls++; assert(index < 4);
    if (null_mask & (1u << index)) return 0;
    native[index].requested = id;
    native[index].actual = mismatch_mask & (1u << index) ? id ^ 1 : id;
    return &native[index];
}
static void release(const void *surface) {
    struct native_surface *value = (struct native_surface *)surface;
    assert(value >= native && value < native + 4 && !value->released);
    value->released++; release_calls++;
}
static unsigned identity(void *surface) { return ((struct native_surface *)surface)->actual; }
static void matched(void *surface) {
    struct native_surface *value = surface;
    assert(value->actual == value->requested && !value->released); metadata_calls++;
}
static unsigned format(void *s) { matched(s); return 0x5f524234; }
static unsigned long planes(void *s) { matched(s); return plane_count; }
static unsigned long width(void *s) { matched(s); return ULONG_MAX; }
static unsigned long height(void *s) { matched(s); return 320; }
static unsigned long allocation(void *s) { matched(s); return 1000000; }
static unsigned long stride(void *s, unsigned long p) { matched(s); assert(p < 7); return 2560 + p; }
static unsigned long element(void *s, unsigned long p) { matched(s); assert(p < 7); return 8; }
static unsigned long components(void *s, unsigned long p) { matched(s); assert(p < 7); return component_count; }
static unsigned name(void *s, unsigned long p, unsigned long c) { matched(s); assert(p < 7 && c < 4); return UINT_MAX; }
static unsigned type(void *s, unsigned long p, unsigned long c) { matched(s); assert(p < 7 && c < 4); return 0x80000000u; }
static unsigned range(void *s, unsigned long p, unsigned long c) { matched(s); assert(p < 7 && c < 4); return 0; }
static unsigned long depth(void *s, unsigned long p, unsigned long c) { matched(s); assert(p < 7 && c < 4); return 16; }
static unsigned long offset(void *s, unsigned long p, unsigned long c) { matched(s); assert(p < 7 && c < 4); return c * 16; }
static struct surface_state_api api(void) {
    return (struct surface_state_api){lookup,release,identity,format,planes,width,height,allocation,
                                    stride,element,components,name,type,range,depth,offset};
}
static object query(struct surface_state_api *a, unsigned size) {
    unsigned ids[] = {0, 7, UINT_MAX, 12}; object row = new_value(DICTIONARY);
    surface_state_query(row, ids, size, a); return row;
}
static void reset(void) {
    memset(pool, 0, sizeof(pool)); memset(native, 0, sizeof(native));
    used = lookup_calls = release_calls = metadata_calls = null_mask = mismatch_mask = 0;
}
int main(int argc, char **argv) {
    assert(argc == 2); message = (void (*)(void))array_add;
    assert(surface_state(new_value(DICTIONARY), "-1") == 7 && !dlopen_calls);
    struct surface_state_api a = api();
    if (!strcmp(argv[1], "ids")) {
        unsigned ids[4] = {19, 20, 21, 22};
        assert(surface_state_ids("0,7,4294967295,12", ids) == 4);
        assert(ids[0] == 0 && ids[1] == 7 && ids[2] == UINT_MAX && ids[3] == 12);
        const char *bad[] = {"", "-1", "+1", " 1", "1 ", "01", "1,,2", ",1", "1,", "1,1", "4294967296", "1,2,3,4,5", "1x", "9999999999999999999999999999999999999999999999"};
        for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
            unsigned before[4]; memcpy(before, ids, sizeof(ids));
            assert(!surface_state_ids(bad[i], ids)); assert(!memcmp(before, ids, sizeof(ids)));
        }
        assert(!surface_state_ids(NULL, ids) && !surface_state_ids("1", NULL));
    } else if (!strcmp(argv[1], "missing")) {
        for (unsigned i = 0; i < 3; ++i) {
            reset(); a = api(); if (i == 0) a.lookup = 0; if (i == 1) a.release = 0; if (i == 2) a.id = 0;
            object row = query(&a, 4); assert(!lookup_calls && !release_calls);
            assert(!numeric(field(row, "native_surface_query_supported")));
        }
    } else if (!strcmp(argv[1], "ownership")) {
        null_mask = 1; mismatch_mask = 2;
        object row = query(&a, 4); assert(lookup_calls == 4 && release_calls == 3);
        struct value *results = field(row, "surfaces"); assert(results->count == 4);
        text_is(field(results->values[0], "state"), "lookup-unavailable");
        text_is(field(results->values[1], "state"), "native-id-mismatch");
        assert(!field(results->values[1], "pixel_format_raw"));
        assert(numeric(field(results->values[2], "actual_native_id")) == UINT_MAX);
    } else if (!strcmp(argv[1], "raw")) {
        object row = query(&a, 1); assert(lookup_calls == 1 && release_calls == 1);
        struct value *results = field(row, "surfaces"); object item = results->values[0];
        assert(numeric(field(item, "width")) == ULONG_MAX);
        assert(numeric(field(item, "pixel_format_raw")) == 0x5f524234);
        struct value *parts = field(item, "planes"); assert(parts->count == 2);
        struct value *fields = field(parts->values[0], "components"); assert(fields->count == 4);
        assert(numeric(field(fields->values[3], "name_raw")) == UINT_MAX);
        assert(numeric(field(fields->values[3], "type_raw")) == 0x80000000u);
        assert(numeric(field(fields->values[3], "range_raw")) == 0);
        assert(numeric(field(fields->values[3], "bit_offset")) == 48);
        assert(!numeric(field(row, "surface_generation_bound")));
        assert(!numeric(field(row, "pixel_contract_qualified")));
    } else if (!strcmp(argv[1], "bounds")) {
        plane_count = 8; object row = query(&a, 1);
        struct value *results = field(row, "surfaces"); struct value *parts = field(results->values[0], "planes");
        assert(!parts->count && release_calls == 1 && metadata_calls == 5);
        reset(); plane_count = 7; component_count = 5; a = api(); row = query(&a, 1);
        results = field(row, "surfaces"); parts = field(results->values[0], "planes"); assert(parts->count == 7);
        for (unsigned i = 0; i < 7; ++i) assert(!((struct value *)field(parts->values[i], "components"))->count);
        assert(release_calls == 1);
    } else {
        assert(!strcmp(argv[1], "zero")); plane_count = 0; object row = query(&a, 1);
        struct value *results = field(row, "surfaces"); assert(!((struct value *)field(results->values[0], "planes"))->count);
        assert(release_calls == 1 && metadata_calls == 5);
        reset(); plane_count = 2; component_count = 0; a = api(); a.name = 0; row = query(&a, 1);
        assert(!numeric(field(row, "component_query_supported")) && release_calls == 1 && metadata_calls == 5);
    }
    return 0;
}
