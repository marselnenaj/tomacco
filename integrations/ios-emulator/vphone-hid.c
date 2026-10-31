/*
 * Tomacco USB consumer-control and single-contact digitizer.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Standard USB HID reports, consumed by the guest's own USB/HID drivers.
 * No guest memory injection or UI acknowledgement is synthesized. QOM writes
 * enqueue reports; the read-only counters advance only on USB interrupt IN.
 */
#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "hw/usb/usb.h"
#include "hw/usb/hid.h"
#include "migration/vmstate.h"
#include "desc.h"

#define TYPE_VPHONE_HID "tomacco-ios-hid"
#define TOUCH_QUEUE 128
#define TOUCH_DOWN 0x07 /* Tip Switch, In Range, Confidence */
#define TOUCH_UP   0x0c /* Confidence, Untouch */
OBJECT_DECLARE_SIMPLE_TYPE(VPhoneHID, VPHONE_HID)

typedef struct HIDIdle {
    VPhoneHID *device;
    unsigned interface;
} HIDIdle;

struct VPhoneHID {
    USBDevice parent;
    QEMUTimer *home_timer;
    QEMUTimer *touch_timer;
    uint8_t home_report, home_queue[2], home_head, home_count;
    uint16_t home_hold_ms;
    bool home_active;
    uint8_t touch_report[7], touch_queue[TOUCH_QUEUE][7];
    unsigned touch_head, touch_count;
    uint8_t idle[2];
    QEMUTimer *idle_timer[2];
    HIDIdle idle_context[2];
    bool idle_pending[2];
    uint64_t home_delivered, touch_delivered;
};

/* Consumer Menu is the iOS Home usage (page 0x0c, usage 0x40).
 * This is deliberately distinct from Keyboard Home and browser AC Home. */
static const uint8_t home_descriptor[] = {
    0x05, 0x0c, 0x09, 0x01, 0xa1, 0x01,
    0x09, 0x40, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x01, 0x81, 0x02,
    0x75, 0x07, 0x95, 0x01, 0x81, 0x03, 0xc0,
};

/* USB-IF Digitizers: Touch Screen / Finger, tip, range, confidence, untouch,
 * contact ID, absolute X/Y, contact count and maximum contact count.
 * Untouch explicitly describes the final lift. Confidence stays set through
 * finger lift so the same contact remains classified as a finger. */
static const uint8_t touch_descriptor[] = {
    0x05, 0x0d, 0x09, 0x04, 0xa1, 0x01,
    0x09, 0x22, 0xa1, 0x02,
    0x09, 0x42, 0x09, 0x32, 0x09, 0x47, 0x09, 0x34,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x04, 0x81, 0x02,
    0x75, 0x04, 0x95, 0x01, 0x81, 0x03,
    0x09, 0x51, 0x25, 0x7f, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x26, 0xff, 0x7f, 0x75, 0x10, 0x95, 0x02, 0x81, 0x02,
    0xc0, 0x05, 0x0d, 0x09, 0x54,
    0x25, 0x01, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
    0x09, 0x55, 0xb1, 0x02, 0xc0,
};

#define HID_INTERFACE(num, endpoint, report) { \
    .bInterfaceNumber = num, .bNumEndpoints = 1, \
    .bInterfaceClass = USB_CLASS_HID, .ndesc = 1, \
    .descs = (USBDescOther[]) {{ .data = (uint8_t[]) { \
        9, USB_DT_HID, 0x11, 0x01, 0, 1, USB_DT_REPORT, sizeof(report), 0 \
    }}}, \
    .eps = (USBDescEndpoint[]) {{ \
        .bEndpointAddress = USB_DIR_IN | endpoint, \
        .bmAttributes = USB_ENDPOINT_XFER_INT, .wMaxPacketSize = 8, \
        .bInterval = 1, \
    }}, \
}

static const USBDescIface interfaces[] = {
    HID_INTERFACE(0, 1, home_descriptor),
    HID_INTERFACE(1, 2, touch_descriptor),
};

static const USBDescDevice device_descriptor = {
    .bcdUSB = 0x0110, .bMaxPacketSize0 = 8, .bNumConfigurations = 1,
    .confs = (USBDescConfig[]) {{
        .bNumInterfaces = 2, .bConfigurationValue = 1,
        .bmAttributes = USB_CFG_ATT_ONE, .bMaxPower = 1,
        .nif = 2, .ifs = interfaces,
    }},
};

static const USBDescStrings strings = {
    [1] = "Tomacco", [2] = "Tomacco Home and Touch",
    [3] = "tomacco-ios-hid-1",
};

static const USBDesc descriptor = {
    /* Experimental virtual device, not an Apple product identity. */
    .id = {.idVendor = 0x1b36, .idProduct = 0xf001, .bcdDevice = 0x0100,
           .iManufacturer = 1, .iProduct = 2, .iSerialNumber = 3},
    .full = &device_descriptor, .str = strings,
};

static void wake(VPhoneHID *s, unsigned endpoint)
{
    usb_wakeup(usb_ep_get(USB_DEVICE(s), USB_TOKEN_IN, endpoint), 0);
}

static void schedule_idle(VPhoneHID *s, unsigned interface)
{
    s->idle_pending[interface] = false;
    if (s->idle[interface]) {
        timer_mod(s->idle_timer[interface], qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL)
                  + 4 * s->idle[interface]);
    } else {
        timer_del(s->idle_timer[interface]);
    }
}

static void idle_expired(void *opaque)
{
    HIDIdle *idle = opaque;
    idle->device->idle_pending[idle->interface] = true;
    wake(idle->device, idle->interface + 1);
}

static void home_release(void *opaque)
{
    VPhoneHID *s = opaque;
    /* A second press cannot be queued until this release is consumed. */
    assert(s->home_active && s->home_count < 2);
    s->home_report = 0;
    s->home_queue[(s->home_head + s->home_count++) % 2] = 0;
    wake(s, 1);
}

static void touch_release(void *opaque)
{
    VPhoneHID *s = opaque;
    if (!(s->touch_report[0] & 1)) {
        return;
    }
    /* Contact-producing writes reserve this slot for release. The lease is
     * renewed by real USB delivery, and also works if the control client dies. */
    assert(s->touch_count < TOUCH_QUEUE);
    s->touch_report[0] = TOUCH_UP;
    memcpy(s->touch_queue[(s->touch_head + s->touch_count++) % TOUCH_QUEUE],
           s->touch_report, sizeof(s->touch_report));
    wake(s, 2);
}

static void set_home(Object *obj, Visitor *v, const char *name,
                     void *opaque, Error **errp)
{
    VPhoneHID *s = VPHONE_HID(obj);
    uint64_t ms;
    if (!visit_type_uint64(v, name, &ms, errp)) {
        return;
    }
    if (!ms || ms > 1000) {
        error_setg(errp, "Home hold must be 1..1000 ms");
    } else if (USB_DEVICE(s)->configuration != 1) {
        error_setg(errp, "USB HID is not configured by the guest");
    } else if (s->home_active) {
        error_setg(errp, "Previous Home report has not been consumed");
    } else {
        s->home_active = true;
        s->home_hold_ms = ms;
        s->home_report = 1;
        s->home_head = 0;
        s->home_count = 1;
        s->home_queue[0] = 1;
        wake(s, 1);
    }
}

/* Bits 0..14 = X, 16..30 = Y, 32 = contact; all other bits reserved.
 * An active contact always retains one queue slot for its final release. */
static void set_touch(Object *obj, Visitor *v, const char *name,
                      void *opaque, Error **errp)
{
    VPhoneHID *s = VPHONE_HID(obj);
    uint64_t value;
    uint8_t report[7];
    bool down;
    if (!visit_type_uint64(v, name, &value, errp)) {
        return;
    }
    if (value & ~UINT64_C(0x17fff7fff)) {
        error_setg(errp, "Invalid absolute touch coordinates or flags");
        return;
    }
    if (USB_DEVICE(s)->configuration != 1) {
        error_setg(errp, "USB HID is not configured by the guest");
        return;
    }
    down = value >> 32;
    report[0] = down ? TOUCH_DOWN : TOUCH_UP;
    report[1] = 1;
    stw_le_p(report + 2, value & 0x7fff);
    stw_le_p(report + 4, (value >> 16) & 0x7fff);
    /* This frame reports one contact, including its final tip-off sample. */
    report[6] = 1;
    if (!down) {
        memcpy(report + 2, s->touch_report + 2, 4);
    }
    if ((!down && !(s->touch_report[0] & 1)) ||
        !memcmp(report, s->touch_report, sizeof(report))) {
        return;
    }
    if (s->touch_count >= TOUCH_QUEUE - (down ? 1 : 0)) {
        error_setg(errp, "Touch reports await guest consumption");
        return;
    }
    memcpy(s->touch_report, report, sizeof(report));
    memcpy(s->touch_queue[(s->touch_head + s->touch_count++) % TOUCH_QUEUE],
           report, sizeof(report));
    if (!down) {
        timer_del(s->touch_timer);
    }
    wake(s, 2);
}

static void get_counter(Object *obj, Visitor *v, const char *name,
                        void *opaque, Error **errp)
{
    VPhoneHID *s = VPHONE_HID(obj);
    uint64_t value;
    if (!strcmp(name, "home-reports-delivered")) {
        value = s->home_delivered;
    } else if (!strcmp(name, "touch-reports-delivered")) {
        value = s->touch_delivered;
    } else if (!strcmp(name, "home-pending")) {
        value = s->home_active;
    } else if (!strcmp(name, "touch-pending")) {
        value = s->touch_count;
    } else if (!strcmp(name, "touch-active")) {
        value = s->touch_report[0] & 1;
    } else {
        value = USB_DEVICE(s)->configuration;
    }
    visit_type_uint64(v, name, &value, errp);
}

static void reset(USBDevice *dev)
{
    VPhoneHID *s = VPHONE_HID(dev);
    timer_del(s->home_timer);
    timer_del(s->touch_timer);
    s->home_report = s->home_head = s->home_count = 0;
    s->home_active = false;
    s->touch_head = s->touch_count = 0;
    memset(s->touch_report, 0, sizeof(s->touch_report));
    memset(s->idle, 0, sizeof(s->idle));
    for (unsigned i = 0; i < 2; i++) {
        schedule_idle(s, i);
    }
}

static void control(USBDevice *dev, USBPacket *p, int request,
                    int value, int index, int length, uint8_t *data)
{
    VPhoneHID *s = VPHONE_HID(dev);
    const uint8_t *report;
    size_t size;
    if (usb_desc_handle_control(dev, p, request, value, index, length, data) >= 0) {
        return;
    }
    if (index < 0 || index > 1) {
        goto stall;
    }
    switch (request) {
    case InterfaceRequest | USB_REQ_GET_DESCRIPTOR:
        if (value != (USB_DT_REPORT << 8)) {
            goto stall;
        }
        report = index ? touch_descriptor : home_descriptor;
        size = index ? sizeof(touch_descriptor) : sizeof(home_descriptor);
        p->actual_length = MIN(length, size);
        memcpy(data, report, p->actual_length);
        return;
    case HID_GET_REPORT:
        if (value == 0x300 && index == 1) {
            if (length) {
                data[0] = 1; /* Feature: maximum simultaneous contacts. */
                p->actual_length = 1;
            }
            return;
        }
        if (value != 0x100) {
            goto stall;
        }
        report = index ? s->touch_report : &s->home_report;
        p->actual_length = MIN(length, index ? 7 : 1);
        memcpy(data, report, p->actual_length);
        return;
    case HID_GET_IDLE:
        if (value || length < 1) {
            goto stall;
        }
        data[0] = s->idle[index];
        p->actual_length = 1;
        return;
    case HID_SET_IDLE:
        if (value & 0xff) {
            goto stall;
        }
        s->idle[index] = value >> 8;
        schedule_idle(s, index);
        return;
    default:
    stall:
        p->status = USB_RET_STALL;
    }
}

static void data(USBDevice *dev, USBPacket *p)
{
    VPhoneHID *s = VPHONE_HID(dev);
    if (p->pid != USB_TOKEN_IN || (p->ep->nr != 1 && p->ep->nr != 2)) {
        p->status = USB_RET_STALL;
        return;
    }
    if (p->iov.size < (p->ep->nr == 1 ? 1 : 7)) {
        p->status = USB_RET_STALL;
        return;
    }
    if (p->ep->nr == 1 && s->home_count) {
        uint8_t report = s->home_queue[s->home_head];
        usb_packet_copy(p, &report, 1);
        s->home_head = (s->home_head + 1) % 2;
        s->home_count--;
        s->home_delivered++;
        schedule_idle(s, 0);
        if (report) {
            timer_mod(s->home_timer, qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) +
                      s->home_hold_ms);
        } else {
            s->home_active = false;
        }
    } else if (p->ep->nr == 2 && s->touch_count) {
        bool down = s->touch_queue[s->touch_head][0] & 1;
        usb_packet_copy(p, s->touch_queue[s->touch_head], 7);
        s->touch_head = (s->touch_head + 1) % TOUCH_QUEUE;
        s->touch_count--;
        s->touch_delivered++;
        schedule_idle(s, 1);
        if (down && (s->touch_report[0] & 1)) {
            timer_mod(s->touch_timer, qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 5000);
        }
    } else if (s->idle_pending[p->ep->nr - 1]) {
        unsigned interface = p->ep->nr - 1;
        usb_packet_copy(p, interface ? s->touch_report : &s->home_report,
                        interface ? 7 : 1);
        if (interface) {
            s->touch_delivered++;
        } else {
            s->home_delivered++;
        }
        schedule_idle(s, interface);
    } else {
        p->status = USB_RET_NAK;
    }
}

static void realize(USBDevice *dev, Error **errp)
{
    VPhoneHID *s = VPHONE_HID(dev);
    s->home_timer = timer_new_ms(QEMU_CLOCK_VIRTUAL, home_release, s);
    s->touch_timer = timer_new_ms(QEMU_CLOCK_VIRTUAL, touch_release, s);
    for (unsigned i = 0; i < 2; i++) {
        s->idle_context[i] = (HIDIdle){s, i};
        s->idle_timer[i] = timer_new_ms(QEMU_CLOCK_VIRTUAL, idle_expired,
                                       &s->idle_context[i]);
    }
    usb_desc_init(dev);
}

static void unrealize(USBDevice *dev)
{
    VPhoneHID *s = VPHONE_HID(dev);
    timer_del(s->home_timer);
    timer_free(s->home_timer);
    timer_del(s->touch_timer);
    timer_free(s->touch_timer);
    for (unsigned i = 0; i < 2; i++) {
        timer_del(s->idle_timer[i]);
        timer_free(s->idle_timer[i]);
    }
}

static void instance_init(Object *obj)
{
    static const char *names[] = {"configured", "home-pending", "touch-pending", "touch-active",
        "home-reports-delivered", "touch-reports-delivered"};
    object_property_add(obj, "home-press-ms", "uint64", NULL, set_home, NULL, NULL);
    object_property_add(obj, "touch-state", "uint64", NULL, set_touch, NULL, NULL);
    for (size_t i = 0; i < G_N_ELEMENTS(names); i++) {
        object_property_add(obj, names[i], "uint64", get_counter, NULL, NULL, NULL);
    }
}

static const VMStateDescription vmstate = {
    .name = TYPE_VPHONE_HID, .unmigratable = 1,
};

static void class_init(ObjectClass *klass, const void *opaque)
{
    USBDeviceClass *uc = USB_DEVICE_CLASS(klass);
    DEVICE_CLASS(klass)->vmsd = &vmstate;
    uc->product_desc = "Tomacco Home and Touch";
    uc->usb_desc = &descriptor;
    uc->realize = realize;
    uc->unrealize = unrealize;
    uc->handle_reset = reset;
    uc->handle_control = control;
    uc->handle_data = data;
    uc->handle_attach = usb_desc_attach;
}

static const TypeInfo type = {
    .name = TYPE_VPHONE_HID, .parent = TYPE_USB_DEVICE,
    .instance_size = sizeof(VPhoneHID), .instance_init = instance_init,
    .class_init = class_init,
};

static void register_types(void)
{
    type_register_static(&type);
}
type_init(register_types)
