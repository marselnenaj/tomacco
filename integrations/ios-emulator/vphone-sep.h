/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef TOMACCO_VPHONE_SEP_H
#define TOMACCO_VPHONE_SEP_H

#include "hw/core/sysbus.h"
#include "cpu.h"

#define TYPE_VPHONE_SEP "tomacco-vphone-sep"

uint8_t *vphone_sep_read_image(const char *path, size_t *size, Error **errp);
void vphone_sep_set_image(DeviceState *dev, const uint8_t *image, size_t size);

DeviceState *vphone_create_interrupts(ARMCPU **cpus, unsigned int cpu_count,
                                      MemoryRegion *memory, Object *owner);

#endif
