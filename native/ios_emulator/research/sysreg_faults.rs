//! Executed system-register faults from strict CPUs, not TB translation logs.
use serde_json::{Value, json};

const PREFIX: &str = "tomacco_arm_sysreg_fault ";

fn hex(value: &str, width: usize) -> Option<u64> {
    let digits = value.strip_prefix("0x")?;
    if digits.is_empty() || digits.len() > width || !digits.bytes().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    u64::from_str_radix(digits, 16).ok()
}

fn event(line: &str) -> Option<Value> {
    if line.len() > 160 {
        return None;
    }
    let mut words = line.strip_prefix(PREFIX)?.split_whitespace();
    let pc = hex(words.next()?.strip_prefix("pc=")?, 16)?;
    let instruction = hex(words.next()?.strip_prefix("instruction=")?, 8)? as u32;
    let el = words.next()?.strip_prefix("el=")?;
    if words.next().is_some()
        || pc % 4 != 0
        || instruction & 0xffc00000 != 0xd5000000
        || !["0", "1", "2", "3"].contains(&el)
    {
        return None;
    }
    let op0 = (instruction >> 19) & 3;
    if op0 == 0 {
        return None;
    }
    let op1 = (instruction >> 16) & 7;
    let crn = (instruction >> 12) & 15;
    let crm = (instruction >> 8) & 15;
    let op2 = (instruction >> 5) & 7;
    let rt = instruction & 31;
    Some(
        json!({"pc":format!("0x{pc:x}"),"instruction":format!("0x{instruction:08x}"),
        "register":format!("S{op0}_{op1}_c{crn}_c{crm}_{op2}"),
        "access":if instruction & (1<<21) != 0 {"read"}else{"write"},
        "general_register":if rt == 31 {"XZR".into()}else{format!("X{rt:02}")},
        "exception_level":el.parse::<u8>().ok()?}),
    )
}

pub(in crate::ios_emulator) fn parse(text: &str) -> Value {
    let (mut events, mut omitted, mut malformed) = (Vec::new(), 0usize, 0usize);
    for line in text.lines().filter(|line| line.starts_with(PREFIX)) {
        match event(line) {
            Some(value) if events.len() < 64 => events.push(value),
            Some(_) => omitted += 1,
            None => malformed += 1,
        }
    }
    json!({"trace_enabled":true,"events":events,"omitted_events":omitted,"malformed_records":malformed})
}

#[cfg(test)]
mod tests {
    use super::*;
    const READ: &str = "tomacco_arm_sysreg_fault pc=0x102adac instruction=0xd53ff4c0 el=1";
    #[test]
    fn decodes_exact_executed_instruction_and_bounds_repetition() {
        let value = parse(&format!("unrelated\n{READ}\n").repeat(70));
        assert_eq!(value["events"].as_array().unwrap().len(), 64);
        assert_eq!(value["omitted_events"], 6);
        assert_eq!(value["events"][0]["register"], "S3_7_c15_c4_6");
        assert_eq!(value["events"][0]["general_register"], "X00");
        assert_eq!(value["events"][0]["access"], "read");
        assert_eq!(value["events"][0]["exception_level"], 1);
        let value = parse(&READ.replace("d53ff4c0", "d51ff4df"));
        assert_eq!(value["events"][0]["general_register"], "XZR");
        assert_eq!(value["events"][0]["access"], "write");
    }
    #[test]
    fn malformed_fields_and_other_instructions_never_become_fault_evidence() {
        let mut records: Vec<String> = [
            ("0x102adac", "0x102adae"),
            ("0x102adac", "0x+1"),
            ("0x102adac", "0x10000000000000000"),
            ("0xd53ff4c0", "0x1d53ff4c0"),
            ("0xd53ff4c0", "0xd503201f"),
            ("el=1", "el=4"),
            ("el=1", "el=-1"),
            ("instruction=", "pc="),
        ]
        .into_iter()
        .map(|(from, to)| READ.replace(from, to))
        .collect();
        records.push(format!("{READ} extra=1"));
        records.push(READ.trim_end_matches(" el=1").into());
        let result = parse(&records.join("\n"));
        assert_eq!(result["events"], json!([]));
        assert_eq!(result["malformed_records"], records.len());
    }
}
