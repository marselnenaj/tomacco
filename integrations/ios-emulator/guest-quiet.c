/* Unload the services that can only fail on this virtual phone.
 *
 * The emulated processor is many times slower than the hardware iOS expects.
 * A handful of original services never get past their own deadlines or
 * assertions here; launchd restarts each of them for the life of the boot,
 * and those doomed attempts take about half of the processor from the
 * interface a person is looking at:
 *
 *   - duetexpertd, intelligencetasksd, searchpartyd and transparencyd abort on
 *     a libxpc assertion about a peer's code requirement, each abort followed
 *     by a crash report.
 *   - watchdogd exits at once because the boot disables the watchdog timer and
 *     is started again every ten seconds.
 *   - axassetsd starts four speech-synthesis extensions that miss their
 *     check-in deadline and are killed, a dozen times each.
 *
 * chronod stays: its widget extensions often miss their deadline and are
 * relaunched, but the Weather and Calendar widgets are drawn,
 * and without chronod the Home screen has none.
 *
 * Nothing is changed on disk: a service is removed from launchd's running
 * state for this boot only (launchctl's "bootout"), through the same private
 * request the existing development setup uses. A service that is absent, or
 * that launchd refuses to unload, is reported and left alone.
 *
 * locationd is kept, and guarded for the life of the boot. Its queue cohorts
 * hand dispatch_sync work between threads through kernel workloop wake-ups,
 * and on this processor a waiter can resume without having been handed the
 * queue: it then runs beside the owner and both release the queue. locationd either crashes, which
 * launchd repairs, or every one of its threads and every client waits for
 * good: Maps never gets past its launch screen. The guard asks CoreLocation
 * every half minute whether location services are enabled, which locationd
 * itself answers; when no answer has come for 90 seconds it ends locationd,
 * and launchd starts it again for the next client. locationd is the only
 * process it may end, and it never ends it twice within five minutes.
 */
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int dprintf(int, const char *, ...);
extern int kill(int, int);
extern unsigned getuid(void), sleep(unsigned), alarm(unsigned);
typedef void *object;
typedef struct tomacco_thread *thread_handle;

enum { ROUTINE_SERVICE_EXISTS = 0x2c8, ROUTINE_UNLOAD = 0x321, DOMAIN_SYSTEM = 1, DOMAIN_USER = 2 };

static const char *const services[] = {
    "com.apple.duetexpertd",
    "com.apple.intelligencetasksd",
    "com.apple.icloud.searchpartyd",
    "com.apple.transparencyd",
    "com.apple.watchdogd",
    "com.apple.accessibility.axassetsd",
};
enum { SERVICES = sizeof(services) / sizeof(services[0]) };

/* Guest seconds. The first question waits for the boot to have started
 * locationd's clients; an answer normally takes well under a second. */
enum { GUARD_START = 120, GUARD_INTERVAL = 30, GUARD_DEADLINE = 90, GUARD_RECOVERY = 300, GUARD_SPACING = 300 };
static const char guarded_process[] = "locationd";

static object (*objc_class)(const char *);
static void *(*objc_selector)(const char *);
static void *objc_message;
static void *(*pool_push)(void);
static void (*pool_pop)(void *);
static int (*list_processes)(int *, int);
static int (*process_name)(int, char *, unsigned);
static int (*spawn)(thread_handle *, const void *, void *(*)(void *), void *);
static int (*detach)(thread_handle);
static unsigned answered;

static void *ask_location(void *question) {
    void *pool = pool_push();
    ((unsigned char (*)(object, void *))objc_message)(objc_class("CLLocationManager"),
                                                      objc_selector("locationServicesEnabled"));
    pool_pop(pool);
    /* A question answered late never hides a later one's answer. */
    unsigned number = (unsigned)(unsigned long)question, latest = __atomic_load_n(&answered, __ATOMIC_ACQUIRE);
    while (number > latest &&
           !__atomic_compare_exchange_n(&answered, &latest, number, 0, __ATOMIC_RELEASE, __ATOMIC_ACQUIRE)) {}
    return 0;
}

/* Waits up to `limit` seconds for the answer to `question`. */
static unsigned answer_within(unsigned question, unsigned limit) {
    unsigned waited = 0;
    while (__atomic_load_n(&answered, __ATOMIC_ACQUIRE) != question && waited < limit) {
        sleep(1);
        ++waited;
    }
    return waited;
}

/* The processes named locationd right now, at most GUARDED_PROCESSES of them;
 * returns how many were stored. */
enum { GUARDED_PROCESSES = 8 };
static unsigned guarded_pids(int *found) {
    static int pids[8192];
    int count = list_processes(pids, (int)sizeof(pids));
    unsigned stored = 0;
    for (int index = 0; index < count && index < (int)(sizeof(pids) / sizeof(pids[0])); ++index) {
        char name[64] = {0};
        if (pids[index] <= 1 || process_name(pids[index], name, sizeof(name)) <= 0) continue;
        unsigned same = 1;
        for (unsigned k = 0; k < sizeof(guarded_process); ++k)
            if (name[k] != guarded_process[k]) same = 0;
        if (!same) continue;
        if (stored < GUARDED_PROCESSES) found[stored++] = pids[index];
    }
    return stored;
}

/* Ends the locationd processes a question was asked of, if they still run;
 * returns how many were signalled. A question proves nothing about a
 * process that was not there when it was asked: launchd starts locationd
 * again after a crash, and a locationd 15 seconds old must not be ended for a
 * question its crashed predecessors left unanswered. */
static unsigned end_guarded_process(const int *asked, unsigned asked_count) {
    int current[GUARDED_PROCESSES];
    unsigned count = guarded_pids(current), ended = 0;
    for (unsigned index = 0; index < asked_count; ++index) {
        unsigned present = 0;
        for (unsigned k = 0; k < count; ++k)
            if (current[k] == asked[index]) present = 1;
        if (!present) continue;
        int result = kill(asked[index], 9);
        dprintf(1, "{\"event\":\"guard\",\"service\":\"%s\",\"state\":\"ended\",\"pid\":%d,\"signalled\":%s}\n",
                guarded_process, asked[index], result ? "false" : "true");
        if (!result) ++ended;
    }
    return ended;
}

static void *guard(void *unused) {
    (void)unused;
    sleep(GUARD_START);
    unsigned question = 0, since_end = GUARD_SPACING;
    for (;;) {
        thread_handle thread;
        int asked[GUARDED_PROCESSES];
        unsigned asked_count = guarded_pids(asked);
        ++question;
        if (spawn(&thread, 0, ask_location, (void *)(unsigned long)question)) {
            dprintf(1, "{\"event\":\"guard\",\"service\":\"%s\",\"state\":\"failed\",\"phase\":\"thread\"}\n", guarded_process);
            return 0;
        }
        detach(thread);
        unsigned waited = answer_within(question, GUARD_DEADLINE);
        since_end += waited;
        if (__atomic_load_n(&answered, __ATOMIC_ACQUIRE) == question) {
            sleep(GUARD_INTERVAL);
            since_end += GUARD_INTERVAL;
            continue;
        }
        dprintf(1, "{\"event\":\"guard\",\"service\":\"%s\",\"state\":\"unanswered\",\"question\":%u,\"seconds\":%u}\n",
                guarded_process, question, waited);
        if (since_end < GUARD_SPACING) {
            waited = answer_within(question, GUARD_SPACING - since_end);
            since_end += waited;
            if (__atomic_load_n(&answered, __ATOMIC_ACQUIRE) == question) continue;
        }
        unsigned ended = end_guarded_process(asked, asked_count);
        if (!ended) {
            /* The process asked is gone on its own: launchd's new locationd
             * gets the boot's allowance before it is asked. */
            dprintf(1, "{\"event\":\"guard\",\"service\":\"%s\",\"state\":\"replaced\",\"question\":%u,\"asked\":%u}\n",
                    guarded_process, question, asked_count);
            sleep(GUARD_START);
            since_end += GUARD_START;
            continue;
        }
        since_end = 0;
        /* The question still waiting is answered, or fails, once the hung
         * locationd is gone; the next one goes to the new instance. */
        waited = answer_within(question, GUARD_RECOVERY);
        since_end += waited;
        dprintf(1, "{\"event\":\"guard\",\"service\":\"%s\",\"state\":\"%s\",\"ended\":%u,\"seconds\":%u}\n",
                guarded_process, __atomic_load_n(&answered, __ATOMIC_ACQUIRE) == question ? "answered" : "still-unanswered",
                ended, waited);
    }
}

/* Starts the guard thread; returns whether it runs. */
static unsigned start_guard(void) {
    void *system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    void *runtime = dlopen("/usr/lib/libobjc.A.dylib", 2);
    dlopen("/System/Library/Frameworks/CoreLocation.framework/CoreLocation", 2);
    spawn = dlsym(system, "pthread_create");
    detach = dlsym(system, "pthread_detach");
    objc_class = dlsym(runtime, "objc_getClass");
    objc_selector = dlsym(runtime, "sel_registerName");
    objc_message = dlsym(runtime, "objc_msgSend");
    pool_push = dlsym(runtime, "objc_autoreleasePoolPush");
    pool_pop = dlsym(runtime, "objc_autoreleasePoolPop");
    list_processes = dlsym(system, "proc_listallpids");
    process_name = dlsym(system, "proc_name");
    thread_handle thread;
    if (!spawn || !detach || !objc_class || !objc_selector || !objc_message || !pool_push || !pool_pop ||
        !list_processes || !process_name || !objc_class("CLLocationManager")) {
        dprintf(1, "{\"event\":\"guard\",\"service\":\"%s\",\"state\":\"failed\",\"phase\":\"location-api-unavailable\"}\n",
                guarded_process);
        return 0;
    }
    if (spawn(&thread, 0, guard, 0)) {
        dprintf(1, "{\"event\":\"guard\",\"service\":\"%s\",\"state\":\"failed\",\"phase\":\"thread\"}\n", guarded_process);
        return 0;
    }
    detach(thread);
    dprintf(1, "{\"event\":\"guard\",\"service\":\"%s\",\"state\":\"watching\",\"start\":%u,\"interval\":%u,\"deadline\":%u}\n",
            guarded_process, GUARD_START, GUARD_INTERVAL, GUARD_DEADLINE);
    return 1;
}

int main(void) {
    if (getuid()) {
        dprintf(1, "{\"event\":\"quiet\",\"state\":\"failed\",\"phase\":\"root-required\"}\n");
        return 69;
    }
    unsigned guarding = start_guard();
    /* The user's services are loaded when the session starts, minutes into a
     * boot on this machine. Bounded: this is a courtesy, not a dependency. */
    alarm(1500);
    object library = dlopen("/usr/lib/system/libxpc.dylib", 2);
    object (*dictionary)(const char *const *, const object *, unsigned long) = dlsym(library, "xpc_dictionary_create");
    void (*u64)(object, const char *, unsigned long long) = dlsym(library, "xpc_dictionary_set_uint64");
    void (*string)(object, const char *, const char *) = dlsym(library, "xpc_dictionary_set_string");
    int (*routine)(unsigned long long, object, object *) = dlsym(library, "_xpc_service_routine");
    long long (*integer)(object, const char *) = dlsym(library, "xpc_dictionary_get_int64");
    object (*get)(object, const char *) = dlsym(library, "xpc_dictionary_get_value");
    object (*type)(object) = dlsym(library, "xpc_get_type");
    object dictionary_type = dlsym(library, "_xpc_type_dictionary");
    void (*release)(object) = dlsym(library, "xpc_release");
    unsigned char (*sessions_enabled)(void) = dlsym(library, "xpc_user_sessions_enabled");
    unsigned (*foreground_uid)(int *) = dlsym(library, "xpc_user_sessions_get_foreground_uid");
    if (!dictionary || !u64 || !string || !routine || !integer || !get || !type || !dictionary_type || !release) {
        dprintf(1, "{\"event\":\"quiet\",\"state\":\"failed\",\"phase\":\"service-api-unavailable\"}\n");
        return 69;
    }
    unsigned char done[SERVICES] = {0};
    unsigned remaining = SERVICES;
    /* 120 rounds of ten seconds of guest time. */
    for (unsigned round = 0; round < 120 && remaining; ++round) {
        unsigned uid = ~0u;
        if (sessions_enabled && foreground_uid && sessions_enabled()) {
            int error = 0;
            uid = foreground_uid(&error);
            if (error) uid = ~0u;
        }
        for (unsigned index = 0; index < SERVICES; ++index) {
            if (done[index]) continue;
            for (unsigned choice = 0; choice < 2 && !done[index]; ++choice) {
                unsigned long long domain = choice ? DOMAIN_SYSTEM : DOMAIN_USER;
                unsigned long long handle = choice ? 0 : uid;
                if (!choice && uid == ~0u) continue;
                object request = dictionary(0, 0, 0), reply = 0;
                if (!request) continue;
                u64(request, "type", domain);
                u64(request, "handle", handle);
                string(request, "name", services[index]);
                int result = routine(ROUTINE_SERVICE_EXISTS, request, &reply);
                long long status = result ? result
                    : reply && type(reply) == dictionary_type && get(reply, "error") ? integer(reply, "error") : 0;
                if (reply) release(reply);
                reply = 0;
                if (!status) {
                    result = routine(ROUTINE_UNLOAD, request, &reply);
                    status = result ? result
                        : reply && type(reply) == dictionary_type && get(reply, "error") ? integer(reply, "error") : 0;
                    if (reply) release(reply);
                    /* 36: the instance is being stopped right now, which is the request taking effect. */
                    dprintf(1, "{\"event\":\"quiet\",\"service\":\"%s\",\"domain\":%llu,\"handle\":%llu,"
                               "\"unloaded\":%s,\"error\":%lld,\"round\":%u}\n",
                            services[index], domain, handle, !status || status == 36 ? "true" : "false", status, round);
                    done[index] = 1;
                    --remaining;
                }
                release(request);
            }
        }
        if (remaining) sleep(10);
    }
    for (unsigned index = 0; index < SERVICES; ++index)
        if (!done[index])
            dprintf(1, "{\"event\":\"quiet\",\"service\":\"%s\",\"unloaded\":false,\"error\":\"absent\"}\n", services[index]);
    dprintf(1, "{\"event\":\"quiet\",\"state\":\"finished\",\"remaining\":%u}\n", remaining);
    if (!guarding) return 0;
    /* The bound covers the unloading only; the guard runs for the boot. */
    alarm(0);
    for (;;) sleep(3600);
}
