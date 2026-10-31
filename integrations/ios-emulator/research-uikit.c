/* Bounded offscreen UIKit research. Uses actual guest Objective-C/UIKit APIs.
 * No UIApplication replacement, fake screen, event server or framework patches.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern char *dlerror(void);
extern int dprintf(int, const char *, ...);

typedef struct { double x, y, width, height; } rectangle;
static void *(*get_class)(const char *);
static void *(*selector)(const char *);
static void (*message)(void);
static void *status_label, *toggle;
static unsigned actions;

static void *get(void *object, const char *method) {
    return ((void *(*)(void *, void *))message)(object, selector(method));
}

static void invoke(void *object, const char *method) {
    ((void (*)(void *, void *))message)(object, selector(method));
}

static void set(void *object, const char *method, void *value) {
    ((void (*)(void *, void *, void *))message)(object, selector(method), value);
}

static void *string(const char *text) {
    return ((void *(*)(void *, void *, const char *))message)(get_class("NSString"), selector("stringWithUTF8String:"), text);
}

static void *color(double red, double green, double blue) {
    return ((void *(*)(void *, void *, double, double, double, double))message)(
        get_class("UIColor"), selector("colorWithRed:green:blue:alpha:"), red, green, blue, 1);
}

static void *view(const char *name, rectangle frame) {
    void *object = get(get_class(name), "alloc");
    return ((void *(*)(void *, void *, rectangle))message)(object, selector("initWithFrame:"), frame);
}

static void integer(void *object, const char *method, long value) {
    ((void (*)(void *, void *, long))message)(object, selector(method), value);
}

static void *label(rectangle frame, const char *text, double size) {
    void *object = view("UILabel", frame);
    if (!object) return 0;
    void *font = ((void *(*)(void *, void *, double))message)(get_class("UIFont"), selector("systemFontOfSize:"), size);
    if (!font) { invoke(object, "release"); return 0; }
    set(object, "setFont:", font);
    set(object, "setTextColor:", color(0.06, 0.08, 0.12));
    set(object, "setText:", string(text));
    return object;
}

static void pressed(void *action) {
    (void)action;
    ++actions;
    set(status_label, "setText:", string("Native UIKit\nAction received"));
    ((void (*)(void *, void *, unsigned char, unsigned char))message)(toggle, selector("setOn:animated:"), 1, 0);
}

int tomacco_render_uikit(void *context, int port, int (*emit)(const char *)) {
    dprintf(port, "TOMACCO_GRAPHICS stage=load-uikit\n");
    void *ui = dlopen("/System/Library/Frameworks/UIKit.framework/UIKit", 2);
    void *objc = dlopen("/usr/lib/libobjc.A.dylib", 2);
    void *cg = dlopen("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", 2);
    if (!ui || !objc || !cg) { dprintf(port, "TOMACCO_GRAPHICS uikit_library_error=%s\n", dlerror()); return 20; }
    get_class = (void *(*)(const char *))dlsym(objc, "objc_getClass");
    selector = (void *(*)(const char *))dlsym(objc, "sel_registerName");
    message = (void (*)(void))dlsym(objc, "objc_msgSend");
    void (*save)(void *) = (void (*)(void *))dlsym(cg, "CGContextSaveGState");
    void (*restore)(void *) = (void (*)(void *))dlsym(cg, "CGContextRestoreGState");
    void (*translate)(void *, double, double) = (void (*)(void *, double, double))dlsym(cg, "CGContextTranslateCTM");
    void (*scale)(void *, double, double) = (void (*)(void *, double, double))dlsym(cg, "CGContextScaleCTM");
    void (*flush)(void *) = (void (*)(void *))dlsym(cg, "CGContextFlush");
    if (!get_class || !selector || !message ||
            !save || !restore || !translate || !scale || !flush) return 21;
    const char *classes[] = {"UIView", "UILabel", "UIButton", "UISwitch", "UIColor", "UIFont", "NSString", "UIAction", "NSAutoreleasePool"};
    for (unsigned i = 0; i < sizeof(classes) / sizeof(classes[0]); ++i) {
        if (!get_class(classes[i])) { dprintf(port, "TOMACCO_GRAPHICS missing_class=%s\n", classes[i]); return 22; }
    }
    void *pool = get(get_class("NSAutoreleasePool"), "new");
    dprintf(port, "TOMACCO_GRAPHICS stage=create-uiview\n");
    void *root = view("UIView", (rectangle){0, 0, 256, 384});
    if (!root) return 23;
    set(root, "setBackgroundColor:", color(1, 1, 1));
    dprintf(port, "TOMACCO_GRAPHICS stage=create-labels\n");
    void *heading = label((rectangle){16, 24, 224, 32}, "Tomacco", 22);
    status_label = label((rectangle){16, 72, 224, 60}, "Native UIKit\nReady", 16);
    void *caption = label((rectangle){84, 246, 152, 32}, "Native control", 14);
    if (!heading || !status_label || !caption) return 24;
    integer(status_label, "setNumberOfLines:", 2);
    set(root, "addSubview:", heading);
    set(root, "addSubview:", status_label);
    set(root, "addSubview:", caption);
    dprintf(port, "TOMACCO_GRAPHICS stage=create-controls\n");
    void *button = ((void *(*)(void *, void *, long))message)(get_class("UIButton"), selector("buttonWithType:"), 1);
    toggle = view("UISwitch", (rectangle){16, 246, 51, 31});
    if (!button || !toggle) return 25;
    ((void (*)(void *, void *, rectangle))message)(button, selector("setFrame:"), (rectangle){16, 160, 224, 48});
    ((void (*)(void *, void *, void *, unsigned long))message)(button, selector("setTitle:forState:"), string("Run native"), 0);
    ((void (*)(void *, void *, void *, unsigned long))message)(button, selector("setTitleColor:forState:"), color(1, 1, 1), 0);
    set(button, "setBackgroundColor:", color(0, 0.35, 0.9));
    ((void (*)(void *, void *, double))message)(get(button, "layer"), selector("setCornerRadius:"), 12);
    set(root, "addSubview:", button);
    set(root, "addSubview:", toggle);
    /* The public UIAction handler works without a UIApplication target/action
     * dispatcher. UIKit invokes this compiler-generated block; the probe never
     * calls the callback directly or substitutes UIApplication/event services.
     */
    void (^handler)(void *) = ^(void *action) { pressed(action); };
    void *action = ((void *(*)(void *, void *, void *, void *, void *, void (^)(void *)))message)(
        get_class("UIAction"), selector("actionWithTitle:image:identifier:handler:"), string("Run native"), 0, 0, handler);
    if (!action) return 26;
    ((void (*)(void *, void *, void *, unsigned long))message)(button, selector("addAction:forControlEvents:"), action, 0x40);
    for (unsigned phase = 0; phase < 2; ++phase) {
        if (phase) {
            dprintf(port, "TOMACCO_GRAPHICS stage=send-control-action\n");
            ((void (*)(void *, void *, unsigned long))message)(button, selector("sendActionsForControlEvents:"), 0x40);
        }
        unsigned on = ((unsigned char (*)(void *, void *))message)(toggle, selector("isOn")) != 0;
        dprintf(port, "TOMACCO_GRAPHICS_STATE phase=%s action_count=%u switch_on=%u\n", phase ? "after" : "before", actions, on);
        if (actions != phase || on != phase) return 27;
        dprintf(port, "TOMACCO_GRAPHICS stage=render-uikit-%s\n", phase ? "after" : "before");
        invoke(root, "layoutIfNeeded");
        invoke(get(root, "layer"), "displayIfNeeded");
        save(context);
        translate(context, 0, 384);
        scale(context, 1, -1);
        set(get(root, "layer"), "renderInContext:", context);
        restore(context);
        flush(context);
        if (emit(phase ? "uikit-after" : "uikit-before")) return 28;
    }
    invoke(heading, "release");
    invoke(status_label, "release");
    invoke(caption, "release");
    invoke(toggle, "release");
    invoke(root, "release");
    invoke(pool, "drain");
    return 0;
}
