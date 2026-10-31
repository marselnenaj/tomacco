/* Bounded reader for launchd's native service-print response. This is a
 * readback of one explicitly addressed service, not a launchd log heuristic.
 * Unknown, incomplete or ambiguous output never proves controller release.
 */
#ifndef TOMACCO_SERVICE_STATE_H
#define TOMACCO_SERVICE_STATE_H
struct tomacco_service_state {
    int pid;
    int controller_pid;
};

static inline unsigned long tomacco_service_text_length(const char *text) {
    unsigned long n = 0;
    while (text[n]) ++n;
    return n;
}

static inline int tomacco_service_equal(const char *data, unsigned long size, const char *text) {
    unsigned long n = tomacco_service_text_length(text);
    if (n != size) return 0;
    for (unsigned long i = 0; i < n; ++i) if (data[i] != text[i]) return 0;
    return 1;
}

static inline int tomacco_service_prefix(const char *data, unsigned long size, const char *text) {
    unsigned long n = tomacco_service_text_length(text);
    return n <= size && tomacco_service_equal(data, n, text);
}

static inline int tomacco_service_pid(const char *data, unsigned long size) {
    if (!size || size > 10) return -1;
    unsigned value = 0;
    for (unsigned long i = 0; i < size; ++i) {
        if (data[i] < '0' || data[i] > '9') return -1;
        unsigned digit = (unsigned)(data[i] - '0');
        if (value > (2147483647u - digit) / 10u) return -1;
        value = value * 10u + digit;
    }
    return value ? (int)value : -1;
}

static inline int tomacco_service_state_read(const char *data, unsigned long size,
                                           const char *specifier,
                                           struct tomacco_service_state *result) {
    struct tomacco_service_state state = {0, 0};
    unsigned long name_size = tomacco_service_text_length(specifier);
    if (!data || !result || size < name_size + 8 ||
        !tomacco_service_equal(data, name_size, specifier) ||
        !tomacco_service_equal(data + name_size, 5, " = {\n")) return 0;
    unsigned seen_pid = 0, seen_program = 0, seen_controller = 0, seen_state = 0;
    unsigned long at = name_size + 5;
    while (at < size) {
        unsigned long end = at;
        while (end < size && data[end] != '\n') {
            if (!data[end] || (unsigned char)data[end] < 0x20) {
                if (data[end] != '\t') return 0;
            }
            ++end;
        }
        if (end == size) return 0;
        const char *line = data + at;
        unsigned long length = end - at;
        if (tomacco_service_equal(line, length, "}")) {
            if (end + 1 != size || !seen_pid || !seen_program || !seen_state) return 0;
            *result = state;
            return 1;
        }
        /* Nested environments and endpoint dictionaries do not describe the
         * service's own controller. The native printer uses one tab per level. */
        if (length && line[0] != '\t') return 0;
        if (tomacco_service_prefix(line, length, "\tpid = ")) {
            if (seen_pid++ || (state.pid = tomacco_service_pid(line + 7, length - 7)) < 1) return 0;
        } else if (tomacco_service_prefix(line, length, "\tprogram = ")) {
            if (seen_program++ || !tomacco_service_equal(line + 11, length - 11, "/usr/libexec/installd")) return 0;
        } else if (tomacco_service_prefix(line, length, "\tstate = ")) {
            if (seen_state++ || !tomacco_service_equal(line + 9, length - 9, "running")) return 0;
        } else if (tomacco_service_prefix(line, length, "\tcontroller = ")) {
            if (seen_controller++) return 0;
            unsigned long dot = length;
            while (dot > 14 && line[dot - 1] != '.') --dot;
            if (dot <= 15 || (state.controller_pid = tomacco_service_pid(line + dot, length - dot)) < 1) return 0;
        } else if (tomacco_service_prefix(line, length, "\tcontroller") &&
                   !tomacco_service_prefix(line, length, "\tcontroller-supplied ")) {
            return 0;
        }
        at = end + 1;
    }
    return 0;
}
#endif
