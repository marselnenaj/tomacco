/* Actual metadata guard with scripted libobjc records. No iOS execution. */
#include <stdio.h>
#include <string.h>

typedef void *object;
struct method_shape {
    unsigned argc;
    const char *result;
    const char *arguments[4];
};
static struct method_shape shape;
static int missing_method;
static unsigned char receiver, receiver_class;
static object (*object_class)(object);
static object (*selector)(const char *);
static object (*instance_method)(object, object);
static unsigned (*argument_count)(object);
static void (*return_type)(object, char *, unsigned long);
static void (*argument_type)(object, unsigned, char *, unsigned long);

static object fake_class(object value) { return value == &receiver ? &receiver_class : 0; }
static object fake_selector(const char *name) { return (object)name; }
static object fake_method(object cls, object sel) {
    return !missing_method && cls == &receiver_class && sel ? &shape : 0;
}
static unsigned fake_count(object method) { return ((struct method_shape *)method)->argc; }
static void fake_return(object method, char *out, unsigned long size) {
    snprintf(out, size, "%s", ((struct method_shape *)method)->result);
}
static void fake_argument(object method, unsigned index, char *out, unsigned long size) {
    snprintf(out, size, "%s", ((struct method_shape *)method)->arguments[index]);
}

#include "../../integrations/ios-emulator/guest-objc-contract.h"

#define REQUIRE(condition) do { if (!(condition)) return __LINE__; } while (0)

int main(void) {
    object_class=fake_class; selector=fake_selector; instance_method=fake_method;
    argument_count=fake_count; return_type=fake_return; argument_type=fake_argument;
    shape=(struct method_shape){3, "B", {"@", ":", "#", "@"}};
    REQUIRE(accepts_class(&receiver, "isKindOfClass:", 'B'));
    REQUIRE(!accepts(&receiver, "isKindOfClass:", 'B', 1));
    REQUIRE(!accepts_class(0, "isKindOfClass:", 'B'));
    missing_method=1;
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    missing_method=0;
    shape.argc=2;
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    shape.argc=4;
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    shape.argc=3;
    shape.result="c";
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    shape.result="Bqualified";
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    shape.result="B";
    shape.arguments[0]="#";
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    shape.arguments[0]="@";
    shape.arguments[1]="@";
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    shape.arguments[1]=":";
    shape.arguments[2]="@";
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    REQUIRE(accepts(&receiver, "objectArgument:", 'B', 1));
    shape.arguments[2]="r#";
    REQUIRE(!accepts_class(&receiver, "isKindOfClass:", 'B'));
    shape=(struct method_shape){2, "i", {"@", ":", "", ""}};
    REQUIRE(accepts(&receiver, "pid", 'i', 0));
    shape.result="q";
    REQUIRE(!accepts(&receiver, "pid", 'i', 0));
    return 0;
}
