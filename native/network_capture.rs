//! Opt-in capture of an emulator's network traffic with the tcpdump of its
//! system image, as root through `su`, and a summary that names hosts and
//! requests but keeps no contents. With an app given, only the packets the
//! app sends are captured: an iptables rule logs them by its user ID.
use crate::{core::*, devices, toolchain as tc};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    thread::sleep,
    time::Duration,
};

/// The NFLOG group that carries one app's packets to tcpdump.
const GROUP: &str = "47";

fn shell(device: &str, args: &[&str]) -> Result<String> {
    let result = tc::run(&devices::shell(device, &argv(args))?, 30, None, true)?;
    Ok(s(&result, "stdout").replace('\r', ""))
}

fn root(device: &str, script: &str) -> Result<String> {
    shell(device, &["su", "0", "sh", "-c", script])
}

fn state_path(device: &str) -> Result<PathBuf> {
    Ok(home()?
        .join("network-captures")
        .join(format!("{device}.json")))
}

fn rules(uid: &str, verb: &str) -> String {
    ["iptables", "ip6tables"]
        .iter()
        .map(|table| {
            format!(
                "{table} -{verb} OUTPUT -m owner --uid-owner {uid} -j NFLOG --nflog-group {GROUP}"
            )
        })
        .collect::<Vec<_>>()
        .join("; ")
}

pub fn capture(a: &Value) -> Result {
    let action = s(a, "action");
    if action == "summary" {
        let file = expand(s(a, "path"));
        if !file.is_file() {
            return Err(Error::new(
                "invalid_path",
                format!("No capture file at {}", file.display()),
            ));
        }
        let mut result = summarize(&fs::read(&file)?)?;
        result["path"] = json!(file);
        return Ok(result);
    }
    let device = s(a, "device");
    if !device.starts_with("emulator-") {
        return Err(
            Error::new("not_emulator", "Network capture needs an emulator serial")
                .remedy("Phones do not let adb capture packets without root"),
        );
    }
    identifier(device, "device ID", None)?;
    let state_file = state_path(device)?;
    if action == "start" {
        if !b(a, "opt_in") {
            return Err(Error::new(
                "opt_in_required",
                "Network capture records every packet, contents included, of the emulator or the app. Pass opt_in:true to start it",
            )
            .remedy("Capture only test accounts and test data; the capture file stays on this computer"));
        }
        if state_file.exists() {
            return Err(Error::new(
                "capture_running",
                format!("A capture is already running on {device}"),
            )
            .remedy("Stop it with action stop"));
        }
        if root(device, "id -u")?.trim() != "0" {
            return Err(Error::new(
                "root_unavailable",
                "This emulator does not allow root through su",
            )
            .remedy(
                "Use a Google APIs or AOSP system image; Google Play images do not allow root",
            ));
        }
        let id = s(a, "bundle_id");
        let uid = if id.is_empty() {
            None
        } else {
            identifier(
                id,
                "application ID",
                Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
            )?;
            let listing = shell(device, &["cmd", "package", "list", "packages", "-U", id])?;
            Some(devices::package_uid(&listing, id).ok_or_else(|| {
                Error::new(
                    "app_not_installed",
                    format!("{id} is not installed on {device}"),
                )
            })?)
        };
        let remote = format!("/data/local/tmp/tomacco-capture-{device}.pcap");
        let interface = if let Some(uid) = &uid {
            root(device, &rules(uid, "I"))?;
            format!("nflog:{GROUP}")
        } else {
            "any".into()
        };
        let started = root(
            device,
            &format!(
                "setsid tcpdump -i {interface} -s 0 -U -w {remote} </dev/null >/dev/null 2>&1 & echo $!"
            ),
        )?;
        let pid = started.trim().to_string();
        if pid.is_empty() || !pid.bytes().all(|c| c.is_ascii_digit()) {
            if let Some(uid) = &uid {
                let _ = root(device, &rules(uid, "D"));
            }
            return Err(Error::new("capture_failed", "tcpdump did not start")
                .details(json!({"output":started})));
        }
        // tcpdump writes the file's header once it listens.
        for _ in 0..20 {
            if root(device, &format!("test -s {remote} && echo ready || true"))?.contains("ready") {
                break;
            }
            sleep(Duration::from_millis(250));
        }
        let state = json!({"device":device,"pid":pid,"uid":uid,"bundle_id":id,"remote":remote,
            "interface":interface,"started":now()});
        private_dir(state_file.parent().unwrap())?;
        atomic_write(&state_file, state.to_string().as_bytes())?;
        return Ok(json!({"device":device,"action":action,"capturing":true,
            "scope":if uid.is_some() {"app"} else {"device"},"bundle_id":id,"interface":interface}));
    }
    let state: Value = serde_json::from_slice(&fs::read(&state_file).map_err(|_| {
        Error::new(
            "capture_not_running",
            format!("No capture is running on {device}"),
        )
    })?)?;
    let pid = s(&state, "pid");
    let _ = root(device, &format!("kill -INT {pid}"));
    sleep(Duration::from_millis(700));
    if let Some(uid) = state["uid"].as_str() {
        let _ = root(device, &rules(uid, "D"));
    }
    let output = if s(a, "path").is_empty() {
        home()?
            .join("network-captures")
            .join(format!("{device}-{}.pcap", now() as u64))
    } else {
        expand(s(a, "path"))
    };
    if !output.is_absolute() {
        return Err(Error::new("invalid_path", "path must be absolute"));
    }
    let mut pull = devices::adb(device)?;
    pull.extend(argv(&["pull", s(&state, "remote")]));
    pull.push(output.to_string_lossy().into_owned());
    let pulled = tc::run(&pull, 120, None, true);
    let _ = root(device, &format!("rm -f {}", s(&state, "remote")));
    fs::remove_file(&state_file)?;
    pulled?;
    set_private(&output)?;
    let mut result = summarize(&fs::read(&output)?)?;
    result["device"] = json!(device);
    result["action"] = json!(action);
    result["path"] = json!(output);
    result["scope"] = json!(if state["uid"].is_string() {
        "app"
    } else {
        "device"
    });
    result["bundle_id"] = state["bundle_id"].clone();
    result["seconds"] =
        json!(((now() - state["started"].as_f64().unwrap_or(now())) * 10.0).round() / 10.0);
    Ok(result)
}

fn set_private(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

/// One packet's IP payload: protocol, addresses, ports, TCP flags and the
/// transport payload.
struct Packet<'a> {
    tcp: bool,
    source: String,
    destination: String,
    source_port: u16,
    destination_port: u16,
    syn_only: bool,
    payload: &'a [u8],
    length: usize,
}

fn u16_at(data: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_be_bytes(data.get(at..at + 2)?.try_into().ok()?))
}

fn ip_packet(data: &[u8]) -> Option<Packet<'_>> {
    let version = data.first()? >> 4;
    let (protocol, source, destination, rest) = if version == 4 {
        let header = ((data[0] & 0x0f) as usize) * 4;
        let total = (u16_at(data, 2)? as usize).min(data.len());
        let source = std::net::Ipv4Addr::from(<[u8; 4]>::try_from(data.get(12..16)?).ok()?);
        let destination = std::net::Ipv4Addr::from(<[u8; 4]>::try_from(data.get(16..20)?).ok()?);
        (
            *data.get(9)?,
            source.to_string(),
            destination.to_string(),
            data.get(header..total)?,
        )
    } else if version == 6 {
        let source = std::net::Ipv6Addr::from(<[u8; 16]>::try_from(data.get(8..24)?).ok()?);
        let destination = std::net::Ipv6Addr::from(<[u8; 16]>::try_from(data.get(24..40)?).ok()?);
        (
            *data.get(6)?,
            source.to_string(),
            destination.to_string(),
            data.get(40..)?,
        )
    } else {
        return None;
    };
    let (tcp, payload, syn_only) = match protocol {
        6 => {
            let offset = ((rest.get(12)? >> 4) as usize) * 4;
            let flags = *rest.get(13)?;
            (true, rest.get(offset..)?, flags & 0x12 == 0x02)
        }
        17 => (false, rest.get(8..)?, false),
        _ => return None,
    };
    Some(Packet {
        tcp,
        source,
        destination,
        source_port: u16_at(rest, 0)?,
        destination_port: u16_at(rest, 2)?,
        syn_only,
        payload,
        length: data.len(),
    })
}

/// The IP packet inside a captured frame of this link type.
fn frame(link: u32, data: &[u8]) -> Option<&[u8]> {
    match link {
        // Ethernet, with one VLAN tag at most.
        1 => {
            let (kind, at) = match u16_at(data, 12)? {
                0x8100 => (u16_at(data, 16)?, 18),
                kind => (kind, 14),
            };
            matches!(kind, 0x0800 | 0x86dd).then(|| data.get(at..))?
        }
        101 | 12 => Some(data),
        113 => data.get(16..),
        276 => data.get(20..),
        // NFLOG: a 4-byte header, then type-length-value attributes in host
        // order, each padded to 4 bytes; type 9 is the packet.
        239 => {
            let mut at = 4;
            while at + 4 <= data.len() {
                let length = u16::from_le_bytes([data[at], data[at + 1]]) as usize;
                let kind = u16::from_le_bytes([data[at + 2], data[at + 3]]);
                if length < 4 {
                    return None;
                }
                if kind == 9 {
                    return data.get(at + 4..at + length);
                }
                at += (length + 3) & !3;
            }
            None
        }
        _ => None,
    }
}

/// The server name of a TLS ClientHello.
fn server_name(data: &[u8]) -> Option<String> {
    if data.first()? != &0x16 || data.get(5)? != &0x01 {
        return None;
    }
    let mut at = 9 + 2 + 32;
    at += 1 + *data.get(at)? as usize;
    at += 2 + u16_at(data, at)? as usize;
    at += 1 + *data.get(at)? as usize;
    let end = at + 2 + u16_at(data, at)? as usize;
    at += 2;
    while at + 4 <= end.min(data.len()) {
        let kind = u16_at(data, at)?;
        let length = u16_at(data, at + 2)? as usize;
        if kind == 0 {
            let name_length = u16_at(data, at + 7)? as usize;
            let name = data.get(at + 9..at + 9 + name_length)?;
            return String::from_utf8(name.to_vec()).ok();
        }
        at += 4 + length;
    }
    None
}

/// Method, host and path without its query of an HTTP request.
fn http_request(data: &[u8]) -> Option<(String, String, String)> {
    let text = std::str::from_utf8(data.get(..data.len().min(4096))?).ok()?;
    let mut lines = text.split("\r\n");
    let mut parts = lines.next()?.split(' ');
    let method = parts.next()?;
    if !["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].contains(&method) {
        return None;
    }
    let target = parts.next()?;
    if !parts.next()?.starts_with("HTTP/") {
        return None;
    }
    let host = lines
        .find_map(|l| {
            l.split_once(':')
                .filter(|(k, _)| k.eq_ignore_ascii_case("host"))
                .map(|(_, v)| v.trim().to_string())
        })
        .unwrap_or_default();
    let path: String = target
        .split(['?', '#'])
        .next()
        .unwrap_or("")
        .chars()
        .take(200)
        .collect();
    Some((method.to_string(), host, path))
}

/// The name asked for by a DNS query.
fn dns_query(data: &[u8]) -> Option<String> {
    if data.get(2)? & 0x80 != 0 || u16_at(data, 4)? == 0 {
        return None;
    }
    let mut at = 12;
    let mut labels = Vec::new();
    loop {
        let length = *data.get(at)? as usize;
        if length == 0 || length > 63 {
            break;
        }
        labels.push(String::from_utf8(data.get(at + 1..at + 1 + length)?.to_vec()).ok()?);
        at += 1 + length;
    }
    (!labels.is_empty()).then(|| labels.join("."))
}

fn count(map: &mut BTreeMap<String, u64>, key: String) {
    *map.entry(key).or_default() += 1;
}

fn counted(map: BTreeMap<String, u64>, key: &str) -> Value {
    let mut list: Vec<_> = map.into_iter().collect();
    list.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    Value::Array(
        list.into_iter()
            .take(200)
            .map(|(name, n)| json!({key:name,"count":n}))
            .collect(),
    )
}

fn well_known(port: u16) -> bool {
    matches!(port, 53 | 80 | 443 | 853 | 8080 | 8443) || port < 1024
}

/// Hosts, TLS server names, HTTP requests and DNS queries of a pcap file.
/// Payloads, headers other than Host, and query strings are left out.
pub fn summarize(data: &[u8]) -> Result {
    let invalid = || Error::new("invalid_capture", "Not a pcap capture file");
    let magic = data.get(..4).ok_or_else(invalid)?;
    let little = match magic {
        [0xd4, 0xc3, 0xb2, 0xa1] | [0x4d, 0x3c, 0xb2, 0xa1] => true,
        [0xa1, 0xb2, 0xc3, 0xd4] | [0xa1, 0xb2, 0x3c, 0x4d] => false,
        _ => return Err(invalid()),
    };
    let word = |at: usize| -> Option<u32> {
        let bytes: [u8; 4] = data.get(at..at + 4)?.try_into().ok()?;
        Some(if little {
            u32::from_le_bytes(bytes)
        } else {
            u32::from_be_bytes(bytes)
        })
    };
    let link = word(20).ok_or_else(invalid)?;
    let mut at = 24;
    let (mut packets, mut bytes) = (0u64, 0u64);
    let (mut first, mut last) = (None, 0u32);
    let mut hosts: BTreeMap<(String, u16, &str), Value> = BTreeMap::new();
    let (mut names, mut requests, mut queries) =
        (BTreeMap::new(), BTreeMap::new(), BTreeMap::new());
    while let (Some(seconds), Some(length)) = (word(at), word(at + 8)) {
        let Some(captured) = data.get(at + 16..at + 16 + length as usize) else {
            break;
        };
        at += 16 + length as usize;
        packets += 1;
        bytes += captured.len() as u64;
        first.get_or_insert(seconds);
        last = seconds;
        let Some(packet) = frame(link, captured).and_then(ip_packet) else {
            continue;
        };
        let to_server = packet.syn_only
            || (well_known(packet.destination_port) && !well_known(packet.source_port))
            || (well_known(packet.destination_port) == well_known(packet.source_port)
                && packet.destination_port < packet.source_port);
        let (server, port) = if to_server {
            (packet.destination.clone(), packet.destination_port)
        } else {
            (packet.source.clone(), packet.source_port)
        };
        let protocol = match (packet.tcp, port) {
            (_, 53) => "dns",
            (true, 853) => "dns-over-tls",
            (true, 443 | 8443) => "tls",
            (true, 80 | 8080) => "http",
            (false, 443) => "quic",
            (true, _) => "tcp",
            (false, _) => "udp",
        };
        let entry = hosts
            .entry((server.clone(), port, protocol))
            .or_insert_with(|| json!({"address":server,"port":port,"protocol":protocol,"names":[],"packets":0,"bytes_sent":0,"bytes_received":0,"connections":0}));
        entry["packets"] = json!(entry["packets"].as_u64().unwrap_or(0) + 1);
        let direction = if to_server {
            "bytes_sent"
        } else {
            "bytes_received"
        };
        entry[direction] = json!(entry[direction].as_u64().unwrap_or(0) + packet.length as u64);
        if packet.syn_only {
            entry["connections"] = json!(entry["connections"].as_u64().unwrap_or(0) + 1);
        }
        let mut name = None;
        if to_server && packet.tcp {
            if let Some(sni) = server_name(packet.payload) {
                count(&mut names, sni.clone());
                name = Some(sni);
            } else if let Some((method, host, path)) = http_request(packet.payload) {
                count(&mut requests, format!("{method} {host}{path}"));
                name = Some(host);
            }
        }
        if to_server && !packet.tcp && port == 53 {
            if let Some(query) = dns_query(packet.payload) {
                count(&mut queries, query);
            }
        }
        if let Some(name) = name.filter(|n| !n.is_empty()) {
            let list = entry["names"].as_array_mut().unwrap();
            if !list.contains(&json!(name)) {
                list.push(json!(name));
            }
        }
    }
    let mut hosts: Vec<Value> = hosts.into_values().collect();
    hosts.sort_by_key(|h| {
        std::cmp::Reverse(
            h["bytes_sent"].as_u64().unwrap_or(0) + h["bytes_received"].as_u64().unwrap_or(0),
        )
    });
    hosts.truncate(200);
    let requests = counted(requests, "request");
    let requests: Vec<Value> = requests
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            let text = s(r, "request");
            let (method, rest) = text.split_once(' ').unwrap_or((text, ""));
            let (host, path) = rest
                .find('/')
                .map(|i| rest.split_at(i))
                .unwrap_or((rest, ""));
            json!({"method":method,"host":host,"path":path,"count":r["count"]})
        })
        .collect();
    Ok(json!({"link_type":link,"packets":packets,"bytes":bytes,
        "duration_seconds":first.map(|f| last.saturating_sub(f)).unwrap_or(0),
        "hosts":hosts,"tls_server_names":counted(names,"name"),"http_requests":requests,
        "dns_queries":counted(queries,"name"),
        "privacy":"The summary has no contents: names, paths without their query, sizes and counts. The capture file holds every packet, readable only by you."}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ipv4_tcp(source_port: u16, destination_port: u16, flags: u8, payload: &[u8]) -> Vec<u8> {
        let mut tcp = vec![0u8; 20];
        tcp[0..2].copy_from_slice(&source_port.to_be_bytes());
        tcp[2..4].copy_from_slice(&destination_port.to_be_bytes());
        tcp[12] = 5 << 4;
        tcp[13] = flags;
        tcp.extend_from_slice(payload);
        let mut ip = vec![
            0x45, 0, 0, 0, 0, 0, 0x40, 0, 64, 6, 0, 0, 10, 0, 2, 16, 93, 184, 215, 14,
        ];
        if source_port < destination_port {
            ip[12..20].rotate_left(4);
        }
        let total = (20 + tcp.len()) as u16;
        ip[2..4].copy_from_slice(&total.to_be_bytes());
        ip.extend(tcp);
        ip
    }

    fn client_hello(name: &str) -> Vec<u8> {
        let mut extension = vec![0, 0];
        let list_length = (name.len() + 3) as u16;
        extension.extend((list_length + 2).to_be_bytes());
        extension.extend(list_length.to_be_bytes());
        extension.push(0);
        extension.extend((name.len() as u16).to_be_bytes());
        extension.extend(name.as_bytes());
        let mut body = vec![3, 3];
        body.extend([0u8; 32]);
        body.push(0);
        body.extend([0, 2, 0x13, 0x01]);
        body.extend([1, 0]);
        body.extend((extension.len() as u16).to_be_bytes());
        body.extend(extension);
        let mut handshake = vec![1, 0];
        handshake.extend((body.len() as u16).to_be_bytes());
        handshake.extend(body);
        let mut record = vec![0x16, 3, 1];
        record.extend((handshake.len() as u16).to_be_bytes());
        record.extend(handshake);
        record
    }

    fn pcap(link: u32, frames: &[Vec<u8>]) -> Vec<u8> {
        let mut out = vec![0xd4, 0xc3, 0xb2, 0xa1, 2, 0, 4, 0];
        out.extend([0u8; 8]);
        out.extend(65535u32.to_le_bytes());
        out.extend(link.to_le_bytes());
        for (i, f) in frames.iter().enumerate() {
            out.extend((100 + i as u32).to_le_bytes());
            out.extend(0u32.to_le_bytes());
            out.extend((f.len() as u32).to_le_bytes());
            out.extend((f.len() as u32).to_le_bytes());
            out.extend(f);
        }
        out
    }

    fn sll2(ip: Vec<u8>) -> Vec<u8> {
        let mut frame = vec![0x08, 0x00];
        frame.extend([0u8; 18]);
        frame.extend(ip);
        frame
    }

    fn nflog(ip: Vec<u8>) -> Vec<u8> {
        let mut frame = vec![2, 0, 0, 47];
        frame.extend([8, 0, 1, 0, 0, 0, 0, 0]);
        let length = (ip.len() + 4) as u16;
        frame.extend(length.to_le_bytes());
        frame.extend(9u16.to_le_bytes());
        frame.extend(&ip);
        frame.extend(vec![0u8; (4 - ip.len() % 4) % 4]);
        frame
    }

    #[test]
    fn names_and_requests_are_summarised_without_contents() {
        let hello = client_hello("api.example.com");
        assert_eq!(server_name(&hello).as_deref(), Some("api.example.com"));
        let request =
            b"GET /items/7?token=secret HTTP/1.1\r\nHost: example.com\r\nCookie: a=b\r\n\r\n";
        let frames = vec![
            sll2(ipv4_tcp(40000, 443, 0x02, &[])),
            sll2(ipv4_tcp(40000, 443, 0x18, &hello)),
            sll2(ipv4_tcp(443, 40000, 0x18, &[0x17; 100])),
            sll2(ipv4_tcp(40001, 80, 0x18, request)),
        ];
        let summary = summarize(&pcap(276, &frames)).unwrap();
        assert_eq!(summary["packets"], 4);
        assert_eq!(summary["duration_seconds"], 3);
        assert_eq!(
            summary["tls_server_names"],
            json!([{"name":"api.example.com","count":1}])
        );
        assert_eq!(
            summary["http_requests"],
            json!([{"method":"GET","host":"example.com","path":"/items/7","count":1}])
        );
        let text = summary.to_string();
        assert!(!text.contains("secret") && !text.contains("Cookie"));
        let tls = &summary["hosts"][0];
        assert_eq!(tls["address"], "93.184.215.14");
        assert_eq!(tls["protocol"], "tls");
        assert_eq!(tls["connections"], 1);
        assert_eq!(tls["names"], json!(["api.example.com"]));
        assert!(tls["bytes_received"].as_u64().unwrap() > 100);
        // One app's packets arrive through NFLOG.
        let app = summarize(&pcap(239, &[nflog(ipv4_tcp(40002, 80, 0x18, request))])).unwrap();
        assert_eq!(app["http_requests"][0]["path"], "/items/7");
        assert_eq!(
            summarize(b"not a capture").unwrap_err().code,
            "invalid_capture"
        );
    }

    #[test]
    fn dns_queries_are_named() {
        let mut query = vec![0x12, 0x34, 0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0];
        for label in ["www", "example", "com"] {
            query.push(label.len() as u8);
            query.extend(label.as_bytes());
        }
        query.extend([0, 0, 1, 0, 1]);
        assert_eq!(dns_query(&query).as_deref(), Some("www.example.com"));
        query[2] = 0x81;
        assert_eq!(dns_query(&query), None);
    }
}
