/* Synthetic Objective-C boundary. No Apple implementation or guest data. */
#include <assert.h>
#include <stdint.h>
#include <string.h>

/* Host tests exercise ownership and forwarding; the production cross-build
 * separately checks the ARM64e calling convention and pointer authentication. */
#define __asm(name)
#define __attribute__(value)
#define __builtin_ptrauth_strip(pointer, key) (pointer)
#define getuid tomacco_harness_getuid
#define geteuid tomacco_harness_geteuid
#include "../../integrations/ios-emulator/ios-gpu-bridge.c"
#undef __asm
#undef __attribute__
#undef __builtin_ptrauth_strip
#undef getuid
#undef geteuid

/* This ownership test has no native diagnostic producer. Keep the Darwin
 * boundary hermetic, including on a host whose UID happens to be 276/501. */
unsigned tomacco_harness_getuid(void) { return ~0u; }
unsigned tomacco_harness_geteuid(void) { return ~0u; }
static int harness_errno;
int *__error(void) { return &harness_errno; }
const char *getprogname(void) { return "host-memoryless-test"; }
int proc_pidpath(int pid,void *path,unsigned bytes) { (void)pid;(void)path;(void)bytes;return 0; }

typedef struct { Object cls; unsigned long storage, options; unsigned width; } FakeObject;
typedef struct { uintptr_t implementation; } FakeMethod;
static unsigned char image[0x20000];
static int device_type, texture_type, memoryless_type, other_type;
static FakeMethod capability, creator, view, storage, options;
static FakeObject descriptor, copied, texture, viewed, device;
static int prevalid, valid, fail_copy, fail_alloc, fail_view, wrong_class;
static int copy_count, release_count, create_count, view_count, pre_count, validate_count;
static int setter_count, additions, fail_add, registered, disposed, duplicate, missing_view;
static Object last_descriptor;
static NewTexture installed_creator;
static TextureRange last_levels, last_slices;
static TextureSwizzle last_swizzle;
static unsigned long last_format, last_type;

Object objc_getClass(const char *name) {
    if (!strcmp(name,"AppleParavirtDevice")) return &device_type;
    if (!strcmp(name,"AppleParavirtTexture")) return &texture_type;
    if (!strcmp(name,"TomaccoMemorylessTexture") && duplicate) return &memoryless_type;
    return 0;
}
Selector sel_registerName(const char *name) { return (Selector)name; }
Method class_getInstanceMethod(Object cls, Selector selector) {
    const char *name=selector;
    if (cls==&device_type) {
        if (!strcmp(name,"supportsMemorylessRenderTargets")) return &capability;
        if (!strcmp(name,"newTextureWithDescriptor:")) return &creator;
    }
    if (cls==&texture_type) {
        if (!strcmp(name,"storageMode")) return &storage;
        if (!strcmp(name,"resourceOptions")) return &options;
        if (!strcmp(name,"newTextureViewWithPixelFormat:textureType:levels:slices:swizzle:") && !missing_view) return &view;
    }
    return 0;
}
Initializer method_getImplementation(Method method) { return (Initializer)((FakeMethod *)method)->implementation; }
Initializer method_setImplementation(Method method, Initializer imp) { (void)method;return imp; }
NewTexture get_texture_creator(Method method) { return (NewTexture)((FakeMethod *)method)->implementation; }
NewTexture set_texture_creator(Method method, NewTexture imp) {
    assert(method==&creator);setter_count++;installed_creator=imp;
    return get_texture_creator(method);
}
Property get_property(Method method) { return (Property)((FakeMethod *)method)->implementation; }
TextureView get_texture_view(Method method) { return (TextureView)((FakeMethod *)method)->implementation; }
const char *method_getTypeEncoding(Method method) { (void)method;return "synthetic ABI"; }
Object objc_allocateClassPair(Object cls,const char *name,unsigned long bytes) {
    assert(cls==&texture_type && !strcmp(name,"TomaccoMemorylessTexture") && !bytes);
    return &memoryless_type;
}
void objc_registerClassPair(Object cls) { assert(cls==&memoryless_type);registered++; }
void objc_disposeClassPair(Object cls) { assert(cls==&memoryless_type);disposed++; }
Object object_getClass(Object obj) { return ((FakeObject *)obj)->cls; }
Object object_setClass(Object obj,Object cls) {
    Object old=((FakeObject *)obj)->cls;((FakeObject *)obj)->cls=cls;return old;
}
signed char add_property(Object cls,Selector sel,Property imp,const char *encoding) {
    assert(cls==&memoryless_type && !strcmp(encoding,"synthetic ABI"));
    assert((!strcmp(sel,"storageMode") && imp==memoryless_storage) ||
           (!strcmp(sel,"resourceOptions") && imp==memoryless_options));
    return ++additions!=fail_add;
}
signed char add_texture_view(Object cls,Selector sel,TextureView imp,const char *encoding) {
    assert(cls==&memoryless_type && !strcmp(encoding,"synthetic ABI"));
    assert(!strcmp(sel,"newTextureViewWithPixelFormat:textureType:levels:slices:swizzle:") && imp==memoryless_view);
    return ++additions!=fail_add;
}
unsigned _dyld_image_count(void) { return 1; }
const unsigned char *_dyld_get_image_header(unsigned i) { assert(!i);return image; }
const char *_dyld_get_image_name(unsigned i) { assert(!i);return "synthetic driver"; }
long _dyld_get_image_vmaddr_slide(unsigned i) { assert(!i);return 0; }

const char *string_utf8(Object self,Selector sel) { (void)sel;return self; }
Object string_create(Object self,Selector sel,const char *text) { (void)self;(void)sel;return (Object)text; }
unsigned char *connection_manager(Object self,Selector sel,int version) {
    (void)self;(void)sel;(void)version;return 0;
}
unsigned long read_property(Object self,Selector sel) {
    assert(!strcmp(sel,"storageMode"));return ((FakeObject *)self)->storage;
}
signed char validate_descriptor(Object self,Selector sel,Object argument) {
    if (!strcmp(sel,"preValidateTextureDescriptor:")) {
        assert(self==&device && argument==&descriptor);pre_count++;return prevalid;
    }
    assert(!strcmp(sel,"validateWithDevice:") && self==&descriptor && argument==&device);
    validate_count++;return valid;
}
Object copy_object(Object self,Selector sel) {
    assert(!strcmp(sel,"copy") && self==&descriptor);copy_count++;copied=descriptor;
    return fail_copy?0:&copied;
}
void set_property(Object self,Selector sel,unsigned long value) {
    assert(!strcmp(sel,"setStorageMode:") && self==&copied);
    copied.storage=value;copied.options=(copied.options&~0xf0UL)|(value<<4);
}
void release_object(Object self,Selector sel) {
    assert(!strcmp(sel,"release") && (self==&copied || self==&texture || self==&viewed));release_count++;
}
static Object allocate_texture(Object self,Selector sel,Object desc) {
    assert(self==&device && !strcmp(sel,"newTextureWithDescriptor:"));
    create_count++;last_descriptor=desc;
    if (fail_alloc) return 0;
    texture.cls=wrong_class?&other_type:&texture_type;
    if (desc) texture.options=((FakeObject *)desc)->options;
    return &texture;
}
static unsigned long resource_options(Object self,Selector sel) {
    assert(!strcmp(sel,"resourceOptions"));return ((FakeObject *)self)->options;
}
static Object allocate_view(Object self,Selector sel,unsigned long format,unsigned long type,
                            TextureRange levels,TextureRange slices,TextureSwizzle swizzle) {
    assert(self==&texture && !strcmp(sel,"view"));view_count++;
    last_format=format;last_type=type;last_levels=levels;last_slices=slices;last_swizzle=swizzle;
    viewed.cls=wrong_class?&other_type:&texture_type;
    return fail_view?0:&viewed;
}
static void reset(void) {
    prevalid=valid=1;fail_copy=fail_alloc=fail_view=wrong_class=0;
    copy_count=release_count=create_count=view_count=pre_count=validate_count=0;
    setter_count=additions=fail_add=registered=disposed=duplicate=missing_view=0;
    installed_creator=0;last_descriptor=0;
    original_new_texture=allocate_texture;original_resource_options=resource_options;original_texture_view=allocate_view;
    texture_class=&texture_type;memoryless_class=&memoryless_type;
    descriptor=(FakeObject){&other_type,3,0x232,127};
    memset(image,0,sizeof(image));
    unsigned words[]={0xfeedfacf,0,0,0,1,24,0,0,0x1b,24};
    memcpy(image,words,sizeof(words));
    const unsigned char uuid[]={0x7e,0x55,0xcc,0xea,0x96,0x21,0x3a,0xf3,0x93,0x96,0xe2,0x85,0xab,0x7d,0xee,0x23};
    const unsigned char getter[]={0x20,0,0x80,0x52,0xc0,3,0x5f,0xd6};
    memcpy(image+40,uuid,16);memcpy(image+0x1f8a8,getter,8);
    capability.implementation=(uintptr_t)image+0x1f8a8;
    creator.implementation=(uintptr_t)image+0x1d6ac;
    view.implementation=(uintptr_t)image+0x8f4c;
    options.implementation=(uintptr_t)resource_options;
}
static void check_ownership(void) {
    reset();Object result=new_backed_texture(&device,"newTextureWithDescriptor:",&descriptor);
    assert(result==&texture && texture.cls==&memoryless_type);
    assert(descriptor.storage==3 && descriptor.options==0x232 && descriptor.width==127);
    assert(last_descriptor==&copied && copied.storage==2 && copied.options==0x222 && copied.width==127);
    assert(copy_count==1 && release_count==1 && create_count==1 && pre_count==1 && validate_count==1);
    assert(memoryless_storage(result,"storageMode")==3);
    assert(memoryless_options(result,"resourceOptions")==0x232);
    for (unsigned long mode=0;mode<3;mode++) {
        reset();descriptor.storage=mode;new_backed_texture(&device,"newTextureWithDescriptor:",&descriptor);
        assert(last_descriptor==&descriptor && !copy_count && !release_count && !pre_count);
        assert(texture.cls==&texture_type);
    }
    reset();new_backed_texture(&device,"newTextureWithDescriptor:",0);
    assert(!last_descriptor && create_count==1 && !copy_count);
    for (int failure=0;failure<5;failure++) {
        reset();
        if (failure==0) prevalid=0;
        if (failure==1) valid=0;
        if (failure==2) fail_copy=1;
        if (failure==3) fail_alloc=1;
        if (failure==4) wrong_class=1;
        assert(!new_backed_texture(&device,"newTextureWithDescriptor:",&descriptor));
        assert(descriptor.storage==3 && descriptor.options==0x232);
        assert(create_count==(failure>=3));assert(release_count==(failure==3?1:failure==4?2:0));
    }
}
static void check_views(void) {
    reset();TextureRange levels={2,3},slices={4,5};TextureSwizzle swizzle={5,4,3,2};
    assert(memoryless_view(&texture,"view",88,7,levels,slices,swizzle)==&viewed);
    assert(viewed.cls==&memoryless_type && view_count==1 && !release_count);
    assert(last_format==88 && last_type==7 && !memcmp(&last_levels,&levels,sizeof(levels)));
    assert(!memcmp(&last_slices,&slices,sizeof(slices)) && !memcmp(&last_swizzle,&swizzle,sizeof(swizzle)));
    fail_view=1;assert(!memoryless_view(&texture,"view",88,7,levels,slices,swizzle));assert(!release_count);
    fail_view=0;wrong_class=1;assert(!memoryless_view(&texture,"view",88,7,levels,slices,swizzle));assert(release_count==1);
}
static void check_profile_guard(void) {
    for (int failure=0;failure<9;failure++) {
        reset();
        if (failure==1) image[40]^=1;
        if (failure==2) image[0x1f8a8]^=1;
        if (failure==3) creator.implementation++;
        if (failure==4) view.implementation++;
        if (failure==5) duplicate=1;
        if (failure==6) missing_view=1;
        if (failure==7) image[20]=1;
        if (failure==8) image[36]=0xff;
        prepare_path();
        assert(setter_count==(failure==0));assert(registered==(failure==0));
        if (!failure) assert(installed_creator==new_backed_texture && additions==3);
        assert(capability.implementation==(uintptr_t)image+0x1f8a8);
    }
    for (int failure=1;failure<=3;failure++) {
        reset();fail_add=failure;configure_render_storage();
        assert(!setter_count && !registered && disposed==1);
    }
}
/* Driver limits as observed natively: a 16-byte public minimum, a 256-byte
 * legacy requirement, no linear support (0) and one stronger requirement. */
static unsigned long driver_minimum(Object self,Selector selector,unsigned long format) {
    (void)self;(void)selector;harness_errno=35;
    return format==1 ? 0 : format==2 ? 1024 : format==3 ? 3 : 16;
}
static unsigned long driver_legacy(Object self,Selector selector) { (void)self;(void)selector;return 256; }
static void check_linear_alignment(void) {
    original_format_alignment=driver_minimum;original_legacy_alignment=driver_legacy;
    alignment_device_class=&device_type;device.cls=&device_type;
    /* R8, BGRA8, BGRA8_sRGB and RGBA16Float: the formats the validator refused. */
    unsigned long raised[]={10,80,90,115};
    for (unsigned i=0;i<sizeof(raised)/sizeof(raised[0]);i++) {
        harness_errno=0;
        assert(compatible_format_alignment(&device,"minimum",raised[i])==256);
        assert(harness_errno==35);
    }
    /* RGBA8 and RGBA32Float keep the driver's answer, as do unusable limits. */
    assert(compatible_format_alignment(&device,"minimum",70)==16);
    assert(compatible_format_alignment(&device,"minimum",125)==16);
    assert(compatible_format_alignment(&device,"minimum",1)==0);
    assert(compatible_format_alignment(&device,"minimum",2)==1024);
    assert(compatible_format_alignment(&device,"minimum",3)==3);
    device.cls=&other_type;
    assert(compatible_format_alignment(&device,"minimum",115)==16);
}
int main(void) { check_ownership();check_views();check_profile_guard();check_linear_alignment();return 0; }
