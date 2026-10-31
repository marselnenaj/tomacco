/*
 * Apple virtual USB device transport for the vphone600 research board.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Two modern VirtIO queues carry actual USB transactions. The private host
 * stream uses Orchard BDIF's little-endian length framing, not USB/IP. No USB
 * descriptors, acknowledgements or restore-service responses are synthesized.
 * See vphone-usb.md for the original driver evidence and protocol boundaries.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/module.h"
#include "qemu/iov.h"
#include "qemu/main-loop.h"
#include "chardev/char-fe.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "hw/virtio/virtio.h"
#include "hw/virtio/virtio-pci.h"
#include "migration/vmstate.h"

#define TYPE_VPHONE_USB "tomacco-vphone-usb"
#define TYPE_VPHONE_USB_PCI "tomacco-vphone-usb-pci"
#define VUSB_MAX_FRAME (128 * 1024)
#define VUSB_QUEUE_SIZE 64

OBJECT_DECLARE_SIMPLE_TYPE(VPhoneUSB, VPHONE_USB)
OBJECT_DECLARE_SIMPLE_TYPE(VPhoneUSBPCI, VPHONE_USB_PCI)

struct VPhoneUSB {
    VirtIODevice parent_obj;
    CharFrontend chr;
    VirtQueue *rxq, *txq;
    VirtQueueElement *tx;
    QEMUBH *bh;
    guint watch;
    bool connected;
    uint8_t rxbuf[VUSB_MAX_FRAME + 4], txbuf[VUSB_MAX_FRAME + 4];
    size_t rxlen, txlen, txoff;
};

struct VPhoneUSBPCI {
    VirtIOPCIProxy parent_obj;
    VPhoneUSB vdev;
};

static bool vusb_ready(VPhoneUSB *s)
{
    VirtIODevice *vdev = VIRTIO_DEVICE(s);
    return s->connected && vdev->vm_running && !virtio_device_disabled(vdev) &&
           (vdev->status & VIRTIO_CONFIG_S_DRIVER_OK);
}

static void vusb_clear(VPhoneUSB *s)
{
    if (s->watch) {
        g_source_remove(s->watch);
        s->watch = 0;
    }
    if (s->tx) {
        virtqueue_detach_element(s->txq, s->tx, 0);
        g_clear_pointer(&s->tx, g_free);
    }
    s->rxlen = s->txlen = s->txoff = 0;
}

static void vusb_fail(VPhoneUSB *s, const char *message)
{
    virtio_error(VIRTIO_DEVICE(s), "vphone USB: %s", message);
    vusb_clear(s);
    qemu_chr_fe_disconnect(&s->chr);
}

/* Invalid frames must never reach the original driver's assertion paths. */
static bool vusb_valid_request(const uint8_t *p, size_t len)
{
    uint32_t transfer = ldl_le_p(p);
    uint8_t ep = p[4], kind = p[5];
    size_t payload = len - 6;

    if (ep & 0x70 || transfer > VUSB_MAX_FRAME - 6) {
        return false;
    }
    if (kind == 2 || kind == 3) {
        return !transfer && !payload && (kind == 3 || ep == 0);
    }
    if (kind != 1 || ep == 0x80) {
        return false;
    }
    if (!ep) {
        if (payload < 8 || transfer != payload) {
            return false;
        }
        return payload == 8 + ((p[6] & 0x80) ? 0 : lduw_le_p(p + 12));
    }
    return ep & 0x80 ? !payload : payload == transfer;
}

static void vusb_receive_frames(VPhoneUSB *s)
{
    VirtIODevice *vdev = VIRTIO_DEVICE(s);

    while (s->rxlen >= 4) {
        uint32_t len = ldl_le_p(s->rxbuf);
        VirtQueueElement *elem;

        if (len < 6 || len > VUSB_MAX_FRAME) {
            vusb_fail(s, "invalid host frame length");
            return;
        }
        if (s->rxlen < len + 4 || !virtio_queue_ready(s->rxq)) {
            return;
        }
        if (!vusb_valid_request(s->rxbuf + 4, len)) {
            vusb_fail(s, "invalid host USB request");
            return;
        }
        elem = virtqueue_pop(s->rxq, sizeof(*elem));
        if (!elem) {
            return;
        }
        if (elem->out_num || iov_size(elem->in_sg, elem->in_num) < len) {
            virtqueue_detach_element(s->rxq, elem, 0);
            g_free(elem);
            vusb_fail(s, "guest receive buffer is too small or has wrong direction");
            return;
        }
        iov_from_buf(elem->in_sg, elem->in_num, 0, s->rxbuf + 4, len);
        virtqueue_push(s->rxq, elem, len);
        g_free(elem);
        virtio_notify(vdev, s->rxq);
        s->rxlen -= len + 4;
        memmove(s->rxbuf, s->rxbuf + len + 4, s->rxlen);
    }
}

static gboolean vusb_writable(void *unused, GIOCondition condition, void *opaque)
{
    VPhoneUSB *s = opaque;
    s->watch = 0;
    qemu_bh_schedule(s->bh);
    return G_SOURCE_REMOVE;
}

static void vusb_transmit_frames(VPhoneUSB *s)
{
    VirtIODevice *vdev = VIRTIO_DEVICE(s);

    /* Yield after a bounded batch so a busy USB client cannot starve QMP. */
    for (unsigned int batch = 0; batch < VUSB_QUEUE_SIZE; batch++) {
        int written;

        if (!s->tx) {
            size_t len;
            if (!virtio_queue_ready(s->txq)) {
                return;
            }
            s->tx = virtqueue_pop(s->txq, sizeof(*s->tx));
            if (!s->tx) {
                return;
            }
            len = iov_size(s->tx->out_sg, s->tx->out_num);
            if (s->tx->in_num || len < 2 || len > VUSB_MAX_FRAME) {
                vusb_fail(s, "invalid guest response descriptor");
                return;
            }
            stl_le_p(s->txbuf, len);
            iov_to_buf(s->tx->out_sg, s->tx->out_num, 0, s->txbuf + 4, len);
            s->txlen = len + 4;
            s->txoff = 0;
        }
        written = qemu_chr_fe_write(&s->chr, s->txbuf + s->txoff,
                                   s->txlen - s->txoff);
        if (!s->tx || !s->connected) {
            return;
        }
        if (written > 0) {
            s->txoff += written;
        }
        if (s->txoff < s->txlen) {
            if (!s->watch) {
                s->watch = qemu_chr_fe_add_watch(&s->chr, G_IO_OUT | G_IO_HUP,
                                                 vusb_writable, s);
                if (!s->watch) {
                    vusb_fail(s, "host stream cannot resume a partial write");
                }
            }
            return;
        }
        /* Completion follows the actual host write, never an absent peer. */
        virtqueue_push(s->txq, s->tx, 0);
        g_clear_pointer(&s->tx, g_free);
        s->txlen = s->txoff = 0;
        virtio_notify(vdev, s->txq);
    }
    qemu_bh_schedule(s->bh);
}

static void vusb_process(void *opaque)
{
    VPhoneUSB *s = opaque;
    if (!vusb_ready(s)) {
        return;
    }
    vusb_receive_frames(s);
    if (vusb_ready(s)) {
        vusb_transmit_frames(s);
        qemu_chr_fe_accept_input(&s->chr);
    }
}

static int vusb_can_read(void *opaque)
{
    VPhoneUSB *s = opaque;
    return vusb_ready(s) ? sizeof(s->rxbuf) - s->rxlen : 0;
}

static void vusb_read(void *opaque, const uint8_t *buf, int size)
{
    VPhoneUSB *s = opaque;
    if (size < 0 || size > sizeof(s->rxbuf) - s->rxlen) {
        vusb_fail(s, "host input exceeds bounded buffer");
        return;
    }
    memcpy(s->rxbuf + s->rxlen, buf, size);
    s->rxlen += size;
    qemu_bh_schedule(s->bh);
}

static void vusb_event(void *opaque, QEMUChrEvent event)
{
    VPhoneUSB *s = opaque;
    if (event == CHR_EVENT_OPENED) {
        s->connected = true;
        qemu_bh_schedule(s->bh);
    } else if (event == CHR_EVENT_CLOSED) {
        bool active = vusb_ready(s);
        s->connected = false;
        vusb_clear(s);
        if (active) {
            virtio_error(VIRTIO_DEVICE(s), "vphone USB: host disconnected; reset required");
        }
    }
}

static void vusb_kick(VirtIODevice *vdev, VirtQueue *vq)
{
    qemu_bh_schedule(VPHONE_USB(vdev)->bh);
}

static int vusb_status(VirtIODevice *vdev, uint8_t status)
{
    qemu_bh_schedule(VPHONE_USB(vdev)->bh);
    return 0;
}

static void vusb_reset(VirtIODevice *vdev)
{
    VPhoneUSB *s = VPHONE_USB(vdev);
    bool connected = s->connected;
    s->connected = false;
    qemu_bh_cancel(s->bh);
    vusb_clear(s);
    if (connected) {
        /* Partial stream frames cannot survive a queue/device reset. */
        qemu_chr_fe_disconnect(&s->chr);
    }
}

static uint64_t vusb_features(VirtIODevice *vdev, uint64_t features, Error **errp)
{
    return features;
}

static void vusb_realize(DeviceState *dev, Error **errp)
{
    VPhoneUSB *s = VPHONE_USB(dev);
    VirtIODevice *vdev = VIRTIO_DEVICE(dev);
    if (!qemu_chr_fe_backend_connected(&s->chr)) {
        error_setg(errp, "tomacco-vphone-usb requires a host stream chardev");
        return;
    }
    /* Apple private device identity; PCI overrides the generic 0x1040 base. */
    virtio_init(vdev, 0x1a01, 0);
    s->rxq = virtio_add_queue(vdev, VUSB_QUEUE_SIZE, vusb_kick);
    s->txq = virtio_add_queue(vdev, VUSB_QUEUE_SIZE, vusb_kick);
    s->bh = qemu_bh_new(vusb_process, s);
    qemu_chr_fe_set_handlers(&s->chr, vusb_can_read, vusb_read, vusb_event,
                             NULL, s, NULL, true);
}

static void vusb_unrealize(DeviceState *dev)
{
    VPhoneUSB *s = VPHONE_USB(dev);
    vusb_reset(VIRTIO_DEVICE(dev));
    qemu_chr_fe_set_handlers(&s->chr, NULL, NULL, NULL, NULL, NULL, NULL, false);
    qemu_bh_delete(s->bh);
    qemu_chr_fe_deinit(&s->chr, false);
    virtio_del_queue(VIRTIO_DEVICE(dev), 0);
    virtio_del_queue(VIRTIO_DEVICE(dev), 1);
    virtio_cleanup(VIRTIO_DEVICE(dev));
}

static const VMStateDescription vusb_vmstate = {
    .name = TYPE_VPHONE_USB,
    .unmigratable = true,
};

static const Property vusb_properties[] = {
    DEFINE_PROP_CHR("chardev", VPhoneUSB, chr),
};

static void vusb_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    VirtioDeviceClass *vc = VIRTIO_DEVICE_CLASS(klass);
    device_class_set_props(dc, vusb_properties);
    dc->vmsd = &vusb_vmstate;
    vc->realize = vusb_realize;
    vc->unrealize = vusb_unrealize;
    vc->get_features = vusb_features;
    vc->set_status = vusb_status;
    vc->reset = vusb_reset;
}

static void vusb_pci_realize(VirtIOPCIProxy *proxy, Error **errp)
{
    VPhoneUSBPCI *s = VPHONE_USB_PCI(proxy);
    virtio_pci_force_virtio_1(proxy);
    if (proxy->nvectors == DEV_NVECTORS_UNSPECIFIED) {
        proxy->nvectors = 3;
    }
    if (!qdev_realize(DEVICE(&s->vdev), BUS(&proxy->bus), errp)) {
        return;
    }
    pci_config_set_vendor_id(proxy->pci_dev.config, PCI_VENDOR_ID_APPLE);
    pci_config_set_device_id(proxy->pci_dev.config, 0x1a01);
}

static const Property vusb_pci_properties[] = {
    DEFINE_PROP_UINT32("vectors", VirtIOPCIProxy, nvectors, DEV_NVECTORS_UNSPECIFIED),
};

static void vusb_pci_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    PCIDeviceClass *pc = PCI_DEVICE_CLASS(klass);
    VIRTIO_PCI_CLASS(klass)->realize = vusb_pci_realize;
    device_class_set_props(dc, vusb_pci_properties);
    pc->vendor_id = PCI_VENDOR_ID_APPLE;
    pc->device_id = 0x1a01;
    pc->revision = 1;
    pc->class_id = PCI_CLASS_SERIAL_USB;
    set_bit(DEVICE_CATEGORY_USB, dc->categories);
}

static void vusb_pci_init(Object *obj)
{
    VPhoneUSBPCI *s = VPHONE_USB_PCI(obj);
    virtio_instance_init_common(obj, &s->vdev, sizeof(s->vdev), TYPE_VPHONE_USB);
}

static void vusb_register_types(void)
{
    static const TypeInfo info = {
        .name = TYPE_VPHONE_USB,
        .parent = TYPE_VIRTIO_DEVICE,
        .instance_size = sizeof(VPhoneUSB),
        .class_init = vusb_class_init,
    };
    static const VirtioPCIDeviceTypeInfo pci = {
        .generic_name = TYPE_VPHONE_USB_PCI,
        .instance_size = sizeof(VPhoneUSBPCI),
        .instance_init = vusb_pci_init,
        .class_init = vusb_pci_class_init,
    };
    type_register_static(&info);
    virtio_pci_types_register(&pci);
}
type_init(vusb_register_types)
