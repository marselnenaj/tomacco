/* Read original iOS27 IOSurface metadata after native public texture creation
 * or a shared reference with a matched retained lookup. Target0x21e/_RB4 only.
 * UUID, executable ranges, entry bytes, selector, IMP and encoding are pinned.
 * Public functions retain their ABI; enum results are recorded as raw bits.
 * No pixels, shader, texture arguments, validation or ownership are changed.
 */
#ifndef TOMACCO_IOS_SURFACE_OBSERVER_H
#define TOMACCO_IOS_SURFACE_OBSERVER_H
#include "guest-surface-log.h"
extern unsigned IOSurfaceGetPixelFormat(void *);
extern unsigned IOSurfaceGetID(void *);
extern void *IOSurfaceLookup(unsigned);
extern void CFRelease(const void *);
extern unsigned long IOSurfaceGetPlaneCount(void *);
extern unsigned long IOSurfaceGetWidth(void *);
extern unsigned long IOSurfaceGetHeight(void *);
extern unsigned long IOSurfaceGetAllocSize(void *);
extern unsigned long IOSurfaceGetBytesPerRowOfPlane(void *,unsigned long);
extern unsigned long IOSurfaceGetBytesPerElementOfPlane(void *,unsigned long);
extern unsigned long IOSurfaceGetNumberOfComponentsOfPlane(void *,unsigned long);
extern unsigned IOSurfaceGetNameOfComponentOfPlane(void *,unsigned long,unsigned long);
extern unsigned IOSurfaceGetTypeOfComponentOfPlane(void *,unsigned long,unsigned long);
extern unsigned IOSurfaceGetRangeOfComponentOfPlane(void *,unsigned long,unsigned long);
extern unsigned long IOSurfaceGetBitDepthOfComponentOfPlane(void *,unsigned long,unsigned long);
extern unsigned long IOSurfaceGetBitOffsetOfComponentOfPlane(void *,unsigned long,unsigned long);
typedef Object (*SurfaceTextureCreator)(Object,Selector,Object,void *,unsigned long);
extern SurfaceTextureCreator get_surface_texture_creator(Method) __asm("_method_getImplementation");
extern SurfaceTextureCreator set_surface_texture_creator(Method,SurfaceTextureCreator) __asm("_method_setImplementation");
extern const char *object_getClassName(Object);
typedef unsigned (*SharedTextureCreator)(Object,Selector,unsigned,Object);
extern SharedTextureCreator get_shared_texture_creator(Method) __asm("_method_getImplementation");
extern SharedTextureCreator set_shared_texture_creator(Method,SharedTextureCreator) __asm("_method_setImplementation");
static SharedTextureCreator original_shared_texture_creator;
static unsigned shared_texture_calls,shared_texture_lookups,shared_lookup_limit_reported;
static SurfaceTextureCreator original_surface_texture_creator;
static unsigned surface_texture_observations,surface_texture_log_failures,surface_texture_calls;

static int surface_observer_process(void) {
    unsigned uid=getuid();
    return uid!=~0u && uid==geteuid();
}
static void surface_log(const char *line,unsigned size) {
    int saved=*__error(),failure=0;
    unsigned uid=geteuid();
    char name[TOMACCO_SURFACE_LOG_NAME_LIMIT];
    if (!tomacco_surface_log_name(uid,name) || !size || size>4096) { *__error()=saved;return; }
    int directory=tomacco_buffer_log_directory();
    if (directory<0) failure=*__error();
    else {
        struct tomacco_darwin_stat directory_info;
        if (fstat(directory,&directory_info)) failure=*__error();
        else if (!tomacco_surface_directory_identity(&directory_info)) failure=22;
        else {
            int created=1;
            int file=openat(directory,name,0x1000b0d,0644);
            if (file<0 && *__error()==17) {
                created=0;
                file=openat(directory,name,0x100010d);
            }
            if (file<0) failure=*__error();
            else {
                struct tomacco_darwin_stat info;
                if (flock(file,6)) failure=*__error();
                else if (fstat(file,&info)) failure=*__error();
                else if ((info.mode&0170000)!=0100000 || info.uid!=uid || info.links!=1) failure=22;
                else if (created && fchmod(file,0644)) failure=*__error();
                else if (fstat(file,&info)) failure=*__error();
                else if (!tomacco_surface_log_identity(&info,uid)) failure=22;
                else if (info.size>TOMACCO_SURFACE_LOG_LIMIT-(long long)size) failure=27;
                else {
                    long written=write(file,line,size);
                    if (written!=(long)size) failure=written<0 ? *__error() : 5;
                }
                close(file);
            }
        }
        close(directory);
    }
    if (failure && __atomic_fetch_add(&surface_texture_log_failures,1,__ATOMIC_RELAXED)<4) {
        char error[192];
        int count=snprintf(error,sizeof(error),"TOMACCO_IOSURFACE_LOG_FAILED pid=%d uid=%u errno=%d\n",
            getpid(),getuid(),failure);
        if (count>0 && (unsigned)count<sizeof(error)) (void)write(2,error,(unsigned)count);
    }
    *__error()=saved;
}
static int surface_path_hex(char hex[2049]) {
    char path[1024]={0};
    int length=proc_pidpath(getpid(),path,sizeof(path));
    hex[0]=0;
    if (length<=0 || length>=(int)sizeof(path) || path[length]!=0) return 0;
    static const char digits[]="0123456789abcdef";
    for (int k=0;k<length;++k) {
        unsigned byte=(unsigned char)path[k];
        hex[2*k]=digits[byte>>4];hex[2*k+1]=digits[byte&15];
    }
    hex[2*length]=0;
    return 1;
}
static void surface_observer_status_for(const char *state,const char *declaring_class) {
    if (!surface_observer_process()) return;
    int saved=*__error();
    char line[3072],path_hex[2049];
    int path_known=surface_path_hex(path_hex);
    int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"iosurface-observer-status\","
        "\"revision\":\"native-surface-v4\",\"pid\":%d,\"uid\":%u,\"effective_uid\":%u,\"state\":\"%s\","
        "\"declaring_class\":\"%s\",\"executable_path_known\":%s,\"executable_path_hex\":\"%s\"}\n",
        getpid(),getuid(),geteuid(),state,declaring_class,path_known ? "true" : "false",path_hex);
    if (size>0 && (unsigned)size<sizeof(line)) surface_log(line,(unsigned)size);
    *__error()=saved;
}
static void surface_observer_status(const char *state) {
    surface_observer_status_for(state,"AppleParavirtDevice");
}
static int surface_getters_match(void) {
    static const unsigned char uuid[16]={0x59,0x33,0xa7,0xc7,0xf1,0x36,0x3d,0xe3,0x83,0xf4,0x34,0xa5,0x45,0x1f,0x9c,0x1e};
    struct SurfaceGetterGuard {
        UIntPtr address;
        const void *function;
        unsigned char entry[16];
    };
    const struct SurfaceGetterGuard guards[]={
        {0x1c2845360UL,(const void *)IOSurfaceGetID,
         {0xe0,0x00,0x00,0xb4,0x48,0xa7,0x11,0xd0,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c284aa28UL,(const void *)IOSurfaceLookup,
         {0x7f,0x23,0x03,0xd5,0xf4,0x4f,0xbe,0xa9,0xfd,0x7b,0x01,0xa9,0xfd,0x43,0x00,0x91}},
        {0x1c2853a0cUL,(const void *)IOSurfaceGetNumberOfComponentsOfPlane,
         {0x00,0x01,0x00,0xb4,0xe8,0xa6,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c2853a30UL,(const void *)IOSurfaceGetNameOfComponentOfPlane,
         {0x20,0x01,0x00,0xb4,0xe8,0xa6,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c2853a58UL,(const void *)IOSurfaceGetTypeOfComponentOfPlane,
         {0x20,0x01,0x00,0xb4,0xe8,0xa6,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c2853a80UL,(const void *)IOSurfaceGetRangeOfComponentOfPlane,
         {0x20,0x01,0x00,0xb4,0xe8,0xa6,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c2853aa8UL,(const void *)IOSurfaceGetBitDepthOfComponentOfPlane,
         {0x20,0x01,0x00,0xb4,0xe8,0xa6,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c2853ad0UL,(const void *)IOSurfaceGetBitOffsetOfComponentOfPlane,
         {0x20,0x01,0x00,0xb4,0xe8,0xa6,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c2845e84UL,(const void *)IOSurfaceGetPixelFormat,
         {0xe0,0x00,0x00,0xb4,0x48,0xa7,0x11,0xd0,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c2845704UL,(const void *)IOSurfaceGetPlaneCount,
         {0xa0,0x01,0x00,0xb4,0x7f,0x23,0x03,0xd5,0xfd,0x7b,0xbf,0xa9,0xfd,0x03,0x00,0x91}},
        {0x1c2845e64UL,(const void *)IOSurfaceGetWidth,
         {0xe0,0x00,0x00,0xb4,0x48,0xa7,0x11,0xd0,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c2845eb4UL,(const void *)IOSurfaceGetHeight,
         {0xe0,0x00,0x00,0xb4,0x48,0xa7,0x11,0xd0,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c284714cUL,(const void *)IOSurfaceGetAllocSize,
         {0xe0,0x00,0x00,0xb4,0x48,0xa7,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c284716cUL,(const void *)IOSurfaceGetBytesPerRowOfPlane,
         {0xe0,0x00,0x00,0xb4,0x48,0xa7,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}},
        {0x1c284b398UL,(const void *)IOSurfaceGetBytesPerElementOfPlane,
         {0xe0,0x00,0x00,0xb4,0x28,0xa7,0x11,0x90,0x08,0xd1,0x25,0x91,0x08,0x01,0x80,0xb9}}
    };
    for (unsigned i=0,n=_dyld_image_count();i<n && i<4096;++i) {
        if (!same(_dyld_get_image_name(i),"/System/Library/Frameworks/IOSurface.framework/IOSurface")) continue;
        const unsigned char *header=_dyld_get_image_header(i);
        UIntPtr slide=(UIntPtr)_dyld_get_image_vmaddr_slide(i);
        for (unsigned k=0;k<sizeof(guards)/sizeof(guards[0]);++k) {
            UIntPtr address=(UIntPtr)__builtin_ptrauth_strip((void *)guards[k].function,0);
            if (address!=slide+guards[k].address ||
                !alignment_entry_matches(header,slide,uuid,address,guards[k].entry,sizeof(guards[k].entry))) return 0;
        }
        return 1;
    }
    return 0;
}
static void record_surface_components(void *surface,unsigned event,unsigned known,
        unsigned long format,unsigned pixel_format,unsigned long plane,int plane_known,
        const char *api,const char *source,unsigned long long *thread) {
    unsigned long planes=IOSurfaceGetPlaneCount(surface);
    unsigned long width=IOSurfaceGetWidth(surface),height=IOSurfaceGetHeight(surface);
    unsigned long allocation=IOSurfaceGetAllocSize(surface);
    int thread_status=pthread_threadid_np((void *)0,thread);
    char path_hex[2049];
    int path_known=surface_path_hex(path_hex);
    char line[3072];
    int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-iosurface-validated\","
        "\"revision\":\"native-surface-v4\",\"pid\":%d,\"uid\":%u,\"effective_uid\":%u,"
        "\"thread_id\":%llu,\"thread_id_known\":%s,\"diagnostic_event_id\":%u,"
        "\"descriptor_format_known\":%s,\"metal_pixel_format\":%lu,\"surface_pixel_format\":%u,"
        "\"api\":\"%s\",\"metadata_source\":\"%s\",\"surface_id\":%u,\"plane_known\":%s,\"plane\":%lu,\"plane_count\":%lu,"
        "\"native_texture_result_known\":%s,"
        "\"width\":%lu,\"height\":%lu,\"allocation_bytes\":%lu,\"component_planes_bounded\":%s,"
        "\"executable_path_known\":%s,\"executable_path_hex\":\"%s\"}\n",
        getpid(),getuid(),geteuid(),*thread,thread_status ? "false" : "true",event,
        known&1 ? "true" : "false",format,pixel_format,api,source,IOSurfaceGetID(surface),plane_known ? "true" : "false",plane,planes,
        same(source,"native-texture-result") ? "true" : "false",width,height,allocation,
        planes>0 && planes<=4 ? "true" : "false",path_known ? "true" : "false",path_hex);
    if (size>0 && (unsigned)size<sizeof(line)) surface_log(line,(unsigned)size);
    if (planes>0 && planes<=4) {
        for (unsigned long p=0;p<planes;++p) {
            unsigned long components=IOSurfaceGetNumberOfComponentsOfPlane(surface,p);
            unsigned long stride=IOSurfaceGetBytesPerRowOfPlane(surface,p);
            unsigned long element=IOSurfaceGetBytesPerElementOfPlane(surface,p);
            size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-iosurface-plane\","
                "\"revision\":\"native-surface-v4\",\"pid\":%d,\"thread_id\":%llu,"
                "\"diagnostic_event_id\":%u,\"plane\":%lu,\"component_count\":%lu,\"component_count_bounded\":%s,"
                "\"bytes_per_row\":%lu,\"bytes_per_element\":%lu}\n",
                getpid(),*thread,event,p,components,components>0 && components<=4 ? "true" : "false",stride,element);
            if (size>0 && (unsigned)size<sizeof(line)) surface_log(line,(unsigned)size);
            if (components>0 && components<=4) {
                for (unsigned long c=0;c<components;++c) {
                    unsigned name=IOSurfaceGetNameOfComponentOfPlane(surface,p,c);
                    unsigned type=IOSurfaceGetTypeOfComponentOfPlane(surface,p,c);
                    unsigned range=IOSurfaceGetRangeOfComponentOfPlane(surface,p,c);
                    unsigned long depth=IOSurfaceGetBitDepthOfComponentOfPlane(surface,p,c);
                    unsigned long offset=IOSurfaceGetBitOffsetOfComponentOfPlane(surface,p,c);
                    size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-iosurface-component\","
                        "\"revision\":\"native-surface-v4\",\"pid\":%d,\"thread_id\":%llu,"
                        "\"diagnostic_event_id\":%u,\"plane\":%lu,\"component\":%lu,"
                        "\"name_raw\":%u,\"type_raw\":%u,\"range_raw\":%u,\"bit_depth\":%lu,\"bit_offset\":%lu}\n",
                        getpid(),*thread,event,p,c,name,type,range,depth,offset);
                    if (size>0 && (unsigned)size<sizeof(line)) surface_log(line,(unsigned)size);
                }
            }
        }
    }
}
static Object observed_surface_texture(Object self,Selector selector,Object descriptor,
                                       void *surface,unsigned long plane) {
    Object result=original_surface_texture_creator(self,selector,descriptor,surface,plane);
    int native_errno=*__error();
    unsigned event=0,known=0,pixel_format=0,call=0;
    unsigned long format=0;
    unsigned long long thread=0;
    int eligible=surface_observer_process();
    if (eligible && __atomic_load_n(&surface_texture_calls,__ATOMIC_RELAXED)<4) {
        unsigned sequence=__atomic_fetch_add(&surface_texture_calls,1,__ATOMIC_RELAXED);
        if (sequence<4) call=sequence+1;
    }
    if (result && surface && eligible &&
        (call || __atomic_load_n(&surface_texture_observations,__ATOMIC_RELAXED)<8)) {
        format=observed_property(descriptor,"pixelFormat",&known,0);
        pixel_format=IOSurfaceGetPixelFormat(surface);
        if (((known&1) && format==0x21eUL) || pixel_format==0x5f524234u) {
            if (__atomic_load_n(&surface_texture_observations,__ATOMIC_RELAXED)<8) {
                unsigned sequence=__atomic_fetch_add(&surface_texture_observations,1,__ATOMIC_RELAXED);
                if (sequence<8) event=sequence+1;
            }
        }
    }
    if (call) {
        char line[4096],path_hex[2049],class_hex[513]={0};
        int path_known=surface_path_hex(path_hex);
        const char *class_name=object_getClassName(self);
        unsigned length=0;
        while (class_name && length<256 && class_name[length]) ++length;
        int class_known=class_name && length<256;
        if (class_known) {
            static const char digits[]="0123456789abcdef";
            for (unsigned k=0;k<length;++k) {
                unsigned byte=(unsigned char)class_name[k];
                class_hex[2*k]=digits[byte>>4];class_hex[2*k+1]=digits[byte&15];
            }
        }
        int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-iosurface-api-call\","
            "\"revision\":\"native-surface-v4\",\"pid\":%d,\"uid\":%u,\"effective_uid\":%u,\"diagnostic_call_id\":%u,"
            "\"api\":\"newTextureWithDescriptor:iosurface:plane:\",\"plane\":%lu,"
            "\"descriptor_nonnull\":%s,\"surface_nonnull\":%s,\"native_texture_nonnull\":%s,"
            "\"descriptor_format_known\":%s,\"surface_format_known\":%s,\"metal_pixel_format\":%lu,\"surface_pixel_format\":%u,"
            "\"target_metadata_selected\":%s,\"device_class_known\":%s,\"device_class_hex\":\"%s\","
            "\"executable_path_known\":%s,\"executable_path_hex\":\"%s\"}\n",
            getpid(),getuid(),geteuid(),call,plane,descriptor ? "true" : "false",surface ? "true" : "false",
            result ? "true" : "false",known&1 ? "true" : "false",result && surface ? "true" : "false",
            format,pixel_format,event ? "true" : "false",class_known ? "true" : "false",class_hex,
            path_known ? "true" : "false",path_hex);
        if (size>0 && (unsigned)size<sizeof(line)) surface_log(line,(unsigned)size);
    }
    if (event) record_surface_components(surface,event,known,format,pixel_format,plane,1,
        "newTextureWithDescriptor:iosurface:plane:","native-texture-result",&thread);
    if (event) {
        char line[384];
        int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-iosurface-creation-result\","
            "\"revision\":\"native-surface-v4\",\"pid\":%d,\"thread_id\":%llu,"
            "\"diagnostic_event_id\":%u,\"native_texture_nonnull\":%s}\n",
            getpid(),thread,event,result ? "true" : "false");
        if (size>0 && (unsigned)size<sizeof(line)) surface_log(line,(unsigned)size);
    }
    *__error()=native_errno;
    return result;
}
/* This serializer API returns a reference number, not a texture object. The
 * native retained lookup owns its surface independently of the caller's later
 * validation. Null/mismatched lookups do not establish any surface metadata. */
static unsigned observed_shared_texture(Object self,Selector selector,unsigned surface_id,Object allocator) {
    unsigned result=original_shared_texture_creator(self,selector,surface_id,allocator);
    int native_errno=*__error();
    int eligible=surface_observer_process();
    unsigned call=0,event=0,pixel_format=0,looked_up_id=0;
    const char *lookup_state="not-attempted";
    if (eligible && __atomic_load_n(&shared_texture_calls,__ATOMIC_RELAXED)<4) {
        unsigned sequence=__atomic_fetch_add(&shared_texture_calls,1,__ATOMIC_RELAXED);
        if (sequence<4) call=sequence+1;
    }
    void *surface=(void *)0;
    if (result && eligible && (call || __atomic_load_n(&surface_texture_observations,__ATOMIC_RELAXED)<8)) {
        unsigned sequence=32;
        if (__atomic_load_n(&shared_texture_lookups,__ATOMIC_RELAXED)<32)
            sequence=__atomic_fetch_add(&shared_texture_lookups,1,__ATOMIC_RELAXED);
        if (sequence<32) {
            surface=IOSurfaceLookup(surface_id);
            lookup_state="null-result";
        } else {
            lookup_state="budget-exhausted";
            if (!__atomic_exchange_n(&shared_lookup_limit_reported,1,__ATOMIC_RELAXED))
                surface_observer_status_for("shared-lookup-budget-exhausted","PGSerializer");
        }
        if (surface) {
            looked_up_id=IOSurfaceGetID(surface);
            lookup_state="id-mismatch";
            if (looked_up_id==surface_id) {
                lookup_state="matched-id";
                pixel_format=IOSurfaceGetPixelFormat(surface);
                if (pixel_format==0x5f524234u &&
                    __atomic_load_n(&surface_texture_observations,__ATOMIC_RELAXED)<8) {
                    unsigned sequence=__atomic_fetch_add(&surface_texture_observations,1,__ATOMIC_RELAXED);
                    if (sequence<8) event=sequence+1;
                }
            }
        }
    }
    if (call) {
        char line[3072],path_hex[2049];
        int path_known=surface_path_hex(path_hex);
        int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-iosurface-shared-api-call\","
            "\"revision\":\"native-surface-v4\",\"pid\":%d,\"uid\":%u,\"effective_uid\":%u,\"diagnostic_call_id\":%u,"
            "\"api\":\"newSharedTextureWithHandle:allocator:\",\"surface_id_argument\":%u,\"native_reference_result\":%u,"
            "\"native_texture_result_known\":false,\"lookup_state\":\"%s\",\"lookup_surface_id\":%u,"
            "\"surface_pixel_format\":%u,\"target_metadata_selected\":%s,\"id_generation_bound\":false,"
            "\"executable_path_known\":%s,\"executable_path_hex\":\"%s\"}\n",
            getpid(),getuid(),geteuid(),call,surface_id,result,lookup_state,looked_up_id,pixel_format,
            event ? "true" : "false",path_known ? "true" : "false",path_hex);
        if (size>0 && (unsigned)size<sizeof(line)) surface_log(line,(unsigned)size);
    }
    unsigned long long thread=0;
    if (event) {
        record_surface_components(surface,event,0,0,pixel_format,0,0,
            "newSharedTextureWithHandle:allocator:","native-retained-lookup",&thread);
        char line[640];
        int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-iosurface-shared-reference-result\","
            "\"revision\":\"native-surface-v4\",\"pid\":%d,\"thread_id\":%llu,\"diagnostic_event_id\":%u,"
            "\"surface_id_argument\":%u,\"lookup_surface_id\":%u,\"native_reference_result\":%u,"
            "\"native_texture_result_known\":false,\"id_generation_bound\":false}\n",
            getpid(),thread,event,surface_id,looked_up_id,result);
        if (size>0 && (unsigned)size<sizeof(line)) surface_log(line,(unsigned)size);
    }
    if (surface) CFRelease(surface);
    *__error()=native_errno;
    return result;
}
static void configure_shared_surface_observation(UIntPtr image,UIntPtr slide,const unsigned char *uuid) {
    Object cls=objc_getClass("PGSerializer");
    Method method=cls ? class_getInstanceMethod(cls,sel_registerName("newSharedTextureWithHandle:allocator:")) : (Method)0;
    if (!method || !same(method_getTypeEncoding(method),"I28@0:8I16@20")) {
        surface_observer_status_for("shared-method-contract-mismatch","PGSerializer");return;
    }
    SharedTextureCreator found=get_shared_texture_creator(method);
    UIntPtr address=(UIntPtr)__builtin_ptrauth_strip((void *)found,0);
    static const unsigned char entry[16]={0x08,0xa4,0x40,0x39,0x1f,0x05,0x00,0x71,0xe1,0x02,0x00,0x54,0x7f,0x23,0x03,0xd5};
    if (address!=image+0xc570UL ||
        !alignment_entry_matches((const unsigned char *)image,slide,uuid,address,entry,sizeof(entry))) {
        surface_observer_status_for("shared-image-contract-mismatch","PGSerializer");return;
    }
    original_shared_texture_creator=found;
    set_shared_texture_creator(method,observed_shared_texture);
    surface_observer_status_for("shared-method-installed","PGSerializer");
}
static void configure_surface_observation(UIntPtr image) {
    if (!surface_observer_process()) return;
    Object cls=objc_getClass("AppleParavirtDevice");
    Method method=cls ? class_getInstanceMethod(cls,sel_registerName("newTextureWithDescriptor:iosurface:plane:")) : (Method)0;
    if (!method || !same(method_getTypeEncoding(method),"@40@0:8@16^{__IOSurface=}24Q32")) {
        surface_observer_status("surface-method-contract-mismatch");return;
    }
    SurfaceTextureCreator found=get_surface_texture_creator(method);
    UIntPtr address=(UIntPtr)__builtin_ptrauth_strip((void *)found,0);
    static const unsigned char uuid[16]={0x7e,0x55,0xcc,0xea,0x96,0x21,0x3a,0xf3,0x93,0x96,0xe2,0x85,0xab,0x7d,0xee,0x23};
    static const unsigned char entry[16]={0x7f,0x23,0x03,0xd5,0xf6,0x57,0xbd,0xa9,0xf4,0x4f,0x01,0xa9,0xfd,0x7b,0x02,0xa9};
    int driver_ok=0;
    UIntPtr driver_slide=0;
    for (unsigned i=0,n=_dyld_image_count();i<n && i<4096;++i) {
        if ((UIntPtr)_dyld_get_image_header(i)!=image) continue;
        driver_slide=(UIntPtr)_dyld_get_image_vmaddr_slide(i);
        driver_ok=address==image+0x1d7c0UL &&
            alignment_entry_matches((const unsigned char *)image,driver_slide,
                                    uuid,address,entry,sizeof(entry));
        break;
    }
    if (!driver_ok || !surface_getters_match()) {
        surface_observer_status("surface-image-contract-mismatch");return;
    }
    original_surface_texture_creator=found;
    set_surface_texture_creator(method,observed_surface_texture);
    surface_observer_status("surface-method-installed");
    configure_shared_surface_observation(image,driver_slide,uuid);
}
#endif

