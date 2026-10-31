// SPDX-License-Identifier: Apache-2.0 OR BSD-3-Clause
// Owned TX snapshot for virtio-vsock streams whose header/data cross descriptors.
// Installed into the pinned rust-vmm backend by build_ios_guest_control.py.
use std::{
    io::{self, Read},
    ops::Deref,
};
use virtio_queue::DescriptorChain;
use virtio_vsock::packet::{VsockPacket, PKT_HEADER_SIZE};
use crate::GuestMemoryMmap;

pub(crate) struct TxPacket {
    header: [u8; PKT_HEADER_SIZE],
    data: Vec<u8>,
}

impl TxPacket {
    pub(crate) fn read<T>(
        mem: &GuestMemoryMmap,
        chain: DescriptorChain<T>,
        limit: u32,
    ) -> io::Result<Self>
    where
        T: Deref<Target = GuestMemoryMmap> + Clone,
    {
        // TX buffers are entirely device-readable, including trailing buffers.
        if chain.clone().any(|descriptor| descriptor.is_write_only()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "writable TX descriptor",
            ));
        }
        // Reader handles direct/indirect chains and guest memory-region splits.
        // Take one bounded snapshot: later guest writes cannot change the
        // length after validation or the header while forwarding its payload.
        let mut reader = chain.reader(mem).map_err(io::Error::other)?;
        let mut header = [0; PKT_HEADER_SIZE];
        reader.read_exact(&mut header)?;
        let length = u32::from_le_bytes(header[24..28].try_into().unwrap());
        if length > limit {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "TX payload exceeds negotiated limit",
            ));
        }
        let mut data = vec![0; length as usize];
        reader.read_exact(&mut data)?;
        Ok(Self { header, data })
    }

    pub(crate) fn with_packet<R>(
        &mut self,
        use_packet: impl FnOnce(&VsockPacket<'_, ()>) -> R,
    ) -> R {
        let original = self.header;
        // SAFETY: Both buffers are exclusively owned and remain allocated and
        // unmoved until the callback returns. While the packet exists, every
        // access goes through VolatileSlice. The callback cannot retain it.
        let mut packet = unsafe {
            VsockPacket::new(
                &mut self.header,
                if self.data.is_empty() {
                    None
                } else {
                    Some(&mut self.data)
                },
            )
        }
        .expect("fixed-size owned header");
        packet
            .set_header_from_raw(&original)
            .expect("fixed-size owned header");
        use_packet(&packet)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use virtio_bindings::bindings::virtio_ring::VRING_DESC_F_WRITE;
    use virtio_queue::{
        desc::{split::Descriptor, RawDescriptor},
        mock::MockSplitQueue,
    };
    use vm_memory::{Bytes, GuestAddress};

    #[test]
    fn split_header_and_many_data_descriptors_preserve_every_byte() {
        // Deliberately noncontiguous host mappings for adjacent guest regions.
        let mem = GuestMemoryMmap::from_ranges(&[
            (GuestAddress(0), 0x8000),
            (GuestAddress(0x8000), 0x8000),
            (GuestAddress(0x10000), 0x20000),
        ])
        .unwrap();
        let mut wire = vec![0; PKT_HEADER_SIZE];
        wire[0..8].copy_from_slice(&3u64.to_le_bytes());
        wire[8..16].copy_from_slice(&2u64.to_le_bytes());
        wire[24..28].copy_from_slice(&8192u32.to_le_bytes());
        wire.extend((0..8192).map(|n| (n % 251) as u8));
        let queue = MockSplitQueue::new(&mem, 16);
        let mut descriptors = Vec::new();
        let mut offset = 0;
        for (address, size) in [(0x6000, 13), (0x7000, 22), (0x7ff8, 20), (0x12000, 8181)] {
            mem.write_slice(&wire[offset..offset + size], GuestAddress(address))
                .unwrap();
            descriptors.push(RawDescriptor::from(Descriptor::new(
                address,
                size as u32,
                0,
                0,
            )));
            offset += size;
        }
        let mut packet =
            TxPacket::read(&mem, queue.build_desc_chain(&descriptors).unwrap(), 65536).unwrap();
        // Changing guest RAM after the snapshot must not change this packet.
        mem.write_slice(&[0; 13], GuestAddress(0x6000)).unwrap();
        packet.with_packet(|packet| {
            assert_eq!(packet.src_cid(), 3);
            assert_eq!(packet.dst_cid(), 2);
            assert_eq!(packet.len(), 8192);
            let mut actual = vec![0; 8192];
            packet
                .data_slice()
                .unwrap()
                .read_slice(&mut actual, 0)
                .unwrap();
            assert_eq!(&actual, &wire[PKT_HEADER_SIZE..]);
        });
    }

    #[test]
    fn invalid_lengths_permissions_and_short_chains_are_refused() {
        let mem = GuestMemoryMmap::from_ranges(&[(GuestAddress(0), 0x20000)]).unwrap();
        let queue = MockSplitQueue::new(&mem, 16);
        let mut header = [0; PKT_HEADER_SIZE];
        for (declared, size, flags) in [
            (65537u32, 44u32, 0u16),
            (10, 45, 0),
            (0, 43, 0),
            (0, 44, VRING_DESC_F_WRITE as u16),
        ] {
            header[24..28].copy_from_slice(&declared.to_le_bytes());
            mem.write_slice(&header, GuestAddress(0x10000)).unwrap();
            let descriptors = [RawDescriptor::from(Descriptor::new(
                0x10000, size, flags, 0,
            ))];
            assert!(
                TxPacket::read(&mem, queue.build_desc_chain(&descriptors).unwrap(), 65536).is_err()
            );
        }
    }
}
