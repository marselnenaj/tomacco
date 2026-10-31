/* Bounded metadata exchange for the private IOSurface observer. Component
 * names/types/ranges remain raw native enum values; no pixel data is read.
 * Per-UID files retain each native diagnostic owner. Canonical decimal names
 * are derived from native credentials; a sandbox refusal is retained.
 */
#ifndef TOMACCO_GUEST_SURFACE_LOG_H
#define TOMACCO_GUEST_SURFACE_LOG_H
#include "guest-buffer-log.h"
#define TOMACCO_SURFACE_LOG_LIMIT 131072
#define TOMACCO_SURFACE_LOG_VIEW_LIMIT 32768
#define TOMACCO_SURFACE_LOG_TOTAL_VIEW_LIMIT 131072
#define TOMACCO_SURFACE_LOG_OWNER_LIMIT 16
#define TOMACCO_SURFACE_LOG_NAME_LIMIT 96
#define TOMACCO_SURFACE_LOG_PREFIX "io.github.marselnenaj.tomacco.iosurface-components-"
#define TOMACCO_SURFACE_LOG_SUFFIX "-v4.jsonl"
static inline int tomacco_surface_log_name(unsigned uid,char name[TOMACCO_SURFACE_LOG_NAME_LIMIT]) {
    if (uid==~0u) return 0;
    const char *prefix=TOMACCO_SURFACE_LOG_PREFIX,*suffix=TOMACCO_SURFACE_LOG_SUFFIX;
    unsigned size=0,count=0,value=uid;
    char digits[10];
    while (*prefix) name[size++]=*prefix++;
    do { digits[count++]=(char)('0'+value%10);value/=10; } while (value);
    while (count) name[size++]=digits[--count];
    do { name[size++]=*suffix; } while (*suffix++);
    return 1;
}
static inline int tomacco_surface_log_owner(const char *name,unsigned *uid) {
    if (!name || !uid) return 0;
    const char *prefix=TOMACCO_SURFACE_LOG_PREFIX,*suffix=TOMACCO_SURFACE_LOG_SUFFIX;
    while (*prefix) if (*name++!=*prefix++) return 0;
    if (*name<'0' || *name>'9' || (*name=='0' && name[1]>='0' && name[1]<='9')) return 0;
    unsigned value=0;
    while (*name>='0' && *name<='9') {
        unsigned digit=(unsigned)(*name++-'0');
        if (value>(~0u-1u-digit)/10u) return 0;
        value=value*10u+digit;
    }
    do { if (*name++!=*suffix) return 0; } while (*suffix++);
    *uid=value;
    return 1;
}
static inline int tomacco_surface_directory_identity(const struct tomacco_darwin_stat *info) {
    return (info->mode&0170000)==0040000 && info->uid==0 &&
        (info->mode&07777)==01777;
}
static inline int tomacco_surface_log_identity(const struct tomacco_darwin_stat *info,unsigned uid) {
    return uid!=~0u && (info->mode&0170000)==0100000 &&
        info->uid==uid && info->links==1 && (info->mode&07777)==0644 &&
        info->size>=0 && info->size<=TOMACCO_SURFACE_LOG_LIMIT;
}
#endif
