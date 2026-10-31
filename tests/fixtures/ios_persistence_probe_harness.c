#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void *object;
struct value {
    unsigned long numeric;
    char text[128];
    const char *keys[96];
    object values[96];
    unsigned fields;
};
static struct value pool[256];
static unsigned used;
static char key_store[512][48];
static unsigned keys_used;
static object number(unsigned long n) {
    assert(used < 256); struct value *v = &pool[used++]; v->numeric = n; return v;
}
static object boolean(int n) { return number((unsigned)!!n); }
static object string(const char *s) {
    struct value *v = number(0); assert(strlen(s) < sizeof(v->text));
    strcpy(v->text, s); return v;
}
static void put(object row, const char *key, object item) {
    struct value *v = row; unsigned i;
    for (i = 0; i < v->fields && strcmp(v->keys[i], key); ++i) {}
    assert(i < 96);
    /* The owner builds class keys on its stack; keep a copy, as NSString does. */
    if (i == v->fields) {
        assert(keys_used < 512 && strlen(key) < sizeof(key_store[0]));
        strcpy(key_store[keys_used], key); v->keys[i] = key_store[keys_used++]; ++v->fields;
    }
    v->values[i] = item;
}
static struct value *field(object row, const char *key) {
    struct value *v = row;
    for (unsigned i = 0; i < v->fields; ++i)
        if (!strcmp(v->keys[i], key)) return v->values[i];
    return NULL;
}
static int failure(object row, const char *reason) { put(row, "error", string(reason)); return 69; }
static int emit(object row) { assert(field(row, "ok")->numeric == 1); return 0; }

/* Slot 0 is the Class C challenge file; slots 1..4 are the survey's files
 * for classes A..D. One file is open at a time, as descriptor 12. */
#define SLOTS 5
static int saved_errno, virtual_guest = 1, api_available = 1;
static unsigned uid = 501, directories, file_opens, file_closes, directory_closes;
static unsigned sets, gets, writes, full_flushes, directory_syncs, parent_syncs, reads, stats, mkdirs, unlinks;
static unsigned sets_by_slot[SLOTS], gets_by_slot[SLOTS], writes_by_slot[SLOTS], flushes_by_slot[SLOTS];
static int exists[SLOTS], protections[SLOTS], fail_sets[SLOTS], fail_reads[SLOTS], open_fails[SLOTS];
static int file_opened, directory_exists, directory_opened, parent_opened;
static int fail_get, fail_full, fail_sync, fail_write, fail_close, unlink_fails;
static int metadata_fault, replace_on_reopen, change_after_read, interrupt_read;
static int directory_fault, default_class = 3;
static unsigned content_sizes[SLOTS], cursor, slot;
static char contents[SLOTS][65];
#define file_exists exists[slot]
#define fail_set fail_sets[slot]
#define fail_read fail_reads[slot]
#define content_size content_sizes[slot]
#define content contents[slot]
static const char *request_id = "0123456789abcdef0123456789abcdef";
static unsigned mock_getuid(void) { return uid; }
static int setup_virtual_device(void) { return virtual_guest; }
static int *mock_error(void) { return &saved_errno; }

#define open mock_open
#define openat mock_openat
#define close mock_close
#define fstat mock_fstat
#include "../../integrations/ios-emulator/guest-buffer-log.h"
#undef open
#undef openat
#undef close
#undef fstat

static unsigned slot_of(const char *name) {
    if (!strcmp(name, "io.github.marselnenaj.tomacco.persistence-v1")) return 0;
    const char *prefix = "io.github.marselnenaj.tomacco.persistence-class-";
    assert(strlen(name) == strlen(prefix) + 1 && !strncmp(name, prefix, strlen(prefix)));
    assert(name[strlen(prefix)] >= '1' && name[strlen(prefix)] <= '4');
    return (unsigned)(name[strlen(prefix)] - '0');
}
static int expected_class(void) { return slot ? (int)slot : 3; }

int mock_open(const char *path, int flags, ...) {
    assert(!strcmp(path, "/private/var/mobile/Library"));
    assert(flags == 0x21100004); assert(!parent_opened);
    ++directories; parent_opened = 1; return 10;
}
static int mock_mkdirat(int parent, const char *name, unsigned short mode) {
    assert(parent==10 && parent_opened && !strcmp(name,"io.github.marselnenaj.tomacco.persistence"));
    assert(mode==0700); ++mkdirs;
    if(directory_exists) { saved_errno=17; return -1; }
    directory_exists=1; return 0;
}
int mock_openat(int directory, const char *name, int flags, ...) {
    if (directory==10) {
        assert(parent_opened && !directory_opened && flags==0x1100104);
        assert(!strcmp(name,"io.github.marselnenaj.tomacco.persistence"));
        if (!directory_exists) { saved_errno=2; return -1; }
        directory_opened=1; return 11;
    }
    assert(directory == 11 && directory_opened && !file_opened);
    slot = slot_of(name);
    ++file_opens;
    if (flags == 0x1000b06) {
        va_list args; va_start(args, flags); assert(va_arg(args, int) == 0600); va_end(args);
        if (file_exists) { saved_errno = 17; return -1; }
        file_exists = 1; content_size = 0; protections[slot] = default_class;
    } else {
        assert(flags == 0x1000104);
        if (!file_exists) { saved_errno = 2; return -1; }
        if (open_fails[slot]) { saved_errno = open_fails[slot]; return -1; }
    }
    cursor = 0; file_opened = 1; return 12;
}
static int mock_unlinkat(int directory, const char *name, int flags) {
    assert(directory == 11 && directory_opened && !flags && !file_opened);
    unsigned removed = slot_of(name);
    assert(removed && exists[removed]); ++unlinks;
    if (unlink_fails) { saved_errno = 1; return -1; }
    exists[removed] = 0; return 0;
}
int mock_close(int file) {
    if (file == 12) { assert(file_opened); file_opened = 0; ++file_closes; }
    else if(file==11) { assert(directory_opened); directory_opened = 0; ++directory_closes; }
    else { assert(file==10 && parent_opened); parent_opened=0; }
    if (fail_close) { saved_errno = 5; return -1; }
    return 0;
}
int mock_fstat(int file, struct tomacco_darwin_stat *info) {
    ++stats; memset(info, 0, sizeof(*info));
    info->uid = 501; info->device = 42; info->inode = 100 + 2 * slot; info->links = 1;
    if (file == 11) {
        assert(directory_opened); info->mode = 0040700;
        if(directory_fault==1)info->uid=0;
        if(directory_fault==2)info->mode=0040755;
        if(directory_fault==3)info->mode=0100700;
        return 0;
    }
    assert(file == 12 && file_opened);
    info->mode = 0100600; info->size = content_size;
    if (metadata_fault == 1) info->mode = 0010600;
    if (metadata_fault == 2) info->uid = 0;
    if (metadata_fault == 3) info->links = 2;
    if (metadata_fault == 4) info->mode = 0100644;
    if (metadata_fault == 5) info->size = content_size ? content_size - 1 : 1;
    if (replace_on_reopen && file_opens > 1) info->inode = 101 + 2 * slot;
    if (change_after_read && reads) info->times[2] = 99;
    return 0;
}
static int mock_fcntl(int file, int command, ...) {
    assert(file == 12 && file_opened);
    if (command == 64) {
        va_list args; va_start(args, command); assert(va_arg(args, int) == expected_class()); va_end(args);
        assert(!writes_by_slot[slot]); ++sets; ++sets_by_slot[slot];
        if (fail_set) { saved_errno = slot ? 1 : 13; return -1; }
        protections[slot] = expected_class(); return 0;
    }
    if (command == 63) {
        ++gets; ++gets_by_slot[slot]; if (fail_get) { saved_errno = 45; return -1; } return protections[slot];
    }
    assert(command == 51 && writes && directory_syncs && parent_syncs); ++full_flushes; ++flushes_by_slot[slot];
    if (fail_full) { saved_errno = 5; return -1; } return 0;
}
static object mock_dlsym(object handle, const char *name);
static int mock_write_bytes(int file, const char *bytes, unsigned long size) {
    assert(file == 12 && file_opened && protections[slot] == expected_class() && sets_by_slot[slot] && gets_by_slot[slot]);
    assert(size == 64 && !content_size); ++writes; ++writes_by_slot[slot];
    if (fail_write) { saved_errno = 28; return 74; }
    memcpy(content, bytes, size); content_size = (unsigned)size; return 0;
}
static int mock_fsync(int file) {
    assert(writes);
    if(file==11) { assert(directory_opened); ++directory_syncs; }
    else { assert(file==10 && parent_opened); ++parent_syncs; }
    if (fail_sync) { saved_errno = 5; return -1; } return 0;
}
static long mock_read(int file, void *bytes, unsigned long size) {
    assert(file == 12 && file_opened && gets); ++reads;
    if (interrupt_read) { interrupt_read = 0; saved_errno = 4; return -1; }
    if (fail_read) { saved_errno = slot ? 1 : 5; return -1; }
    unsigned n = content_size - cursor;
    if (n > 7) n = 7;
    if (n > size) n = (unsigned)size;
    memcpy(bytes, content + cursor, n); cursor += n; return n;
}
#define getuid mock_getuid
#define mkdirat mock_mkdirat
#define unlinkat mock_unlinkat
#define __error mock_error
#define open mock_open
#define openat mock_openat
#define close mock_close
#define fstat mock_fstat
#define dlsym mock_dlsym
#define write_bytes mock_write_bytes
#define fsync mock_fsync
#define read mock_read
#include "../../integrations/ios-emulator/guest-persistence-probe.h"

/* The inode lookup's Darwin calls, resolved by name like fcntl. */
static int lookup_api_available = 1, fail_volume_identity, fsgetpath_errno, attributes_errno;
static unsigned volume_queries, path_queries, attribute_queries, lookup_object_type = 1, lookup_class = 6;
static unsigned long inode_asked;
static const char *lookup_path = "/private/var/mobile/Library/Caches/example.db";
static int mock_fgetattrlist(int fd, struct tomacco_attribute_list *list, void *buffer, unsigned long size, unsigned options) {
    assert(fd == 10 && parent_opened && list->count == 5 && !list->reserved && list->common == 0x4);
    assert(!list->volume && !list->directory && !list->file && !list->fork && size == 12 && !options);
    ++volume_queries;
    if (fail_volume_identity) { saved_errno = 45; return -1; }
    unsigned *reply = buffer; reply[0] = 12; reply[1] = 42; reply[2] = 7; return 0;
}
static long mock_fsgetpath(char *buffer, unsigned long size, int *fsid, unsigned long inode) {
    assert(size == 1024 && fsid[0] == 42 && fsid[1] == 7 && parent_opened && volume_queries);
    ++path_queries; inode_asked = inode;
    if (fsgetpath_errno) { saved_errno = fsgetpath_errno; return -1; }
    strcpy(buffer, lookup_path); return (long)strlen(lookup_path) + 1;
}
static int mock_getattrlist(const char *path, struct tomacco_attribute_list *list, void *buffer, unsigned long size, unsigned options) {
    assert(!strcmp(path, lookup_path) && list->count == 5 && list->common == 0x40000008 && !list->volume);
    assert(options == 0x800 && size == 12 && path_queries);
    ++attribute_queries;
    if (attributes_errno) { saved_errno = attributes_errno; return -1; }
    unsigned *reply = buffer; reply[0] = 12; reply[1] = lookup_object_type; reply[2] = lookup_class; return 0;
}
static object mock_dlsym(object handle, const char *name) {
    assert(handle == (void *)-2);
    if (!strcmp(name, "fcntl")) return api_available ? (object)mock_fcntl : NULL;
    if (!lookup_api_available) return NULL;
    if (!strcmp(name, "fgetattrlist")) return (object)mock_fgetattrlist;
    if (!strcmp(name, "getattrlist")) return (object)mock_getattrlist;
    assert(!strcmp(name, "fsgetpath")); return (object)mock_fsgetpath;
}

static const char challenge[] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
static void reset(void) {
    memset(pool, 0, sizeof(pool)); used = 0; keys_used = 0; saved_errno = 0;
    virtual_guest = api_available = 1; uid = 501;
    directories = file_opens = file_closes = directory_closes = 0;
    sets = gets = writes = full_flushes = directory_syncs = parent_syncs = reads = stats = mkdirs = unlinks = 0;
    memset(sets_by_slot, 0, sizeof(sets_by_slot)); memset(gets_by_slot, 0, sizeof(gets_by_slot));
    memset(writes_by_slot, 0, sizeof(writes_by_slot)); memset(flushes_by_slot, 0, sizeof(flushes_by_slot));
    memset(exists, 0, sizeof(exists)); memset(fail_sets, 0, sizeof(fail_sets));
    memset(fail_reads, 0, sizeof(fail_reads)); memset(open_fails, 0, sizeof(open_fails));
    memset(content_sizes, 0, sizeof(content_sizes)); memset(contents, 0, sizeof(contents));
    for (unsigned i = 0; i < SLOTS; ++i) protections[i] = 3;
    file_opened = directory_opened = parent_opened = directory_exists = 0;
    fail_get = fail_full = fail_sync = fail_write = fail_close = unlink_fails = 0;
    metadata_fault = replace_on_reopen = change_after_read = interrupt_read = 0;
    directory_fault = 0; default_class = 3;
    lookup_api_available = 1; fail_volume_identity = fsgetpath_errno = attributes_errno = 0;
    volume_queries = path_queries = attribute_queries = 0; lookup_object_type = 1; lookup_class = 6; inode_asked = 0;
    cursor = 0; slot = 0;
    request_id = "0123456789abcdef0123456789abcdef";
}
static object row(void) { return number(0); }
static void existing(void) { directory_exists=file_exists=1; content_size = 64; memcpy(content, challenge, 64); }
static void existing_class(unsigned level, int level_protection) {
    directory_exists = 1; exists[level] = 1; protections[level] = level_protection;
    content_sizes[level] = 64; memcpy(contents[level], challenge, 64);
}
static int run(object result, int create) {
    char argument[72]; snprintf(argument, sizeof(argument), "%s:%s", create ? "create" : "read", challenge);
    int code = persistence_probe(result, argument);
    assert(!file_opened && !directory_opened && !parent_opened); return code;
}
static int lookup(object result, const char *digits) {
    char argument[40]; snprintf(argument, sizeof(argument), "inode:%s", digits);
    int code = persistence_probe(result, argument);
    assert(!file_opened && !directory_opened && !parent_opened); return code;
}
static int survey(object result) {
    char argument[80]; snprintf(argument, sizeof(argument), "classes:%s", challenge);
    int code = persistence_probe(result, argument);
    assert(!file_opened && !directory_opened && !parent_opened); return code;
}
static void error_is(object result, const char *error) {
    struct value *value = field(result, "error"); assert(value && !strcmp(value->text, error));
}
static struct value *class_field(object result, unsigned level, const char *name) {
    char key[48]; snprintf(key, sizeof(key), "class_%u_%s", level, name);
    return field(result, key);
}
static void class_error_is(object result, unsigned level, const char *error) {
    struct value *value = class_field(result, level, "error"); assert(value && !strcmp(value->text, error));
}
int main(int argc, char **argv) {
    assert(argc == 2); reset();
    (void)tomacco_buffer_log_directory; (void)tomacco_buffer_log_identity;
    if (!strcmp(argv[1], "arguments")) {
        char text[80]; snprintf(text, sizeof(text), "create:%s", challenge);
        assert(persistence_probe_argument(text) == 7);
        snprintf(text, sizeof(text), "read:%s", challenge); assert(persistence_probe_argument(text) == 5);
        const char *bad[] = {NULL,"","read:","create:","delete:x","read:../path"};
        for (unsigned i=0; i<sizeof(bad)/sizeof(bad[0]); ++i) assert(!persistence_probe_argument(bad[i]));
        text[5] = 'A'; assert(!persistence_probe_argument(text));
        text[5] = 'g'; assert(!persistence_probe_argument(text));
        snprintf(text, sizeof(text), "read:%sa", challenge); assert(!persistence_probe_argument(text));
    } else if (!strcmp(argv[1], "create")) {
        object result = row(); assert(!run(result, 1));
        assert(sets == 1 && gets == 2 && writes == 1 && full_flushes == 1 && directory_syncs == 1 && parent_syncs == 1 && mkdirs==1);
        assert(file_opens == 2 && file_closes == 2 && directory_closes == 1 && reads > 2);
        assert(field(result,"contents_match")->numeric && field(result,"class_c_verified")->numeric);
        assert(!field(result,"automatic_retry_safe")->numeric && !field(result,"paired_reboot_verified")->numeric);
    } else if (!strcmp(argv[1], "read")) {
        existing(); interrupt_read = 1; object result = row(); assert(!run(result, 0));
        assert(!sets && !writes && !full_flushes && !directory_syncs && !parent_syncs && !mkdirs && gets == 1);
        assert(field(result,"contents_match")->numeric && field(result,"automatic_retry_safe")->numeric);
        assert(!field(result,"file_created")->numeric && !field(result,"paired_reboot_verified")->numeric);
    } else if (!strcmp(argv[1], "guards")) {
        for (unsigned i=0; i<4; ++i) {
            reset();
            if (i==0) request_id = NULL;
            if (i==1) uid=0;
            if (i==2) virtual_guest=0;
            if (i==3) api_available=0;
            assert(run(row(), 1) == 69 && !directories && !file_opens && !writes);
        }
    } else if (!strcmp(argv[1], "metadata")) {
        for (int i=1; i<=5; ++i) {
            reset(); existing(); metadata_fault=i; object result=row();
            assert(run(result, 0)==69); error_is(result,"persistence_file_identity_mismatch");
            assert(!reads && !sets && !writes);
        }
        for(int i=1;i<=3;++i) {
            reset();existing();directory_fault=i;object result=row();
            assert(run(result,0)==69);error_is(result,"persistence_directory_identity_mismatch");
            assert(!file_opens && !reads && !sets && !writes && !mkdirs);
        }
    } else if (!strcmp(argv[1], "no-overwrite")) {
        existing(); object result=row(); assert(run(result,1)==69);
        error_is(result,"persistence_file_open_failed"); assert(field(result,"errno")->numeric==17);
        assert(!sets && !writes && !reads && content_size==64 && !memcmp(content,challenge,64));
    } else if (!strcmp(argv[1], "failures")) {
        for (unsigned i=0; i<7; ++i) {
            reset();
            if(i==0)fail_set=1;
            if(i==1)fail_get=1;
            if(i==2)fail_write=1;
            if(i==3)fail_sync=1;
            if(i==4)fail_full=1;
            if(i==5)fail_read=1;
            if(i==6)fail_close=1;
            object result=row(); assert(run(result,1)==69 && file_exists);
            assert(field(result,"file_created")->numeric && !field(result,"automatic_retry_safe")->numeric);
            if (i<2) assert(!writes);
        }
    } else if (!strcmp(argv[1], "mismatches")) {
        existing(); protections[0]=4; object result=row(); assert(run(result,0)==69);
        error_is(result,"persistence_protection_class_mismatch"); assert(!reads && !sets);
        reset(); existing(); content[2]='f'; result=row(); assert(run(result,0)==69);
        error_is(result,"persistence_content_mismatch"); assert(!field(result,"contents_match")->numeric);
        reset(); existing(); change_after_read=1; result=row(); assert(run(result,0)==69);
        error_is(result,"persistence_file_changed_during_read");
        reset(); replace_on_reopen=1; result=row(); assert(run(result,1)==69);
        error_is(result,"persistence_file_replaced"); assert(!reads);
    } else if (!strcmp(argv[1], "classes-arguments")) {
        char text[80]; snprintf(text, sizeof(text), "classes:%s", challenge);
        assert(persistence_probe_argument(text) == 8);
        const char *bad[] = {"classes:", "class:", "classes", "classes:/", "classes:x"};
        for (unsigned i=0; i<sizeof(bad)/sizeof(bad[0]); ++i) assert(!persistence_probe_argument(bad[i]));
        snprintf(text, sizeof(text), "class:%s", challenge); assert(!persistence_probe_argument(text));
        snprintf(text, sizeof(text), "classes:%s", challenge); text[8] = 'A'; assert(!persistence_probe_argument(text));
        snprintf(text, sizeof(text), "classes:%sa", challenge); assert(!persistence_probe_argument(text));
        snprintf(text, sizeof(text), "classes:%s", challenge); text[71] = 0; assert(!persistence_probe_argument(text));
    } else if (!strcmp(argv[1], "classes-create")) {
        /* Nothing exists: every class is created with its own class, written,
         * flushed, reopened and compared; the challenge file stays untouched. */
        object result = row(); assert(!survey(result));
        assert(mkdirs == 1 && field(result,"private_directory_created")->numeric);
        assert(field(result,"classes_surveyed")->numeric == 4 && field(result,"classes_readable")->numeric == 4);
        assert(field(result,"automatic_retry_safe")->numeric && !field(result,"paired_reboot_verified")->numeric);
        assert(!strcmp(field(result,"probe_action")->text, "classes") && !strcmp(field(result,"challenge")->text, challenge));
        for (unsigned level = 1; level <= 4; ++level) {
            assert(!strcmp(class_field(result, level, "action")->text, "create"));
            assert(class_field(result, level, "protection_class")->numeric == level);
            assert(class_field(result, level, "contents_match")->numeric && class_field(result, level, "read_bytes")->numeric == 64);
            assert(class_field(result, level, "inode")->numeric == 100 + 2 * level);
            assert(!class_field(result, level, "error") && !class_field(result, level, "errno"));
            assert(sets_by_slot[level] == 1 && writes_by_slot[level] == 1 && flushes_by_slot[level] == 1 && gets_by_slot[level] == 1);
            assert(exists[level] && content_sizes[level] == 64 && !memcmp(contents[level], challenge, 64));
        }
        assert(!exists[0] && !sets_by_slot[0] && !writes_by_slot[0]);
        assert(file_opens == 8 && file_closes == 8 && directory_syncs == 4 && parent_syncs == 4 && !unlinks);
        assert(!field(result, "class_c_verified") && !field(result, "file_created"));
    } else if (!strcmp(argv[1], "classes-read")) {
        /* Every class exists from an earlier boot: read back, nothing set,
         * written, flushed or removed. */
        for (unsigned level = 1; level <= 4; ++level) existing_class(level, (int)level);
        existing(); object result = row(); assert(!survey(result));
        assert(!sets && !writes && !full_flushes && !directory_syncs && !parent_syncs && !unlinks && mkdirs == 1);
        assert(field(result,"classes_readable")->numeric == 4 && !field(result,"private_directory_created")->numeric);
        for (unsigned level = 1; level <= 4; ++level) {
            assert(!strcmp(class_field(result, level, "action")->text, "read"));
            assert(class_field(result, level, "contents_match")->numeric && gets_by_slot[level] == 1);
            assert(class_field(result, level, "protection_class")->numeric == level);
        }
        assert(!gets_by_slot[0] && content_sizes[0] == 64);
    } else if (!strcmp(argv[1], "classes-partial")) {
        /* Class A cannot be set (AKS refuses, EPERM): its empty file is removed
         * and the survey goes on. Class B exists with another class, class C
         * exists with other contents, class D is created. */
        fail_sets[1] = 1; existing_class(2, 3); existing_class(3, 3); contents[3][5] = 'f';
        object result = row(); assert(!survey(result));
        assert(field(result,"ok")->numeric && field(result,"classes_surveyed")->numeric == 4);
        assert(field(result,"classes_readable")->numeric == 1);
        class_error_is(result, 1, "persistence_set_class_failed");
        assert(class_field(result, 1, "errno")->numeric == 1 && class_field(result, 1, "removed")->numeric);
        assert(!strcmp(class_field(result, 1, "action")->text, "create") && !class_field(result, 1, "protection_class"));
        assert(!writes_by_slot[1] && !exists[1] && unlinks == 1 && !class_field(result, 1, "contents_match")->numeric);
        class_error_is(result, 2, "persistence_protection_class_mismatch");
        assert(class_field(result, 2, "protection_class")->numeric == 3 && !class_field(result, 2, "contents_match")->numeric);
        class_error_is(result, 3, "persistence_content_mismatch");
        assert(class_field(result, 3, "read_bytes")->numeric == 64 && !class_field(result, 3, "contents_match")->numeric);
        assert(!strcmp(class_field(result, 4, "action")->text, "create") && class_field(result, 4, "contents_match")->numeric);
        assert(!class_field(result, 4, "error") && exists[4] && !memcmp(contents[4], challenge, 64));
        /* A removal that fails is reported, not hidden. */
        reset(); fail_sets[1] = 1; unlink_fails = 1; result = row(); assert(!survey(result));
        assert(!class_field(result, 1, "removed")->numeric && exists[1] && unlinks == 1);
        class_error_is(result, 1, "persistence_set_class_failed");
    } else if (!strcmp(argv[1], "classes-unreadable")) {
        /* A file whose key AKS cannot unwrap fails at open or read with EPERM;
         * the class's row carries that errno and the other classes go on. */
        for (unsigned level = 1; level <= 4; ++level) existing_class(level, (int)level);
        open_fails[1] = 1; fail_reads[2] = 1;
        object result = row(); assert(!survey(result));
        class_error_is(result, 1, "persistence_file_open_failed");
        assert(class_field(result, 1, "errno")->numeric == 1 && !strcmp(class_field(result, 1, "action")->text, "read"));
        class_error_is(result, 2, "persistence_read_failed");
        assert(class_field(result, 2, "errno")->numeric == 1 && !strcmp(class_field(result, 2, "action")->text, "read"));
        assert(class_field(result, 3, "contents_match")->numeric && class_field(result, 4, "contents_match")->numeric);
        assert(field(result,"classes_readable")->numeric == 2 && !sets && !writes && !unlinks);
        assert(exists[1] && exists[2]);
    } else if (!strcmp(argv[1], "classes-guards")) {
        for (unsigned i=0; i<4; ++i) {
            reset();
            if (i==0) request_id = NULL;
            if (i==1) uid=0;
            if (i==2) virtual_guest=0;
            if (i==3) api_available=0;
            assert(survey(row()) == 69 && !directories && !file_opens && !writes && !mkdirs);
        }
        for(int i=1;i<=3;++i) {
            reset(); directory_exists = 1; directory_fault=i; object result=row();
            assert(survey(result)==69); error_is(result,"persistence_directory_identity_mismatch");
            assert(!file_opens && !reads && !sets && !writes && field(result,"classes_surveyed")->numeric == 0);
        }
    } else if (!strcmp(argv[1], "inode-arguments")) {
        assert(persistence_probe_argument("inode:191838") == 6 && persistence_probe_argument("inode:0") == 6);
        assert(persistence_probe_argument("inode:1234567890123456789") == 6);
        const char *bad[] = {"inode:", "inode", "inode:12345678901234567890", "inode:19x", "inode:-1", "inode: 1",
                             "ino:1", "inode:1/", "INODE:1"};
        for (unsigned i=0; i<sizeof(bad)/sizeof(bad[0]); ++i) assert(!persistence_probe_argument(bad[i]));
    } else if (!strcmp(argv[1], "inode-lookup")) {
        /* The path comes from the inode on the Library's volume, the class
         * from the path without opening the file. */
        object result = row(); assert(!lookup(result, "191838"));
        assert(inode_asked == 191838 && volume_queries == 1 && path_queries == 1 && attribute_queries == 1);
        assert(!file_opens && !mkdirs && !sets && !writes && !reads && directories == 1);
        assert(!strcmp(field(result,"probe_action")->text, "inode") && field(result,"inode")->numeric == 191838);
        assert(field(result,"path_known")->numeric && !strcmp(field(result,"path")->text, lookup_path));
        assert(field(result,"volume_fsid")->numeric == 42 && field(result,"object_type")->numeric == 1);
        assert(field(result,"protection_class")->numeric == 6 && field(result,"automatic_retry_safe")->numeric);
        assert(!field(result,"errno") && !field(result,"error"));
    } else if (!strcmp(argv[1], "inode-failures")) {
        /* Missing API before any open; an unknown inode, a path a mobile
         * process may not build and an attribute refusal keep their errno. */
        lookup_api_available = 0; object result = row(); assert(lookup(result, "5") == 69);
        error_is(result, "persistence_api_unavailable"); assert(!directories && !volume_queries);
        reset(); fail_volume_identity = 1; result = row(); assert(lookup(result, "5") == 69);
        error_is(result, "persistence_volume_identity_failed"); assert(field(result,"errno")->numeric == 45 && !path_queries);
        reset(); fsgetpath_errno = 2; result = row(); assert(lookup(result, "5") == 69);
        error_is(result, "persistence_inode_path_failed"); assert(field(result,"errno")->numeric == 2);
        assert(!field(result,"path_known")->numeric && !field(result,"path") && !attribute_queries);
        reset(); fsgetpath_errno = 13; result = row(); assert(lookup(result, "5") == 69);
        error_is(result, "persistence_inode_path_failed"); assert(field(result,"errno")->numeric == 13);
        reset(); attributes_errno = 1; result = row(); assert(lookup(result, "5") == 69);
        error_is(result, "persistence_inode_attributes_failed"); assert(field(result,"errno")->numeric == 1);
        assert(field(result,"path_known")->numeric && !strcmp(field(result,"path")->text, lookup_path));
        assert(!field(result,"protection_class"));
        for (unsigned i=0; i<3; ++i) {
            reset();
            if (i==0) request_id = NULL;
            if (i==1) uid=0;
            if (i==2) virtual_guest=0;
            assert(lookup(row(), "5") == 69 && !directories && !volume_queries);
        }
    } else assert(0);
    return 0;
}
