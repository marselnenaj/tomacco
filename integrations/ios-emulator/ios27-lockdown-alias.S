/* vphone600 23E5207q name2oid, after its empty-name check. Both the
 * CTL_SYSCTL_NAME2OID and direct sysctlbyname routes use this function.
 * x0 is the mutable, terminated kernel copy; x1/x2 and x0 stay intact.
 * An exact alias reaches the original read-only OID and real NVRAM state.
 * No synthetic value or skipped access checks. Stop at the first differing
 * byte (including NUL), so short names never read past their terminator.
 */
.text
.global tomacco_lockdown_alias
tomacco_lockdown_alias:
adr x8, public_name
mov x9, x0
compare:
ldrb w10, [x8], #1
ldrb w11, [x9], #1
cmp w10, w11
b.ne original
cbnz w10, compare
strb wzr, [x0, #(public_suffix - public_name)]
original:
pacibsp
b original_continue
public_name:
.ascii "security.mac.lockdown_mode_state"
public_suffix:
.asciz "_public"
