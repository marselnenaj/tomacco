/* Opt-in policy for the exact applications built or staged with this guest
 * payload: a compiled-in table of bundle identifiers and CodeDirectory hashes
 * (guest-development-protocol.h). No shared-cache text patch, invented MIS
 * result, certificate or profile. The native verifier validates resources and
 * supplies the actual CodeDirectory hash before its own ad-hoc signing switch
 * can be enabled, and only for an identity the table names with that hash.
 */
#include "guest-development-policy.h"
#include "guest-development-policy-table.h"
#include "guest-development-protocol.h"
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int strcmp(const char *, const char *);
extern int dprintf(int, const char *, ...);
extern int close(int);
typedef void *object;
/* Objective-C IMP is a function pointer, including at the replacement API.
 * Keep that type across the call so ARM64e preserves pointer authentication. */
typedef void (*implementation)(void);
static object (*klass)(const char *), (*sel)(const char *), (*object_class)(object);
static object (*method)(object, object);
static unsigned (*argument_count)(object);
static void (*return_type)(object, char *, unsigned long);
static void (*argument_type)(object, unsigned, char *, unsigned long);
static void (*send)(void);
static unsigned char (*original)(object, object);
static int (*validate)(object, object, object *);
/* Entries of the compiled-in table, once its encoding has been accepted. */
static unsigned policy_entries;
/* The running phone's own table (TOMACCO_HOT_POLICY), read when the compiled-in
 * one does not name an app. A table the decoder does not accept admits nothing. */
static unsigned char hot_table[TOMACCO_POLICY_TABLE_BYTES];
static unsigned read_hot_table(void) {
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    int (*open_file)(const char *, int, ...) = dlsym(system, "open");
    long (*read_file)(int, void *, unsigned long) = dlsym(system, "read");
    if (!open_file || !read_file) return 0;
    int file = open_file(TOMACCO_HOT_POLICY, 0x100); /* RDONLY | NOFOLLOW */
    if (file < 0) return 0;
    unsigned long used = 0;
    for (;;) {
        long count = read_file(file, hot_table + used, sizeof(hot_table) - used);
        if (count <= 0) break;
        used += (unsigned long)count;
    }
    close(file);
    return used ? tomacco_policy_count(hot_table, used) : 0;
}

static void acknowledge(object system, unsigned char state) {
    int (*socket_option)(int, int, int, void *, unsigned *) = dlsym(system, "getsockopt");
    long (*send_packet)(int, const void *, unsigned long, int) = dlsym(system, "send");
    int kind = 0;
    unsigned size = sizeof(kind);
    /* SO_TYPE / SOL_SOCKET, SOCK_DGRAM. Never write to a reused file FD.
     * MSG_DONTWAIT | MSG_NOSIGNAL keeps late activation harmless if the
     * bounded helper has already exited. Darwin constants: XNU socket.h. */
    if (!socket_option || !send_packet || socket_option(TOMACCO_DEVELOPMENT_ACK_FD,
        0xffff, 0x1008, &kind, &size) || size != sizeof(kind) || kind != 2) return;
    unsigned char packet[TOMACCO_DEVELOPMENT_ACK_BYTES];
    tomacco_development_ack_encode(packet, state, tomacco_policy_digest);
    long sent = send_packet(TOMACCO_DEVELOPMENT_ACK_FD, packet, sizeof(packet), 0x80080);
    close(TOMACCO_DEVELOPMENT_ACK_FD);
    if (sent != (long)sizeof(packet))
        dprintf(2, "{\"event\":\"development-signing\",\"state\":\"activation-receipt-undelivered\"}\n");
}

static object get(object value, const char *name) {
    return ((object (*)(object, object))send)(value, sel(name));
}
static object with(object value, const char *name, object argument) {
    return ((object (*)(object, object, object))send)(value, sel(name), argument);
}
static object text(const char *value) {
    return ((object (*)(object, object, const char *))send)(
        klass("NSString"), sel("stringWithUTF8String:"), value);
}
static int getter(object value, const char *name, char result) {
    if (!value) return 0;
    object m = method(object_class(value), sel(name));
    if (!m || argument_count(m) != 2) return 0;
    char type[64] = {0};
    return_type(m, type, sizeof(type));
    if (type[0] != result || type[1]) return 0;
    argument_type(m, 0, type, sizeof(type));
    if (strcmp(type, "@")) return 0;
    argument_type(m, 1, type, sizeof(type));
    return !strcmp(type, ":");
}
static int is_kind(object value, const char *name) {
    return value && ((unsigned char (*)(object, object, object))send)(
        value, sel("isKindOfClass:"), klass(name));
}
static void set(object dictionary, const char *key, object value) {
    ((void (*)(object, object, object, object))send)(dictionary,
        sel("setObject:forKey:"), value, text(key));
}
static unsigned char owned_adhoc(object verifier, object selector) {
    if (original(verifier, selector)) return 1;
    if (!getter(verifier, "bundle", '@')) return 0;
    object bundle = get(verifier, "bundle");
    if (!getter(bundle, "identifier", '@') || !getter(bundle, "bundleURL", '@')) return 0;
    object bundle_id = get(bundle, "identifier");
    if (!is_kind(bundle_id, "NSString")) return 0;
    const char *bundle_name = get(bundle_id, "UTF8String");
    /* Other applications keep the original policy: no table entry, no change. */
    const unsigned char *table = tomacco_policy_table;
    int entry = bundle_name ? tomacco_policy_find(table, policy_entries, bundle_name) : -1;
    if (entry < 0 && bundle_name) {
        unsigned hot = read_hot_table();
        table = hot_table;
        entry = hot ? tomacco_policy_find(table, hot, bundle_name) : -1;
    }
    if (entry < 0) return 0;
    const unsigned char *expected_cdhash = tomacco_policy_cdhash(table, (unsigned)entry);
    const char *expected_identifier = tomacco_policy_identifier(table, (unsigned)entry);
    object url = get(bundle, "bundleURL");
    if (!is_kind(url, "NSURL")) return 0;
    object path = get(url, "path");
    if (!is_kind(path, "NSString")) return 0;
    object options = get(klass("NSMutableDictionary"), "dictionary");
    object yes = ((object (*)(object, object, unsigned char))send)(
        klass("NSNumber"), sel("numberWithBool:"), 1);
    object no = ((object (*)(object, object, unsigned char))send)(
        klass("NSNumber"), sel("numberWithBool:"), 0);
    set(options, "AllowAdHocSigning", yes);
    set(options, "ValidateSignatureOnly", no);
    object info = 0;
    int result = validate(path, options, &info);
    int admitted = 0;
    if (!result && is_kind(info, "NSDictionary")) {
        object hash = with(info, "objectForKey:", text("CdHash"));
        object identifier = with(info, "objectForKey:", text("SigningID"));
        if (is_kind(hash, "NSData") && is_kind(identifier, "NSString") &&
            ((unsigned long (*)(object, object))send)(hash, sel("length")) == TOMACCO_POLICY_CDHASH_BYTES) {
            const unsigned char *bytes = get(hash, "bytes");
            const char *name = get(identifier, "UTF8String");
            unsigned different = 0;
            for (unsigned i = 0; i < TOMACCO_POLICY_CDHASH_BYTES; ++i) different |= bytes[i] ^ expected_cdhash[i];
            admitted = !different && name && !strcmp(name, expected_identifier);
        }
    }
    if (info) get(info, "release");
    dprintf(2, "{\"event\":\"development-signing\",\"state\":\"%s\",\"bundle_id\":\"%s\",\"native_validation\":%u,\"resources_verified\":%s}\n",
        admitted ? "admitted" : "declined", expected_identifier, (unsigned)result, admitted ? "true" : "false");
    return admitted;
}

__attribute__((constructor)) static void configure(void) {
    /* Liveness marker, emitted before any dlopen or the program-name gate, over
     * the fd the setup helper redirects into its log: it tells apart "dyld never
     * loaded or ran this inserted library" (no line at all) from "the constructor
     * ran but returned early" (this line, then no active/unavailable). dprintf is
     * the only dependency and is already bound. It fires in every process the
     * library is inserted into, so a line from a non-installd child is expected. */
    dprintf(2, "{\"event\":\"development-signing\",\"state\":\"loaded\"}\n");
    object system = dlopen("/usr/lib/libSystem.B.dylib", 2);
    const char *(*program)(void) = dlsym(system, "getprogname");
    if (!program || strcmp(program(), "installd")) return;
    object objc = dlopen("/usr/lib/libobjc.A.dylib", 2);
    object foundation = dlopen("/System/Library/Frameworks/Foundation.framework/Foundation", 2);
    object content = dlopen("/System/Library/PrivateFrameworks/InstalledContentLibrary.framework/InstalledContentLibrary", 2);
    object mis = dlopen("/usr/lib/libmis.dylib", 2);
    if (!objc || !foundation || !content || !mis) goto unavailable;
    klass = dlsym(objc, "objc_getClass"); sel = dlsym(objc, "sel_registerName");
    send = dlsym(objc, "objc_msgSend"); object_class = dlsym(objc, "object_getClass");
    method = dlsym(objc, "class_getInstanceMethod");
    argument_count = dlsym(objc, "method_getNumberOfArguments");
    return_type = dlsym(objc, "method_getReturnType"); argument_type = dlsym(objc, "method_getArgumentType");
    implementation (*replace)(object, implementation) = dlsym(objc, "method_setImplementation");
    validate = dlsym(mis, "MISValidateSignatureAndCopyInfo");
    if (!klass || !sel || !send || !object_class || !method || !argument_count ||
        !return_type || !argument_type || !replace || !validate) goto unavailable;
    /* A table the decoder does not accept whole admits nothing. */
    policy_entries = tomacco_policy_count(tomacco_policy_table, sizeof(tomacco_policy_table));
    if (!policy_entries || policy_entries != TOMACCO_POLICY_ENTRIES) goto unavailable;
    /* Check the instance method directly without constructing a verifier with
     * an invalid bundle. The recorded ABI on this guest is B16@0:8. */
    object m = method(klass("MICodeSigningVerifier"), sel("allowAdhocSigning"));
    if (!m || argument_count(m) != 2) goto unavailable;
    char type[64] = {0};
    return_type(m, type, sizeof(type)); if (strcmp(type, "B")) goto unavailable;
    argument_type(m, 0, type, sizeof(type)); if (strcmp(type, "@")) goto unavailable;
    argument_type(m, 1, type, sizeof(type)); if (strcmp(type, ":")) goto unavailable;
    original = (unsigned char (*)(object, object))replace(m, (implementation)&owned_adhoc);
    dprintf(2, "{\"event\":\"development-signing\",\"state\":\"active\",\"policy\":\"owned-cdhash\",\"entries\":%u,\"bundle_id\":\"%s\"}\n",
        policy_entries, tomacco_policy_identifier(tomacco_policy_table, 0));
    for (unsigned index = 1; index < policy_entries; ++index)
        dprintf(2, "{\"event\":\"development-signing\",\"state\":\"entry\",\"bundle_id\":\"%s\"}\n",
            tomacco_policy_identifier(tomacco_policy_table, index));
    acknowledge(system, 1);
    return;
unavailable:
    dprintf(2, "{\"event\":\"development-signing\",\"state\":\"unavailable\"}\n");
    acknowledge(system, 2);
}
