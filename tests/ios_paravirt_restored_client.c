/* Read-only interoperability probe using the installed libimobiledevice.
 * Set USBMUXD_SOCKET_ADDRESS to this VM's private bridge before running.
 * No restore, signing request, pairing or authentication override occurs.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libimobiledevice/libimobiledevice.h>
#include <libimobiledevice/restore.h>
#include <plist/plist.h>

int main(void)
{
    idevice_t device = NULL;
    restored_client_t client = NULL;
    idevice_connection_t parallel = NULL;
    plist_t report = plist_new_dict(), value = NULL;
    char *type = NULL, *xml = NULL;
    uint64_t version = 0;
    uint32_t length = 0;
    int result = 1;
    const char *address = getenv("USBMUXD_SOCKET_ADDRESS");
    if (!address || strncmp(address, "UNIX:", 5)) {
        fprintf(stderr, "A private Unix USBMUXD_SOCKET_ADDRESS is required\n");
        goto done;
    }
    if (idevice_new_with_options(&device, NULL, IDEVICE_LOOKUP_USBMUX) != IDEVICE_E_SUCCESS ||
        restored_client_new(device, &client, "TomaccoInterop") != RESTORE_E_SUCCESS ||
        restored_query_type(client, &type, &version) != RESTORE_E_SUCCESS ||
        strcmp(type, "com.apple.mobile.restored")) {
        fprintf(stderr, "Cannot query the original restore service\n");
        goto done;
    }
    plist_dict_set_item(report, "Type", plist_new_string(type));
    plist_dict_set_item(report, "RestoreProtocolVersion", plist_new_uint(version));
    /* Hold a real FDR TCP connection while querying restored. No FDR request
     * is sent: this measures parallel USB transport, not FDR functionality. */
    if (idevice_connect(device, 1082, &parallel) != IDEVICE_E_SUCCESS) {
        fprintf(stderr, "Cannot open the parallel FDR transport\n");
        goto done;
    }
    plist_dict_set_item(report, "ParallelFDRConnection", plist_new_bool(1));
    const char *keys[] = {"HardwareInfo", "SavedDebugInfo"};
    for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
        if (restored_query_value(client, keys[i], &value) != RESTORE_E_SUCCESS) {
            fprintf(stderr, "Cannot query %s\n", keys[i]);
            goto done;
        }
        plist_dict_set_item(report, keys[i], value);
        value = NULL;
    }
    plist_to_xml(report, &xml, &length);
    if (!xml || fwrite(xml, 1, length, stdout) != length)
        goto done;
    result = 0;
done:
    if (parallel) idevice_disconnect(parallel);
    if (client) restored_client_free(client);
    if (device) idevice_free(device);
    plist_free(value);
    plist_free(report);
    free(type);
    free(xml);
    return result;
}
