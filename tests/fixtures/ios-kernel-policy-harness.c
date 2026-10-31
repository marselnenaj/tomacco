/* Freestanding ARM64 negative controls. Synthetic bytes only, no firmware. */
typedef unsigned long u64;
volatile u64 seen[8];
extern u64 tomacco_cryptex_pins(u64, u64, void *, u64, void *, u64, void *, void *);
extern void tomacco_lockdown_alias(char *, u64, u64);
extern u64 tomacco_container_rpc(u64, u64, u64, u64);
extern u64 gpu_mmap_probe(u64, void *);
extern u64 virtio_used_probe(u64, void *, u64);
extern u64 installer_ports_probe(u64, unsigned, void *);
volatile u64 cs_seen[5];
static unsigned csproc_missing, csproc_calls;
/* Keep the freestanding harness's table naturally aligned with the MMU off. */
static const unsigned installer_required_flags[] = {1, 0x04000000, 0x20000000};

void *original_csproc_get_blob(void *process) {
    ++csproc_calls;
    return csproc_missing ? (void *)0 : process;
}

void *original_cs_blob_get(void *vnode, unsigned cpu, unsigned subtype, u64 offset) {
    cs_seen[0] = (u64)vnode;
    cs_seen[1] = cpu;
    cs_seen[2] = subtype;
    cs_seen[3] = offset;
    ++cs_seen[4];
    return vnode; /* Synthetic vnode and code-signing blob share storage. */
}
#define CHECK(condition) do { if (!(condition)) return __LINE__; } while (0)

static int same(const char *a, const char *b) {
    while (*a && *a == *b) { ++a; ++b; }
    return *a == *b;
}

int run(void) {
    unsigned queue[32] = {0};
    const unsigned capacities[] = {8, 64, 256, 1024};
    const unsigned alignments[] = {4096, 16384};
    for (unsigned a = 0; a < 2; ++a) {
        u64 alignment = alignments[a];
        for (unsigned n = 0; n < 4; ++n) {
            queue[0x3c / 4] = capacities[n];
            // Exercise ring headers throughout a page, especially when only
            // the header fits before a boundary and its entries cross it.
            for (u64 offset = 0; offset < alignment; offset += 2) {
                u64 available = 0x89170000ul + offset;
                u64 used = virtio_used_probe(available, queue, alignment);
                u64 end = available + 4 + 2 * capacities[n] + 2;
                CHECK(used >= end && used - end < alignment);
                CHECK(used % alignment == 0);
                CHECK(queue[0x3c / 4] == capacities[n]);
            }
        }
    }
    unsigned char metadata[229], manifest[23];
    for (unsigned i = 0; i < 23; ++i) manifest[i] = 0x33;
    for (unsigned tag = 0; tag < 2; ++tag) {
        for (unsigned i = 0; i < 229; ++i) metadata[i] = tag ? 0x22 : 0x11;
        void *payload = 0;
        u64 size = 0;
        CHECK(tomacco_cryptex_pins(1, tag ? 0x63617379 : 0x63737379,
                                  metadata, 229, manifest, 23, &payload, &size) == 0);
        CHECK(payload == metadata + 21 && size == 208);
    }
    for (unsigned i = 0; i < 229; ++i) metadata[i] = 0x11;
    for (unsigned failure = 0; failure < 10; ++failure) {
        void *payload = (void *)0x5555;
        u64 size = 0x4444;
        u64 args[8] = {1, 0x63737379, (u64)metadata, 229, (u64)manifest, 23,
                       (u64)&payload, (u64)&size};
        if (failure == 0) args[0] = 2;
        if (failure == 1) args[1] ^= 1;
        if (failure == 2) args[3] = 228;
        if (failure == 3) args[5] = 22;
        if (failure == 4) metadata[228] ^= 1;
        if (failure == 5) manifest[22] ^= 1;
        if (failure >= 6) args[(unsigned[]){2, 4, 6, 7}[failure - 6]] = 0;
        CHECK(tomacco_cryptex_pins(args[0], args[1], (void *)args[2], args[3],
                                  (void *)args[4], args[5], (void *)args[6],
                                  (void *)args[7]) == 77);
        CHECK(payload == (void *)0x5555 && size == 0x4444);
        for (unsigned i = 0; i < 8; ++i) CHECK(seen[i] == args[i]);
        if (failure == 4) metadata[228] ^= 1;
        if (failure == 5) manifest[22] ^= 1;
    }
    const char *names[] = {"", "s", "security.mac.lockdown_mode_state_public",
        "security.mac.lockdown_mode_state_public.extra", "security.mac.lockdown_mode_state_publi",
        "security.mac.lockdown_mode_state", "security.mac.lockdown_mode_state_public_"};
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        char buffer[64];
        unsigned n = 0;
        do { buffer[n] = names[i][n]; } while (names[i][n++]);
        tomacco_lockdown_alias(buffer, 0x1111, 0x2222);
        CHECK(same(buffer, i == 2 ? "security.mac.lockdown_mode_state" : names[i]));
        CHECK(seen[0] == (u64)buffer && seen[1] == 0x1111 && seen[2] == 0x2222);
        for (unsigned j = 0; j < n; ++j) {
            CHECK(buffer[j] == (i == 2 && j == 32 ? 0 : names[i][j]));
        }
    }
    const unsigned errors[] = {0, 72, 1, 5, 12, 13, 71, 73, 0x10000003, 0xffffffff};
    for (unsigned i = 0; i < sizeof(errors) / sizeof(errors[0]); ++i) {
        int compatible = errors[i] == 0 || errors[i] == 72;
        CHECK(tomacco_container_rpc(errors[i], 0x1234, 0x5678, 0x9abc) == (u64)compatible);
        CHECK(seen[0] == (compatible ? 0 : errors[i]));
        CHECK(seen[1] == 0x1234 && seen[2] == 0x5678 && seen[3] == 0x9abc);
    }
    unsigned char blob[128] __attribute__((aligned(8))) = {0};
    unsigned *flags = (unsigned *)(blob + 0x20);
    const unsigned required_flags = 0x24000003;
    *flags = required_flags;
    for (unsigned library = 0; library < GPU_HASH_COUNT; ++library) {
        for (unsigned i = 0; i < 20; ++i) blob[0x58 + i] = 0x44 + library * 0x11;
        cs_seen[4] = 0;
        CHECK(gpu_mmap_probe(1, blob) == 0);
        CHECK(cs_seen[0] == (u64)blob && cs_seen[1] == 0xffffffff);
        CHECK(cs_seen[2] == 0xffffffff && cs_seen[3] == 0 && cs_seen[4] == 1);
        for (unsigned i = 0; i < 20; ++i) {
            blob[0x58 + i] ^= 1;
            CHECK(gpu_mmap_probe(1, blob) == 1);
            blob[0x58 + i] ^= 1;
        }
        for (unsigned i = 0; i < 4; ++i) {
            *flags = required_flags & ~((unsigned[]){1, 2, 0x04000000, 0x20000000}[i]);
            CHECK(gpu_mmap_probe(1, blob) == 1);
        }
        *flags = required_flags;
        for (unsigned i = 0; i < sizeof(errors) / sizeof(errors[0]); ++i) {
            if (errors[i] == 1) continue;
            cs_seen[4] = 0;
            CHECK(gpu_mmap_probe(errors[i], blob) == errors[i]);
            CHECK(cs_seen[4] == 0);
        }
    }
    // The next table entry is deliberately present but outside the allowed count.
    for (unsigned i = 0; i < 20; ++i) blob[0x58 + i] = 0x44 + GPU_HASH_COUNT * 0x11;
    CHECK(gpu_mmap_probe(1, blob) == 1);
    CHECK(gpu_mmap_probe(1, 0) == 1);
    // Preserve the full original result when the policy denies the exception.
    blob[0x58] ^= 1;
    CHECK(gpu_mmap_probe(0x6000000500000001ul, blob) == 0x6000000500000001ul);
    *flags = 0x24000001;
    for (unsigned i = 0; i < 20; ++i) blob[0x58 + i] = 0x44;
    CHECK(installer_ports_probe(53, 3404, blob) == 77);
    CHECK(csproc_calls == 1);
    csproc_calls = 0;
    CHECK(installer_ports_probe(0, 3403, 0) == 77);
    CHECK(installer_ports_probe(53, 3403, blob) == 53);
    CHECK(installer_ports_probe(53, 3405, blob) == 53);
    CHECK(installer_ports_probe(52, 3404, blob) == 52);
    CHECK(installer_ports_probe(0x6000000500000005ul, 3404, blob) == 0x6000000500000005ul);
    CHECK(installer_ports_probe(53, 3404, 0) == 53);
    CHECK(csproc_calls == 0);
    csproc_missing = 1;
    CHECK(installer_ports_probe(53, 3404, blob) == 53);
    csproc_missing = 0;
    for (unsigned i = 0; i < 20; ++i) {
        blob[0x58 + i] ^= 1;
        CHECK(installer_ports_probe(53, 3404, blob) == 53);
        blob[0x58 + i] ^= 1;
    }
    for (unsigned i = 0; i < 3; ++i) {
        *flags = 0x24000001 & ~installer_required_flags[i];
        CHECK(installer_ports_probe(53, 3404, blob) == 53);
    }
    return 0;
}
