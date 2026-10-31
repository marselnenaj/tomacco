/* Private, inherited datagram channel between our setup helper and adapter.
 * Receipt means the adapter's constructor ran; never app installation success.
 * The receipt carries the 20-byte digest of the adapter's policy table below,
 * so it identifies the exact admission policy that became active.
 */
#ifndef TOMACCO_DEVELOPMENT_PROTOCOL_H
#define TOMACCO_DEVELOPMENT_PROTOCOL_H
enum { TOMACCO_DEVELOPMENT_ACK_FD = 198, TOMACCO_DEVELOPMENT_ACK_BYTES = 25 };

static inline void tomacco_development_ack_encode(unsigned char *packet,
                                                 unsigned char state,
                                                 const unsigned char *cdhash) {
    packet[0] = 'P'; packet[1] = 'S'; packet[2] = 'D'; packet[3] = 1;
    packet[4] = state;
    for (unsigned i = 0; i < 20; ++i) packet[5 + i] = cdhash[i];
}

static inline int tomacco_development_ack_decode(const unsigned char *packet,
                                                unsigned long length,
                                                const unsigned char *cdhash) {
    if (length != TOMACCO_DEVELOPMENT_ACK_BYTES || packet[0] != 'P' || packet[1] != 'S' ||
        packet[2] != 'D' || packet[3] != 1 || (packet[4] != 1 && packet[4] != 2)) return 0;
    unsigned different = 0;
    for (unsigned i = 0; i < 20; ++i) different |= packet[5 + i] ^ cdhash[i];
    return different ? 0 : packet[4];
}

/* Development-signing policy table.
 *
 * The apps installd's adapter admits with an ad-hoc signature, once the
 * original libmis validator has accepted their resource seals: for each, the
 * bundle identifier and the SHA-256 CodeDirectory hash (truncated to 20 bytes,
 * the cdhash) of the executable the guest-tools build signed. The build
 * compiles the table into the adapter; the host verifier finds the same bytes
 * inside the signed library and checks them against the build record.
 *
 *   header, 8 bytes:  'P' 'S' 'P' 'T'   version 1   count (1..32)   0   0
 *   entries, count times 280 bytes, in ascending byte order of identifier:
 *     cdhash[20]        the executable's CodeDirectory hash
 *     flags[4]          0 (reserved)
 *     identifier[256]   bundle identifier, NUL-terminated; the bytes after
 *                       the terminator are 0. Characters: A-Z a-z 0-9 . -
 *
 * The decoder accepts one canonical encoding of a set only: identifiers are
 * unique and strictly ascending, so a table either decodes completely or is
 * refused as a whole. Nothing in the table is executed or trusted by itself;
 * every entry still needs the matching trust-cache entry for its executable.
 */
enum { TOMACCO_POLICY_HEADER_BYTES = 8, TOMACCO_POLICY_ENTRY_BYTES = 280, TOMACCO_POLICY_MAX_ENTRIES = 32,
       TOMACCO_POLICY_CDHASH_BYTES = 20, TOMACCO_POLICY_IDENTIFIER_BYTES = 256 };

static inline int tomacco_policy_identifier_character(unsigned char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.' || c == '-';
}

static inline const unsigned char *tomacco_policy_cdhash(const unsigned char *table, unsigned index) {
    return table + TOMACCO_POLICY_HEADER_BYTES + index * TOMACCO_POLICY_ENTRY_BYTES;
}

static inline const char *tomacco_policy_identifier(const unsigned char *table, unsigned index) {
    return (const char *)tomacco_policy_cdhash(table, index) + TOMACCO_POLICY_CDHASH_BYTES + 4;
}

/* Byte order of two NUL-terminated identifiers: negative, zero or positive. */
static inline int tomacco_policy_compare(const char *left, const char *right) {
    unsigned i = 0;
    while (left[i] && left[i] == right[i]) ++i;
    return (int)(unsigned char)left[i] - (int)(unsigned char)right[i];
}

/* The number of entries of a valid table, or 0 for anything else. */
static inline unsigned tomacco_policy_count(const unsigned char *table, unsigned long length) {
    if (length < TOMACCO_POLICY_HEADER_BYTES || table[0] != 'P' || table[1] != 'S' || table[2] != 'P' ||
        table[3] != 'T' || table[4] != 1 || table[6] || table[7]) return 0;
    unsigned count = table[5];
    if (!count || count > TOMACCO_POLICY_MAX_ENTRIES ||
        length != TOMACCO_POLICY_HEADER_BYTES + (unsigned long)count * TOMACCO_POLICY_ENTRY_BYTES) return 0;
    for (unsigned index = 0; index < count; ++index) {
        const unsigned char *entry = tomacco_policy_cdhash(table, index);
        const unsigned char *flags = entry + TOMACCO_POLICY_CDHASH_BYTES;
        if (flags[0] || flags[1] || flags[2] || flags[3]) return 0;
        const unsigned char *identifier = flags + 4;
        unsigned end = 0;
        while (end < TOMACCO_POLICY_IDENTIFIER_BYTES && identifier[end]) {
            if (!tomacco_policy_identifier_character(identifier[end])) return 0;
            ++end;
        }
        if (!end || end >= TOMACCO_POLICY_IDENTIFIER_BYTES || identifier[0] == '.' || identifier[0] == '-') return 0;
        for (unsigned rest = end; rest < TOMACCO_POLICY_IDENTIFIER_BYTES; ++rest) if (identifier[rest]) return 0;
        if (index && tomacco_policy_compare(tomacco_policy_identifier(table, index - 1), (const char *)identifier) >= 0)
            return 0;
    }
    return count;
}

/* The table of what a running phone was given since its boot (policy_add in
 * guest-host-channel.h), read again by the adapter whenever the compiled-in
 * table does not name an app. Same encoding. The file lies in installd's own
 * cache, because installd's sandbox reads there; the setup helper
 * creates it for the mobile user at each boot, empty. The trust cache is the
 * boot's own too, so it holds nothing that outlives the boot. */
#define TOMACCO_HOT_POLICY "/private/var/installd/Library/Caches/Tomacco/hot-policy.table"
enum { TOMACCO_POLICY_TABLE_BYTES = TOMACCO_POLICY_HEADER_BYTES + TOMACCO_POLICY_MAX_ENTRIES * TOMACCO_POLICY_ENTRY_BYTES };

/* The entry of an identifier in a table tomacco_policy_count accepted, or -1. */
static inline int tomacco_policy_find(const unsigned char *table, unsigned count, const char *identifier) {
    for (unsigned index = 0; index < count; ++index)
        if (!tomacco_policy_compare(tomacco_policy_identifier(table, index), identifier)) return (int)index;
    return -1;
}

/* Set an identifier's cdhash in a table of `count` accepted entries, in a buffer
 * of TOMACCO_POLICY_TABLE_BYTES, keeping the one canonical encoding. The size of
 * the new table, or 0 when it is full. An identifier listed already gets the new
 * hash: a redeployed app has another one. */
static inline unsigned long tomacco_policy_insert(unsigned char *table, unsigned count,
                                                 const unsigned char *cdhash, const char *identifier) {
    unsigned at = 0;
    int replace = 0;
    for (; at < count; ++at) {
        int order = tomacco_policy_compare(tomacco_policy_identifier(table, at), identifier);
        if (order >= 0) { replace = !order; break; }
    }
    if (!replace && count >= TOMACCO_POLICY_MAX_ENTRIES) return 0;
    if (!replace) {
        for (unsigned index = count; index > at; --index) {
            unsigned char *to = table + TOMACCO_POLICY_HEADER_BYTES + index * TOMACCO_POLICY_ENTRY_BYTES;
            const unsigned char *from = to - TOMACCO_POLICY_ENTRY_BYTES;
            for (unsigned byte = 0; byte < TOMACCO_POLICY_ENTRY_BYTES; ++byte) to[byte] = from[byte];
        }
        ++count;
    }
    unsigned char *entry = table + TOMACCO_POLICY_HEADER_BYTES + at * TOMACCO_POLICY_ENTRY_BYTES;
    for (unsigned byte = 0; byte < TOMACCO_POLICY_ENTRY_BYTES; ++byte) entry[byte] = 0;
    for (unsigned byte = 0; byte < TOMACCO_POLICY_CDHASH_BYTES; ++byte) entry[byte] = cdhash[byte];
    for (unsigned byte = 0; identifier[byte] && byte < TOMACCO_POLICY_IDENTIFIER_BYTES - 1; ++byte)
        entry[TOMACCO_POLICY_CDHASH_BYTES + 4 + byte] = (unsigned char)identifier[byte];
    table[0] = 'P'; table[1] = 'S'; table[2] = 'P'; table[3] = 'T';
    table[4] = 1; table[5] = (unsigned char)count; table[6] = 0; table[7] = 0;
    return TOMACCO_POLICY_HEADER_BYTES + (unsigned long)count * TOMACCO_POLICY_ENTRY_BYTES;
}

/* Development-signing activation state, read from the tail of the setup
 * helper's log (its own "development-setup" events and the adapter's
 * "development-signing" events, interleaved). A start must know this before it
 * installs a staged app: installd admits an ad-hoc app only while the adapter
 * is active, so an install sent before activation is rejected as an invalid
 * signature. The last boot's cycle is the text after the final
 * library-integrity line; its outcome is one of these. A truncated leading
 * line cannot forge a marker past the real ones, and the caller passes enough
 * of the log's tail to hold the last whole cycle (a cycle is well under 2 KB).
 */
enum { TOMACCO_DEVELOPMENT_ACTIVATION_ABSENT = 0, TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED = 1,
       TOMACCO_DEVELOPMENT_ACTIVATION_FAILED = 2, TOMACCO_DEVELOPMENT_ACTIVATION_PENDING = 3 };

/* The highest offset in text[0..length) where needle occurs, or -1. */
static inline long tomacco_development_find_last(const char *text, unsigned long length, const char *needle) {
    unsigned long n = 0;
    while (needle[n]) ++n;
    if (!n || length < n) return -1;
    for (unsigned long start = length - n;; --start) {
        unsigned long i = 0;
        while (i < n && text[start + i] == needle[i]) ++i;
        if (i == n) return (long)start;
        if (!start) return -1;
    }
}

static inline int tomacco_development_activation_scan(const char *text, unsigned long length) {
    long cycle = tomacco_development_find_last(text, length, "\"phase\":\"library-integrity\"");
    if (cycle < 0) return TOMACCO_DEVELOPMENT_ACTIVATION_ABSENT;
    /* The adapter's own "active" event, or the setup's "activated" confirmation,
     * after the last cycle began: either means the adapter is hooked in installd. */
    if (tomacco_development_find_last(text, length, "\"event\":\"development-signing\",\"state\":\"active\"") > cycle ||
        tomacco_development_find_last(text, length, "\"event\":\"development-setup\",\"state\":\"activated\"") > cycle)
        return TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED;
    if (tomacco_development_find_last(text, length, "\"event\":\"development-setup\",\"state\":\"failed\"") > cycle)
        return TOMACCO_DEVELOPMENT_ACTIVATION_FAILED;
    return TOMACCO_DEVELOPMENT_ACTIVATION_PENDING;
}
#endif
