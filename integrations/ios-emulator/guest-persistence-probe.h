/* One owned, immutable Class C challenge, for paired clean-reboot checks,
 * and a survey of the app-facing protection classes A to D.
 * Darwin fcntl.h: F_GETPROTECTIONCLASS=63, F_SETPROTECTIONCLASS=64,
 * F_FULLFSYNC=51; content_protection.h: PROTECTION_CLASS_A=1 .. _D=4
 * (E=5, F=6 and CX=7 are the system's own and are not touched).
 * https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/fcntl.h
 * https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/content_protection.h
 * An individual response proves only this worker's file operation. The host
 * must bind both boots, unchanged challenge, media, flush and actual exits.
 * The files a boot wrote on the data volume are what a later boot's read: and
 * classes: compare against.
 */
#define TOMACCO_PERSISTENCE_FILE "io.github.marselnenaj.tomacco.persistence-v1"
#define TOMACCO_PERSISTENCE_DIRECTORY "io.github.marselnenaj.tomacco.persistence"
#define TOMACCO_PERSISTENCE_CLASS_FILE "io.github.marselnenaj.tomacco.persistence-class-"
#define TOMACCO_PERSISTENCE_CLASS_FIRST 1u
#define TOMACCO_PERSISTENCE_CLASS_LAST 4u
#define TOMACCO_PERSISTENCE_BYTES 64u
extern int mkdirat(int, const char *, unsigned short);
extern int unlinkat(int, const char *, int);

/* create:/read: own the Class C challenge (prefix 7 marks the mutation);
 * classes: surveys one file per class A..D with the same 64-byte challenge
 * and is safe to repeat; inode:<1..19 decimal digits> looks one file up
 * without opening it. The prefix length names the action. */
static int persistence_probe_argument(const char *value) {
    if (!value) return 0;
    const char *actions[] = {"create:", "read:", "classes:"};
    for (unsigned action = 0; action < 3; ++action) {
        unsigned prefix = (unsigned)strlen(actions[action]);
        if (strlen(value) != prefix + TOMACCO_PERSISTENCE_BYTES) continue;
        unsigned matched = 0;
        for (; matched < prefix && value[matched] == actions[action][matched]; ++matched) {}
        if (matched != prefix) continue;
        for (unsigned i = 0; i < TOMACCO_PERSISTENCE_BYTES; ++i) {
            char c = value[prefix + i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 0;
        }
        return (int)prefix;
    }
    const char *lookup = "inode:";
    unsigned matched = 0;
    for (; matched < 6 && value[matched] == lookup[matched]; ++matched) {}
    if (matched != 6) return 0;
    unsigned long digits = strlen(value) - 6;
    if (!digits || digits > 19) return 0;
    for (unsigned long i = 0; i < digits; ++i)
        if (value[6 + i] < '0' || value[6 + i] > '9') return 0;
    return 6;
}

/* inode:<decimal>: the path and protection class of one file on the volume
 * that holds the mobile Library, for a boot log's "retain_crypto_states
 * failed on ino N" line. fsgetpath(2) builds the path from the inode (XNU
 * checks search access on every directory for a non-root caller) and
 * getattrlist(2) reads ATTR_CMN_DATA_PROTECT_FLAGS without opening the file,
 * so a file whose key the keystore cannot unwrap still shows its class.
 * Darwin attr.h: ATTR_CMN_FSID=0x4, ATTR_CMN_OBJTYPE=0x8,
 * ATTR_CMN_DATA_PROTECT_FLAGS=0x40000000, FSOPT_NOFOLLOW_ANY=0x800; a reply
 * starts with its own length. Nothing is opened for reading or written. */
struct tomacco_attribute_list { unsigned short count, reserved; unsigned common, volume, directory, file, fork; };

static int persistence_inode_lookup(object row, const char *digits) {
    unsigned long inode = 0;
    for (; *digits; ++digits) inode = inode * 10 + (unsigned long)(*digits - '0');
    put(row, "probe_action", string("inode"));
    put(row, "inode", number(inode));
    put(row, "automatic_retry_safe", boolean(1));
    put(row, "path_known", boolean(0));
    int (*attributes_of)(int, struct tomacco_attribute_list *, void *, unsigned long, unsigned) =
        dlsym((void *)-2, "fgetattrlist");
    int (*attributes_at)(const char *, struct tomacco_attribute_list *, void *, unsigned long, unsigned) =
        dlsym((void *)-2, "getattrlist");
    long (*path_of)(char *, unsigned long, int *, unsigned long) = dlsym((void *)-2, "fsgetpath");
    if (!attributes_of || !attributes_at || !path_of) return failure(row, "persistence_api_unavailable");
    int parent = open("/private/var/mobile/Library", 0x21100004);
    int saved_error = 0;
    const char *error = 0;
    if (parent < 0) { error = "persistence_parent_unavailable"; goto system_error; }
    struct tomacco_attribute_list list = {5, 0, 0x4, 0, 0, 0, 0};
    unsigned volume[3] = {0}; /* length, fsid.val[0], fsid.val[1] */
    if (attributes_of(parent, &list, volume, sizeof(volume), 0)) {
        error = "persistence_volume_identity_failed"; goto system_error;
    }
    if (volume[0] != sizeof(volume)) { error = "persistence_volume_identity_invalid"; goto finish; }
    int fsid[2] = {(int)volume[1], (int)volume[2]};
    put(row, "volume_fsid", number(volume[1]));
    char path[1024];
    long length = path_of(path, sizeof(path), fsid, inode);
    if (length < 0) { error = "persistence_inode_path_failed"; goto system_error; }
    if (length < 2 || (unsigned long)length > sizeof(path) || path[length - 1] || path[0] != '/') {
        error = "persistence_inode_path_invalid"; goto finish;
    }
    put(row, "path", string(path));
    put(row, "path_known", boolean(1));
    list.common = 0x8 | 0x40000000;
    unsigned attributes[3] = {0}; /* length, object type, protection class */
    if (attributes_at(path, &list, attributes, sizeof(attributes), 0x800)) {
        error = "persistence_inode_attributes_failed"; goto system_error;
    }
    if (attributes[0] != sizeof(attributes)) { error = "persistence_inode_attributes_invalid"; goto finish; }
    put(row, "object_type", number(attributes[1]));
    put(row, "protection_class", number(attributes[2]));
    goto finish;

system_error:
    saved_error = *__error();
finish:
    if (parent >= 0 && close(parent) && !error) {
        saved_error = *__error(); error = "persistence_parent_close_failed";
    }
    if (saved_error) put(row, "errno", number((unsigned)saved_error));
    if (error) return failure(row, error);
    put(row, "ok", boolean(1));
    return emit(row);
}

static int persistence_probe_file_identity(const struct tomacco_darwin_stat *info,
                                           unsigned expected_size) {
    return (info->mode & 0170000) == 0100000 && info->uid == 501 &&
        info->links == 1 && (info->mode & 07777) == 0600 &&
        info->size == expected_size;
}

static void persistence_class_put(object row, unsigned level, const char *name, object value) {
    char key[48];
    snprintf(key, sizeof(key), "class_%u_%s", level, name);
    put(row, key, value);
}

/* One file per class, created exclusively with that class when missing and
 * read back when present, so a survey in a later boot reads what an earlier
 * boot wrote. The result is this class's row; a failure names the step and
 * errno and never stops the other classes. A file whose class could not be
 * set carries the directory's default class and is removed again, so the next
 * survey can try the class again. Returns 1 when the contents read back. */
static int persistence_class_file(object row, unsigned level, const char *challenge,
                                  int directory, int parent, int (*fcntl_fn)(int, int, ...)) {
    char name[sizeof(TOMACCO_PERSISTENCE_CLASS_FILE) + 2];
    snprintf(name, sizeof(name), "%s%u", TOMACCO_PERSISTENCE_CLASS_FILE, level);
    const char *error = 0;
    int saved_error = 0, file = -1, created = 0, matched = 0;
    struct tomacco_darwin_stat info = {0}, original = {0};
    persistence_class_put(row, level, "contents_match", boolean(0));
    file = openat(directory, name, 0x1000b06, 0600);
    if (file >= 0) created = 1;
    else if (*__error() == 17) {
        persistence_class_put(row, level, "action", string("read"));
        file = openat(directory, name, 0x1000104);
    }
    if (created) persistence_class_put(row, level, "action", string("create"));
    if (file < 0) { error = "persistence_file_open_failed"; goto system_error; }
    if (fstat(file, &info)) { error = "persistence_file_stat_failed"; goto system_error; }
    if (!persistence_probe_file_identity(&info, created ? 0 : TOMACCO_PERSISTENCE_BYTES)) {
        error = "persistence_file_identity_mismatch"; goto finish;
    }
    original = info;
    persistence_class_put(row, level, "inode", number(info.inode));
    if (created && fcntl_fn(file, 64, (int)level)) { error = "persistence_set_class_failed"; goto system_error; }
    int protection = fcntl_fn(file, 63);
    if (protection < 0) { error = "persistence_get_class_failed"; goto system_error; }
    persistence_class_put(row, level, "protection_class", number((unsigned)protection));
    if ((unsigned)protection != level) { error = "persistence_protection_class_mismatch"; goto finish; }
    if (created) {
        if (write_bytes(file, challenge, TOMACCO_PERSISTENCE_BYTES)) {
            error = "persistence_write_failed"; goto system_error;
        }
        if (fsync(directory)) { error = "persistence_directory_sync_failed"; goto system_error; }
        if (fsync(parent)) { error = "persistence_parent_sync_failed"; goto system_error; }
        if (fcntl_fn(file, 51)) { error = "persistence_full_flush_failed"; goto system_error; }
        if (close(file)) { file = -1; error = "persistence_file_close_failed"; goto system_error; }
        file = openat(directory, name, 0x1000104);
        if (file < 0) { error = "persistence_reopen_failed"; goto system_error; }
        if (fstat(file, &info)) { error = "persistence_file_stat_failed"; goto system_error; }
        if (!persistence_probe_file_identity(&info, TOMACCO_PERSISTENCE_BYTES)) {
            error = "persistence_file_identity_mismatch"; goto finish;
        }
        if (info.device != original.device || info.inode != original.inode) {
            error = "persistence_file_replaced"; goto finish;
        }
    }
    char bytes[TOMACCO_PERSISTENCE_BYTES + 1];
    unsigned used = 0;
    while (used < sizeof(bytes)) {
        long got = read(file, bytes + used, sizeof(bytes) - used);
        if (got < 0 && *__error() == 4) continue;
        if (got < 0) { error = "persistence_read_failed"; goto system_error; }
        if (!got) break;
        if ((unsigned long)got > sizeof(bytes) - used) {
            error = "persistence_invalid_read_count"; goto finish;
        }
        used += (unsigned)got;
    }
    persistence_class_put(row, level, "read_bytes", number(used));
    if (used != TOMACCO_PERSISTENCE_BYTES) { error = "persistence_content_size_mismatch"; goto finish; }
    for (unsigned i = 0; i < used; ++i)
        if (bytes[i] != challenge[i]) { error = "persistence_content_mismatch"; goto finish; }
    matched = 1;
    persistence_class_put(row, level, "contents_match", boolean(1));
    goto finish;

system_error:
    saved_error = *__error();
finish:
    if (file >= 0 && close(file) && !error) {
        saved_error = *__error(); error = "persistence_file_close_failed";
    }
    if (created && error && !strcmp(error, "persistence_set_class_failed"))
        persistence_class_put(row, level, "removed", boolean(!unlinkat(directory, name, 0)));
    if (saved_error) persistence_class_put(row, level, "errno", number((unsigned)saved_error));
    if (error) persistence_class_put(row, level, "error", string(error));
    return matched;
}

static int persistence_class_survey(object row, const char *challenge, int (*fcntl_fn)(int, int, ...)) {
    put(row, "probe_action", string("classes"));
    put(row, "challenge", string(challenge));
    put(row, "uid", number(getuid()));
    put(row, "automatic_retry_safe", boolean(1));
    put(row, "paired_reboot_verified", boolean(0));
    put(row, "private_directory_created", boolean(0));
    put(row, "classes_surveyed", number(0));
    put(row, "classes_readable", number(0));
    int parent = -1, directory = -1, saved_error = 0;
    const char *error = 0;
    struct tomacco_darwin_stat info = {0};
    parent = open("/private/var/mobile/Library", 0x21100004);
    if (parent < 0) { error = "persistence_parent_unavailable"; goto system_error; }
    if (!mkdirat(parent, TOMACCO_PERSISTENCE_DIRECTORY, 0700))
        put(row, "private_directory_created", boolean(1));
    else if (*__error() != 17) {
        error = "persistence_directory_create_failed"; goto system_error;
    }
    directory = openat(parent, TOMACCO_PERSISTENCE_DIRECTORY, 0x1100104);
    if (directory < 0) { error = "persistence_directory_unavailable"; goto system_error; }
    if (fstat(directory, &info)) { error = "persistence_directory_stat_failed"; goto system_error; }
    put(row, "private_directory_uid", number(info.uid));
    put(row, "private_directory_mode", number(info.mode & 07777));
    if ((info.mode & 0170000) != 0040000 || info.uid != 501 || (info.mode & 07777) != 0700) {
        error = "persistence_directory_identity_mismatch"; goto finish;
    }
    unsigned surveyed = 0, readable = 0;
    for (unsigned level = TOMACCO_PERSISTENCE_CLASS_FIRST; level <= TOMACCO_PERSISTENCE_CLASS_LAST; ++level) {
        readable += (unsigned)persistence_class_file(row, level, challenge, directory, parent, fcntl_fn);
        ++surveyed;
    }
    put(row, "classes_surveyed", number(surveyed));
    put(row, "classes_readable", number(readable));
    goto finish;

system_error:
    saved_error = *__error();
finish:
    if (directory >= 0 && close(directory) && !error) {
        saved_error = *__error(); error = "persistence_directory_close_failed";
    }
    if (parent >= 0 && close(parent) && !error) {
        saved_error = *__error(); error = "persistence_parent_close_failed";
    }
    if (saved_error) put(row, "errno", number((unsigned)saved_error));
    if (error) return failure(row, error);
    put(row, "ok", boolean(1));
    return emit(row);
}

static int persistence_probe(object row, const char *argument) {
    int prefix = persistence_probe_argument(argument);
    if (!prefix) return failure(row, "invalid_persistence_challenge");
    if (!request_id) return failure(row, "persistence_request_identity_required");
    if (getuid() != 501 || !setup_virtual_device())
        return failure(row, "virtual_mobile_guest_required");
    if (prefix == 6) return persistence_inode_lookup(row, argument + prefix);
    if (prefix == 8) {
        int (*survey_fcntl)(int, int, ...) = dlsym((void *)-2, "fcntl");
        if (!survey_fcntl) return failure(row, "persistence_api_unavailable");
        return persistence_class_survey(row, argument + prefix, survey_fcntl);
    }
    int create = prefix == 7;
    put(row, "probe_action", string(create ? "create" : "read"));
    put(row, "challenge", string(argument + prefix));
    put(row, "uid", number(getuid()));
    put(row, "file_created", boolean(0));
    put(row, "class_c_verified", boolean(0));
    put(row, "contents_match", boolean(0));
    put(row, "read_completed", boolean(0));
    put(row, "file_fully_flushed", boolean(0));
    put(row, "directory_synced", boolean(0));
    put(row, "parent_directory_synced", boolean(0));
    put(row, "private_directory_created", boolean(0));
    put(row, "automatic_retry_safe", boolean(!create));
    put(row, "paired_reboot_verified", boolean(0));
    int (*fcntl_fn)(int, int, ...) = dlsym((void *)-2, "fcntl");
    if (!fcntl_fn) return failure(row, "persistence_api_unavailable");

    int parent = -1, directory = -1, file = -1, saved_error = 0;
    const char *error = 0;
    struct tomacco_darwin_stat info = {0}, original = {0};
    /* DIRECTORY|NOFOLLOW_ANY|NONBLOCK|CLOEXEC. No Foundation file metadata,
     * symlink traversal, arbitrary guest path or privilege change. */
    parent = open("/private/var/mobile/Library", 0x21100004);
    if (parent < 0) { error = "persistence_parent_unavailable"; goto system_error; }
    if (create) {
        if (!mkdirat(parent, TOMACCO_PERSISTENCE_DIRECTORY, 0700))
            put(row, "private_directory_created", boolean(1));
        else if (*__error() != 17) {
            error = "persistence_directory_create_failed"; goto system_error;
        }
    }
    /* The shared parent has its original OS-managed permissions. The owned
     * child, pinned relative to it, supplies the private identity boundary. */
    directory = openat(parent, TOMACCO_PERSISTENCE_DIRECTORY, 0x1100104);
    if (directory < 0) { error = "persistence_directory_unavailable"; goto system_error; }
    if (fstat(directory, &info)) { error = "persistence_directory_stat_failed"; goto system_error; }
    put(row, "private_directory_uid", number(info.uid));
    put(row, "private_directory_mode", number(info.mode & 07777));
    if ((info.mode & 0170000) != 0040000 || info.uid != 501 || (info.mode & 07777) != 0700) {
        error = "persistence_directory_identity_mismatch"; goto finish;
    }
    /* A fixed filename bounds storage. Create never truncates or replaces an
     * existing challenge, including one left by an interrupted create. */
    file = openat(directory, TOMACCO_PERSISTENCE_FILE,
                  create ? 0x1000b06 : 0x1000104, 0600);
    /* create: RDWR|CREAT|EXCL|NOFOLLOW|NONBLOCK|CLOEXEC; read: RDONLY with
     * NOFOLLOW|NONBLOCK|CLOEXEC. Check regular-file identity before any I/O. */
    if (file < 0) { error = "persistence_file_open_failed"; goto system_error; }
    if (create) {
        put(row, "file_created", boolean(1));
    }
    if (fstat(file, &info)) { error = "persistence_file_stat_failed"; goto system_error; }
    if (!persistence_probe_file_identity(&info, create ? 0 : TOMACCO_PERSISTENCE_BYTES)) {
        error = "persistence_file_identity_mismatch"; goto finish;
    }
    original = info;
    put(row, "file_inode", number(info.inode));
    if (create && fcntl_fn(file, 64, 3)) {
        error = "persistence_set_class_failed"; goto system_error;
    }
    int protection = fcntl_fn(file, 63);
    if (protection < 0) { error = "persistence_get_class_failed"; goto system_error; }
    put(row, "protection_class", number((unsigned)protection));
    if (protection != 3) { error = "persistence_protection_class_mismatch"; goto finish; }
    put(row, "class_c_verified", boolean(1));
    if (create) {
        if (write_bytes(file, argument + prefix, TOMACCO_PERSISTENCE_BYTES)) {
            error = "persistence_write_failed"; goto system_error;
        }
        if (fsync(directory)) { error = "persistence_directory_sync_failed"; goto system_error; }
        put(row, "directory_synced", boolean(1));
        if (fsync(parent)) { error = "persistence_parent_sync_failed"; goto system_error; }
        put(row, "parent_directory_synced", boolean(1));
        if (fcntl_fn(file, 51)) { error = "persistence_full_flush_failed"; goto system_error; }
        put(row, "file_fully_flushed", boolean(1));
        if (close(file)) { file = -1; error = "persistence_file_close_failed"; goto system_error; }
        file = openat(directory, TOMACCO_PERSISTENCE_FILE, 0x1000104);
        if (file < 0) { error = "persistence_reopen_failed"; goto system_error; }
        if (fstat(file, &info)) { error = "persistence_file_stat_failed"; goto system_error; }
        if (!persistence_probe_file_identity(&info, TOMACCO_PERSISTENCE_BYTES)) {
            error = "persistence_file_identity_mismatch"; goto finish;
        }
        if (info.device != original.device || info.inode != original.inode) {
            error = "persistence_file_replaced"; goto finish;
        }
        original = info;
        protection = fcntl_fn(file, 63);
        if (protection < 0) { error = "persistence_get_class_failed"; goto system_error; }
        if (protection != 3) {
            put(row, "class_c_verified", boolean(0));
            put(row, "protection_class", number((unsigned)protection));
            error = "persistence_protection_class_mismatch"; goto finish;
        }
    }
    char bytes[TOMACCO_PERSISTENCE_BYTES + 1];
    unsigned used = 0;
    while (used < sizeof(bytes)) {
        long got = read(file, bytes + used, sizeof(bytes) - used);
        if (got < 0 && *__error() == 4) continue;
        if (got < 0) { error = "persistence_read_failed"; goto system_error; }
        if (!got) break;
        if ((unsigned long)got > sizeof(bytes) - used) {
            error = "persistence_invalid_read_count"; goto finish;
        }
        used += (unsigned)got;
    }
    put(row, "read_bytes", number(used));
    if (used != TOMACCO_PERSISTENCE_BYTES) { error = "persistence_content_size_mismatch"; goto finish; }
    for (unsigned i = 0; i < used; ++i)
        if (bytes[i] != argument[prefix + i]) { error = "persistence_content_mismatch"; goto finish; }
    if (fstat(file, &info)) { error = "persistence_file_stat_failed"; goto system_error; }
    if (!persistence_probe_file_identity(&info, TOMACCO_PERSISTENCE_BYTES)) {
        error = "persistence_file_identity_mismatch"; goto finish;
    }
    if (info.device != original.device || info.inode != original.inode ||
        info.times[2] != original.times[2] || info.times[3] != original.times[3] ||
        info.times[4] != original.times[4] || info.times[5] != original.times[5]) {
        error = "persistence_file_changed_during_read"; goto finish;
    }
    put(row, "read_completed", boolean(1));
    put(row, "contents_match", boolean(1));
    goto finish;

system_error:
    saved_error = *__error();
finish:
    if (file >= 0 && close(file) && !error) {
        saved_error = *__error(); error = "persistence_file_close_failed";
    }
    if (directory >= 0 && close(directory) && !error) {
        saved_error = *__error(); error = "persistence_directory_close_failed";
    }
    if (parent >= 0 && close(parent) && !error) {
        saved_error = *__error(); error = "persistence_parent_close_failed";
    }
    if (saved_error) put(row, "errno", number((unsigned)saved_error));
    if (error) return failure(row, error);
    put(row, "ok", boolean(1));
    return emit(row);
}
