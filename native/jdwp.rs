//! Bounded JDWP transport and source-level Java/Kotlin debugging.
use crate::core::*;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    io::{Read, Write},
    net::{Shutdown, TcpStream},
    sync::mpsc::{self, Receiver},
    thread,
    time::Duration,
};

struct Packet {
    id: u32,
    reply: bool,
    code: u16,
    data: Vec<u8>,
}
struct Reader {
    bytes: Vec<u8>,
    at: usize,
}
impl Reader {
    fn new(bytes: Vec<u8>) -> Self {
        Self { bytes, at: 0 }
    }
    fn id(&mut self, size: usize) -> Result<u64> {
        if size > 8 || self.at + size > self.bytes.len() {
            return Err(Error::new("debug_protocol", "Truncated JDWP value"));
        }
        let mut value = 0;
        for x in &self.bytes[self.at..self.at + size] {
            value = (value << 8) | *x as u64;
        }
        self.at += size;
        Ok(value)
    }
    fn int(&mut self) -> Result<u32> {
        Ok(self.id(4)? as u32)
    }
    fn byte(&mut self) -> Result<u8> {
        Ok(self.id(1)? as u8)
    }
    fn string(&mut self) -> Result<String> {
        let count = self.int()? as usize;
        if count > 8 * 1024 * 1024 || self.at + count > self.bytes.len() {
            return Err(Error::new("debug_protocol", "Invalid JDWP string size"));
        }
        let value = String::from_utf8_lossy(&self.bytes[self.at..self.at + count]).into_owned();
        self.at += count;
        Ok(value)
    }
    fn count(&mut self) -> Result<usize> {
        let n = self.int()? as usize;
        if n > 100000 {
            return Err(Error::new(
                "debug_protocol",
                "JDWP collection exceeds limit",
            ));
        }
        Ok(n)
    }
}
fn put(out: &mut Vec<u8>, value: u64, size: usize) {
    out.extend_from_slice(&value.to_be_bytes()[8 - size..]);
}
fn string(out: &mut Vec<u8>, value: &str) {
    put(out, value.len() as u64, 4);
    out.extend(value.as_bytes());
}
fn number(value: &str) -> Result<u64> {
    value
        .parse()
        .map_err(|_| Error::new("invalid_argument", "Expected a debugger numeric ID"))
}
#[derive(Clone)]
struct Location {
    tag: u8,
    class: u64,
    method: u64,
    index: u64,
}
#[derive(Clone)]
struct Breakpoint {
    line: u32,
    requests: Vec<u32>,
}
pub struct Jdwp {
    stream: TcpStream,
    rx: Receiver<Result<Packet>>,
    pending: VecDeque<Packet>,
    next: u32,
    sizes: [usize; 5],
    pub stopped: bool,
    pub terminated: bool,
    pub thread: String,
    breakpoints: BTreeMap<String, Vec<Breakpoint>>,
    prepares: BTreeMap<String, u32>,
    frames: BTreeMap<String, Location>,
    steps: Vec<u32>,
    pub events: Vec<Value>,
}
impl Jdwp {
    pub fn connect(port: u16) -> Result<Self> {
        let mut stream = TcpStream::connect_timeout(
            &format!("127.0.0.1:{port}").parse().unwrap(),
            Duration::from_secs(10),
        )?;
        stream.set_read_timeout(Some(Duration::from_secs(10)))?;
        stream.set_write_timeout(Some(Duration::from_secs(10)))?;
        stream.write_all(b"JDWP-Handshake")?;
        let mut handshake = [0; 14];
        stream.read_exact(&mut handshake)?;
        if handshake != *b"JDWP-Handshake" {
            return Err(Error::new(
                "debug_protocol",
                "Target did not acknowledge JDWP handshake",
            ));
        }
        stream.set_read_timeout(None)?;
        let mut input = stream.try_clone()?;
        let (tx, rx) = mpsc::sync_channel(1024);
        thread::spawn(move || {
            loop {
                let result = (|| -> Result<Packet> {
                    let mut head = [0; 11];
                    input.read_exact(&mut head)?;
                    let size = u32::from_be_bytes(head[..4].try_into().unwrap()) as usize;
                    if !(11..=8 * 1024 * 1024).contains(&size) {
                        return Err(Error::new("debug_protocol", "Invalid JDWP packet size"));
                    }
                    let mut data = vec![0; size - 11];
                    input.read_exact(&mut data)?;
                    Ok(Packet {
                        id: u32::from_be_bytes(head[4..8].try_into().unwrap()),
                        reply: head[8] & 0x80 != 0,
                        code: u16::from_be_bytes([head[9], head[10]]),
                        data,
                    })
                })();
                let failed = result.is_err();
                if tx.send(result).is_err() || failed {
                    break;
                }
            }
        });
        let mut session = Self {
            stream,
            rx,
            pending: VecDeque::new(),
            next: 0,
            sizes: [8; 5],
            stopped: false,
            terminated: false,
            thread: String::new(),
            breakpoints: BTreeMap::new(),
            prepares: BTreeMap::new(),
            frames: BTreeMap::new(),
            steps: Vec::new(),
            events: Vec::new(),
        };
        let mut r = session.command(1, 7, vec![])?;
        for size in &mut session.sizes {
            *size = r.int()? as usize;
            if !(1..=8).contains(size) {
                return Err(Error::new("debug_protocol", "Unsupported JDWP ID size"));
            }
        }
        session.poll()?;
        Ok(session)
    }
    fn command(&mut self, set: u8, cmd: u8, data: Vec<u8>) -> Result<Reader> {
        self.next += 1;
        let mut out = Vec::with_capacity(data.len() + 11);
        put(&mut out, (data.len() + 11) as u64, 4);
        put(&mut out, self.next as u64, 4);
        out.extend([0, set, cmd]);
        out.extend(data);
        self.stream.write_all(&out)?;
        loop {
            let packet = self
                .rx
                .recv_timeout(Duration::from_secs(10))
                .map_err(|e| Error::new("debug_timeout", e.to_string()))??;
            if packet.reply && packet.id == self.next {
                if packet.code != 0 {
                    return Err(Error::new(
                        "jdwp_error",
                        format!("JDWP {set}/{cmd} returned error {}", packet.code),
                    )
                    .details(json!({"command_set":set,"command":cmd,"error_code":packet.code})));
                }
                return Ok(Reader::new(packet.data));
            }
            if !packet.reply {
                if self.pending.len() >= 1024 {
                    return Err(Error::new(
                        "debug_overflow",
                        "Too many unprocessed debug events",
                    ));
                }
                self.pending.push_back(packet);
            }
        }
    }
    fn id_arg(&self, id: u64, kind: usize) -> Vec<u8> {
        let mut v = vec![];
        put(&mut v, id, self.sizes[kind]);
        v
    }
    fn location(&self, r: &mut Reader) -> Result<Location> {
        Ok(Location {
            tag: r.byte()?,
            class: r.id(self.sizes[3])?,
            method: r.id(self.sizes[1])?,
            index: r.id(8)?,
        })
    }
    fn encode_location(&self, loc: &Location) -> Vec<u8> {
        let mut data = vec![loc.tag];
        put(&mut data, loc.class, self.sizes[3]);
        put(&mut data, loc.method, self.sizes[1]);
        put(&mut data, loc.index, 8);
        data
    }
    pub fn poll(&mut self) -> Result<()> {
        while let Ok(packet) = self.rx.try_recv() {
            match packet {
                Ok(p) => self.pending.push_back(p),
                Err(e) => {
                    self.terminated = true;
                    self.events
                        .push(json!({"event":"terminated","error":e.value()}));
                    return Ok(());
                }
            }
        }
        while let Some(packet) = self.pending.pop_front() {
            if packet.reply {
                continue;
            }
            // ART also publishes one-way DDMS chunks (APNM/WAIT/THST).
            // They are extension notifications, not composite JDWP events.
            if packet.code == 0xc701 {
                let kind = packet
                    .data
                    .get(..4)
                    .map(|v| String::from_utf8_lossy(v).into_owned())
                    .unwrap_or_default();
                self.events
                    .push(json!({"event":"android_ddm","chunk":kind,"bytes":packet.data.len()}));
                continue;
            }
            if packet.code != 0x4064 {
                return Err(Error::new(
                    "debug_protocol",
                    format!("Unsupported JDWP event packet 0x{:04x}", packet.code),
                ));
            }
            let mut r = Reader::new(packet.data);
            let policy = r.byte()?;
            let count = r.count()?;
            let mut prepared = false;
            let mut user_stop = false;
            for _ in 0..count {
                let kind = r.byte()?;
                let request = r.int()?;
                match kind {
                    1 | 2 => {
                        let thread = r.id(self.sizes[2])?;
                        let loc = self.location(&mut r)?;
                        self.thread = thread.to_string();
                        self.stopped = policy != 0;
                        user_stop = true;
                        let source = self
                            .describe(&loc)
                            .unwrap_or_else(|e| json!({"error":e.value()}));
                        self.events.push(json!({"event":"stopped","reason":if kind==1{"step"}else{"breakpoint"},"thread_id":self.thread,"location":source,"request":request}));
                        if kind == 1 {
                            let mut clear = vec![1];
                            put(&mut clear, request as u64, 4);
                            let _ = self.command(15, 2, clear);
                            self.steps.retain(|id| *id != request);
                        }
                    }
                    8 => {
                        let _thread = r.id(self.sizes[2])?;
                        let tag = r.byte()?;
                        let class = r.id(self.sizes[3])?;
                        let signature = r.string()?;
                        let _status = r.int()?;
                        let name = signature
                            .trim_start_matches('L')
                            .trim_end_matches(';')
                            .replace('/', ".");
                        if self.breakpoints.contains_key(&name) {
                            self.bind(&name, tag, class)?;
                        }
                        prepared = true;
                    }
                    90 => {
                        self.thread = r.id(self.sizes[2])?.to_string();
                        self.stopped = policy != 0;
                        user_stop = policy != 0;
                        self.events.push(json!({"event":"started","thread_id":self.thread,"stopped":self.stopped}));
                    }
                    99 | 100 => {
                        self.terminated = true;
                        self.events.push(json!({"event":"terminated"}));
                    }
                    _ => {
                        return Err(Error::new(
                            "debug_protocol",
                            format!("Unexpected JDWP event kind {kind}"),
                        ));
                    }
                }
            }
            if prepared && policy != 0 && !user_stop {
                self.command(1, 9, vec![])?;
            }
        }
        Ok(())
    }
    fn methods(&mut self, class: u64) -> Result<Vec<(u64, String, String)>> {
        let mut r = self.command(2, 5, self.id_arg(class, 3))?;
        let mut result = Vec::new();
        for _ in 0..r.count()? {
            let id = r.id(self.sizes[1])?;
            let name = r.string()?;
            let signature = r.string()?;
            r.int()?;
            result.push((id, name, signature));
        }
        Ok(result)
    }
    fn lines(&mut self, class: u64, method: u64) -> Result<Vec<(u64, u32)>> {
        let mut data = self.id_arg(class, 3);
        put(&mut data, method, self.sizes[1]);
        let mut r = self.command(6, 1, data)?;
        r.id(8)?;
        r.id(8)?;
        let mut lines = Vec::new();
        for _ in 0..r.count()? {
            lines.push((r.id(8)?, r.int()?));
        }
        Ok(lines)
    }
    fn describe(&mut self, loc: &Location) -> Result {
        let class = self.command(2, 1, self.id_arg(loc.class, 3))?.string()?;
        let file = self
            .command(2, 7, self.id_arg(loc.class, 3))
            .and_then(|mut r| r.string())
            .unwrap_or_default();
        let methods = self.methods(loc.class)?;
        let method = methods
            .iter()
            .find(|v| v.0 == loc.method)
            .map(|v| v.1.clone())
            .unwrap_or_default();
        let lines = self.lines(loc.class, loc.method).unwrap_or_default();
        let line = lines
            .iter()
            .filter(|v| v.0 <= loc.index)
            .max_by_key(|v| v.0)
            .map(|v| v.1);
        Ok(json!({"class":class,"method":method,"source":file,"line":line,"code_index":loc.index}))
    }
    fn bind(&mut self, name: &str, tag: u8, class: u64) -> Result<()> {
        let wanted: Vec<_> = self
            .breakpoints
            .get(name)
            .unwrap()
            .iter()
            .map(|b| b.line)
            .collect();
        let methods = self.methods(class)?;
        let mut all = Vec::new();
        for (method, _, _) in methods {
            match self.lines(class, method) {
                Ok(lines) => {
                    for (index, line) in lines {
                        all.push((
                            line,
                            Location {
                                tag,
                                class,
                                method,
                                index,
                            },
                        ));
                    }
                }
                Err(e)
                    if e.details
                        .as_ref()
                        .is_some_and(|v| [101, 511].contains(&(n(v, "error_code") as i32))) => {}
                Err(e) => return Err(e),
            }
        }
        for line in wanted {
            let actual = all.iter().filter(|v| v.0 >= line).map(|v| v.0).min();
            let mut requests = Vec::new();
            for (_, loc) in all.iter().filter(|v| Some(v.0) == actual) {
                let mut data = vec![2, 2];
                put(&mut data, 1, 4);
                data.push(7);
                data.extend(self.encode_location(loc));
                let request = self.command(15, 1, data)?.int()?;
                requests.push(request);
            }
            let b = self
                .breakpoints
                .get_mut(name)
                .unwrap()
                .iter_mut()
                .find(|b| b.line == line)
                .unwrap();
            b.requests.extend(&requests);
            self.events.push(json!({"event":"breakpoint","source":name,"line":line,"actual_line":actual,"verified":!requests.is_empty()}));
        }
        Ok(())
    }
    pub fn breakpoints(&mut self, source: &str, lines: &[u32]) -> Result {
        identifier(
            source,
            "Java/Kotlin class",
            Some(r"[A-Za-z_$][A-Za-z0-9_.$]*"),
        )?;
        if let Some(old) = self.breakpoints.remove(source) {
            for b in old {
                for id in b.requests {
                    let mut data = vec![2];
                    put(&mut data, id as u64, 4);
                    self.command(15, 2, data)?;
                }
            }
        }
        if let Some(id) = self.prepares.remove(source) {
            let mut data = vec![8];
            put(&mut data, id as u64, 4);
            self.command(15, 2, data)?;
        }
        self.breakpoints.insert(
            source.into(),
            lines
                .iter()
                .map(|line| Breakpoint {
                    line: *line,
                    requests: Vec::new(),
                })
                .collect(),
        );
        if !lines.is_empty() {
            let mut watch = vec![8, 2];
            put(&mut watch, 1, 4);
            watch.push(5);
            string(&mut watch, source);
            let id = self.command(15, 1, watch)?.int()?;
            self.prepares.insert(source.into(), id);
            let mut data = vec![];
            string(&mut data, &format!("L{};", source.replace('.', "/")));
            let mut r = self.command(1, 2, data)?;
            for _ in 0..r.count()? {
                let tag = r.byte()?;
                let class = r.id(self.sizes[3])?;
                let status = r.int()?;
                if status & 2 != 0 {
                    self.bind(source, tag, class)?;
                }
            }
        }
        Ok(
            json!({"source":source,"breakpoints":self.breakpoints[source].iter().map(|b|json!({"line":b.line,"verified":!b.requests.is_empty(),"pending_class_load":b.requests.is_empty()})).collect::<Vec<_>>()}),
        )
    }
    pub fn threads(&mut self) -> Result {
        let mut r = self.command(1, 4, vec![])?;
        let mut result = Vec::new();
        for _ in 0..r.count()? {
            let id = r.id(self.sizes[2])?;
            let name = self.command(11, 1, self.id_arg(id, 2))?.string()?;
            let mut state = self.command(11, 4, self.id_arg(id, 2))?;
            result.push(json!({"id":id.to_string(),"name":name,"status":state.int()?,"suspended":state.int()?!=0}));
        }
        Ok(json!({"threads":result}))
    }
    pub fn stack(&mut self, thread: &str, limit: u64) -> Result {
        let mut data = self.id_arg(number(thread)?, 2);
        put(&mut data, 0, 4);
        put(&mut data, limit.min(100), 4);
        let mut r = match self.command(11, 6, data) {
            Ok(v) => v,
            Err(e)
                if e.details
                    .as_ref()
                    .is_some_and(|v| n(v, "error_code") == 504) =>
            {
                let mut data = self.id_arg(number(thread)?, 2);
                put(&mut data, 0, 4);
                put(&mut data, u32::MAX as u64, 4);
                self.command(11, 6, data)?
            }
            Err(e) => return Err(e),
        };
        let mut result = Vec::new();
        self.frames.clear();
        for _ in 0..r.count()? {
            let id = r.id(self.sizes[4])?.to_string();
            let loc = self.location(&mut r)?;
            let mut frame = self.describe(&loc)?;
            frame["id"] = json!(id);
            self.frames.insert(id, loc);
            result.push(frame);
        }
        Ok(json!({"frames":result}))
    }
    fn value(&mut self, r: &mut Reader, tag: u8) -> Result {
        let kind = (tag as char).to_string();
        let value = match tag {
            b'B' => json!(r.byte()? as i8),
            b'C' => json!(
                char::from_u32(r.id(2)? as u32)
                    .unwrap_or('\u{FFFD}')
                    .to_string()
            ),
            b'S' => json!(r.id(2)? as i16),
            b'I' => json!(r.int()? as i32),
            b'J' => json!((r.id(8)? as i64).to_string()),
            b'Z' => json!(r.byte()? != 0),
            b'F' => json!(f32::from_bits(r.int()?)),
            b'D' => json!(f64::from_bits(r.id(8)?)),
            b'V' => Value::Null,
            b'L' | b's' | b'[' | b't' | b'g' | b'l' | b'c' => {
                let id = r.id(self.sizes[2])?;
                let text = if tag == b's' && id != 0 {
                    self.command(10, 1, self.id_arg(id, 2))?.string()?
                } else if id == 0 {
                    "null".into()
                } else {
                    format!("{}@{id}", kind)
                };
                return Ok(
                    json!({"type":kind,"value":text,"reference":if id==0{String::new()}else{format!("{}:{id}",if tag==b'['{"a"}else{"o"})}}),
                );
            }
            _ => {
                return Err(Error::new(
                    "debug_protocol",
                    format!("Unknown JDWP type tag {tag}"),
                ));
            }
        };
        Ok(json!({"type":kind,"value":value,"reference":""}))
    }
    pub fn variables(&mut self, thread: &str, frame: &str, reference: &str) -> Result {
        if !reference.is_empty() {
            return self.object(reference);
        }
        let loc = self.frames.get(frame).cloned().ok_or_else(|| {
            Error::new(
                "frame_missing",
                "Read the stack before reading frame variables",
            )
        })?;
        let mut data = self.id_arg(loc.class, 3);
        put(&mut data, loc.method, self.sizes[1]);
        let mut r = self.command(6, 2, data)?;
        r.int()?;
        let mut vars = Vec::new();
        for _ in 0..r.count()? {
            let start = r.id(8)?;
            let name = r.string()?;
            let signature = r.string()?;
            let length = r.int()? as u64;
            let slot = r.int()?;
            if loc.index >= start && loc.index < start.saturating_add(length) {
                vars.push((name, signature, slot));
            }
        }
        let mut data = self.id_arg(number(thread)?, 2);
        put(&mut data, number(frame)?, self.sizes[4]);
        put(&mut data, vars.len() as u64, 4);
        for (_, signature, slot) in &vars {
            put(&mut data, *slot as u64, 4);
            data.push(*signature.as_bytes().first().unwrap_or(&b'L'));
        }
        let mut values = self.command(16, 1, data)?;
        let count = values.count()?;
        if count != vars.len() {
            return Err(Error::new("debug_protocol", "JDWP variable count mismatch"));
        }
        let mut result = Vec::new();
        for (name, signature, _) in vars {
            let tag = values.byte()?;
            let mut value = self.value(&mut values, tag)?;
            value["name"] = json!(name);
            value["signature"] = json!(signature);
            result.push(value);
        }
        Ok(json!({"variables":result}))
    }
    fn object(&mut self, reference: &str) -> Result {
        let (kind, id) = reference
            .split_once(':')
            .ok_or_else(|| Error::new("invalid_reference", "Invalid object reference"))?;
        let id = number(id)?;
        if kind == "a" {
            let length = self.command(13, 1, self.id_arg(id, 2))?.int()?;
            let mut data = self.id_arg(id, 2);
            put(&mut data, 0, 4);
            put(&mut data, length.min(1000) as u64, 4);
            let mut r = self.command(13, 2, data)?;
            let tag = r.byte()?;
            let mut values = Vec::new();
            for i in 0..r.count()? {
                let actual = if b"L[stglc".contains(&tag) {
                    r.byte()?
                } else {
                    tag
                };
                let mut value = self.value(&mut r, actual)?;
                value["name"] = json!(format!("[{i}]"));
                values.push(value);
            }
            return Ok(json!({"variables":values,"total":length,"truncated":length>1000}));
        }
        if kind != "o" {
            return Err(Error::new(
                "invalid_reference",
                "Unknown object reference type",
            ));
        }
        let mut r = self.command(9, 1, self.id_arg(id, 2))?;
        r.byte()?;
        let class = r.id(self.sizes[3])?;
        let mut fields = self.command(2, 4, self.id_arg(class, 3))?;
        let mut names = Vec::new();
        for _ in 0..fields.count()? {
            let field = fields.id(self.sizes[0])?;
            let name = fields.string()?;
            let signature = fields.string()?;
            let modifiers = fields.int()?;
            if modifiers & 8 == 0 {
                names.push((field, name, signature));
            }
        }
        let mut data = self.id_arg(id, 2);
        put(&mut data, names.len() as u64, 4);
        for (field, _, _) in &names {
            put(&mut data, *field, self.sizes[0]);
        }
        let mut r = self.command(9, 2, data)?;
        if r.count()? != names.len() {
            return Err(Error::new("debug_protocol", "Field count mismatch"));
        }
        let mut values = Vec::new();
        for (_, name, signature) in names {
            let tag = r.byte()?;
            let mut value = self.value(&mut r, tag)?;
            value["name"] = json!(name);
            value["signature"] = json!(signature);
            values.push(value);
        }
        Ok(json!({"variables":values}))
    }
    pub fn control(&mut self, action: &str, thread: &str) -> Result {
        match action {
            "pause" => {
                if !self.stopped {
                    self.command(1, 8, vec![])?;
                    self.stopped = true;
                }
            }
            "continue" => {
                self.command(1, 9, vec![])?;
                self.stopped = false;
                self.frames.clear();
            }
            "next" | "stepIn" | "stepOut" => {
                let id = number(thread)?;
                for request in std::mem::take(&mut self.steps) {
                    let mut data = vec![1];
                    put(&mut data, request as u64, 4);
                    self.command(15, 2, data)?;
                }
                let mut data = vec![1, 2];
                put(&mut data, 2, 4);
                data.push(10);
                put(&mut data, id, self.sizes[2]);
                put(&mut data, 1, 4);
                put(
                    &mut data,
                    match action {
                        "stepIn" => 0,
                        "next" => 1,
                        _ => 2,
                    },
                    4,
                );
                data.push(1);
                put(&mut data, 1, 4);
                let request = self.command(15, 1, data)?.int()?;
                self.steps.push(request);
                self.command(1, 9, vec![])?;
                self.stopped = false;
                self.frames.clear();
            }
            "disconnect" => {
                let result = self.command(1, 6, vec![]);
                self.terminated = true;
                result?;
            }
            _ => return Err(Error::new("invalid_argument", "Unknown debugger action")),
        }
        Ok(json!({"action":action,"stopped":self.stopped,"terminated":self.terminated}))
    }
}
impl Drop for Jdwp {
    fn drop(&mut self) {
        let _ = self.stream.shutdown(Shutdown::Both);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_packets_fail_without_panics() {
        assert!(Reader::new(vec![1, 2]).id(4).is_err());
        assert!(Reader::new(vec![255; 4]).string().is_err());
        let mut bytes = vec![];
        string(&mut bytes, "a💡");
        assert_eq!(Reader::new(bytes).string().unwrap(), "a💡");
    }
}
