//! Preserve actual engine diagnostics. A bus fault may be recovered by the guest;
//! neither its presence nor its absence qualifies hardware or fails the job.
use serde_json::{Value, json};

const PREFIX: &str = "tomacco_arm_bus_fault ";
const FIELDS: [&str; 7] = [
    "pc", "physical", "virtual", "size", "access", "mmu", "response",
];

fn event(line: &str) -> Option<Value> {
    if line.len() > 512 {
        return None;
    }
    let mut words = line.strip_prefix(PREFIX)?.split_whitespace();
    let mut values = [0u64; 7];
    for (i, field) in FIELDS.iter().enumerate() {
        let (name, value) = words.next()?.split_once('=')?;
        if name != *field {
            return None;
        }
        values[i] = if i < 3 {
            let digits = value.strip_prefix("0x")?;
            if digits.is_empty()
                || digits.len() > 16
                || !digits.bytes().all(|c| c.is_ascii_hexdigit())
            {
                return None;
            }
            u64::from_str_radix(digits, 16).ok()?
        } else {
            if value.is_empty() || value.len() > 10 || !value.bytes().all(|c| c.is_ascii_digit()) {
                return None;
            }
            u64::from(value.parse::<u32>().ok()?)
        };
    }
    if words.next().is_some()
        || !values[3].is_power_of_two()
        || values[4] > 2
        || values[5] > i32::MAX as u64
        || !(1..=7).contains(&values[6])
    {
        return None;
    }
    let access = ["read", "write", "execute"][values[4] as usize];
    Some(json!({"pc":format!("0x{:x}",values[0]),
        "physical":format!("0x{:x}",values[1]),"virtual":format!("0x{:x}",values[2]),
        "bytes":values[3],"access":access,
        "mmu_index":values[5],"memory_response":values[6]}))
}

pub(in crate::ios_emulator) fn parse(text: &str, enabled: bool) -> Value {
    let (mut events, mut omitted, mut malformed) = (Vec::new(), 0usize, 0usize);
    for line in text.lines().filter(|line| line.starts_with(PREFIX)) {
        match event(line) {
            Some(value) if events.len() < 64 => events.push(value),
            Some(_) => omitted += 1,
            None => malformed += 1,
        }
    }
    json!({"trace_enabled":enabled,"events":events,
        "omitted_events":omitted,"malformed_records":malformed})
}

#[cfg(test)]
mod tests {
    use super::*;
    const READ: &str = "tomacco_arm_bus_fault pc=0xfffffff029822e98 physical=0x220401d48 virtual=0xffffffe209669d48 size=4 access=0 mmu=8 response=2";

    #[test]
    fn actual_addresses_are_exact_strings_and_repeated_faults_are_bounded() {
        let value = parse(&format!("unrelated\n{}\n", READ).repeat(70), true);
        assert_eq!(value["events"].as_array().unwrap().len(), 64);
        assert_eq!(value["events"][0]["pc"], "0xfffffff029822e98");
        assert_eq!(value["events"][0]["physical"], "0x220401d48");
        assert_eq!(value["events"][0]["access"], "read");
        assert_eq!(value["omitted_events"], 6);
        assert_eq!(value["malformed_records"], 0);
        let value = parse(&READ.replace("access=0", "access=1"), true);
        assert_eq!(value["events"][0]["access"], "write");
        let value = parse(&READ.replace("access=0", "access=2"), true);
        assert_eq!(value["events"][0]["access"], "execute");
        assert_eq!(parse("", false)["events"], json!([]));
    }

    #[test]
    fn malformed_trace_records_are_counted_without_inventing_faults() {
        let mut lines: Vec<String> = [
            ("size=4", "size=0"),
            ("size=4", "size=3"),
            ("size=4", "size=4294967296"),
            ("access=0", "access=3"),
            ("mmu=8", "mmu=-1"),
            ("response=2", "response=0"),
            ("response=2", "response=8"),
            ("physical=0x220401d48", "physical=0x10000000000000000"),
            ("pc=0xfffffff029822e98", "pc=0x+123"),
            ("mmu=8", "response=8"),
        ]
        .into_iter()
        .map(|(from, to)| READ.replace(from, to))
        .collect();
        lines.push(format!("{READ} extra=1"));
        lines.push(format!("{READ} {}", "x".repeat(512)));
        lines.push(READ.trim_end_matches("response=2").to_owned());
        let value = parse(&lines.join("\n"), true);
        assert_eq!(value["events"], json!([]));
        assert_eq!(value["malformed_records"], lines.len());
    }
}
