/* Synthetic IOSurface/public ABI boundary. This is owned test data and
 * cannot establish any Apple component ordinal or real guest behavior. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef void *Object;
typedef void *Selector;
typedef void *Method;
typedef unsigned long UIntPtr;
static unsigned test_uid=501;
static unsigned test_euid=501;
static int guest_errno,original_calls,setter_calls,component_queries;
static int property_queries,pixel_queries;
static unsigned lookup_calls,lookup_releases,shared_calls;
static unsigned shared_result=91,lookup_id=17;
static int lookup_null;
static int malformed_directory,malformed_file,null_result;
static unsigned long test_planes=2,test_components[4]={1,2,0,0};
static unsigned test_format=542,test_fourcc=0x5f524234;
static char captured[131072];
static unsigned captured_bytes;
static int fake_surface,fake_descriptor,fake_self,fake_return,fake_allocator,fake_shared_method;
static unsigned char fake_image[64];
extern unsigned getuid(void);
extern unsigned geteuid(void);
extern int getpid(void);
extern int *__error(void);
extern int fchmod(int,unsigned short);
extern int flock(int,int);
extern long write(int,const void *,unsigned long);
extern int proc_pidpath(int,void *,unsigned);
extern int pthread_threadid_np(void *,unsigned long long *);
extern unsigned _dyld_image_count(void);
extern const unsigned char *_dyld_get_image_header(unsigned);
extern const char *_dyld_get_image_name(unsigned);
extern long _dyld_get_image_vmaddr_slide(unsigned);
extern Object objc_getClass(const char *);
extern Selector sel_registerName(const char *);
extern Method class_getInstanceMethod(Object,Selector);
extern const char *method_getTypeEncoding(Method);
static int same(const char *a,const char *b) { return a && !strcmp(a,b); }
static unsigned long observed_property(Object value,const char *name,unsigned *known,unsigned bit) {
    assert(value==&fake_descriptor && !strcmp(name,"pixelFormat"));
    property_queries++;*known|=1u<<bit;guest_errno=71;return test_format;
}
static int alignment_entry_matches(const unsigned char *header,UIntPtr slide,
        const unsigned char *uuid,UIntPtr address,const unsigned char *entry,unsigned bytes) {
    (void)header;(void)slide;(void)uuid;(void)address;(void)entry;(void)bytes;return 0;
}
#define __asm(name)
#define __builtin_ptrauth_strip(pointer,key) (pointer)
#include "../../integrations/ios-emulator/ios-surface-observer.h"
#undef __asm
#undef __builtin_ptrauth_strip

unsigned getuid(void) { return test_uid; }
unsigned geteuid(void) { return test_euid; }
int getpid(void) { return 7; }
int *__error(void) { return &guest_errno; }
int open(const char *name,int flags,...) { assert(!strcmp(name,"/private/var/tmp"));(void)flags;return 10; }
int openat(int directory,const char *name,int flags,...) {
    char expected[TOMACCO_SURFACE_LOG_NAME_LIMIT];
    assert(tomacco_surface_log_name(test_uid,expected));
    assert(directory==10 && !strcmp(name,expected));(void)flags;return 11;
}
int close(int file) { assert(file==10 || file==11);guest_errno=72;return 0; }
int fstat(int file,struct tomacco_darwin_stat *info) {
    memset(info,0,sizeof(*info));
    if (file==10) { info->mode=0041777;info->uid=malformed_directory ? 501 : 0; }
    else {
        assert(file==11);info->mode=0100644;info->uid=malformed_file ? 1000 : test_uid;
        info->links=1;info->size=captured_bytes;
    }
    return 0;
}
int fchmod(int file,unsigned short mode) { assert(file==11 && mode==0644);return 0; }
int flock(int file,int mode) { assert(file==11 && mode==6);return 0; }
long write(int file,const void *data,unsigned long size) {
    guest_errno=73;
    if (file==2) return (long)size;
    assert(file==11 && captured_bytes+size<sizeof(captured));
    memcpy(captured+captured_bytes,data,size);captured_bytes+=(unsigned)size;captured[captured_bytes]=0;
    return (long)size;
}
int proc_pidpath(int pid,void *buffer,unsigned size) {
    const char *path="/test/\"surface\"";assert(pid==7 && strlen(path)+1<size);
    strcpy(buffer,path);guest_errno=74;return (int)strlen(path);
}
int pthread_threadid_np(void *thread,unsigned long long *identifier) {
    assert(!thread);*identifier=55;guest_errno=75;return 0;
}
unsigned _dyld_image_count(void) { return 1; }
const unsigned char *_dyld_get_image_header(unsigned index) { assert(!index);return fake_image; }
const char *_dyld_get_image_name(unsigned index) {
    assert(!index);return "/System/Library/Frameworks/IOSurface.framework/IOSurface";
}
long _dyld_get_image_vmaddr_slide(unsigned index) { assert(!index);return 0; }
Object objc_getClass(const char *name) {
    assert(!strcmp(name,"AppleParavirtDevice") || !strcmp(name,"PGSerializer"));return &fake_self;
}
const char *object_getClassName(Object value) {
    assert(value==&fake_self);guest_errno=78;return "ActualDeviceSubclass";
}
Selector sel_registerName(const char *name) { return (Selector)name; }
Method class_getInstanceMethod(Object cls,Selector selector) {
    assert(cls==&fake_self);
    if (!strcmp(selector,"newSharedTextureWithHandle:allocator:")) return &fake_shared_method;
    assert(!strcmp(selector,"newTextureWithDescriptor:iosurface:plane:"));
    return &fake_self;
}
const char *method_getTypeEncoding(Method method) {
    if (method==&fake_shared_method) return "I28@0:8I16@20";
    assert(method==&fake_self);return "@40@0:8@16^{__IOSurface=}24Q32";
}
static Object forwarded(Object self,Selector selector,Object descriptor,
        void *surface,unsigned long plane) {
    assert(self==&fake_self && !strcmp(selector,"constructor"));
    assert(descriptor==&fake_descriptor && (!surface || surface==&fake_surface));
    assert(plane==1 && guest_errno==42);original_calls++;guest_errno=43;
    return null_result ? 0 : &fake_return;
}
SurfaceTextureCreator get_surface_texture_creator(Method method) { assert(method==&fake_self);return forwarded; }
SurfaceTextureCreator set_surface_texture_creator(Method method,SurfaceTextureCreator function) {
    assert(method==&fake_self);setter_calls++;return function;
}
unsigned IOSurfaceGetPixelFormat(void *surface) { assert(surface==&fake_surface);pixel_queries++;guest_errno=76;return test_fourcc; }
unsigned IOSurfaceGetID(void *surface) { assert(surface==&fake_surface);guest_errno=80;return lookup_id; }
void *IOSurfaceLookup(unsigned identifier) {
    assert(identifier==17);lookup_calls++;guest_errno=81;return lookup_null ? 0 : &fake_surface;
}
void CFRelease(const void *surface) { assert(surface==&fake_surface);lookup_releases++;guest_errno=82; }
static unsigned forwarded_shared(Object self,Selector selector,unsigned identifier,Object allocator) {
    assert(self==&fake_self && !strcmp(selector,"shared-constructor"));
    assert(identifier==17 && allocator==&fake_allocator && guest_errno==42);
    shared_calls++;guest_errno=43;return shared_result;
}
SharedTextureCreator get_shared_texture_creator(Method method) { assert(method==&fake_shared_method);return forwarded_shared; }
SharedTextureCreator set_shared_texture_creator(Method method,SharedTextureCreator function) {
    assert(method==&fake_shared_method);setter_calls++;return function;
}
unsigned long IOSurfaceGetPlaneCount(void *surface) { assert(surface==&fake_surface);return test_planes; }
unsigned long IOSurfaceGetWidth(void *surface) { assert(surface==&fake_surface);return 320; }
unsigned long IOSurfaceGetHeight(void *surface) { assert(surface==&fake_surface);return 320; }
unsigned long IOSurfaceGetAllocSize(void *surface) { assert(surface==&fake_surface);return 307200; }
unsigned long IOSurfaceGetBytesPerRowOfPlane(void *surface,unsigned long plane) {
    assert(surface==&fake_surface && plane<test_planes);return 320*(plane+1);
}
unsigned long IOSurfaceGetBytesPerElementOfPlane(void *surface,unsigned long plane) {
    assert(surface==&fake_surface && plane<test_planes);return plane+1;
}
unsigned long IOSurfaceGetNumberOfComponentsOfPlane(void *surface,unsigned long plane) {
    assert(surface==&fake_surface && plane<test_planes && plane<4);return test_components[plane];
}
static void component(void *surface,unsigned long plane,unsigned long index) {
    assert(surface==&fake_surface && plane<test_planes && plane<4 && index<test_components[plane] && index<4);
    component_queries++;guest_errno=77;
}
unsigned IOSurfaceGetNameOfComponentOfPlane(void *surface,unsigned long plane,unsigned long index) {
    component(surface,plane,index);return (unsigned)(100+plane*10+index);
}
unsigned IOSurfaceGetTypeOfComponentOfPlane(void *surface,unsigned long plane,unsigned long index) {
    component(surface,plane,index);return 0xffffffffu;
}
unsigned IOSurfaceGetRangeOfComponentOfPlane(void *surface,unsigned long plane,unsigned long index) {
    component(surface,plane,index);return 888;
}
unsigned long IOSurfaceGetBitDepthOfComponentOfPlane(void *surface,unsigned long plane,unsigned long index) {
    component(surface,plane,index);return 8;
}
unsigned long IOSurfaceGetBitOffsetOfComponentOfPlane(void *surface,unsigned long plane,unsigned long index) {
    component(surface,plane,index);return index*8;
}
static void reset(void) {
    captured_bytes=0;captured[0]=0;component_queries=original_calls=setter_calls=0;
    malformed_directory=malformed_file=null_result=0;test_uid=test_euid=501;
    property_queries=pixel_queries=0;
    lookup_calls=lookup_releases=shared_calls=0;shared_result=91;lookup_id=17;lookup_null=0;
    test_planes=2;test_components[0]=1;test_components[1]=2;test_format=542;test_fourcc=0x5f524234;
    surface_texture_observations=surface_texture_log_failures=surface_texture_calls=0;
    shared_texture_calls=shared_texture_lookups=shared_lookup_limit_reported=0;
    original_shared_texture_creator=forwarded_shared;
    original_surface_texture_creator=forwarded;guest_errno=42;
}
static void invoke(void *surface) {
    guest_errno=42;
    Object result=observed_surface_texture(&fake_self,"constructor",&fake_descriptor,surface,1);
    assert(result==(null_result ? 0 : &fake_return) && guest_errno==43);
}
static void invoke_shared(void) {
    guest_errno=42;
    unsigned result=observed_shared_texture(&fake_self,"shared-constructor",17,&fake_allocator);
    assert(result==shared_result && guest_errno==43);
}
int main(void) {
    char name[TOMACCO_SURFACE_LOG_NAME_LIMIT];unsigned uid=123;
    for (unsigned i=0;i<5;++i) {
        unsigned owners[]={0,276,501,237,4294967294u};
        assert(tomacco_surface_log_name(owners[i],name));
        assert(tomacco_surface_log_owner(name,&uid) && uid==owners[i]);
    }
    assert(!tomacco_surface_log_name(~0u,name));
    const char *invalid[]={"",TOMACCO_SURFACE_LOG_PREFIX TOMACCO_SURFACE_LOG_SUFFIX,
        TOMACCO_SURFACE_LOG_PREFIX "0501" TOMACCO_SURFACE_LOG_SUFFIX,
        TOMACCO_SURFACE_LOG_PREFIX "4294967295" TOMACCO_SURFACE_LOG_SUFFIX,
        TOMACCO_SURFACE_LOG_PREFIX "42949672940" TOMACCO_SURFACE_LOG_SUFFIX,
        TOMACCO_SURFACE_LOG_PREFIX "501-v3.jsonl",
        TOMACCO_SURFACE_LOG_PREFIX "501-v4.jsonl/other",
        TOMACCO_SURFACE_LOG_PREFIX "../501-v4.jsonl"};
    for (unsigned i=0;i<sizeof(invalid)/sizeof(invalid[0]);++i) {
        uid=123;assert(!tomacco_surface_log_owner(invalid[i],&uid) && uid==123);
    }
    struct tomacco_darwin_stat legacy={0};legacy.mode=0100644;legacy.uid=276;legacy.links=1;
    assert(tomacco_buffer_log_identity(&legacy));legacy.mode=0100600;
    assert(!tomacco_buffer_log_identity(&legacy));
    reset();invoke(&fake_surface);
    assert(original_calls==1 && component_queries==15 && surface_texture_observations==1);
    assert(strstr(captured,"\"metal_pixel_format\":542") && strstr(captured,"\"type_raw\":4294967295"));
    assert(strstr(captured,"\"bit_offset\":8") && strstr(captured,"2f746573742f227375726661636522"));
    assert(strstr(captured,"\"native_texture_nonnull\":true"));
    assert(strstr(captured,"\"operation\":\"native-iosurface-api-call\""));
    assert(strstr(captured,"41637475616c446576696365537562636c617373"));
    reset();null_result=1;invoke(&fake_surface);assert(original_calls==1);
    assert(captured_bytes && !component_queries && !property_queries && !pixel_queries);
    assert(strstr(captured,"\"surface_format_known\":false")); /* Refusal only records call coverage. */
    reset();for (int index=0;index<12;index++) invoke(&fake_surface);
    assert(original_calls==12 && surface_texture_observations==8 && component_queries==120);
    assert(surface_texture_calls==4 && property_queries==8 && pixel_queries==8);
    reset();invoke(0);assert(original_calls==1 && captured_bytes && !component_queries && !pixel_queries);
    reset();test_uid=1000;invoke(&fake_surface);assert(original_calls==1 && !captured_bytes && !component_queries);
    reset();test_uid=test_euid=~0u;invoke(&fake_surface);assert(original_calls==1 && !captured_bytes && !component_queries);
    reset();test_format=80;test_fourcc=0;
    for (unsigned k=0;k<20;++k) invoke(&fake_surface);
    assert(captured_bytes && !component_queries && surface_texture_calls==4 && !surface_texture_observations);
    assert(strstr(captured,"\"target_metadata_selected\":false"));
    reset();test_format=80;invoke(&fake_surface);assert(component_queries==15); /* _RB4 alone selects observation. */
    for (unsigned i=0;i<4;++i) {
        unsigned owners[]={0,276,237,1000};
        reset();test_uid=test_euid=owners[i];invoke(&fake_surface);assert(component_queries==15);
    }
    for (int invalid=0;invalid<2;invalid++) {
        reset();test_planes=invalid ? 5 : 0;invoke(&fake_surface);
        assert(!component_queries && strstr(captured,"\"component_planes_bounded\":false"));
    }
    reset();test_components[0]=5;test_components[1]=0;invoke(&fake_surface);
    assert(!component_queries && strstr(captured,"\"component_count_bounded\":false"));
    reset();malformed_directory=1;invoke(&fake_surface);assert(original_calls==1 && !captured_bytes);
    reset();malformed_file=1;invoke(&fake_surface);assert(original_calls==1 && !captured_bytes);
    reset();configure_surface_observation((UIntPtr)fake_image);
    assert(!setter_calls && strstr(captured,"surface-image-contract-mismatch"));
    reset();invoke_shared();
    assert(shared_calls==1 && lookup_calls==1 && lookup_releases==1 && component_queries==15 && !property_queries);
    assert(strstr(captured,"\"native_reference_result\":91") && strstr(captured,"\"surface_id_argument\":17"));
    assert(strstr(captured,"\"plane_known\":false") && strstr(captured,"\"native_texture_result_known\":false"));
    assert(strstr(captured,"native-retained-lookup") && strstr(captured,"\"descriptor_format_known\":false"));
    reset();shared_result=0;invoke_shared();assert(shared_calls==1 && !lookup_calls && !lookup_releases && !component_queries);
    reset();lookup_null=1;invoke_shared();assert(shared_calls==1 && lookup_calls==1 && !lookup_releases && !component_queries);
    assert(strstr(captured,"\"lookup_state\":\"null-result\""));
    reset();lookup_id=18;invoke_shared();assert(lookup_calls==1 && lookup_releases==1 && !pixel_queries && !component_queries);
    assert(strstr(captured,"\"lookup_state\":\"id-mismatch\""));
    reset();test_uid=1000;invoke_shared();assert(shared_calls==1 && !lookup_calls && !captured_bytes);
    reset();for (unsigned k=0;k<12;++k) invoke_shared();
    assert(shared_calls==12 && shared_texture_calls==4 && surface_texture_observations==8);
    assert(lookup_calls==8 && lookup_releases==8 && component_queries==120);
    reset();for (unsigned k=0;k<4;++k) invoke(&fake_surface);
    for (unsigned k=0;k<8;++k) invoke_shared();
    assert(surface_texture_observations==8 && lookup_calls==4 && lookup_releases==4 && component_queries==120);
    reset();malformed_directory=1;invoke_shared();assert(shared_calls==1 && lookup_releases==1 && !captured_bytes);
    reset();malformed_file=1;invoke_shared();assert(shared_calls==1 && lookup_releases==1 && !captured_bytes);
    reset();test_fourcc=0;
    for (unsigned k=0;k<80;++k) invoke_shared();
    assert(shared_calls==80 && lookup_calls==32 && lookup_releases==32 && !component_queries);
    assert(strstr(captured,"shared-lookup-budget-exhausted"));
    reset();static const unsigned char uuid[16]={0};
    configure_shared_surface_observation((UIntPtr)fake_image,0,uuid);
    assert(!setter_calls && strstr(captured,"shared-image-contract-mismatch") && strstr(captured,"PGSerializer"));
    return 0;
}
