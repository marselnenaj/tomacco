#include <jni.h>

__attribute__((noinline)) int tomacco_calculate(int seed) {
    volatile int count = seed;
    count += 2;
    return count;
}

JNIEXPORT jint JNICALL Java_dev_tomacco_nativedebugfixture_MainActivity_calculate(
    JNIEnv *environment, jclass type, jint seed) {
    (void)environment;
    (void)type;
    return tomacco_calculate(seed);
}
