extern long write(int, const void *, unsigned long);
extern int tomacco_swift_main(long count);

long tomacco_write(const void *bytes, long count) {
    return write(1, bytes, count);
}

int main(int argc, char **argv) {
    long count = 100;
    if (argc > 2) return 7;
    if (argc == 2) {
        count = 0;
        if (!argv[1][0]) return 7;
        for (const char *p = argv[1]; *p; ++p) {
            if (*p < '0' || *p > '9' || count > 1000) return 7;
            count = count * 10 + *p - '0';
        }
    }
    return tomacco_swift_main(count);
}
