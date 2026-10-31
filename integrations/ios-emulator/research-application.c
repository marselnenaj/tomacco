/* Bounded, real UIApplicationMain/scene lifecycle research on the guest's main
 * thread. No replacement application, display, compositor or event dispatcher.
 * Public UIKit APIs are resolved from the user's matching iOS frameworks.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int dprintf(int, const char *, ...);
extern int open(const char *, int, ...);
extern int close(int);
extern int strcmp(const char *, const char *);
extern const char *getenv(const char *);
extern int snprintf(char *, unsigned long, const char *, ...);
extern int mkdir(const char *, unsigned short);
extern long write(int, const void *, unsigned long);
extern int *__error(void);
extern unsigned getuid(void);
extern unsigned getgid(void);
extern unsigned sleep(unsigned);
extern unsigned alarm(unsigned);
extern void (*signal(int, void (*)(int)))(int);
extern int sigprocmask(int, const unsigned *, unsigned *);
extern int pthread_set_qos_class_self_np(unsigned, int);
extern int tomacco_application_trace(int, unsigned *);
/* The bundle identity this build runs as. The guest-tools build compiles the
 * owned diagnostic with the default; a stand-in for a developer's own app is
 * the same program under another identifier (tools/build_ios_sample_app.py). */
#ifndef TOMACCO_APPLICATION_IDENTIFIER
#define TOMACCO_APPLICATION_IDENTIFIER "io.github.marselnenaj.tomacco.research-app"
#endif

struct termios {
    unsigned long input, output, control, local;
    unsigned char characters[20];
    unsigned long input_speed, output_speed;
};
_Static_assert(sizeof(struct termios) == 72, "Darwin termios ABI");
extern int tcgetattr(int, struct termios *);
extern void cfmakeraw(struct termios *);
extern int tcsetattr(int, int, const struct termios *);

typedef struct { double x, y, width, height; } rectangle;
static void *(*get_class)(const char *);
static void *(*selector)(const char *);
static void (*message)(void);
static int port = -1;
static unsigned sequence, actions;
static int request_initial_scene;
static void *window, *label;

static unsigned next_sequence(void) {
    return __atomic_add_fetch(&sequence, 1, __ATOMIC_RELAXED);
}

static void event(const char *name) {
    dprintf(port, "{\"sequence\":%u,\"event\":\"%s\"}\n", next_sequence(), name);
}

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

static void scene_failure(void *error) {
    long code = ((long (*)(void *, void *))message)(error, selector("code"));
    unsigned current = next_sequence();
    void *row = get(get_class("NSMutableDictionary"), "dictionary");
    const char *keys[] = {"sequence", "event", "code", "domain", "message"};
    void *values[] = {
        ((void *(*)(void *, void *, unsigned))message)(get_class("NSNumber"), selector("numberWithUnsignedInt:"), current),
        string("scene-request-failed"),
        ((void *(*)(void *, void *, long))message)(get_class("NSNumber"), selector("numberWithLong:"), code),
        get(error, "domain"), get(error, "localizedDescription")
    };
    for (unsigned index = 0; index < 5; ++index) {
        void *value = values[index];
        if (index >= 3) {
            if (!value) value = string("");
            unsigned long length = ((unsigned long (*)(void *, void *))message)(value, selector("length"));
            if (length > 256) value = ((void *(*)(void *, void *, unsigned long))message)(
                value, selector("substringToIndex:"), 256);
        }
        ((void (*)(void *, void *, void *, void *))message)(row, selector("setObject:forKey:"), value, string(keys[index]));
    }
    void *encoding_error = 0;
    void *data = ((void *(*)(void *, void *, void *, unsigned long, void **))message)(
        get_class("NSJSONSerialization"), selector("dataWithJSONObject:options:error:"), row, 0, &encoding_error);
    unsigned long length = ((unsigned long (*)(void *, void *))message)(data, selector("length"));
    const char *bytes = get(data, "bytes");
    if (encoding_error || !bytes || !length || length > 4095) {
        dprintf(port, "{\"sequence\":%u,\"event\":\"scene-request-failed\",\"code\":%ld}\n", current, code);
        return;
    }
    while (length) {
        long count = write(port, bytes, length);
        if (count < 0 && *__error() == 4) continue;
        if (count <= 0) return;
        bytes += count;
        length -= (unsigned long)count;
    }
    write(port, "\n", 1);
}

static unsigned char launched(void *self, void *command, void *app, void *options) {
    (void)self; (void)command; (void)options;
    event("did-finish-launching");
    /* A normal LaunchServices launch supplies the first scene. Only the
     * separate launchd diagnostic requests one itself. */
    if (!request_initial_scene) return 1;
    /* A development process started by launchd has no foreground scene request
     * from SpringBoard. Ask UIKit for one through its public lifecycle API. The
     * callback reports the actual OS refusal; it never manufactures a scene. */
    void (^failed)(void *) = ^(void *error) {
        scene_failure(error);
    };
    event("request-scene");
    ((void (*)(void *, void *, void *, void *, void *, void (^)(void *)))message)(
        app, selector("requestSceneSessionActivation:userActivity:options:errorHandler:"), 0, 0, 0, failed);
    return 1;
}

static void *get_window(void *self, void *command) {
    (void)self; (void)command;
    return window;
}

static void connect_scene(void *self, void *command, void *scene, void *session, void *options) {
    (void)self; (void)command; (void)session; (void)options;
    event("scene-connected");
    if (window) { event("duplicate-scene"); return; }
    window = ((void *(*)(void *, void *, void *))message)(get(get_class("UIWindow"), "alloc"), selector("initWithWindowScene:"), scene);
    void *controller = get(get_class("UIViewController"), "new");
    if (!window || !controller) { event("window-creation-failed"); return; }
    void *root = get(controller, "view");
    set(root, "setBackgroundColor:", color(1, 1, 1));
    label = ((void *(*)(void *, void *, rectangle))message)(get(get_class("UILabel"), "alloc"),
        selector("initWithFrame:"), (rectangle){24, 100, 300, 90});
    if (!label) { event("window-creation-failed"); return; }
    ((void (*)(void *, void *, long))message)(label, selector("setNumberOfLines:"), 2);
    set(label, "setTextColor:", color(0.06, 0.08, 0.12));
    set(label, "setText:", string("Tomacco\nNative application"));
    set(label, "setFont:", ((void *(*)(void *, void *, double))message)(get_class("UIFont"), selector("systemFontOfSize:"), 24));
    set(root, "addSubview:", label);
    void *button = ((void *(*)(void *, void *, long))message)(get_class("UIButton"), selector("buttonWithType:"), 1);
    ((void (*)(void *, void *, rectangle))message)(button, selector("setFrame:"), (rectangle){24, 220, 280, 48});
    ((void (*)(void *, void *, void *, unsigned long))message)(button, selector("setTitle:forState:"), string("Run native action"), 0);
    void (^handler)(void *) = ^(void *action) {
        (void)action;
        ++actions;
        set(label, "setText:", string("Tomacco\nAction received"));
        dprintf(port, "{\"sequence\":%u,\"event\":\"control-action\",\"count\":%u}\n", next_sequence(), actions);
    };
    void *action = ((void *(*)(void *, void *, void *, void *, void *, void (^)(void *)))message)(
        get_class("UIAction"), selector("actionWithTitle:image:identifier:handler:"), string("Run native action"), 0, 0, handler);
    if (!button || !action) { event("window-creation-failed"); return; }
    ((void (*)(void *, void *, void *, unsigned long))message)(button, selector("addAction:forControlEvents:"), action, 0x40);
    set(root, "addSubview:", button);
    set(window, "setRootViewController:", controller);
    invoke(window, "makeKeyAndVisible");
    void *screen = get(window, "screen");
    rectangle bounds = ((rectangle (*)(void *, void *))message)(window, selector("bounds"));
    double scale = ((double (*)(void *, void *))message)(screen, selector("scale"));
    unsigned hidden = ((unsigned char (*)(void *, void *))message)(window, selector("isHidden")) != 0;
    unsigned key = ((unsigned char (*)(void *, void *))message)(window, selector("isKeyWindow")) != 0;
    dprintf(port, "{\"sequence\":%u,\"event\":\"window-state\",\"width\":%.3f,\"height\":%.3f,\"scale\":%.3f,\"hidden\":%u,\"key\":%u}\n",
            next_sequence(), bounds.width, bounds.height, scale, hidden, key);
    invoke(controller, "release");
}

static void active_scene(void *self, void *command, void *scene) {
    (void)self; (void)command;
    long state = ((long (*)(void *, void *))message)(scene, selector("activationState"));
    dprintf(port, "{\"sequence\":%u,\"event\":\"scene-active\",\"activation_state\":%ld}\n", next_sequence(), state);
}

static int run(int argc, char **argv) {
    void *system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    unsigned *bootstrap = system ? dlsym(system, "bootstrap_port") : 0;
    unsigned *task = system ? dlsym(system, "mach_task_self_") : 0;
    int (*lookup)(unsigned, const char *, unsigned *) = system ? dlsym(system, "bootstrap_look_up") : 0;
    int (*deallocate)(unsigned, unsigned) = system ? dlsym(system, "mach_port_deallocate") : 0;
    if (!bootstrap || !task || !lookup || !deallocate) return 15;
    unsigned display = 0;
    int result = lookup(*bootstrap, "com.apple.backboard.display.services", &display);
    dprintf(port, "{\"sequence\":%u,\"event\":\"service-lookup\",\"bootstrap_port\":%u,\"result\":%d,\"display_port\":%u}\n",
            next_sequence(), *bootstrap, result, display);
    if (display && display != 0xffffffffu) deallocate(*task, display);
    event("load-uikit");
    void *ui = dlopen("/System/Library/Frameworks/UIKit.framework/UIKit", 2);
    void *objc = dlopen("/usr/lib/libobjc.A.dylib", 2);
    if (!ui || !objc) return 20;
    get_class = (void *(*)(const char *))dlsym(objc, "objc_getClass");
    selector = (void *(*)(const char *))dlsym(objc, "sel_registerName");
    message = (void (*)(void))dlsym(objc, "objc_msgSend");
    void *(*allocate)(void *, const char *, unsigned long) = (void *(*)(void *, const char *, unsigned long))dlsym(objc, "objc_allocateClassPair");
    unsigned char (*method)(void *, void *, void (*)(void), const char *) = (unsigned char (*)(void *, void *, void (*)(void), const char *))dlsym(objc, "class_addMethod");
    void (*register_class)(void *) = (void (*)(void *))dlsym(objc, "objc_registerClassPair");
    void *(*protocol)(const char *) = (void *(*)(const char *))dlsym(objc, "objc_getProtocol");
    unsigned char (*add_protocol)(void *, void *) = (unsigned char (*)(void *, void *))dlsym(objc, "class_addProtocol");
    int (*application_main)(int, char **, void *, void *) = (int (*)(int, char **, void *, void *))dlsym(ui, "UIApplicationMain");
    if (!get_class || !selector || !message || !allocate || !method || !register_class || !protocol || !add_protocol || !application_main) return 21;
    void *pool = get(get_class("NSAutoreleasePool"), "new");
    void *bundle = get(get_class("NSBundle"), "mainBundle");
    const char *identifier = (const char *)get(get(bundle, "bundleIdentifier"), "UTF8String");
    if (!identifier || strcmp(identifier, TOMACCO_APPLICATION_IDENTIFIER)) return 22;
    event("bundle-verified");
    void *app = allocate(get_class("NSObject"), "TomaccoResearchAppDelegate", 0);
    void *scene = allocate(get_class("NSObject"), "TomaccoResearchSceneDelegate", 0);
    void *app_protocol = protocol("UIApplicationDelegate"), *scene_protocol = protocol("UIWindowSceneDelegate");
    dprintf(port, "{\"sequence\":%u,\"event\":\"delegate-registration\",\"app_class\":%u,\"scene_class\":%u,\"app_protocol\":%u,\"scene_protocol\":%u}\n",
            next_sequence(), app != 0, scene != 0, app_protocol != 0, scene_protocol != 0);
    /* Protocol objects can be emitted by an SDK-compiled client rather than
     * the framework. UIKit invokes these optional delegate selectors through
     * ordinary Objective-C dispatch; never invent missing protocol metadata.
     */
    if (!app || !scene || (app_protocol && !add_protocol(app, app_protocol)) || (scene_protocol && !add_protocol(scene, scene_protocol)) ||
        !method(app, selector("application:didFinishLaunchingWithOptions:"), (void (*)(void))launched, "B@:@@") ||
        !method(scene, selector("scene:willConnectToSession:options:"), (void (*)(void))connect_scene, "v@:@@@") ||
        !method(scene, selector("sceneDidBecomeActive:"), (void (*)(void))active_scene, "v@:@") ||
        !method(scene, selector("window"), (void (*)(void))get_window, "@@:")) return 23;
    register_class(app);
    register_class(scene);
    if (tomacco_application_trace(port, &sequence)) return 24;
    event("enter-application-main");
    result = application_main(argc, argv, 0, string("TomaccoResearchAppDelegate"));
    dprintf(port, "{\"sequence\":%u,\"event\":\"application-main-returned\",\"code\":%d}\n", next_sequence(), result);
    invoke(pool, "drain");
    return result;
}

int main(int argc, char **argv) {
    int stdio = argc == 2 && !strcmp(argv[1], "--stdio");
    if (argc != 1 && !stdio) return 64;
    unsigned alarms = 1u << 13;
    if (signal(14, 0) == (void (*)(int))-1 || sigprocmask(2, &alarms, 0)) return 10;
    /* The bootstrap probe waits for display startup. A normal app launch must
     * enter UIKit immediately so FrontBoard's launch watchdog can observe it. */
#ifdef TOMACCO_APPLICATION_FILE_LOG
    unsigned remaining = stdio ? 20 : 0;
    request_initial_scene = stdio;
#else
    unsigned remaining = 20;
    request_initial_scene = 1;
#endif
    while (remaining) remaining = sleep(remaining);
    /* The bootstrap probe is bounded; an installed app runs as long as it is used. */
#ifdef TOMACCO_APPLICATION_FILE_LOG
    if (stdio) alarm(120);
#else
    alarm(120);
#endif
    if (pthread_set_qos_class_self_np(0x21, 0)) return 11;
    /* launchd diagnostics use stdout; LaunchServices starts the installed
     * bundle without our transport argument, so that build owns a fixed log.
     * The separate bootstrap build retains its dedicated UART contract. */
    if (stdio) port = 1;
    else {
#ifdef TOMACCO_APPLICATION_FILE_LOG
        /* LaunchServices supplies the app container as HOME. Never require
         * access to mobile's global Library before entering UIKit. A logging
         * failure must not turn a valid native launch into an early exit. */
        const char *home = getenv("HOME");
        char directory[4096], path[4096];
        int length = home && home[0] == '/' ?
            snprintf(directory, sizeof(directory), "%s/Library/Logs", home) : -1;
        if (length > 0 && (unsigned long)length < sizeof(directory) &&
                (!mkdir(directory, 0700) || *__error() == 17)) {
            length = snprintf(path, sizeof(path),
                "%s/" TOMACCO_APPLICATION_IDENTIFIER ".jsonl", directory);
            if (length > 0 && (unsigned long)length < sizeof(path)) {
                /* O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW | O_CLOEXEC. */
                port = open(path, 0x1000309, 0600);
            }
        }
        if (port < 0) { port = 2; event("diagnostic-log-unavailable"); }
#else
        port = open("/dev/cu.qemuport1", 0x20106);
        struct termios settings;
        if (port < 0 || tcgetattr(port, &settings)) return 12;
        cfmakeraw(&settings);
        if (tcsetattr(port, 0, &settings)) return 12;
#endif
    }
    event("begin");
    unsigned mobile = 501;
    dprintf(port, "{\"sequence\":%u,\"event\":\"identity\",\"uid\":%u,\"gid\":%u}\n", next_sequence(), getuid(), getgid());
    if (getuid() != mobile || getgid() != mobile) return 14;
    /* --stdio is our transport option, not a UIKit launch argument. */
    int result = run(1, argv);
    dprintf(port, "{\"sequence\":%u,\"event\":\"exit\",\"code\":%d}\n", next_sequence(), result);
    dprintf(2, "TOMACCO_APPLICATION_PROCESS result=%d\n", result);
    if (!stdio) close(port);
    return result;
}
