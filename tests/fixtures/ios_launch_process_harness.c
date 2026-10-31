/* Actual C completion owner and public record layout. Fake retain/release
 * functions check ownership only; this is not Objective-C or iOS execution. */
#include <stdint.h>
#include <string.h>
#include <unistd.h>

typedef void *object;
extern object get(object, const char *);
extern object string(const char *);
extern object number(unsigned long);
extern object boolean(unsigned char);
extern void put(object, const char *, object);
extern int accepts(object, const char *, char, int);
extern int accepts_class(object, const char *, char);
extern object (*get_class)(const char *);
extern object (*selector)(const char *);
extern object (*object_class)(object);
extern void (*message)(void);
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int *__error(void);

#include "../../integrations/ios-emulator/guest-launch-process.h"

struct token { unsigned retained, released; };
static object retain_token(object value) {
    ++((struct token *)value)->retained;
    return value;
}
static void release_token(object value) { ++((struct token *)value)->released; }

#define REQUIRE(condition) do { if (!(condition)) return __LINE__; } while (0)

int main(void) {
    struct token first = {0}, duplicate = {0}, error = {0};
    struct launch_completion value = {0};
    REQUIRE(launch_completion_publish(&value, &first, 0, retain_token) == 1);
    REQUIRE(value.state == LAUNCH_READY && value.accepted == 1);
    REQUIRE(value.process == &first && value.error == 0 && first.retained == 1);
    REQUIRE(launch_completion_publish(&value, &duplicate, &error, retain_token) == 0);
    REQUIRE(!duplicate.retained && !error.retained && value.process == &first);
    launch_completion_dispose(&value, release_token);
    REQUIRE(value.state == LAUNCH_DISPOSED && value.process == 0 && value.error == 0);
    REQUIRE(first.released == 1);
    launch_completion_dispose(&value, release_token);
    REQUIRE(first.released == 1);
    REQUIRE(launch_completion_publish(&value, &duplicate, &error, retain_token) == 0);
    REQUIRE(!duplicate.retained && !error.retained);

    value = (struct launch_completion){0};
    REQUIRE(launch_completion_publish(&value, &duplicate, &error, retain_token) == 1);
    REQUIRE(!value.accepted && duplicate.retained == 1 && error.retained == 1);
    launch_completion_dispose(&value, release_token);
    REQUIRE(duplicate.released == 1 && error.released == 1);

    value = (struct launch_completion){0};
    REQUIRE(launch_completion_publish(&value, 0, 0, retain_token) == 1);
    REQUIRE(!value.accepted && !value.process && !value.error);
    launch_completion_dispose(&value, release_token);
    REQUIRE(value.state == LAUNCH_DISPOSED);

    value = (struct launch_completion){0};
    launch_completion_dispose(&value, release_token);
    REQUIRE(value.state == LAUNCH_PENDING);

    struct graphics_process_info process = {0};
    REQUIRE(!process_start_fields_known(0));
    REQUIRE(!process_start_fields_known(&process));
    process.start_seconds = 1;
    REQUIRE(process_start_fields_known(&process));
    process.start_microseconds = 999999;
    REQUIRE(process_start_fields_known(&process));
    process.start_microseconds = 1000000;
    REQUIRE(!process_start_fields_known(&process));
    return 0;
}
