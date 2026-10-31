/* The actual bounded reply decoder; no SpringBoard or Objective-C execution. */
#include <string.h>
typedef void *object;
extern object string(const char *);
extern object boolean(unsigned char);
extern void put(object, const char *, object);
extern int display_contract(object);
extern int failure(object, const char *);
extern int emit(object);
extern object (*get_class)(const char *);
extern object (*selector)(const char *);
extern void (*message)(void);
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);

#include "../../integrations/ios-emulator/guest-foreground-state.h"

#define REQUIRE(condition) do { if (!(condition)) return __LINE__; } while (0)
int main(void) {
    REQUIRE(tomacco_foreground_reply_decode(5, 0).kind == TOMACCO_FOREGROUND_RPC_ERROR);
    REQUIRE(tomacco_foreground_reply_decode(-307, 0).kind == TOMACCO_FOREGROUND_RPC_ERROR);
    char bytes[TOMACCO_FOREGROUND_CAPACITY];
    memset(bytes, 0xff, sizeof(bytes));
    REQUIRE(tomacco_foreground_reply_decode(0, bytes).kind == TOMACCO_FOREGROUND_UNTERMINATED);
    REQUIRE(tomacco_foreground_reply_decode(4, bytes).kind == TOMACCO_FOREGROUND_RPC_ERROR);
    bytes[0] = 0;
    struct tomacco_foreground_reply reply = tomacco_foreground_reply_decode(0, bytes);
    REQUIRE(reply.kind == TOMACCO_FOREGROUND_EMPTY && reply.length == 0);
    memcpy(bytes, "com.apple.Preferences", sizeof("com.apple.Preferences"));
    reply = tomacco_foreground_reply_decode(0, bytes);
    REQUIRE(reply.kind == TOMACCO_FOREGROUND_IDENTIFIER && reply.length == 21);
    REQUIRE(tomacco_foreground_reply_decode(5, bytes).kind == TOMACCO_FOREGROUND_RPC_ERROR);
    memset(bytes, 'a', sizeof(bytes));
    bytes[sizeof(bytes)-1] = 0;
    reply = tomacco_foreground_reply_decode(0, bytes);
    REQUIRE(reply.kind == TOMACCO_FOREGROUND_IDENTIFIER && reply.length == 1023);
    bytes[5] = 0;
    REQUIRE(tomacco_foreground_reply_decode(0, bytes).length == 5);
    return 0;
}
