/* iOS 27.0.1 / Metal 15BB7854-D1F3-33DA-8121-C3CC4C472D1D compatibility.
 * Loaded only by the locally adapted, hash-pinned PCC graphics bundle.
 * Select the original in-process compiler on a fresh scheduler and give its
 * path validator a System/Library alias resolving to the same Preboot file.
 * No shader, device, compilation result or framebuffer is synthesized.
 * The original method and arguments are retained for every other plugin.
 */
#include "ios-gpu-path.h"
#include "guest-buffer-log.h"
typedef void *Object;
typedef void *Selector;
typedef void *Method;
typedef Object (*Initializer)(Object, Selector, Object, Object, Object, Object, unsigned long);
extern Object objc_getClass(const char *);
extern Selector sel_registerName(const char *);
extern Method class_getInstanceMethod(Object, Selector);
extern Initializer method_getImplementation(Method);
extern Initializer method_setImplementation(Method, Initializer);
extern const char *string_utf8(Object,Selector) __asm("_objc_msgSend");
extern Object string_create(Object,Selector,const char *) __asm("_objc_msgSend");
extern unsigned char *connection_manager(Object,Selector,int) __asm("_objc_msgSend");
extern unsigned long read_property(Object,Selector) __asm("_objc_msgSend");
extern signed char validate_descriptor(Object,Selector,Object) __asm("_objc_msgSend");
extern Object copy_object(Object,Selector) __asm("_objc_msgSend");
extern void release_object(Object,Selector) __asm("_objc_msgSend");
extern void set_property(Object,Selector,unsigned long) __asm("_objc_msgSend");
extern const char *getprogname(void);
extern unsigned getuid(void);
extern unsigned geteuid(void);
extern int getpid(void);
extern int proc_pidpath(int,void *,unsigned);
extern int pthread_threadid_np(void *,unsigned long long *);
extern void *pthread_getspecific(unsigned long);
extern int open(const char *,int,...);
extern int close(int);
extern long long lseek(int,long long,int);
extern long write(int,const void *,unsigned long);
extern int snprintf(char *,unsigned long,const char *,...);
extern int *__error(void);
extern int fchmod(int,unsigned short);
extern int flock(int,int);
static Initializer original;
static int same(const char *a, const char *b) {
    if (!a) return 0;
    while (*a && *a == *b) { ++a; ++b; }
    return *a == *b;
}
typedef unsigned long UIntPtr;
extern unsigned _dyld_image_count(void);
extern const unsigned char *_dyld_get_image_header(unsigned);
extern const char *_dyld_get_image_name(unsigned);
extern long _dyld_get_image_vmaddr_slide(unsigned);
static unsigned word(const unsigned char *p) {
    return p[0] | (unsigned)p[1]<<8 | (unsigned)p[2]<<16 | (unsigned)p[3]<<24;
}
static int equal_bytes(const unsigned char *a, const unsigned char *b, unsigned n) {
    for (unsigned i=0;i<n;++i) if (a[i]!=b[i]) return 0;
    return 1;
}
static void configure_compiler(Object device) {
    if (!device) return;
    static const unsigned char uuid[16]={0x15,0xbb,0x78,0x54,0xd1,0xf3,0x33,0xda,0x81,0x21,0xc3,0xcc,0x4c,0x47,0x2d,0x1d};
    static const unsigned char getter[8]={0x00,0xf0,0x40,0x39,0xc0,0x03,0x5f,0xd6};
    for (unsigned i=0,n=_dyld_image_count();i<n;++i) {
        if (!same(_dyld_get_image_name(i),"/System/Library/Frameworks/Metal.framework/Metal")) continue;
        const unsigned char *header=_dyld_get_image_header(i);
        if (!header || word(header)!=0xfeedfacf) return;
        unsigned ncmd=word(header+16),size=word(header+20),offset=32;
        if (ncmd>256 || size<8 || size>65536) return;
        int matched=0;
        for (unsigned c=0;c<ncmd;++c) {
            if (offset>32+size-8) return;
            unsigned cmd=word(header+offset),length=word(header+offset+4);
            if (length<8 || length>32+size-offset) return;
            if (cmd==0x1b && length==24 && equal_bytes(header+offset+8,uuid,16)) matched=1;
            offset+=length;
        }
        if (!matched) return;
        UIntPtr slide=(UIntPtr)_dyld_get_image_vmaddr_slide(i);
        if (!equal_bytes((const unsigned char *)(slide+0x1a55cc82cUL),getter,8)) return;
        unsigned char *manager=connection_manager(
            device,sel_registerName("getCompilerConnectionManager:"),32023);
        if (!manager || word(manager+0x38)!=32023) return;
        UIntPtr vtable=(UIntPtr)__builtin_ptrauth_strip(*(void **)manager,2);
        if (vtable!=slide+0x1eb625e68UL) return;
        // Only configure a fresh original scheduler, before it creates connections.
        if (word(manager+0x3c)>1 || *(UIntPtr *)(manager+0xc8) || *(UIntPtr *)(manager+0xd0)) return;
        manager[0x3c]=1;
        return;
    }
}

static Object initialize(Object self, Selector sel, Object data, Object uuid,
                         Object path, Object device, unsigned long flags) {
    const char *value = path ? string_utf8(
        path, sel_registerName("UTF8String")) : (const char *)0;
    if (same(value, TOMACCO_GPU_COMPILER_PATH)) {
        configure_compiler(device);
        path = string_create(
            objc_getClass("NSString"), sel_registerName("stringWithUTF8String:"),
            TOMACCO_GPU_COMPILER_ALIAS_PATH);
    }
    return original(self, sel, data, uuid, path, device, flags);
}
/* Private-backed storage for logical memoryless render attachments.
 * The original driver allocates and retains the backing. A no-ivar subclass
 * keeps storageMode/resourceOptions and view identity memoryless, so the
 * original render encoder still enforces its discard/resolve/load rules. */
typedef Object (*NewTexture)(Object,Selector,Object);
typedef unsigned long (*Property)(Object,Selector);
typedef struct { unsigned long location,length; } TextureRange;
typedef struct { unsigned char red,green,blue,alpha; } TextureSwizzle;
typedef Object (*TextureView)(Object,Selector,unsigned long,unsigned long,
                             TextureRange,TextureRange,TextureSwizzle);
extern NewTexture get_texture_creator(Method) __asm("_method_getImplementation");
extern NewTexture set_texture_creator(Method,NewTexture) __asm("_method_setImplementation");
extern Property get_property(Method) __asm("_method_getImplementation");
extern TextureView get_texture_view(Method) __asm("_method_getImplementation");
extern Object objc_allocateClassPair(Object,const char *,unsigned long);
extern void objc_registerClassPair(Object);
extern void objc_disposeClassPair(Object);
extern Object object_getClass(Object);
extern Object object_setClass(Object,Object);
extern const char *method_getTypeEncoding(Method);
extern signed char add_property(Object,Selector,Property,const char *) __asm("_class_addMethod");
extern signed char add_texture_view(Object,Selector,TextureView,const char *) __asm("_class_addMethod");
static NewTexture original_new_texture;
static Property original_resource_options;
static TextureView original_texture_view;
static Object texture_class,memoryless_class;
typedef Object (*BufferTextureInitializer)(Object,Selector,Object,Object,unsigned long,unsigned long);
extern BufferTextureInitializer get_buffer_texture_initializer(Method) __asm("_method_getImplementation");
extern BufferTextureInitializer set_buffer_texture_initializer(Method,BufferTextureInitializer) __asm("_method_setImplementation");
static BufferTextureInitializer original_buffer_texture_initializer;
static unsigned buffer_texture_observations;
static unsigned buffer_texture_log_failures;
static UIntPtr buffer_virtual_address_getter;
static int buffer_thread_marker_supported;
typedef unsigned long (*FormatAlignment)(Object,Selector,unsigned long);
extern FormatAlignment get_format_alignment(Method) __asm("_method_getImplementation");
extern signed char add_format_alignment(Object,Selector,FormatAlignment,const char *) __asm("_class_addMethod");
static FormatAlignment original_format_alignment;
static Property original_legacy_alignment;
static Object alignment_device_class;
static unsigned alignment_observations;
static void buffer_log(const char *,unsigned);
static void buffer_observer_status(const char *);
static int buffer_observer_process(void);
/* Both limits are native requirements. Raising the public allocation minimum
 * makes callers pad their own buffers before the original validator runs;
 * texture arguments, buffer aliases and validator instructions stay intact.
 * With its thread marker clear the validator checks rows against the legacy
 * alignment. Only formats observed to reach it with narrower rows are raised:
 * R8, BGRA8, BGRA8_sRGB and RGBA16Float (84x84 at 672 bytes aborts without
 * it). The override applies in every process that loads the
 * driver, so formats without that evidence keep the driver's answer.
 * Preserve any stronger original requirement; unknown limits and other
 * classes retain the original result. */
static int raised_linear_format(unsigned long format) {
    return format==10 || format==80 || format==90 || format==115;
}
static unsigned long compatible_format_alignment(Object self,Selector selector,unsigned long format) {
    unsigned long minimum=original_format_alignment(self,selector,format);
    int native_errno=*__error();
    if (object_getClass(self)!=alignment_device_class || !raised_linear_format(format))
        return minimum;
    unsigned long legacy=original_legacy_alignment(self,sel_registerName("linearTextureAlignmentBytes"));
    int valid=minimum && legacy && minimum<=65536 && legacy<=65536 &&
        !(minimum&(minimum-1)) && !(legacy&(legacy-1));
    unsigned long result=valid && legacy>minimum ? legacy : minimum;
    if (buffer_observer_process() && __atomic_fetch_add(&alignment_observations,1,__ATOMIC_RELAXED)<32) {
        char line[512];
        int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-linear-alignment-query\","
            "\"revision\":\"native-user-v9-validated-formats\",\"pid\":%d,\"uid\":%u,\"pixel_format\":%lu,"
            "\"original_public_minimum\":%lu,\"native_legacy_alignment\":%lu,\"returned_minimum\":%lu,"
            "\"limits_supported\":%s}\n",getpid(),getuid(),format,minimum,legacy,result,valid ? "true" : "false");
        if (size>0 && (unsigned)size<sizeof(line)) buffer_log(line,(unsigned)size);
    }
    *__error()=native_errno;
    return result;
}
/* Verify every byte read against the pinned Mach-O's executable mapping. */
static int alignment_entry_matches(const unsigned char *header,UIntPtr slide,
                                   const unsigned char *uuid,UIntPtr address,
                                   const unsigned char *entry,unsigned entry_bytes) {
    if (!header || word(header)!=0xfeedfacf || !entry_bytes) return 0;
    unsigned commands=word(header+16),size=word(header+20),offset=32;
    if (commands>256 || size<8 || size>65536) return 0;
    int matched=0,in_code=0;
    for (unsigned command=0;command<commands;++command) {
        if (offset>32+size-8) return 0;
        unsigned kind=word(header+offset),length=word(header+offset+4);
        if (length<8 || length>32+size-offset) return 0;
        if (kind==0x1b && length==24 && equal_bytes(header+offset+8,uuid,16)) matched=1;
        if (kind==0x19 && length>=72 && (word(header+offset+60)&5)==5) {
            UIntPtr start=(UIntPtr)word(header+offset+24)|((UIntPtr)word(header+offset+28)<<32);
            UIntPtr bytes=(UIntPtr)word(header+offset+32)|((UIntPtr)word(header+offset+36)<<32);
            start+=slide;
            if (bytes>=entry_bytes && address>=start && address-start<=bytes-entry_bytes) in_code=1;
        }
        offset+=length;
    }
    return offset==32+size && matched && in_code &&
        equal_bytes((const unsigned char *)address,entry,entry_bytes);
}
static void configure_linear_alignment(UIntPtr driver_image) {
    Object cls=objc_getClass("AppleParavirtDevice"),base=objc_getClass("_MTLDevice");
    if (!cls || !base) { buffer_observer_status("alignment-class-unavailable"); return; }
    Selector selector=sel_registerName("minimumLinearTextureAlignmentForPixelFormat:");
    Method method=class_getInstanceMethod(cls,selector),inherited=class_getInstanceMethod(base,selector);
    Method legacy=class_getInstanceMethod(cls,sel_registerName("linearTextureAlignmentBytes"));
    if (!method || method!=inherited || !legacy ||
        !same(method_getTypeEncoding(method),"Q24@0:8Q16") ||
        !same(method_getTypeEncoding(legacy),"Q16@0:8")) {
        buffer_observer_status("alignment-method-contract-mismatch"); return;
    }
    FormatAlignment found=get_format_alignment(method);
    Property legacy_found=get_property(legacy);
    UIntPtr address=(UIntPtr)__builtin_ptrauth_strip((void *)found,0);
    UIntPtr legacy_address=(UIntPtr)__builtin_ptrauth_strip((void *)legacy_found,0);
    if (legacy_address!=driver_image+0x1e78cUL) {
        buffer_observer_status("alignment-legacy-entry-mismatch"); return;
    }
    static const unsigned char driver_uuid[16]={0x7e,0x55,0xcc,0xea,0x96,0x21,0x3a,0xf3,0x93,0x96,0xe2,0x85,0xab,0x7d,0xee,0x23};
    static const unsigned char legacy_code[36]={0x68,0x02,0x00,0xb0,0x08,0xe5,0x89,0xb9,0x08,0x00,0x08,0x8b,
        0x09,0xd1,0x42,0x39,0x69,0x00,0x00,0x34,0x00,0x31,0x40,0xb9,0xc0,0x03,0x5f,0xd6,0x00,0x02,0x80,0x52,0xc0,0x03,0x5f,0xd6};
    static const unsigned char metal_uuid[16]={0x15,0xbb,0x78,0x54,0xd1,0xf3,0x33,0xda,0x81,0x21,0xc3,0xcc,0x4c,0x47,0x2d,0x1d};
    static const unsigned char public_code[32]={0x7f,0x23,0x03,0xd5,0xff,0xc3,0x01,0xd1,0xf4,0x4f,0x05,0xa9,
        0xfd,0x7b,0x06,0xa9,0xfd,0x83,0x01,0x91,0xf3,0x03,0x02,0xaa,0xf4,0x03,0x00,0xaa,0xff,0x23,0x00,0xf9};
    int driver_ok=0,metal_ok=0;
    for (unsigned i=0,count=_dyld_image_count();i<count && i<4096;++i) {
        const unsigned char *header=_dyld_get_image_header(i);
        UIntPtr slide=(UIntPtr)_dyld_get_image_vmaddr_slide(i);
        if ((UIntPtr)header==driver_image)
            driver_ok=alignment_entry_matches(header,slide,driver_uuid,legacy_address,legacy_code,sizeof(legacy_code));
        if (same(_dyld_get_image_name(i),"/System/Library/Frameworks/Metal.framework/Metal") &&
            address==(UIntPtr)header+0xcdf0UL)
            metal_ok=alignment_entry_matches(header,slide,metal_uuid,address,public_code,sizeof(public_code));
    }
    if (!driver_ok || !metal_ok) { buffer_observer_status("alignment-image-contract-mismatch"); return; }
    original_format_alignment=found;
    original_legacy_alignment=legacy_found;
    alignment_device_class=cls;
    /* Add only on the concrete APV class. Mutating the inherited Method would
     * change Metal's base class and unrelated device implementations. */
    if (!add_format_alignment(cls,selector,compatible_format_alignment,"Q24@0:8Q16")) {
        buffer_observer_status("alignment-class-override-refused"); return;
    }
    buffer_observer_status("alignment-class-override-installed");
}
/* Target-bound equivalence: original libpthread's exported getter is exactly
 * MRS TPIDRRO_EL0; LDR [base,key,LSL#3]; RET. Key36 therefore reads the same
 * +0x120 Metal marker on this thread. Verify the image UUID, executable range
 * and complete entry before using the public API. No TLS pointer dereference,
 * guessed address mask, marker write or different-thread memory access. */
static void configure_thread_marker_observation(void) {
    UIntPtr address=(UIntPtr)__builtin_ptrauth_strip((void *)pthread_getspecific,0);
    static const unsigned char uuid[16]={0x69,0x4c,0x77,0x2a,0xa9,0xf8,0x3a,0xc0,0x94,0x17,0x7c,0x30,0x40,0x43,0xa7,0x71};
    static const unsigned char entry[12]={0x68,0xd0,0x3b,0xd5,0x00,0x79,0x60,0xf8,0xc0,0x03,0x5f,0xd6};
    for (unsigned index=0,count=_dyld_image_count();index<count && index<4096;++index) {
        if (!same(_dyld_get_image_name(index),"/usr/lib/system/libsystem_pthread.dylib")) continue;
        const unsigned char *header=_dyld_get_image_header(index);
        if (!header || word(header)!=0xfeedfacf) return;
        unsigned commands=word(header+16),size=word(header+20),offset=32;
        if (commands>256 || size<8 || size>65536) return;
        int matched=0,in_code=0;
        UIntPtr slide=(UIntPtr)_dyld_get_image_vmaddr_slide(index);
        for (unsigned command=0;command<commands;++command) {
            if (offset>32+size-8) return;
            unsigned kind=word(header+offset),length=word(header+offset+4);
            if (length<8 || length>32+size-offset) return;
            if (kind==0x1b && length==24 && equal_bytes(header+offset+8,uuid,16)) matched=1;
            if (kind==0x19 && length>=72 && (word(header+offset+60)&5)==5) {
                UIntPtr start=(UIntPtr)word(header+offset+24)|((UIntPtr)word(header+offset+28)<<32);
                UIntPtr bytes=(UIntPtr)word(header+offset+32)|((UIntPtr)word(header+offset+36)<<32);
                start+=slide;
                if (bytes>=sizeof(entry) && address>=start && address-start<=bytes-sizeof(entry)) in_code=1;
            }
            offset+=length;
        }
        if (offset!=32+size || !matched || !in_code) return;
        if (equal_bytes((const unsigned char *)address,entry,sizeof(entry))) buffer_thread_marker_supported=1;
        return;
    }
}
extern unsigned long read_format_property(Object,Selector,unsigned long) __asm("_objc_msgSend");
static unsigned long observed_format_alignment(Object device,unsigned long format,unsigned *known) {
    /* The format is the one the guest's own descriptor carries into the
     * initializer, so the public query receives no invented enum. */
    if (!device) return 0;
    Selector selector=sel_registerName("minimumLinearTextureAlignmentForPixelFormat:");
    Method method=class_getInstanceMethod(object_getClass(device),selector);
    if (!method || !same(method_getTypeEncoding(method),"Q24@0:8Q16")) return 0;
    *known|=1u<<14;
    return read_format_property(device,selector,format);
}
extern void *read_virtual_address(Object,Selector) __asm("_objc_msgSend");
static void configure_virtual_address_observation(void) {
    Object cls=objc_getClass("IOGPUMetalResource");
    Method method=cls ? class_getInstanceMethod(cls,sel_registerName("virtualAddress")) : (Method)0;
    if (!method || !same(method_getTypeEncoding(method),"^v16@0:8")) return;
    UIntPtr address=(UIntPtr)__builtin_ptrauth_strip((void *)method_getImplementation(method),0);
    static const unsigned char uuid[16]={0x40,0x48,0x24,0x8c,0x0b,0x2f,0x3d,0x98,0xb8,0xc1,0x12,0x96,0x08,0xcd,0xf1,0x79};
    static const unsigned char code[20]={0x88,0x37,0x30,0xb0,0x08,0x51,0x81,0xb9,0x08,0x00,0x08,0x8b,0x00,0x41,0x40,0xf9,0xc0,0x03,0x5f,0xd6};
    for (unsigned i=0,n=_dyld_image_count();i<n;++i) {
        const unsigned char *header=_dyld_get_image_header(i);
        if (!header || (UIntPtr)header+0x886cUL!=address || word(header)!=0xfeedfacf) continue;
        unsigned commands=word(header+16),size=word(header+20),offset=32;
        if (commands>256 || size<8 || size>65536) return;
        int matched=0;
        for (unsigned c=0;c<commands;++c) {
            if (offset>32+size-8) return;
            unsigned cmd=word(header+offset),length=word(header+offset+4);
            if (length<8 || length>32+size-offset) return;
            if (cmd==0x1b && length==24 && equal_bytes(header+offset+8,uuid,16)) matched=1;
            offset+=length;
        }
        if (matched && equal_bytes((const unsigned char *)address,code,20))
            buffer_virtual_address_getter=address;
        return;
    }
}
static unsigned long observed_property(Object value,const char *name,unsigned *known,unsigned bit) {
    if (!value) return 0;
    Selector selector=sel_registerName(name);
    Method method=class_getInstanceMethod(object_getClass(value),selector);
    if (!method || !same(method_getTypeEncoding(method),"Q16@0:8")) return 0;
    *known|=1u<<bit;
    return read_property(value,selector);
}
static void buffer_log(const char *line,unsigned size) {
    int saved=*__error(), failure=0;
    int directory=tomacco_buffer_log_directory();
    if (directory<0) failure=*__error();
    else {
        int created=1;
        int file=openat(directory,TOMACCO_BUFFER_LOG_NAME,0x1000b0d,0644);
        /* WRONLY|CREAT|EXCL|APPEND|NONBLOCK|NOFOLLOW|CLOEXEC */
        if (file<0 && *__error()==17) {
            created=0;
            file=openat(directory,TOMACCO_BUFFER_LOG_NAME,0x100010d);
        }
        if (file<0) failure=*__error();
        else {
            struct tomacco_darwin_stat info;
            if (flock(file,6)) failure=*__error(); /* EX|NB: never wait on another writer */
            else if (fstat(file,&info)) failure=*__error();
            else if ((info.mode&0170000)!=0100000 || info.uid!=TOMACCO_BUFFER_LOG_UID || info.links!=1)
                failure=22;
            else if (created && fchmod(file,0644)) failure=*__error();
            else if (fstat(file,&info)) failure=*__error();
            else if (!tomacco_buffer_log_identity(&info)) failure=22;
            else if (info.size>TOMACCO_BUFFER_LOG_LIMIT-(long long)size) failure=27;
            else {
                long written=write(file,line,size);
                if (written!=(long)size) failure=written<0 ? *__error() : 5;
            }
            close(file);
        }
        close(directory);
    }
    if (failure && __atomic_fetch_add(&buffer_texture_log_failures,1,__ATOMIC_RELAXED)<4) {
        char error[192];
        int count=snprintf(error,sizeof(error),"TOMACCO_BUFFER_TEXTURE_LOG_FAILED pid=%d uid=%u errno=%d\n",
            getpid(),getuid(),failure);
        if (count>0 && (unsigned)count<sizeof(error)) (void)write(2,error,(unsigned)count);
    }
    *__error()=saved;
}
static int buffer_observer_process(void) {
    if (geteuid()!=TOMACCO_BUFFER_LOG_UID) return 0;
    int saved=*__error();
    char path[1024]={0};
    int size=proc_pidpath(getpid(),path,sizeof(path));
    int matched=size==(int)(sizeof(TOMACCO_BUFFER_LOG_EXECUTABLE)-1) &&
        path[sizeof(TOMACCO_BUFFER_LOG_EXECUTABLE)-1]==0 && same(path,TOMACCO_BUFFER_LOG_EXECUTABLE);
    *__error()=saved;
    return matched;
}
static void buffer_observer_status(const char *state) {
    if (!buffer_observer_process()) return;
    char line[384];
    int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"buffer-texture-observer-status\","
        "\"revision\":\"native-user-v5-path\",\"pid\":%d,\"uid\":%u,\"effective_uid\":%u,"
        "\"identity_source\":\"native-executable-path\",\"program_name_matches_expected\":%s,\"state\":\"%s\"}\n",
        getpid(),getuid(),geteuid(),same(getprogname(),"iconservicesagent") ? "true" : "false",state);
    if (size>0 && (unsigned)size<sizeof(line)) buffer_log(line,(unsigned)size);
}
/* Observe the effective parameters before original Metal validation. Neither
 * the arguments, native validation, return value nor object ownership change.
 * Only iconservicesagent writes our fixed bounded diagnostic log. */
static Object observed_buffer_texture(Object self,Selector selector,Object buffer,
                                      Object descriptor,unsigned long offset,unsigned long row) {
    int saved_errno=*__error();
    unsigned event=0;
    unsigned long long thread=0;
    if (buffer_observer_process()) {
        unsigned sequence=__atomic_fetch_add(&buffer_texture_observations,1,__ATOMIC_RELAXED);
        if (sequence<48) event=sequence+1;
    }
    if (event) {
        unsigned long marker=buffer_thread_marker_supported ? (unsigned long)pthread_getspecific(36UL) : 0;
        unsigned known=0;
        const char *keys[]={"pixelFormat","width","height","depth","textureType",
            "mipmapLevelCount","arrayLength","sampleCount","storageMode","resourceOptions"};
        unsigned long values[10];
        for (unsigned n=0;n<10;++n) values[n]=observed_property(descriptor,keys[n],&known,n);
        unsigned long length=observed_property(buffer,"length",&known,10),alignment=0;
        Object device=(Object)0;
        Method getter=buffer ? class_getInstanceMethod(object_getClass(buffer),sel_registerName("device")) : (Method)0;
        if (getter && same(method_getTypeEncoding(getter),"@16@0:8"))
            device=copy_object(buffer,sel_registerName("device"));
        alignment=observed_property(device,"linearTextureAlignmentBytes",&known,11);
        unsigned long usage=observed_property(descriptor,"usage",&known,13);
        unsigned long minimum=(known&1) ? observed_format_alignment(device,values[0],&known) : 0;
        unsigned long device_alignment=observed_property(device,"deviceLinearTextureAlignmentBytes",&known,15);
        unsigned long readonly_alignment=observed_property(device,"deviceLinearReadOnlyTextureAlignmentBytes",&known,16);
        unsigned long virtual_address=0;
        getter=buffer ? class_getInstanceMethod(object_getClass(buffer),sel_registerName("virtualAddress")) : (Method)0;
        if (buffer_virtual_address_getter && getter && same(method_getTypeEncoding(getter),"^v16@0:8") &&
            (UIntPtr)__builtin_ptrauth_strip((void *)method_getImplementation(getter),0)==buffer_virtual_address_getter) {
            virtual_address=(unsigned long)read_virtual_address(buffer,sel_registerName("virtualAddress"));
            known|=1u<<12;
        }
        int thread_status=pthread_threadid_np((void *)0,&thread);
        char line[2048];
        int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-buffer-texture-before-validation\","
            "\"pid\":%d,\"uid\":%u,\"effective_uid\":%u,\"thread_id\":%llu,\"thread_id_known\":%s,\"apv_offset\":%lu,\"bytes_per_row\":%lu,"
            "\"virtual_address\":%lu,\"validation_address\":%lu,\"validation_address_known\":%s,"
            "\"known_property_bits\":%u,\"pixel_format\":%lu,\"width\":%lu,\"height\":%lu,\"depth\":%lu,"
            "\"texture_type\":%lu,\"mipmap_level_count\":%lu,\"array_length\":%lu,\"sample_count\":%lu,"
            "\"storage_mode\":%lu,\"resource_options\":%lu,\"buffer_length\":%lu,\"device_linear_alignment\":%lu,"
            "\"revision\":\"native-user-v9-validated-formats\",\"diagnostic_event_id\":%u,\"metal_thread_marker\":%lu,\"metal_thread_marker_known\":%s,"
            "\"texture_usage\":%lu,\"public_minimum_linear_alignment\":%lu,"
            "\"device_private_linear_alignment\":%lu,\"device_private_readonly_alignment\":%lu}\n",
            getpid(),getuid(),geteuid(),thread,thread_status ? "false" : "true",offset,row,virtual_address,
            known&(1u<<12) ? virtual_address+offset : 0,known&(1u<<12) ? "true" : "false",known,values[0],values[1],values[2],
            values[3],values[4],values[5],values[6],values[7],values[8],values[9],length,alignment,
            event,marker,buffer_thread_marker_supported ? "true" : "false",usage,minimum,device_alignment,readonly_alignment);
        if (size>0 && (unsigned)size<sizeof(line)) buffer_log(line,(unsigned)size);
    }
    *__error()=saved_errno;
    Object result=original_buffer_texture_initializer(self,selector,buffer,descriptor,offset,row);
    int native_errno=*__error();
    if (event) {
        char line[384];
        int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"native-buffer-texture-after-validation\","
            "\"revision\":\"native-user-v9-validated-formats\",\"pid\":%d,\"thread_id\":%llu,"
            "\"diagnostic_event_id\":%u,\"native_texture_nonnull\":%s}\n",getpid(),thread,event,result ? "true" : "false");
        if (size>0 && (unsigned)size<sizeof(line)) buffer_log(line,(unsigned)size);
    }
    *__error()=native_errno;
    return result;
}
static void configure_buffer_observation(UIntPtr image) {
    Object cls=objc_getClass("AppleParavirtTexture");
    Method method=cls ? class_getInstanceMethod(cls,sel_registerName("initWithBuffer:descriptor:offset:bytesPerRow:")) : (Method)0;
    if (!method) { buffer_observer_status("buffer-method-unavailable"); return; }
    if (!same(method_getTypeEncoding(method),"@48@0:8@16@24Q32Q40")) {
        buffer_observer_status("buffer-method-encoding-mismatch"); return;
    }
    BufferTextureInitializer found=get_buffer_texture_initializer(method);
    UIntPtr address=(UIntPtr)__builtin_ptrauth_strip((void *)found,0);
    static const unsigned char entry[16]={0x7f,0x23,0x03,0xd5,0xff,0x03,0x05,0xd1,0xfc,0x6f,0x0e,0xa9,0xfa,0x67,0x0f,0xa9};
    if (address!=image+0x7134UL || !equal_bytes((const unsigned char *)address,entry,16)) {
        buffer_observer_status("buffer-method-entry-mismatch"); return;
    }
    configure_thread_marker_observation();
    configure_virtual_address_observation();
    original_buffer_texture_initializer=found;
    set_buffer_texture_initializer(method,observed_buffer_texture);
    buffer_observer_status("buffer-method-installed");
    if (buffer_observer_process()) {
        char line[192];
        int size=snprintf(line,sizeof(line),"{\"format\":1,\"operation\":\"buffer-texture-observer-installed\","
            "\"pid\":%d,\"uid\":%u}\n",getpid(),getuid());
        if (size>0 && (unsigned)size<sizeof(line)) buffer_log(line,(unsigned)size);
    }
}
static unsigned long memoryless_storage(Object self,Selector selector) {
    (void)self;(void)selector;return 3;
}
static unsigned long memoryless_options(Object self,Selector selector) {
    return (original_resource_options(self,selector)&~0xf0UL)|0x30UL;
}
static Object mark_memoryless(Object texture) {
    if (!texture) return (Object)0;
    Object cls=object_getClass(texture);
    if (cls==texture_class) object_setClass(texture,memoryless_class);
    else if (cls!=memoryless_class) {
        // The pinned allocator returns this concrete class. If that contract
        // changes, fail allocation instead of returning a private texture as
        // a memoryless one. Both callers own their +1 result here.
        release_object(texture,sel_registerName("release"));
        return (Object)0;
    }
    return texture;
}
static Object memoryless_view(Object self,Selector selector,unsigned long format,unsigned long type,
                              TextureRange levels,TextureRange slices,TextureSwizzle swizzle) {
    return mark_memoryless(original_texture_view(self,selector,format,type,levels,slices,swizzle));
}
static int configure_memoryless_class(UIntPtr image) {
    Object cls=objc_getClass("AppleParavirtTexture");
    if (!cls || objc_getClass("TomaccoMemorylessTexture")) return 0;
    Selector storage=sel_registerName("storageMode"),options=sel_registerName("resourceOptions");
    Selector view=sel_registerName("newTextureViewWithPixelFormat:textureType:levels:slices:swizzle:");
    Method storage_method=class_getInstanceMethod(cls,storage),options_method=class_getInstanceMethod(cls,options);
    Method view_method=class_getInstanceMethod(cls,view);
    if (!storage_method || !options_method || !view_method) return 0;
    TextureView view_imp=get_texture_view(view_method);
    if ((UIntPtr)__builtin_ptrauth_strip((void *)view_imp,0)!=image+0x8f4cUL) return 0;
    Object subclass=objc_allocateClassPair(cls,"TomaccoMemorylessTexture",0);
    if (!subclass) return 0;
    if (!add_property(subclass,storage,memoryless_storage,method_getTypeEncoding(storage_method)) ||
        !add_property(subclass,options,memoryless_options,method_getTypeEncoding(options_method)) ||
        !add_texture_view(subclass,view,memoryless_view,method_getTypeEncoding(view_method))) {
        objc_disposeClassPair(subclass);return 0;
    }
    original_resource_options=get_property(options_method);
    original_texture_view=view_imp;texture_class=cls;memoryless_class=subclass;
    objc_registerClassPair(subclass);return 1;
}
static Object new_backed_texture(Object self,Selector selector,Object descriptor) {
    if (!descriptor || read_property(
            descriptor,sel_registerName("storageMode"))!=3)
        return original_new_texture(self,selector,descriptor);
    // Run the original validation on the logical descriptor before adapting
    // its physical storage. Invalid memoryless usage must not become valid
    // merely because the backing descriptor is private.
    if (!validate_descriptor(
            self,sel_registerName("preValidateTextureDescriptor:"),descriptor) ||
        !validate_descriptor(
            descriptor,sel_registerName("validateWithDevice:"),self)) return (Object)0;
    Object copy=copy_object(descriptor,sel_registerName("copy"));
    if (!copy) return (Object)0;
    set_property(copy,sel_registerName("setStorageMode:"),2);
    Object result=original_new_texture(self,selector,copy);
    release_object(copy,sel_registerName("release"));
    return mark_memoryless(result);
}
/* Bounded native process coverage for public and scalar shared IOSurface imports. */
#include "ios-surface-observer.h"
static void configure_render_storage(void) {
    Object cls=objc_getClass("AppleParavirtDevice");
    if (!cls) {
        buffer_observer_status("render-device-class-unavailable");
        surface_observer_status("render-device-class-unavailable");return;
    }
    Method method=class_getInstanceMethod(cls,sel_registerName("supportsMemorylessRenderTargets"));
    if (!method) {
        buffer_observer_status("render-support-method-unavailable");
        surface_observer_status("render-support-method-unavailable");return;
    }
    UIntPtr implementation=(UIntPtr)__builtin_ptrauth_strip((void *)method_getImplementation(method),0);
    static const unsigned char uuid[16]={0x7e,0x55,0xcc,0xea,0x96,0x21,0x3a,0xf3,0x93,0x96,0xe2,0x85,0xab,0x7d,0xee,0x23};
    static const unsigned char getter[8]={0x20,0x00,0x80,0x52,0xc0,0x03,0x5f,0xd6};
    for (unsigned i=0,n=_dyld_image_count();i<n;++i) {
        const unsigned char *header=_dyld_get_image_header(i);
        if ((UIntPtr)header+0x1f8a8UL!=implementation) continue;
        if (!header || word(header)!=0xfeedfacf) return;
        unsigned commands=word(header+16),size=word(header+20),offset=32;
        if (commands>256 || size<8 || size>65536) return;
        int matched=0;
        for (unsigned c=0;c<commands;++c) {
            if (offset>32+size-8) return;
            unsigned cmd=word(header+offset),length=word(header+offset+4);
            if (length<8 || length>32+size-offset) return;
            if (cmd==0x1b && length==24 && equal_bytes(header+offset+8,uuid,16)) matched=1;
            offset+=length;
        }
        if (!matched || !equal_bytes((const unsigned char *)implementation,getter,8)) {
            buffer_observer_status("render-driver-identity-mismatch");
            surface_observer_status("render-driver-identity-mismatch");return;
        }
        configure_buffer_observation((UIntPtr)header);
        configure_surface_observation((UIntPtr)header);
        configure_linear_alignment((UIntPtr)header);
        Method creator=class_getInstanceMethod(cls,sel_registerName("newTextureWithDescriptor:"));
        if (!creator) return;
        NewTexture found=get_texture_creator(creator);
        if ((UIntPtr)__builtin_ptrauth_strip((void *)found,0)!=(UIntPtr)header+0x1d6acUL) return;
        if (!configure_memoryless_class((UIntPtr)header)) return;
        original_new_texture=found;
        set_texture_creator(creator,new_backed_texture);
        return;
    }
    buffer_observer_status("render-driver-image-unavailable");
    surface_observer_status("render-driver-image-unavailable");
}

__attribute__((constructor)) static void prepare_path(void) {
    buffer_observer_status("bridge-loaded");
    configure_render_storage();
    Object cls = objc_getClass("MTLCompiler");
    if (!cls) return;
    Method method = class_getInstanceMethod(cls, sel_registerName(
        "initWithTargetData:cacheUUID:pluginPath:device:compilerFlags:"));
    if (!method) return;
    Initializer found = method_getImplementation(method);
    if (!found || found == initialize) return;
    original = found;
    method_setImplementation(method, initialize);
}
