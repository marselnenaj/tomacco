/* Opt-in CPU graphics research inside the real iOS guest. No SDK, replacement
 * framework, GPU or display controller. Calls Apple's CoreGraphics/QuartzCore.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern char *dlerror(void);
extern int dprintf(int, const char *, ...);
extern int snprintf(char *, unsigned long, const char *, ...);
extern int open(const char *, int, ...);
extern int close(int);
extern long write(int, const void *, unsigned long);
extern int *__error(void);
extern int usleep(unsigned);
extern unsigned sleep(unsigned);
extern unsigned alarm(unsigned);
extern void (*signal(int, void (*)(int)))(int);
extern int sigprocmask(int, const unsigned *, unsigned *);
extern int pthread_set_qos_class_self_np(unsigned, int);

struct tomacco_graphics_termios {
    unsigned long input, output, control, local;
    unsigned char characters[20];
    unsigned long input_speed, output_speed;
};
_Static_assert(sizeof(struct tomacco_graphics_termios) == 72, "Darwin termios ABI");
extern int tcgetattr(int, struct tomacco_graphics_termios *);
extern void cfmakeraw(struct tomacco_graphics_termios *);
extern int tcsetattr(int, int, const struct tomacco_graphics_termios *);

typedef struct { double x, y, width, height; } rectangle;
enum { WIDTH = 256, HEIGHT = 384, STRIDE = WIDTH * 4, PIXELS = STRIDE * HEIGHT };
static unsigned char pixels[PIXELS];
static unsigned char compressed[PIXELS + 4096];
static int (*compress2_fn)(unsigned char *, unsigned long *, const unsigned char *, unsigned long, int);
static int port = -1;
static unsigned retries;
#if TOMACCO_UIKIT_DIAGNOSTICS
extern int tomacco_render_uikit(void *, int, int (*)(const char *));
#endif

static int output(const char *data, unsigned long length) {
    while (length) {
        long count = write(port, data, length);
        if (count < 0 && *__error() == 4) continue;
        if (count < 0 && *__error() == 35 && retries++ < 500) { usleep(10000); continue; }
        if (count <= 0) return -1;
        data += count;
        length -= count;
    }
    return 0;
}

static int frame(const char *renderer) {
    unsigned long compressed_size = sizeof(compressed);
    if (!compress2_fn || compress2_fn(compressed, &compressed_size, pixels, PIXELS, 1) ||
            !compressed_size || compressed_size > sizeof(compressed)) return -1;
    unsigned crc = ~0u;
    for (unsigned i = 0; i < PIXELS; ++i) {
        crc ^= pixels[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
    }
    char line[768];
    int size = snprintf(line, sizeof(line), "TOMACCO_GRAPHICS_BEGIN renderer=%s width=%u height=%u stride=%u format=bgra8-premultiplied encoding=zlib bytes=%u encoded_bytes=%lu crc32=%08x\n",
                        renderer, WIDTH, HEIGHT, STRIDE, PIXELS, compressed_size, ~crc);
    if (size < 0 || (unsigned)size >= sizeof(line) || output(line, size)) return -1;
    static const char hex[] = "0123456789abcdef";
    for (unsigned offset = 0; offset < compressed_size; offset += 256) {
        unsigned count = compressed_size - offset < 256 ? (unsigned)(compressed_size - offset) : 256;
        int prefix = snprintf(line, sizeof(line), "TOMACCO_GRAPHICS_DATA offset=%u hex=", offset);
        if (prefix < 0 || prefix > 200) return -1;
        for (unsigned i = 0; i < count; ++i) {
            line[prefix + 2 * i] = hex[compressed[offset + i] >> 4];
            line[prefix + 2 * i + 1] = hex[compressed[offset + i] & 15];
        }
        line[prefix + count * 2] = '\n';
        if (output(line, prefix + count * 2 + 1)) return -1;
    }
    size = snprintf(line, sizeof(line), "TOMACCO_GRAPHICS_END renderer=%s bytes=%u crc32=%08x\n", renderer, PIXELS, ~crc);
    return size < 0 || (unsigned)size >= sizeof(line) ? -1 : output(line, size);
}

static int render_graphics(void) {
    port = open("/dev/cu.qemuport1", 0x20106);
    struct tomacco_graphics_termios settings;
    if (port < 0 || tcgetattr(port, &settings)) return 1;
    cfmakeraw(&settings);
    if (tcsetattr(port, 0, &settings)) { close(port); return 1; }
#if TOMACCO_UIKIT_DIAGNOSTICS
    dprintf(port, "TOMACCO_GRAPHICS_PROFILE profile=uikit frames=4\n");
#endif
    dprintf(port, "TOMACCO_GRAPHICS stage=load-coregraphics\n");
    void *zlib = dlopen("/usr/lib/libz.1.dylib", 2);
    if (zlib) compress2_fn = (int (*)(unsigned char *, unsigned long *, const unsigned char *, unsigned long, int))dlsym(zlib, "compress2");
    if (!compress2_fn) { dprintf(port, "TOMACCO_GRAPHICS compression_unavailable\n"); return 2; }
    dprintf(port, "TOMACCO_GRAPHICS stage=compression-loaded\n");
    void *cg = dlopen("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", 2);
    if (!cg) { dprintf(port, "TOMACCO_GRAPHICS library_error=%s\n", dlerror()); return 2; }
    dprintf(port, "TOMACCO_GRAPHICS stage=coregraphics-loaded\n");
#define BIND(handle, type, name) type name = (type)dlsym(handle, #name); \
    if (!name) { dprintf(port, "TOMACCO_GRAPHICS missing_symbol=%s\n", #name); return 3; }
    typedef void *(*create_fn)(void);
    typedef void *(*context_fn)(void *, unsigned long, unsigned long, unsigned long, unsigned long, void *, unsigned);
    typedef void (*release_fn)(void *);
    typedef void (*color_fn)(void *, double, double, double, double);
    typedef void (*rect_fn)(void *, rectangle);
    typedef void (*point_fn)(void *, double, double);
    typedef void *(*color_create_fn)(void *, const double *);
    BIND(cg, create_fn, CGColorSpaceCreateDeviceRGB);
    BIND(cg, context_fn, CGBitmapContextCreate);
    BIND(cg, release_fn, CGContextRelease);
    BIND(cg, release_fn, CGColorSpaceRelease);
    BIND(cg, color_fn, CGContextSetRGBFillColor);
    BIND(cg, rect_fn, CGContextFillRect);
    BIND(cg, rect_fn, CGContextFillEllipseInRect);
    BIND(cg, point_fn, CGContextMoveToPoint);
    BIND(cg, point_fn, CGContextAddLineToPoint);
    BIND(cg, release_fn, CGContextFillPath);
    BIND(cg, release_fn, CGContextFlush);
    BIND(cg, color_create_fn, CGColorCreate);
    BIND(cg, release_fn, CGColorRelease);
    void *space = CGColorSpaceCreateDeviceRGB();
    void *context = space ? CGBitmapContextCreate(pixels, WIDTH, HEIGHT, 8, STRIDE, space, 0x2002) : 0;
    if (!context) { dprintf(port, "TOMACCO_GRAPHICS context_unavailable\n"); return 4; }
    dprintf(port, "TOMACCO_GRAPHICS stage=render-bitmap\n");
    CGContextSetRGBFillColor(context, 0, 0, 0, 1);
    CGContextFillRect(context, (rectangle){0, 0, WIDTH, HEIGHT});
    CGContextSetRGBFillColor(context, 1, 0, 0, 1);
    CGContextFillRect(context, (rectangle){16, 16, 96, 96});
    CGContextSetRGBFillColor(context, 0, 1, 0, 1);
    CGContextFillEllipseInRect(context, (rectangle){136, 16, 96, 96});
    CGContextSetRGBFillColor(context, 0, 0, 1, 1);
    CGContextMoveToPoint(context, 128, 160);
    CGContextAddLineToPoint(context, 224, 288);
    CGContextAddLineToPoint(context, 32, 288);
    CGContextFillPath(context);
    CGContextFlush(context);
    if (frame("coregraphics")) return 5;

    dprintf(port, "TOMACCO_GRAPHICS stage=load-quartzcore\n");
    void *qc = dlopen("/System/Library/Frameworks/QuartzCore.framework/QuartzCore", 2);
    void *objc = dlopen("/usr/lib/libobjc.A.dylib", 2);
    if (!qc || !objc) { dprintf(port, "TOMACCO_GRAPHICS library_error=%s\n", dlerror()); return 6; }
    typedef void *(*lookup_fn)(const char *);
    typedef void (*message_fn)(void);
    BIND(objc, lookup_fn, objc_getClass);
    BIND(objc, lookup_fn, sel_registerName);
    BIND(objc, message_fn, objc_msgSend);
#undef BIND
    void *layer_class = objc_getClass("CALayer");
    void *layer = layer_class ? ((void *(*)(void *, void *))objc_msgSend)(layer_class, sel_registerName("new")) : 0;
    if (!layer) { dprintf(port, "TOMACCO_GRAPHICS layer_unavailable\n"); return 7; }
    ((void (*)(void *, void *, rectangle))objc_msgSend)(layer, sel_registerName("setBounds:"), (rectangle){0, 0, WIDTH, HEIGHT});
    double background[] = {1, 0, 1, 1};
    void *color = CGColorCreate(space, background);
    if (!color) return 8;
    ((void (*)(void *, void *, void *))objc_msgSend)(layer, sel_registerName("setBackgroundColor:"), color);
    CGColorRelease(color);
    ((void (*)(void *, void *, double))objc_msgSend)(layer, sel_registerName("setCornerRadius:"), 32);
    CGContextSetRGBFillColor(context, 0, 0, 0, 1);
    CGContextFillRect(context, (rectangle){0, 0, WIDTH, HEIGHT});
    dprintf(port, "TOMACCO_GRAPHICS stage=render-layer\n");
    ((void (*)(void *, void *, void *))objc_msgSend)(layer, sel_registerName("renderInContext:"), context);
    CGContextFlush(context);
    if (frame("calayer")) return 9;
    ((void (*)(void *, void *))objc_msgSend)(layer, sel_registerName("release"));
#if TOMACCO_UIKIT_DIAGNOSTICS
    int uikit_result = tomacco_render_uikit(context, port, frame);
    if (uikit_result) { dprintf(port, "TOMACCO_GRAPHICS uikit_error=%d\n", uikit_result); return uikit_result; }
#endif
    CGContextRelease(context);
    CGColorSpaceRelease(space);
#if TOMACCO_UIKIT_DIAGNOSTICS
    dprintf(port, "TOMACCO_GRAPHICS finished=1 frames=4\n");
#else
    dprintf(port, "TOMACCO_GRAPHICS finished=1 frames=2\n");
#endif
    close(port);
    return 0;
}

int main(void) {
    /* Darwin SIGALRM=14, SIG_UNBLOCK=2, sigset_t=uint32_t. Ensure an inherited
     * mask/handler cannot disable the deadline after the fresh exec.
     */
    unsigned alarms = 1u << 13;
    if (signal(14, 0) == (void (*)(int))-1 || sigprocmask(2, &alarms, 0)) return 10;
    unsigned remaining = 20;
    while (remaining) remaining = sleep(remaining);
    alarm(60);
    /* This bounded drawing job is interactive. During full-system bring-up,
     * dozens of launch jobs can otherwise starve a default-QoS CLI thread.
     * This changes guest scheduling only; it does not raise host QEMU priority.
     */
    int qos = pthread_set_qos_class_self_np(0x21, 0);
    if (qos) { dprintf(2, "TOMACCO_GRAPHICS_PROCESS qos_error=%d\n", qos); return 11; }
    int result = render_graphics();
    dprintf(2, "TOMACCO_GRAPHICS_PROCESS result=%d\n", result);
    return result;
}
