/* A full battery on external power for the virtual phone.
 *
 * The virtual phone has no battery hardware. Its kernel publishes no
 * IOPMPowerSource, so powerd lists no internal battery, BatteryCenter reports
 * none, and the status bar (SpringBoard's SBSystemStatusBatteryDataProvider,
 * which shows BatteryCenter's internal device) draws an empty grey battery
 * with a red sliver: 0 %.
 *
 * powerd also takes power sources from user space, through IOKit's
 * IOPSCreatePowerSource and IOPSSetPowerSourceDetails: bluetoothd and
 * BTLEServer publish accessories that way. powerd checks no entitlement for
 * either request, and it drops the source once the process that published it
 * exits (a DISPATCH_SOURCE_TYPE_PROC exit source on the caller), so this
 * helper stays for the whole boot.
 *
 * What the status bar reads (iOS 27.0.1, 24A446). SpringBoard's
 * -[SBSystemStatusBatteryDataProvider _updateData] publishes battery data
 * only for -[BCBatteryDeviceController sb_deviceInternalBattery], the first
 * connected device whose isInternal is set; with none it publishes nothing
 * and the status bar keeps its empty default. BatteryCenter's
 * -[_BCPowerSourceController connectedDevices] asks powerd with
 * IOPSCopyPowerSourcesByTypePrecise(all) (the XPC "powerInfoPrecise" request,
 * which needs com.apple.private.iokit.batterydataprecise; SpringBoard has
 * it), at SpringBoard's start and on every com.apple.system.powersources.*
 * notification, and sets isInternal when a description's "Type" is
 * "InternalBattery".
 *
 * powerd keeps user-space sources in sixteen slots and gives each a kind
 * from the "Type" of its FIRST description only: "InternalBattery" 1, "UPS"
 * or "Battery Case" 2, "Accessory Source" 3; later descriptions never change
 * it. For a precise request it merges, into every kind-1 description, the
 * precise data of the kernel battery it found at start, through a pointer
 * that is set only when the kernel has a battery, without checking it. On
 * this phone the pointer is NULL, so the first precise request that meets a
 * kind-1 source faults in powerd. SpringBoard's query then fails (no devices,
 * empty battery), launchd restarts powerd, and the new powerd knows no
 * user-space source: the next IOPSSetPowerSourceDetails returns
 * kIOReturnNotFound. A plain IOPSCopyPowerSourcesInfo, as this helper logs
 * it, never takes that branch, so the source looked fine from here.
 *
 * So the source's first description has the "Type" "UPS" (kind 2: powerd
 * stores and announces it exactly as a kind-1 source, and returns it to a
 * precise request unchanged), and the very next one is the internal battery.
 * Every reader of the description then sees an "InternalBattery": BatteryCenter,
 * and through it the status bar and SpringBoard's charging state, and (by the
 * keys it reads) UIKit's UIDevice battery. Only a request for internal
 * batteries alone (kIOPSSourceInternal) does not list it.
 *
 * The battery it describes is charged (100 of 100) on AC power and never
 * running down: there is nothing for the low-battery alert or the Low Power
 * Mode offer to react to. Nothing is written to the guest's disk, and no
 * kernel or IORegistry state is touched.
 *
 * powerd announces nothing for a source published this way, so the helper
 * posts the two notifications BatteryCenter and UIKit read power sources
 * again on. Both are in com.apple.system., which notifyd lets only root post
 * (/etc/notify.conf: "reserve com.apple.system. 0 0 rwr-r-").
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int dprintf(int, const char *, ...);
extern int snprintf(char *, unsigned long, const char *, ...);
extern unsigned getuid(void), sleep(unsigned);
typedef void *object;

/* "Type" first: publish() swaps its value for the classifying one. */
static const char *const texts[][2] = {
    {"Type", "InternalBattery"},
    {"Transport Type", "Internal"},
    {"Name", "InternalBattery-0"},
    {"Power Source State", "AC Power"},
};
static const char *const numbers[] = {"Current Capacity", "Max Capacity"};
enum { CAPACITY = 100 };
static const struct { const char *key; unsigned char value; } flags[] = {
    {"Is Present", 1},
    {"Is Charging", 0},
    {"Is Charged", 1},
    /* A plugged-in phone at 100 % shows the charging bolt; BatteryCenter
     * reads it from here when the battery is not charging any more. */
    {"Show Charging UI", 1},
    {"Raw External Connected", 1},
    {"Battery Provides Time Remaining", 1},
};
static const char *const announcements[] = {
    "com.apple.system.powersources.timeremaining",
    "com.apple.system.powersources.percent",
};
enum { TEXTS = sizeof(texts) / sizeof(texts[0]), NUMBERS = sizeof(numbers) / sizeof(numbers[0]),
       FLAGS = sizeof(flags) / sizeof(flags[0]), ANNOUNCEMENTS = sizeof(announcements) / sizeof(announcements[0]) };

/* Guest seconds. powerd is normally running long before this job starts; a
 * slow boot gets a retry every ten seconds. The description is set and
 * announced again after 30 s, doubling to every ten minutes, so a SpringBoard
 * or powerd that started after the first announcement still sees it. */
enum { RETRY = 10, REFRESH_FIRST = 30, REFRESH_LAST = 600, LOGGED_FAILURES = 5 };
enum { SINT32 = 3, UTF8 = 0x08000100 };

/* The first description's "Type": it makes powerd file the source as kind 2
 * (see the top of this file). */
static const char classifying_type[] = "UPS";

static object (*dictionary_create)(object, long, const void *, const void *);
static void (*dictionary_set)(object, const void *, const void *);
static object (*string_create)(object, const char *, unsigned);
static object (*number_create)(object, long, const void *);
static void (*release)(object);
static int (*create_source)(object *);
static int (*set_details)(object, object);
static int (*release_source)(object);
static unsigned (*post)(const char *);
static object (*copy_info)(void);
static object (*copy_list)(object);
static object (*description_of)(object, object);
static long (*array_count)(object);
static object (*array_at)(object, long);
static object (*dictionary_get)(object, const void *);
static unsigned char (*string_get)(object, char *, long, unsigned);
static unsigned char (*number_get)(object, long, void *);
static object type_key, internal_type, classifying_value;

/* Never returns: launchd keeps this job alive, so a helper that cannot do
 * its work waits instead of exiting into a restart loop. */
static void idle(const char *phase) {
    dprintf(1, "{\"event\":\"battery\",\"state\":\"failed\",\"phase\":\"%s\"}\n", phase);
    for (;;) sleep(3600);
}

static object describe(void) {
    void *foundation = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", 2);
    const void *key_callbacks = dlsym(foundation, "kCFTypeDictionaryKeyCallBacks");
    const void *value_callbacks = dlsym(foundation, "kCFTypeDictionaryValueCallBacks");
    object *yes = dlsym(foundation, "kCFBooleanTrue"), *no = dlsym(foundation, "kCFBooleanFalse");
    dictionary_create = dlsym(foundation, "CFDictionaryCreateMutable");
    dictionary_set = dlsym(foundation, "CFDictionarySetValue");
    string_create = dlsym(foundation, "CFStringCreateWithCString");
    number_create = dlsym(foundation, "CFNumberCreate");
    release = dlsym(foundation, "CFRelease");
    array_count = dlsym(foundation, "CFArrayGetCount");
    array_at = dlsym(foundation, "CFArrayGetValueAtIndex");
    dictionary_get = dlsym(foundation, "CFDictionaryGetValue");
    string_get = dlsym(foundation, "CFStringGetCString");
    number_get = dlsym(foundation, "CFNumberGetValue");
    if (!key_callbacks || !value_callbacks || !yes || !no || !*yes || !*no || !dictionary_create ||
        !dictionary_set || !string_create || !number_create || !release)
        return 0;
    type_key = string_create(0, texts[0][0], UTF8);
    internal_type = string_create(0, texts[0][1], UTF8);
    classifying_value = string_create(0, classifying_type, UTF8);
    if (!type_key || !internal_type || !classifying_value) return 0;
    object details = dictionary_create(0, 0, key_callbacks, value_callbacks);
    if (!details) return 0;
    int capacity = CAPACITY;
    for (unsigned index = 0; index < TEXTS + NUMBERS + FLAGS; ++index) {
        const char *name = index < TEXTS ? texts[index][0] : index < TEXTS + NUMBERS ? numbers[index - TEXTS]
                                                                                       : flags[index - TEXTS - NUMBERS].key;
        object key = string_create(0, name, UTF8), value;
        if (index < TEXTS) value = string_create(0, texts[index][1], UTF8);
        else if (index < TEXTS + NUMBERS) value = number_create(0, SINT32, &capacity);
        else value = flags[index - TEXTS - NUMBERS].value ? *yes : *no;
        if (!key || !value) return 0;
        dictionary_set(details, key, value);
        release(key);
        if (index < TEXTS + NUMBERS) release(value);
    }
    return details;
}

/* What powerd lists after a publish, the way BatteryCenter asks for it
 * (IOPSCopyPowerSourcesInfo): one line with the number of sources and each
 * one's type, state and current capacity. */
static void report_listing(void) {
    if (!copy_info || !copy_list || !description_of || !array_count || !array_at || !dictionary_get ||
        !string_get || !number_get) {
        dprintf(1, "{\"event\":\"battery\",\"state\":\"listing-unavailable\"}\n");
        return;
    }
    object info = copy_info();
    object list = info ? copy_list(info) : 0;
    long count = list ? array_count(list) : -1;
    /* One write, so the record stays one line of the log. */
    char line[1024];
    int used = snprintf(line, sizeof(line), "{\"event\":\"battery\",\"state\":\"listed\",\"sources\":%ld", count);
    for (long index = 0; index < count && index < 8 && used > 0 && used < (int)sizeof(line); ++index) {
        object description = description_of(info, array_at(list, index));
        char type[64] = "?", state[64] = "?";
        int capacity = -1;
        if (description) {
            object key = string_create(0, "Type", UTF8);
            object value = key ? dictionary_get(description, key) : 0;
            if (value) string_get(value, type, sizeof(type), UTF8);
            if (key) release(key);
            key = string_create(0, "Power Source State", UTF8);
            value = key ? dictionary_get(description, key) : 0;
            if (value) string_get(value, state, sizeof(state), UTF8);
            if (key) release(key);
            key = string_create(0, "Current Capacity", UTF8);
            value = key ? dictionary_get(description, key) : 0;
            if (value) number_get(value, SINT32, &capacity);
            if (key) release(key);
        }
        used += snprintf(line + used, sizeof(line) - (unsigned)used,
                         ",\"source_%ld\":{\"type\":\"%s\",\"state\":\"%s\",\"capacity\":%d}",
                         index, type, state, capacity);
    }
    if (used > 0 && used < (int)sizeof(line)) dprintf(1, "%s}\n", line);
    if (list) release(list);
    if (info) release(info);
}

/* Posts the announcements; returns how many notifyd took. */
static unsigned announce(void) {
    unsigned taken = 0;
    for (unsigned index = 0; index < ANNOUNCEMENTS; ++index)
        if (!post(announcements[index])) ++taken;
    return taken;
}

/* Creates the power source, files it as kind 2 with a first description
 * of the classifying type, gives it the internal battery's description and
 * announces it, retrying until powerd answers; returns the source. Nothing
 * is announced between the two descriptions. */
static object publish(object details) {
    for (unsigned attempt = 1;; ++attempt) {
        object source = 0;
        int result = create_source(&source);
        if (!result && source) {
            dictionary_set(details, type_key, classifying_value);
            result = set_details(source, details);
            dictionary_set(details, type_key, internal_type);
            if (!result) result = set_details(source, details);
            if (!result) {
                dprintf(1, "{\"event\":\"battery\",\"state\":\"published\",\"type\":\"InternalBattery\","
                           "\"filed_as\":\"%s\",\"percent\":%d,\"power\":\"AC Power\",\"charged\":true,"
                           "\"attempts\":%u,\"announced\":%u}\n",
                        classifying_type, CAPACITY, attempt, announce());
                return source;
            }
            if (release_source) release_source(source);
        }
        if (attempt <= LOGGED_FAILURES)
            dprintf(1, "{\"event\":\"battery\",\"state\":\"retrying\",\"attempt\":%u,\"error\":%d}\n", attempt, result);
        sleep(RETRY);
    }
}

int main(void) {
    if (getuid()) idle("root-required");
    void *system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    void *iokit = dlopen("/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit", 2);
    post = dlsym(system, "notify_post");
    create_source = dlsym(iokit, "IOPSCreatePowerSource");
    set_details = dlsym(iokit, "IOPSSetPowerSourceDetails");
    release_source = dlsym(iokit, "IOPSReleasePowerSource");
    copy_info = dlsym(iokit, "IOPSCopyPowerSourcesInfo");
    copy_list = dlsym(iokit, "IOPSCopyPowerSourcesList");
    description_of = dlsym(iokit, "IOPSGetPowerSourceDescription");
    if (!post || !create_source || !set_details) idle("power-source-api-unavailable");
    object details = describe();
    if (!details) idle("description");
    object source = publish(details);
    sleep(2);
    report_listing();
    for (unsigned interval = REFRESH_FIRST;; interval = interval * 2 < REFRESH_LAST ? interval * 2 : REFRESH_LAST) {
        sleep(interval);
        int result = set_details(source, details);
        if (!result) {
            announce();
            if (interval == REFRESH_FIRST) report_listing();
            continue;
        }
        /* powerd no longer knows this source (it restarted, and kept no
         * user-space source): publish it anew, filed as kind 2 again. */
        dprintf(1, "{\"event\":\"battery\",\"state\":\"lost\",\"error\":%d}\n", result);
        if (release_source) release_source(source);
        source = publish(details);
    }
}
