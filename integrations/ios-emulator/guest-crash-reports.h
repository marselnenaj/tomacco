/* Original native crash report bytes, scoped to a pinned disposable worker.
 * Darwin arm64's 64-bit dirent layout: bsd/sys/dirent.h. No arbitrary paths,
 * recursive traversal, symlink following, report deletion or crash suppression.
 */
struct tomacco_crash_dirent {
    unsigned long long inode, seek_offset;
    unsigned short record_length, name_length;
    unsigned char type;
    char name[1024];
};
_Static_assert(__builtin_offsetof(struct tomacco_crash_dirent, name) == 21, "Darwin dirent ABI");
extern long long lseek(int, long long, int);

/* The report name filter: the process name a report file starts with
 * (ReportCrash names a report <process>-<date>.ips). Any name a report file
 * may carry is allowed, 1 to 255 of the characters the scan below accepts,
 * so a developer's own app is read like iOS's processes. A filter is never a path: no '/', and it
 * does not start with '.' or '-'. */
static int crash_process_allowed(const char *name) {
    if (!name || !name[0] || name[0] == '.' || name[0] == '-') return 0;
    unsigned long length = 0;
    for (; name[length]; ++length) {
        char c = name[length];
        if (length >= 255 || !((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')) return 0;
    }
    return 1;
}

static int crash_directory(const char *owner) {
    int fd = open("/private/var", 0x1100100); /* RDONLY|DIRECTORY|NOFOLLOW|CLOEXEC */
    const char *parts[] = {owner, "Library", "Logs", "CrashReporter"};
    for (unsigned n = 0; fd >= 0 && n < 4; ++n) {
        int next = openat(fd, parts[n], 0x1100100);
        int saved = *__error();
        close(fd);
        fd = next;
        *__error() = saved;
    }
    return fd;
}

static int crash_reports(object row, const char *process) {
    int contract = display_contract(row);
    if (contract) return contract;
    if (!crash_process_allowed(process)) return failure(row, "unsupported_crash_process");
    void *(*from_fd)(int) = dlsym((void *)-2, "fdopendir");
    struct tomacco_crash_dirent *(*next_entry)(void *) = dlsym((void *)-2, "readdir");
    int (*close_directory)(void *) = dlsym((void *)-2, "closedir");
    if (!from_fd || !next_entry || !close_directory) return failure(row, "crash_directory_api_unavailable");
    object directories = get(get_class("NSMutableArray"), "array");
    object reports = get(get_class("NSMutableArray"), "array");
    unsigned budget = 512 * 1024;
    unsigned long prefix_length = strlen(process);
    int coverage = 1, limited = 0;
    const char *owners[] = {"mobile", "root"};
    for (unsigned owner = 0; owner < 2; ++owner) {
        object directory_row = get(get_class("NSMutableDictionary"), "dictionary");
        put(directory_row, "owner", string(owners[owner]));
        ((void (*)(object, object, object))message)(directories, selector("addObject:"), directory_row);
        int fd = crash_directory(owners[owner]);
        if (fd < 0) {
            put(directory_row, "errno", number((unsigned)*__error()));
            put(directory_row, "read", boolean(0));
            coverage = 0;
            continue;
        }
        /* fdopendir owns a duplicate; keep the pinned directory for openat. */
        int copy = dup(fd);
        void *directory = copy < 0 ? 0 : from_fd(copy);
        if (!directory) {
            int saved = *__error();
            if (copy >= 0) close(copy);
            close(fd);
            put(directory_row, "errno", number((unsigned)saved));
            put(directory_row, "read", boolean(0));
            coverage = 0;
            continue;
        }
        char names[3][1024] = {{0}};
        unsigned kept = 0, matches = 0, scanned = 0;
        int directory_error = 0, exhausted = 0;
        while (scanned < 4096) {
            *__error() = 0;
            struct tomacco_crash_dirent *entry = next_entry(directory);
            if (!entry) { directory_error = *__error(); exhausted = !directory_error; break; }
            ++scanned;
            unsigned length = entry->name_length;
            if (length >= 1024 || entry->record_length < 22 + length || entry->name[length]) {
                directory_error = 22; break;
            }
            /* Reject all non-regular directory records, including links. */
            if (entry->type != 8 || length <= prefix_length + 4) continue;
            unsigned n = 0;
            for (; n < prefix_length && entry->name[n] == process[n]; ++n) {}
            if (n != prefix_length || (strcmp(entry->name + length - 4, ".ips") &&
                (length < 6 || strcmp(entry->name + length - 6, ".crash")))) continue;
            int safe = 1;
            for (n = 0; n < length; ++n) {
                char c = entry->name[n];
                if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                    (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')) safe = 0;
            }
            if (!safe) { limited = 1; continue; }
            ++matches;
            /* Keep the three greatest filenames, with bounded memory. This
             * is explicitly not a claim about parsed native crash times. */
            unsigned position = 0;
            while (position < kept && strcmp(names[position], entry->name) > 0) ++position;
            if (position < 3) {
                for (unsigned move = kept < 3 ? kept : 2; move > position; --move)
                    snprintf(names[move], sizeof(names[move]), "%s", names[move - 1]);
                snprintf(names[position], sizeof(names[position]), "%s", entry->name);
                if (kept < 3) ++kept;
            }
        }
        if (close_directory(directory) && !directory_error) directory_error = *__error();
        put(directory_row, "read", boolean(!directory_error));
        put(directory_row, "scan_complete", boolean(exhausted && !directory_error));
        put(directory_row, "scanned", number(scanned));
        put(directory_row, "matching_reports", number(matches));
        if (directory_error) put(directory_row, "errno", number((unsigned)directory_error));
        if (!exhausted || directory_error) coverage = 0;
        if (matches > kept) limited = 1;
        for (unsigned n = 0; n < kept; ++n) {
            object item = get(get_class("NSMutableDictionary"), "dictionary");
            ((void (*)(object, object, object))message)(reports, selector("addObject:"), item);
            put(item, "owner", string(owners[owner]));
            put(item, "name", string(names[n]));
            put(item, "file_consistency_verified", boolean(0));
            int file = openat(fd, names[n], 0x1000104); /* NOFOLLOW|CLOEXEC|NONBLOCK */
            if (file < 0) {
                put(item, "errno", number((unsigned)*__error())); coverage = 0; continue;
            }
            long long size = lseek(file, 0, 2);
            if (size < 0 || lseek(file, 0, 0) != 0) {
                put(item, "error", string("unseekable_report")); close(file); coverage = 0; continue;
            }
            put(item, "observed_file_bytes", number((unsigned long)size));
            unsigned cap = budget < 256 * 1024 ? budget : 256 * 1024;
            object data = get(get_class("NSMutableData"), "data");
            unsigned total = 0;
            int read_error = 0;
            char bytes[4096];
            while (total < cap) {
                unsigned remaining = cap - total;
                long got = read(file, bytes, remaining < sizeof(bytes) ? remaining : sizeof(bytes));
                if (got < 0 && *__error() == 4) continue;
                if (got < 0) { read_error = *__error(); break; }
                if (!got) break;
                ((void (*)(object, object, const void *, unsigned long))message)(data,
                    selector("appendBytes:length:"), bytes, (unsigned long)got);
                total += (unsigned)got;
            }
            long long after_size = lseek(file, 0, 2);
            if (close(file) && !read_error) read_error = *__error();
            int complete = !read_error && size == after_size && (unsigned long long)size == total;
            if (!complete) { coverage = 0; limited = 1; }
            if (read_error) put(item, "errno", number((unsigned)read_error));
            put(item, "content_base64", ((object (*)(object, object, unsigned long))message)(
                data, selector("base64EncodedStringWithOptions:"), 0));
            put(item, "content_bytes", number(total));
            put(item, "content_complete", boolean(complete));
            budget -= total;
        }
        close(fd);
    }
    put(row, "process", string(process));
    put(row, "directories", directories);
    put(row, "reports", reports);
    put(row, "collection_complete", boolean(coverage && !limited));
    put(row, "selection_order", string("descending filename within mobile then root; not native crash time"));
    put(row, "absence_proves_no_crash", boolean(0));
    put(row, "max_content_bytes", number(512 * 1024));
    put(row, "ok", boolean(1));
    return emit(row);
}
