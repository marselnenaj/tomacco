/* SPDX-License-Identifier: GPL-2.0-or-later
 * Opt-in, local research policy for TWO restore sealing metadata containers.
 * This changes authentication policy. It does NOT verify an Apple signature.
 * Every other input reaches the original Image4 verifier unchanged.
 * See restore-seal-pins.S for the exact, hash-guarded 23E5207q callsites.
 */
typedef unsigned long size_t;
extern const unsigned char *CFDataGetBytePtr(void *);
extern long CFDataGetLength(void *);
extern unsigned char *CC_SHA384(const void *, unsigned int, unsigned char *);
extern void *malloc_type_malloc(size_t, unsigned long);
extern void *memcpy(void *, const void *, size_t);
extern int memcmp(const void *, const void *, size_t);
extern int puts(const char *);
extern int original(void *, void *, unsigned int, unsigned long, void **, size_t *, void **);
struct seal_pin {
    unsigned int tag, bytes, offset, size;
    unsigned char digest[48];
};
extern const struct seal_pin tomacco_seal_pins[2];

int tomacco_local_seal(void *data, void *manifest, unsigned int tag, unsigned long variant,
                      void **payload, size_t *size, void **error) {
    if (!data || manifest || variant)
        return original(data, manifest, tag, variant, payload, size, error);
    for (int i = 0; i < 2; i++) {
        const struct seal_pin *pin = &tomacco_seal_pins[i];
        if (tag != pin->tag || !pin->size || pin->offset > pin->bytes ||
            pin->size > pin->bytes - pin->offset || CFDataGetLength(data) != pin->bytes)
            continue;
        const unsigned char *bytes = CFDataGetBytePtr(data);
        unsigned char digest[48];
        if (!bytes || !CC_SHA384(bytes, pin->bytes, digest) || memcmp(digest, pin->digest, 48))
            continue;
        if (payload) {
            void *copy = malloc_type_malloc(pin->size, 0x97db7d03);
            if (!copy) return 0;
            memcpy(copy, bytes + pin->offset, pin->size);
            *payload = copy;
        }
        if (size) *size = pin->size;
        if (error) *error = 0;
        puts("TOMACCO_LOCAL_SEAL_METADATA_PIN_MATCH: development hash policy; Apple signature NOT verified");
        return 1;
    }
    return original(data, manifest, tag, variant, payload, size, error);
}
