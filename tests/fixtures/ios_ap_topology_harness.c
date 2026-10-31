/* Executes the extracted production C owner with synthetic ADT accessors.
 * This fixture does not execute QEMU CPUs, Apple firmware or guest storage. */
#include <assert.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FIXTURE_CPUS 32
struct dtree_node {
    uint32_t nChildren;
    const char *name;
    struct dtree_node *children[FIXTURE_CPUS];
    bool reg_present;
    uint32_t reg_length;
    uint8_t reg[8];
};

struct topology_fixture {
    struct dtree_node root, cpus, cpu[FIXTURE_CPUS];
    char names[FIXTURE_CPUS][16];
};

static unsigned find_calls, property_reads, length_reads, value_reads;
static unsigned error_reports;
static char last_error[256];

static struct dtree_node *adt_find_node(struct dtree_node *node,
                                        const char *path)
{
    const char *separator = strchr(path, '/');
    size_t length = separator ? (size_t)(separator - path) : strlen(path);

    find_calls++;
    assert(node && node->nChildren <= FIXTURE_CPUS);
    for (uint32_t i = 0; i < node->nChildren; i++) {
        struct dtree_node *child = node->children[i];
        if (strlen(child->name) == length && !memcmp(child->name, path, length)) {
            return separator ? adt_find_node(child, separator + 1) : child;
        }
    }
    return NULL;
}

static void *adt_get_prop_val(struct dtree_node *node, const char *name)
{
    assert(node && !strcmp(name, "reg"));
    property_reads++;
    return node->reg_present ? node->reg : NULL;
}

static uint32_t adt_get_prop_len(struct dtree_node *node, const char *name)
{
    /* The real accessor dereferences the result of its property lookup. */
    assert(node && node->reg_present && !strcmp(name, "reg"));
    length_reads++;
    return node->reg_length;
}

static uint32_t ldl_le_p(const void *pointer)
{
    const uint8_t *bytes = pointer;
    value_reads++;
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

static void error_report(const char *format, ...)
{
    va_list arguments;
    va_start(arguments, format);
    vsnprintf(last_error, sizeof(last_error), format, arguments);
    va_end(arguments);
    error_reports++;
}

#include "vphone-ap-topology-owner.h"

static void set_reg(struct dtree_node *node, uint32_t value)
{
    for (unsigned i = 0; i < 4; i++) {
        node->reg[i] = (uint8_t)(value >> (8 * i));
    }
}

static void prepare_tree(struct topology_fixture *fixture, unsigned count)
{
    assert(count <= FIXTURE_CPUS);
    memset(fixture, 0, sizeof(*fixture));
    fixture->root.name = "/";
    fixture->root.nChildren = 1;
    fixture->root.children[0] = &fixture->cpus;
    fixture->cpus.name = "cpus";
    fixture->cpus.nChildren = count;
    for (unsigned i = 0; i < count; i++) {
        struct dtree_node *node = &fixture->cpu[i];
        snprintf(fixture->names[i], sizeof(fixture->names[i]), "cpu%u", i);
        node->name = fixture->names[i];
        node->reg_present = true;
        node->reg_length = 4;
        set_reg(node, i);
        fixture->cpus.children[i] = node;
    }
}

static void expect_result(struct topology_fixture *fixture, unsigned ap_cpus,
                          bool expected)
{
    uint8_t before[sizeof(*fixture)];
    memcpy(before, fixture, sizeof(before));
    find_calls = property_reads = length_reads = value_reads = error_reports = 0;
    last_error[0] = '\0';
    bool actual = vphone_validate_ap_topology(&fixture->root, ap_cpus);
    if (actual != expected) {
        fprintf(stderr, "ap_cpus=%u nChildren=%u expected=%u actual=%u: %s\n",
                ap_cpus, fixture->cpus.nChildren, (unsigned)expected,
                (unsigned)actual, last_error);
    }
    assert(actual == expected);
    assert(error_reports == (expected ? 0u : 1u));
    assert(!memcmp(before, fixture, sizeof(before)));
}

static void check_valid(unsigned count)
{
    struct topology_fixture fixture;
    prepare_tree(&fixture, count);
    expect_result(&fixture, count, true);
    assert(find_calls == count * 2 + 1);
    assert(property_reads == count && length_reads == count && value_reads == count);
}

static void check_counts(void)
{
    const unsigned counts[][2] = {
        {0, 0}, {17, 17}, {1, 0}, {8, 7}, {8, 9},
        {16, 15}, {16, 17}, {16, 32},
    };
    struct topology_fixture fixture;
    for (unsigned i = 0; i < sizeof(counts) / sizeof(counts[0]); i++) {
        prepare_tree(&fixture, counts[i][1]);
        expect_result(&fixture, counts[i][0], false);
        assert(!property_reads && !length_reads && !value_reads);
        assert(find_calls <= 1);
    }
    prepare_tree(&fixture, 16);
    fixture.cpus.nChildren = UINT32_MAX;
    expect_result(&fixture, 16, false);
    assert(find_calls == 1 && !property_reads);
    prepare_tree(&fixture, 8);
    fixture.root.nChildren = 0;
    expect_result(&fixture, 8, false);
    assert(find_calls == 1 && !property_reads);
}

static void check_names(void)
{
    const char *bad_names[] = {"cpu8", "extra", "cpu6", "cpu07"};
    struct topology_fixture fixture;
    for (unsigned i = 0; i < sizeof(bad_names) / sizeof(bad_names[0]); i++) {
        prepare_tree(&fixture, 8);
        fixture.cpu[7].name = bad_names[i];
        expect_result(&fixture, 8, false);
        assert(property_reads == 7 && length_reads == 7 && value_reads == 7);
    }
    prepare_tree(&fixture, 17);
    fixture.cpu[16].name = "extra";
    expect_result(&fixture, 16, false);
    assert(find_calls == 1 && !property_reads);
}

static void check_missing_reg(void)
{
    const unsigned counts[] = {1, 8, 16};
    struct topology_fixture fixture;
    for (unsigned i = 0; i < sizeof(counts) / sizeof(counts[0]); i++) {
        for (unsigned endpoint = 0; endpoint < 2; endpoint++) {
            unsigned missing = endpoint ? counts[i] - 1 : 0;
            prepare_tree(&fixture, counts[i]);
            fixture.cpu[missing].reg_present = false;
            expect_result(&fixture, counts[i], false);
            assert(property_reads == missing + 1);
            assert(length_reads == missing && value_reads == missing);
        }
    }
}

static void check_reg_length(void)
{
    const unsigned counts[] = {1, 8, 16};
    const uint32_t lengths[] = {0, 1, 3, 5, 8};
    struct topology_fixture fixture;
    for (unsigned i = 0; i < sizeof(counts) / sizeof(counts[0]); i++) {
        for (unsigned j = 0; j < sizeof(lengths) / sizeof(lengths[0]); j++) {
            unsigned invalid = counts[i] - 1;
            prepare_tree(&fixture, counts[i]);
            fixture.cpu[invalid].reg_length = lengths[j];
            expect_result(&fixture, counts[i], false);
            assert(property_reads == invalid + 1 && length_reads == invalid + 1);
            assert(value_reads == invalid);
        }
    }
}

static void check_reg_affinity(void)
{
    const unsigned counts[] = {1, 8, 16};
    const uint32_t values[] = {1, 16, UINT32_MAX};
    struct topology_fixture fixture;
    for (unsigned i = 0; i < sizeof(counts) / sizeof(counts[0]); i++) {
        for (unsigned j = 0; j < sizeof(values) / sizeof(values[0]); j++) {
            unsigned invalid = counts[i] - 1;
            prepare_tree(&fixture, counts[i]);
            set_reg(&fixture.cpu[invalid], values[j]);
            expect_result(&fixture, counts[i], false);
            assert(property_reads == invalid + 1 && length_reads == invalid + 1);
            assert(value_reads == invalid + 1);
        }
    }
    prepare_tree(&fixture, 8);
    set_reg(&fixture.cpu[0], 1);
    set_reg(&fixture.cpu[1], 0);
    expect_result(&fixture, 8, false);
    assert(property_reads == 1 && length_reads == 1 && value_reads == 1);
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "valid-1")) {
        check_valid(1);
    } else if (!strcmp(argv[1], "valid-8")) {
        check_valid(8);
    } else if (!strcmp(argv[1], "valid-16")) {
        check_valid(16);
    } else if (!strcmp(argv[1], "counts")) {
        check_counts();
    } else if (!strcmp(argv[1], "names")) {
        check_names();
    } else if (!strcmp(argv[1], "missing-reg")) {
        check_missing_reg();
    } else if (!strcmp(argv[1], "reg-length")) {
        check_reg_length();
    } else if (!strcmp(argv[1], "reg-affinity")) {
        check_reg_affinity();
    } else {
        fprintf(stderr, "Unknown fixture case: %s\n", argv[1]);
        return 2;
    }
    return 0;
}
