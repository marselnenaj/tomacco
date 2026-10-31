/* Owned diagnostic exchange for the observed iOS 27 iconservices UID.
 * Darwin ARM64 stat layout: apple-oss-distributions/xnu, bsd/sys/stat.h.
 * This is a declaration of the public ABI, not an implementation of it.
 * /private/var/tmp is a candidate sink; native sandbox refusal stays a refusal.
 */
#ifndef TOMACCO_GUEST_BUFFER_LOG_H
#define TOMACCO_GUEST_BUFFER_LOG_H
#define TOMACCO_BUFFER_LOG_UID 276u
#define TOMACCO_BUFFER_LOG_EXECUTABLE "/System/Library/CoreServices/iconservicesagent"
#define TOMACCO_BUFFER_LOG_NAME "io.github.marselnenaj.tomacco.metal-buffer-texture.jsonl"
#define TOMACCO_BUFFER_LOG_PATH "/private/var/tmp/" TOMACCO_BUFFER_LOG_NAME
#define TOMACCO_BUFFER_LOG_LIMIT 131072
struct tomacco_darwin_stat {
    int device;
    unsigned short mode, links;
    unsigned long inode;
    unsigned uid, gid;
    int rdevice;
    long times[8];
    long long size, blocks;
    int block_size;
    unsigned flags, generation;
    int reserved;
    long long spare[2];
};
_Static_assert(sizeof(struct tomacco_darwin_stat)==144, "Darwin ARM64 stat size");
_Static_assert(__builtin_offsetof(struct tomacco_darwin_stat,uid)==16, "Darwin stat UID");
_Static_assert(__builtin_offsetof(struct tomacco_darwin_stat,size)==96, "Darwin stat size field");
extern int open(const char *,int,...);
extern int openat(int,const char *,int,...);
extern int close(int);
extern int fstat(int,struct tomacco_darwin_stat *);
static int tomacco_buffer_log_directory(void) {
    /* Open only the declared sink directory, without separately requesting
     * read access to /private and /private/var. Darwin O_NOFOLLOW_ANY rejects
     * symlinks anywhere in the complete path and cannot be combined with
     * O_NOFOLLOW. An unsupported flag or sandbox denial stays a refusal.
     * Public ABI: XNU bsd/sys/fcntl.h, vn_open_auth in bsd/vfs/vfs_vnops.c. */
    return open("/private/var/tmp",0x21100004);
    /* DIRECTORY|NOFOLLOW_ANY|NONBLOCK|CLOEXEC */
}
static int tomacco_buffer_log_identity(const struct tomacco_darwin_stat *info) {
    return (info->mode&0170000)==0100000 && info->uid==TOMACCO_BUFFER_LOG_UID &&
        info->links==1 && (info->mode&07777)==0644 &&
        info->size>=0 && info->size<=TOMACCO_BUFFER_LOG_LIMIT;
}
#endif
