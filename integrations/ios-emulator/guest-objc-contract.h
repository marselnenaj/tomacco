/* Exact runtime method shapes. The includer supplies the libobjc metadata
 * functions and the object alias; this never invokes the inspected method. */
#ifndef TOMACCO_GUEST_OBJC_CONTRACT_H
#define TOMACCO_GUEST_OBJC_CONTRACT_H

static int accepts_types(object receiver, const char *name, char result,
                         const char *const *arguments, unsigned size) {
    if (!receiver) return 0;
    object method = instance_method(object_class(receiver), selector(name));
    if (!method || argument_count(method) != 2 + size) return 0;
    char type[128] = {0};
    return_type(method, type, sizeof(type));
    if (type[0] != result || type[1]) return 0;
    for (unsigned index = 0; index < 2 + size; ++index) {
        argument_type(method, index, type, sizeof(type));
        const char *expected = index == 0 ? "@" : index == 1 ? ":" : arguments[index - 2];
        if (strcmp(type, expected)) return 0;
    }
    return 1;
}

static int accepts(object receiver, const char *name, char result, int object_argument) {
    const char *arguments[] = {"@"};
    return accepts_types(receiver, name, result, arguments, (unsigned)object_argument);
}

/* Class and id are pointer-sized, but their runtime encodings are distinct.
 * NSObject's isKindOfClass: takes Class (#), not id (@). */
static int accepts_class(object receiver, const char *name, char result) {
    const char *arguments[] = {"#"};
    return accepts_types(receiver, name, result, arguments, 1);
}

#endif
