/* Pinned iOS 27.0.1 SpringBoardServices contract. The background server's
 * status-preserving MIG client writes at most 1024 bytes after a validated
 * successful reply. SpringBoard authenticates the caller and reports the
 * bundle identifier of its primary layout's application scene entity.
 * This is a separate read, not proof of visible pixels or process lifetime. */
#ifndef TOMACCO_GUEST_FOREGROUND_STATE_H
#define TOMACCO_GUEST_FOREGROUND_STATE_H

#define TOMACCO_FOREGROUND_CAPACITY 1024
enum tomacco_foreground_reply_kind {
    TOMACCO_FOREGROUND_RPC_ERROR,
    TOMACCO_FOREGROUND_UNTERMINATED,
    TOMACCO_FOREGROUND_EMPTY,
    TOMACCO_FOREGROUND_IDENTIFIER
};
struct tomacco_foreground_reply {
    enum tomacco_foreground_reply_kind kind;
    unsigned length;
};

static struct tomacco_foreground_reply tomacco_foreground_reply_decode(
        int native_status, const char *bytes) {
    if (native_status)
        return (struct tomacco_foreground_reply){TOMACCO_FOREGROUND_RPC_ERROR, 0};
    unsigned length = 0;
    while (length < TOMACCO_FOREGROUND_CAPACITY && bytes[length]) ++length;
    if (length == TOMACCO_FOREGROUND_CAPACITY)
        return (struct tomacco_foreground_reply){TOMACCO_FOREGROUND_UNTERMINATED, 0};
    return (struct tomacco_foreground_reply){
        length ? TOMACCO_FOREGROUND_IDENTIFIER : TOMACCO_FOREGROUND_EMPTY, length};
}

static int foreground_state(object row) {
    put(row, "foreground_identifier_known", boolean(0));
    put(row, "application_scene_verified", boolean(0));
    int contract = display_contract(row);
    if (contract) return contract;
    object framework = dlopen("/System/Library/PrivateFrameworks/SpringBoardServices.framework/SpringBoardServices", 2);
    if (!framework) return failure(row, "foreground_query_framework_unavailable");
    unsigned (*server_port)(void) = dlsym(framework, "SBSSpringBoardBackgroundServerPort");
    int (*query)(unsigned, char *) = dlsym(framework, "_SBSFrontmostApplicationDisplayIdentifier");
    if (!server_port || !query) return failure(row, "foreground_query_api_unavailable");
    /* This framework-owned cached send right is borrowed, not released. */
    unsigned port = server_port();
    if (!port || port == ~0u) return failure(row, "foreground_query_service_unavailable");
    char bytes[TOMACCO_FOREGROUND_CAPACITY];
    for (unsigned index = 0; index < sizeof(bytes); ++index) bytes[index] = (char)0xff;
    int status = query(port, bytes);
    put(row, "foreground_native_status", ((object (*)(object, object, int))message)(
        get_class("NSNumber"), selector("numberWithInt:"), status));
    struct tomacco_foreground_reply reply = tomacco_foreground_reply_decode(status, bytes);
    if (reply.kind == TOMACCO_FOREGROUND_RPC_ERROR)
        return failure(row, "foreground_query_rpc_failed");
    if (reply.kind == TOMACCO_FOREGROUND_UNTERMINATED)
        return failure(row, "foreground_query_invalid_reply");
    if (reply.kind == TOMACCO_FOREGROUND_IDENTIFIER) {
        object identifier = string(bytes); /* Foundation validates UTF-8. */
        if (!identifier) return failure(row, "foreground_query_invalid_utf8");
        put(row, "frontmost_bundle_id", identifier);
    }
    put(row, "frontmost_application_present", boolean(reply.kind == TOMACCO_FOREGROUND_IDENTIFIER));
    put(row, "foreground_identifier_known", boolean(1));
    put(row, "foreground_query_scope", string("SpringBoard-primary-layout-application"));
    put(row, "ok", boolean(1));
    return emit(row);
}

#endif
