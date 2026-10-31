/* Uses the real libSystem in the guest. No Apple SDK headers are needed. */
extern long write(int, const void *, unsigned long);

int main(void) {
    static const char message[] =
        "Tomacco: compiled on Linux, running native ARM64 in iOS\n";
    return write(1, message, sizeof(message) - 1) == sizeof(message) - 1 ? 0 : 7;
}
