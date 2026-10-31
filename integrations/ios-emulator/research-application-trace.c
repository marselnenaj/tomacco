/* Crash evidence only: preserve the signal and never alter the saved PC.
 * Layout references: XNU bsd/sys/{signal,_types/_ucontext}.h,
 * bsd/arm/_mcontext.h and osfmk/mach/arm/_structs.h (arm64 user ABI).
 * The handler uses bounded local formatting and async-signal-safe syscalls.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern long write(int, const void *, unsigned long);
extern int *__error(void);
extern int getpid(void);
extern int kill(int, int);

struct action { void (*handler)(int, void *, void *); unsigned mask; int flags; };
extern int sigaction(int, const struct action *, struct action *);
struct stack { void *address; unsigned long size; int flags; };
struct registers {
    unsigned long far;
    unsigned esr, exception;
    unsigned long x[29], fp, lr, sp, pc;
    unsigned cpsr, flags;
};
struct context {
    int on_stack;
    unsigned mask;
    struct stack stack;
    void *link;
    unsigned long machine_size;
    struct registers *machine;
};
_Static_assert(sizeof(struct action) == 16, "Darwin sigaction ABI");
_Static_assert(__builtin_offsetof(struct context, machine) == 48, "Darwin ucontext ABI");
_Static_assert(__builtin_offsetof(struct registers, pc) == 272, "Darwin arm64 mcontext PC offset");
_Static_assert(sizeof(struct registers) == 288, "Darwin mcontext prefix ABI");

static int descriptor;
static unsigned *event_sequence;
static unsigned long stack_bottom, stack_top, cache_base;

static char *append(char *out, const char *text) {
    while (*text) *out++ = *text++;
    return out;
}

static char *decimal(char *out, unsigned value) {
    char reverse[10];
    unsigned count = 0;
    do { reverse[count++] = '0' + value % 10; value /= 10; } while (value);
    while (count) *out++ = reverse[--count];
    return out;
}

static char *address(char *out, unsigned long value) {
    static const char hex[] = "0123456789abcdef";
    out = append(out, "\"0x");
    for (int shift = 60; shift >= 0; shift -= 4) *out++ = hex[(value >> shift) & 15];
    *out++ = '"';
    return out;
}

static void fatal_signal(int number, void *info, void *opaque) {
    (void)info;
    struct context *context = opaque;
    struct registers *registers = context && context->machine_size >= sizeof(struct registers) ? context->machine : 0;
    /* Fits well below 1024 bytes: fixed fields plus at most 16 raw addresses.
     * Capture address bits unchanged; symbolication belongs to the host.
     */
    char buffer[1024], *out = buffer;
    out = append(out, "{\"sequence\":");
    out = decimal(out, __atomic_add_fetch(event_sequence, 1, __ATOMIC_RELAXED));
    out = append(out, ",\"event\":\"fatal-signal\",\"signal\":");
    out = decimal(out, (unsigned)number);
    out = append(out, ",\"pc\":"); out = address(out, registers ? registers->pc : 0);
    out = append(out, ",\"lr\":"); out = address(out, registers ? registers->lr : 0);
    out = append(out, ",\"fp\":"); out = address(out, registers ? registers->fp : 0);
    out = append(out, ",\"cache_base\":"); out = address(out, cache_base);
    out = append(out, ",\"frames\":[");
    unsigned long fp = registers ? registers->fp & 0x0000fffffffffffful : 0;
    for (unsigned count = 0; count < 16 && stack_top >= 16 && fp >= stack_bottom && fp <= stack_top - 16 && !(fp & 15); ++count) {
        const unsigned long *frame = (const unsigned long *)fp;
        if (count) *out++ = ',';
        out = address(out, frame[1]);
        unsigned long next = frame[0] & 0x0000fffffffffffful;
        if (next <= fp) break;
        fp = next;
    }
    out = append(out, "]}\n");
    unsigned long remaining = (unsigned long)(out - buffer);
    const char *cursor = buffer;
    for (unsigned attempt = 0; remaining && attempt < 4; ++attempt) {
        long count = write(descriptor, cursor, remaining);
        if (count < 0 && *__error() == 4) continue;
        if (count <= 0) break;
        cursor += count;
        remaining -= (unsigned long)count;
    }
    struct action reset = {0, 0, 0};
    sigaction(number, &reset, 0);
    kill(getpid(), number);
    /* The pending original signal uses its default action on handler return. */
}

int tomacco_application_trace(int port, unsigned *sequence) {
    void *system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    if (!system) return -1;
    void *(*self)(void) = (void *(*)(void))dlsym(system, "pthread_self");
    void *(*top)(void *) = (void *(*)(void *))dlsym(system, "pthread_get_stackaddr_np");
    unsigned long (*size)(void *) = (unsigned long (*)(void *))dlsym(system, "pthread_get_stacksize_np");
    const void *(*cache)(unsigned long *) = (const void *(*)(unsigned long *))dlsym(system, "_dyld_get_shared_cache_range");
    if (!self || !top || !size || !cache) return -1;
    void *thread = self();
    stack_top = (unsigned long)top(thread);
    unsigned long bytes = size(thread), cache_bytes = 0;
    if (!bytes || bytes > stack_top || bytes > 64ul * 1024 * 1024) return -1;
    stack_bottom = stack_top - bytes;
    cache_base = (unsigned long)cache(&cache_bytes);
    if (!cache_base || !cache_bytes) return -1;
    descriptor = port;
    event_sequence = sequence;
    struct action handler = {fatal_signal, (1u << 4) | (1u << 5), 0x40};
    if (sigaction(5, &handler, 0) || sigaction(6, &handler, 0)) return -1;
    return 0;
}
