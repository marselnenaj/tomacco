//! A bounded RSP client for a single non-mutating TCG instruction breakpoint.
//! The socket belongs to this job; no TCP listener or external debugger process.
use super::*;

const PACKET_LIMIT: usize = 4096;

pub(super) struct Breakpoint {
    stream: BufReader<UnixStream>,
    pub transcript: Vec<Value>,
}

impl Breakpoint {
    pub fn open(socket: &Path, address: u64) -> Result<Self> {
        let stream = UnixStream::connect(socket)?;
        stream.set_write_timeout(Some(Duration::from_secs(2)))?;
        let mut client = Self {
            stream: BufReader::new(stream),
            transcript: Vec::new(),
        };
        client.command("qSupported:hwbreak+")?;
        let stopped = client.command("?")?;
        if !stopped.starts_with('T') && !stopped.starts_with('S') {
            return Err(Error::new("pmp_debug_protocol", "Expected a stopped CPU"));
        }
        let reply = client.command(&format!("Z1,{address:x},4"))?;
        if reply != "OK" {
            return Err(Error::new(
                "pmp_breakpoint_unsupported",
                "Engine rejected the hardware instruction breakpoint",
            )
            .details(json!({"response":reply})));
        }
        Ok(client)
    }

    fn byte(&mut self, deadline: Instant) -> Result<u8> {
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(Error::new(
                    "pmp_debug_timeout",
                    "Debugger response timed out",
                ));
            }
            self.stream
                .get_mut()
                .set_read_timeout(Some(left.min(Duration::from_millis(250))))?;
            let mut byte = [0];
            match self.stream.read_exact(&mut byte) {
                Ok(()) => return Ok(byte[0]),
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock
                            | std::io::ErrorKind::TimedOut
                            | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(error) => return Err(error.into()),
            }
        }
    }

    fn packet(&mut self) -> Result<String> {
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut prefix = 0;
        loop {
            match self.byte(deadline)? {
                b'$' => break,
                b'+' if prefix < 8 => prefix += 1,
                _ => return Err(Error::new("pmp_debug_protocol", "Invalid RSP packet start")),
            }
        }
        let mut encoded = Vec::new();
        loop {
            let next = self.byte(deadline)?;
            if next == b'#' {
                break;
            }
            if encoded.len() == PACKET_LIMIT {
                return Err(Error::new(
                    "pmp_debug_protocol",
                    "RSP packet exceeds 4096 bytes",
                ));
            }
            encoded.push(next);
        }
        let mut expected = 0u8;
        for _ in 0..2 {
            let digit = (self.byte(deadline)? as char)
                .to_digit(16)
                .ok_or_else(|| Error::new("pmp_debug_protocol", "Invalid RSP checksum"))?;
            expected = (expected << 4) | digit as u8;
        }
        if encoded
            .iter()
            .fold(0u8, |sum, byte| sum.wrapping_add(*byte))
            != expected
        {
            return Err(Error::new("pmp_debug_protocol", "RSP checksum mismatch"));
        }
        self.stream.get_mut().write_all(b"+")?;
        let mut decoded = Vec::new();
        let mut bytes = encoded.into_iter();
        while let Some(byte) = bytes.next() {
            match byte {
                b'}' => decoded.push(
                    bytes
                        .next()
                        .ok_or_else(|| Error::new("pmp_debug_protocol", "Truncated RSP escape"))?
                        ^ 0x20,
                ),
                b'*' => {
                    let count = bytes.next().and_then(|n| n.checked_sub(29));
                    let previous = decoded.last().copied();
                    match (count, previous) {
                        (Some(count), Some(previous))
                            if count >= 3 && decoded.len() + usize::from(count) <= PACKET_LIMIT =>
                        {
                            decoded.extend(std::iter::repeat_n(previous, usize::from(count)));
                        }
                        _ => {
                            return Err(Error::new("pmp_debug_protocol", "Invalid RSP repetition"));
                        }
                    }
                }
                _ => decoded.push(byte),
            }
            if decoded.len() > PACKET_LIMIT {
                return Err(Error::new(
                    "pmp_debug_protocol",
                    "Decoded RSP packet too large",
                ));
            }
        }
        if !decoded.is_ascii() {
            return Err(Error::new(
                "pmp_debug_protocol",
                "Expected an ASCII debugger response",
            ));
        }
        Ok(String::from_utf8(decoded).unwrap())
    }

    fn send(&mut self, command: &str) -> Result<()> {
        let checksum = command.bytes().fold(0u8, u8::wrapping_add);
        self.stream
            .get_mut()
            .write_all(format!("${command}#{checksum:02x}").as_bytes())?;
        Ok(())
    }

    fn command(&mut self, command: &str) -> Result<String> {
        self.send(command)?;
        let response = self.packet()?;
        self.transcript
            .push(json!({"request":command,"response":response}));
        Ok(response)
    }

    pub fn resume(&mut self) -> Result<()> {
        self.send("c")?;
        self.transcript.push(json!({"request":"c"}));
        Ok(())
    }

    pub fn stopped(&mut self) -> Result<String> {
        let reply = self.packet()?;
        if !reply.starts_with("T05") && reply != "S05" {
            return Err(Error::new("pmp_debug_stop", "Expected a breakpoint stop")
                .details(json!({"response":reply})));
        }
        self.transcript.push(json!({"stop":reply}));
        Ok(reply)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(payload: &[u8]) -> Vec<u8> {
        let sum = payload
            .iter()
            .fold(0u8, |sum, byte| sum.wrapping_add(*byte));
        [b"$".as_slice(), payload, format!("#{sum:02x}").as_bytes()].concat()
    }

    fn client() -> (Breakpoint, UnixStream) {
        let (client, peer) = UnixStream::pair().unwrap();
        (
            Breakpoint {
                stream: BufReader::new(client),
                transcript: Vec::new(),
            },
            peer,
        )
    }

    #[test]
    fn fragmented_packets_acknowledgement_escaping_and_repetition() {
        let (mut client, mut peer) = client();
        let server = thread::spawn(move || {
            let bytes = [b"+".as_slice(), &frame(b"T05;v:0* ;quoted:}\x03;")].concat();
            for part in bytes.chunks(3) {
                peer.write_all(part).unwrap();
                thread::sleep(Duration::from_millis(1));
            }
            let mut ack = [0];
            peer.read_exact(&mut ack).unwrap();
            assert_eq!(&ack, b"+");
        });
        assert_eq!(client.packet().unwrap(), "T05;v:0000;quoted:#;");
        server.join().unwrap();
    }

    #[test]
    fn rejects_malformed_unbounded_and_non_ascii_packets() {
        let repeated = [b"0".as_slice(), b"*~".repeat(44).as_slice()].concat();
        let cases = [
            b"$OK#00".to_vec(),
            b"-$OK#9a".to_vec(),
            [b"+++++++++".as_slice(), &frame(b"OK")].concat(),
            frame(b"missing}"),
            frame(b"* "),
            frame(b"0*\x1e"),
            frame(&repeated),
            frame(&vec![b'x'; PACKET_LIMIT + 1]),
            frame(&[0xc3, 0xa4]),
        ];
        for bytes in cases {
            let (mut client, mut peer) = client();
            peer.write_all(&bytes).unwrap();
            assert_eq!(client.packet().unwrap_err().code, "pmp_debug_protocol");
        }
    }

    #[test]
    fn disconnected_mid_packet_fails_without_waiting_for_timeout() {
        let (mut client, mut peer) = client();
        peer.write_all(b"$T05thread:").unwrap();
        peer.shutdown(std::net::Shutdown::Write).unwrap();
        assert!(client.packet().is_err());
    }
}
