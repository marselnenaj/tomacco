/* SPDX-License-Identifier: GPL-2.0-or-later
 * Source for the specific ARM64e callsite adapter; see restore-format-retry.S.
 * Reopening after EBUSY obtains a fresh descriptor for the virtual disk.
 * No error is changed to success; all ioctl results come from the guest kernel.
 */
extern int ioctl(int, unsigned long, ...);
extern int fcntl(int, int, ...);
extern int open(const char *, int, ...);
extern int close(int);
extern int *__error(void);
extern int usleep(unsigned int);
extern int printf(const char *, ...);
struct result { int result; int fd; };
struct result tomacco_format_retry(int fd, unsigned long command, ...) {
    __builtin_va_list args;
    __builtin_va_start(args, command);
    void *data = __builtin_va_arg(args, void *);
    __builtin_va_end(args);
    char path[1024];
    int have_path = fcntl(fd, 50, path) == 0;
    for (int attempt = 0; attempt < 50; attempt++) {
        if (fd < 0) {
            fd = open(path, 0);
            if (fd < 0) {
                int error = *__error();
                if ((error != 2 && error != 6) || attempt == 49) return (struct result){-1, fd};
                usleep(10000);
                continue;
            }
        }
        int status = ioctl(fd, command, data);
        if (status != -1) {
            if (attempt) printf("TOMACCO_FORMAT_RECOVERED fd=%d attempt=%d\n",fd,attempt+1);
            return (struct result){status,fd};
        }
        int error = *__error();
        printf("TOMACCO_FORMAT_RETRY fd=%d attempt=%d errno=%d path_ready=%d\n", fd, attempt+1, error, have_path);
        *__error() = error;
        if (error != 16 || attempt == 49 || !have_path) return (struct result){-1,fd};
        close(fd);
        fd = -1;
        usleep(10000);
    }
    return (struct result){-1,fd};
}
