//! A Bluetooth LE pointer and keyboard (HID over GATT) served through BlueZ,
//! so that the desktop drives a phone that has no remote control: an iPhone
//! that mirrors over AirPlay, with AssistiveTouch showing the pointer.
//!
//! BlueZ hosts the GATT application and the advertisement this module serves
//! on the system bus. The phone pairs with "Tomacco" in its Bluetooth
//! settings; it may do so only in the pairing window after the start.
use super::*;
use std::collections::HashMap;
use zbus::{
    Connection, fdo, interface,
    zvariant::{ObjectPath, OwnedObjectPath, OwnedValue, Value as Variant},
};

const APP: &str = "/org/tomacco/hid";
const ADVERTISEMENT: &str = "/org/tomacco/hid/advertisement";
const AGENT: &str = "/org/tomacco/hid/agent";
/// The name the phone lists under Bluetooth.
pub const NAME: &str = "Tomacco";
/// How long after the start a new phone may pair.
const PAIRING_WINDOW: Duration = Duration::from_secs(120);

/// HID report descriptor: report 1 a three-button mouse with 16-bit relative
/// X and Y and a wheel, report 2 a boot-style keyboard, report 3 consumer
/// keys (Home, volume).
const REPORT_MAP: &[u8] = &[
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x01, 0x09, 0x01, 0xA1, 0x00, // mouse, pointer
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81,
    0x02, // 3 buttons
    0x95, 0x01, 0x75, 0x05, 0x81, 0x03, // padding
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x16, 0x01, 0x80, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95,
    0x02, 0x81, 0x06, // X, Y
    0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06, // wheel
    0xC0, 0xC0, //
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x02, // keyboard
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81,
    0x02, // modifiers
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01, // reserved
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81,
    0x00, // keys
    0xC0, //
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x03, // consumer control
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81,
    0x00, //
    0xC0,
];
const MOUSE: u8 = 1;
const KEYBOARD: u8 = 2;
const CONSUMER: u8 = 3;

fn err(message: impl Into<String>) -> Error {
    Error::new("bluetooth_input", message)
}
fn bus(e: zbus::Error) -> Error {
    err(match &e {
        zbus::Error::MethodError(name, Some(text), _) => format!("{name}: {text}"),
        _ => e.to_string(),
    })
}
fn path(text: &str) -> OwnedObjectPath {
    OwnedObjectPath::try_from(text).expect("a valid object path")
}
fn uuid(short: u16) -> String {
    format!("0000{short:04x}-0000-1000-8000-00805f9b34fb")
}

struct Service {
    uuid: String,
}
#[interface(name = "org.bluez.GattService1")]
impl Service {
    #[zbus(property, name = "UUID")]
    fn uuid(&self) -> String {
        self.uuid.clone()
    }
    #[zbus(property)]
    fn primary(&self) -> bool {
        true
    }
}

/// One characteristic; reports keep their latest value for reads and send
/// changes as notifications while the phone listens.
struct Characteristic {
    uuid: String,
    service: OwnedObjectPath,
    flags: Vec<String>,
    value: Vec<u8>,
    notifying: Arc<AtomicBool>,
}
#[interface(name = "org.bluez.GattCharacteristic1")]
impl Characteristic {
    #[zbus(property, name = "UUID")]
    fn uuid(&self) -> String {
        self.uuid.clone()
    }
    #[zbus(property)]
    fn service(&self) -> OwnedObjectPath {
        self.service.clone()
    }
    #[zbus(property)]
    fn flags(&self) -> Vec<String> {
        self.flags.clone()
    }
    #[zbus(property)]
    fn value(&self) -> Vec<u8> {
        self.value.clone()
    }
    fn read_value(&self, options: HashMap<String, OwnedValue>) -> Vec<u8> {
        let offset = options
            .get("offset")
            .and_then(|v| u16::try_from(v).ok())
            .unwrap_or(0) as usize;
        self.value.get(offset..).unwrap_or_default().to_vec()
    }
    fn write_value(&mut self, value: Vec<u8>, _options: HashMap<String, OwnedValue>) {
        // Protocol Mode and the control point: the phone's choice is kept,
        // nothing else depends on it.
        if value.len() <= 8 {
            self.value = value;
        }
    }
    fn start_notify(&self) {
        self.notifying.store(true, Ordering::Relaxed);
    }
    fn stop_notify(&self) {
        self.notifying.store(false, Ordering::Relaxed);
    }
}

struct Descriptor {
    uuid: String,
    characteristic: OwnedObjectPath,
    value: Vec<u8>,
}
#[interface(name = "org.bluez.GattDescriptor1")]
impl Descriptor {
    #[zbus(property, name = "UUID")]
    fn uuid(&self) -> String {
        self.uuid.clone()
    }
    #[zbus(property)]
    fn characteristic(&self) -> OwnedObjectPath {
        self.characteristic.clone()
    }
    #[zbus(property)]
    fn flags(&self) -> Vec<String> {
        vec!["read".into()]
    }
    fn read_value(&self, _options: HashMap<String, OwnedValue>) -> Vec<u8> {
        self.value.clone()
    }
}

struct Advertisement;
#[interface(name = "org.bluez.LEAdvertisement1")]
impl Advertisement {
    fn release(&self) {}
    #[zbus(property, name = "Type")]
    fn kind(&self) -> String {
        "peripheral".into()
    }
    #[zbus(property, name = "ServiceUUIDs")]
    fn service_uuids(&self) -> Vec<String> {
        vec![uuid(0x1812)]
    }
    /// A generic HID device: the phone lists a keyboard-and-mouse symbol.
    #[zbus(property)]
    fn appearance(&self) -> u16 {
        0x03C0
    }
    #[zbus(property)]
    fn local_name(&self) -> String {
        NAME.into()
    }
    #[zbus(property)]
    fn discoverable(&self) -> bool {
        true
    }
}

/// Just Works pairing, refused once the pairing window has closed.
struct Agent {
    until: Instant,
}
impl Agent {
    fn allow(&self) -> fdo::Result<()> {
        if Instant::now() < self.until {
            Ok(())
        } else {
            Err(fdo::Error::AccessDenied(
                "Tomacco's pairing window has closed; start the session again to pair".into(),
            ))
        }
    }
}
#[interface(name = "org.bluez.Agent1")]
impl Agent {
    fn release(&self) {}
    fn request_pin_code(&self, _device: OwnedObjectPath) -> fdo::Result<String> {
        Err(fdo::Error::NotSupported("no PIN".into()))
    }
    fn display_pin_code(&self, _device: OwnedObjectPath, _code: String) {}
    fn request_passkey(&self, _device: OwnedObjectPath) -> fdo::Result<u32> {
        Err(fdo::Error::NotSupported("no passkey".into()))
    }
    fn display_passkey(&self, _device: OwnedObjectPath, _passkey: u32, _entered: u16) {}
    fn request_confirmation(&self, _device: OwnedObjectPath, _passkey: u32) -> fdo::Result<()> {
        self.allow()
    }
    fn request_authorization(&self, _device: OwnedObjectPath) -> fdo::Result<()> {
        self.allow()
    }
    fn authorize_service(&self, _device: OwnedObjectPath, _uuid: String) -> fdo::Result<()> {
        Ok(())
    }
    fn cancel(&self) {}
}

/// The served pointer and keyboard. Reports go out only while the phone
/// listens to them.
pub struct Hid {
    rt: tokio::runtime::Runtime,
    connection: Connection,
    reports: HashMap<u8, (OwnedObjectPath, Arc<AtomicBool>)>,
    buttons: u8,
}

impl Hid {
    pub fn start() -> Result<Hid> {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()?;
        let (connection, reports) = rt.block_on(serve())?;
        Ok(Hid {
            rt,
            connection,
            reports,
            buttons: 0,
        })
    }
    /// Whether the phone listens to the pointer, so input reaches it.
    pub fn listening(&self) -> bool {
        self.reports
            .get(&MOUSE)
            .is_some_and(|(_, on)| on.load(Ordering::Relaxed))
    }
    fn send(&self, id: u8, report: Vec<u8>) -> Result<bool> {
        let Some((at, on)) = self.reports.get(&id) else {
            return Ok(false);
        };
        if !on.load(Ordering::Relaxed) {
            return Ok(false);
        }
        let connection = self.connection.clone();
        let at = at.clone();
        self.rt.block_on(async move {
            let iface = connection
                .object_server()
                .interface::<_, Characteristic>(at)
                .await
                .map_err(bus)?;
            iface.get_mut().await.value = report;
            iface
                .get()
                .await
                .value_changed(iface.signal_emitter())
                .await
                .map_err(bus)
        })?;
        Ok(true)
    }
    /// Moves the pointer by dx, dy in HID units, split into reports of at
    /// most 127 a step so the phone's acceleration treats them alike.
    pub fn pointer(&mut self, dx: i32, dy: i32, buttons: u8) -> Result<bool> {
        let mut delivered = true;
        let (mut x, mut y) = (dx, dy);
        loop {
            let sx = x.clamp(-127, 127);
            let sy = y.clamp(-127, 127);
            self.buttons = buttons;
            delivered &= self.send(MOUSE, mouse(buttons, sx, sy, 0))?;
            x -= sx;
            y -= sy;
            if x == 0 && y == 0 {
                break;
            }
        }
        Ok(delivered)
    }
    pub fn keys(&mut self, modifiers: u8, key: u8) -> Result<bool> {
        let delivered = self.send(KEYBOARD, vec![modifiers, 0, key, 0, 0, 0, 0, 0])?;
        Ok(delivered && self.send(KEYBOARD, vec![0; 8])?)
    }
    pub fn consumer(&mut self, usage: u16) -> Result<bool> {
        let delivered = self.send(CONSUMER, usage.to_le_bytes().to_vec())?;
        Ok(delivered && self.send(CONSUMER, vec![0, 0])?)
    }
}
impl Drop for Hid {
    fn drop(&mut self) {
        let connection = self.connection.clone();
        let _ = self.rt.block_on(async move {
            tokio::time::timeout(Duration::from_secs(2), async {
                for (iface, method, at) in [
                    ("org.bluez.LEAdvertisingManager1", "UnregisterAdvertisement", ADVERTISEMENT),
                    ("org.bluez.GattManager1", "UnregisterApplication", APP),
                ] {
                    if let Ok(adapter) = adapter(&connection).await {
                        let _ = connection
                            .call_method(Some("org.bluez"), adapter.as_str(), Some(iface), method, &(path(at),))
                            .await;
                    }
                }
                let _ = connection
                    .call_method(
                        Some("org.bluez"),
                        "/org/bluez",
                        Some("org.bluez.AgentManager1"),
                        "UnregisterAgent",
                        &(path(AGENT),),
                    )
                    .await;
            })
            .await
        });
    }
}

fn mouse(buttons: u8, dx: i32, dy: i32, wheel: i8) -> Vec<u8> {
    let x = (dx as i16).to_le_bytes();
    let y = (dy as i16).to_le_bytes();
    vec![buttons & 7, x[0], x[1], y[0], y[1], wheel as u8]
}

/// The first powered Bluetooth adapter BlueZ manages.
async fn adapter(connection: &Connection) -> Result<OwnedObjectPath> {
    let manager = fdo::ObjectManagerProxy::builder(connection)
        .destination("org.bluez")
        .map_err(bus)?
        .path("/")
        .map_err(bus)?
        .build()
        .await
        .map_err(bus)?;
    let objects = manager.get_managed_objects().await.map_err(|e| bus(e.into()))?;
    let mut adapters: Vec<_> = objects
        .iter()
        .filter_map(|(at, ifaces)| {
            ifaces
                .get("org.bluez.Adapter1")
                .map(|props| (at.clone(), props.get("Powered").and_then(|v| bool::try_from(v).ok()).unwrap_or(false)))
        })
        .collect();
    adapters.sort_by(|a, b| a.0.as_str().cmp(b.0.as_str()));
    match adapters.iter().find(|(_, powered)| *powered) {
        Some((at, _)) => Ok(OwnedObjectPath::from(at.clone())),
        None if adapters.is_empty() => Err(err("No Bluetooth adapter found")),
        None => Err(err("Bluetooth is off: turn it on to control the phone")),
    }
}

async fn serve() -> Result<(Connection, HashMap<u8, (OwnedObjectPath, Arc<AtomicBool>)>)> {
    let connection = Connection::system().await.map_err(bus)?;
    let adapter = adapter(&connection).await?;
    let server = connection.object_server();
    server.at(APP, fdo::ObjectManager).await.map_err(bus)?;
    let mut reports = HashMap::new();
    // (service UUID, [(characteristic UUID, flags, value, report reference)])
    let services: [(u16, Vec<(u16, &[&str], Vec<u8>, Option<[u8; 2]>)>); 3] = [
        (0x180A, vec![(0x2A50, &["read"], vec![0x02, 0x6B, 0x1D, 0x46, 0x02, 0x01, 0x00], None)]),
        (0x180F, vec![(0x2A19, &["read", "notify"], vec![100], None)]),
        (
            0x1812,
            vec![
                (0x2A4A, &["read"], vec![0x11, 0x01, 0x00, 0x02], None),
                (0x2A4B, &["encrypt-read"], REPORT_MAP.to_vec(), None),
                (0x2A4C, &["write-without-response"], vec![0], None),
                (0x2A4E, &["read", "write-without-response"], vec![1], None),
                (0x2A4D, &["encrypt-read", "notify"], mouse(0, 0, 0, 0), Some([MOUSE, 1])),
                (0x2A4D, &["encrypt-read", "notify"], vec![0; 8], Some([KEYBOARD, 1])),
                (0x2A4D, &["encrypt-read", "notify"], vec![0, 0], Some([CONSUMER, 1])),
            ],
        ),
    ];
    for (s, (service_uuid, characteristics)) in services.into_iter().enumerate() {
        let service_path = path(&format!("{APP}/service{s}"));
        server
            .at(&service_path, Service { uuid: uuid(service_uuid) })
            .await
            .map_err(bus)?;
        for (c, (char_uuid, flags, value, reference)) in characteristics.into_iter().enumerate() {
            let char_path = path(&format!("{}/char{c}", service_path.as_str()));
            let notifying = Arc::new(AtomicBool::new(false));
            server
                .at(
                    &char_path,
                    Characteristic {
                        uuid: uuid(char_uuid),
                        service: service_path.clone(),
                        flags: flags.iter().map(|f| f.to_string()).collect(),
                        value,
                        notifying: notifying.clone(),
                    },
                )
                .await
                .map_err(bus)?;
            if let Some(reference) = reference {
                server
                    .at(
                        format!("{}/desc0", char_path.as_str()),
                        Descriptor {
                            uuid: uuid(0x2908),
                            characteristic: char_path.clone(),
                            value: reference.to_vec(),
                        },
                    )
                    .await
                    .map_err(bus)?;
                reports.insert(reference[0], (char_path.clone(), notifying));
            }
        }
    }
    server.at(ADVERTISEMENT, Advertisement).await.map_err(bus)?;
    server
        .at(AGENT, Agent { until: Instant::now() + PAIRING_WINDOW })
        .await
        .map_err(bus)?;
    let no_options: HashMap<&str, Variant> = HashMap::new();
    connection
        .call_method(
            Some("org.bluez"),
            "/org/bluez",
            Some("org.bluez.AgentManager1"),
            "RegisterAgent",
            &(ObjectPath::try_from(AGENT).unwrap(), "NoInputNoOutput"),
        )
        .await
        .map_err(bus)?;
    // The default agent answers the phone's pairing; another one may be default
    // again later, which keeps pairing in its hands.
    let _ = connection
        .call_method(
            Some("org.bluez"),
            "/org/bluez",
            Some("org.bluez.AgentManager1"),
            "RequestDefaultAgent",
            &(ObjectPath::try_from(AGENT).unwrap(),),
        )
        .await;
    connection
        .call_method(
            Some("org.bluez"),
            adapter.as_str(),
            Some("org.bluez.GattManager1"),
            "RegisterApplication",
            &(ObjectPath::try_from(APP).unwrap(), &no_options),
        )
        .await
        .map_err(bus)?;
    connection
        .call_method(
            Some("org.bluez"),
            adapter.as_str(),
            Some("org.bluez.LEAdvertisingManager1"),
            "RegisterAdvertisement",
            &(ObjectPath::try_from(ADVERTISEMENT).unwrap(), &no_options),
        )
        .await
        .map_err(bus)?;
    Ok((connection, reports))
}

/// The HID keyboard usage and shift state of a character on a US layout,
/// the layout the phone assumes unless its hardware keyboard is set otherwise.
pub fn usage(c: char) -> Option<(u8, bool)> {
    const SHIFTED: &str = "!@#$%^&*()";
    Some(match c {
        'a'..='z' => (0x04 + (c as u8 - b'a'), false),
        'A'..='Z' => (0x04 + (c as u8 - b'A'), true),
        '1'..='9' => (0x1E + (c as u8 - b'1'), false),
        '0' => (0x27, false),
        '\n' => (0x28, false),
        '\u{8}' => (0x2A, false),
        '\t' => (0x2B, false),
        ' ' => (0x2C, false),
        '-' => (0x2D, false),
        '_' => (0x2D, true),
        '=' => (0x2E, false),
        '+' => (0x2E, true),
        '[' => (0x2F, false),
        '{' => (0x2F, true),
        ']' => (0x30, false),
        '}' => (0x30, true),
        '\\' => (0x31, false),
        '|' => (0x31, true),
        ';' => (0x33, false),
        ':' => (0x33, true),
        '\'' => (0x34, false),
        '"' => (0x34, true),
        '`' => (0x35, false),
        '~' => (0x35, true),
        ',' => (0x36, false),
        '<' => (0x36, true),
        '.' => (0x37, false),
        '>' => (0x37, true),
        '/' => (0x38, false),
        '?' => (0x38, true),
        _ => {
            let at = SHIFTED.find(c)?;
            (if at == 9 { 0x27 } else { 0x1E + at as u8 }, true)
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reports_and_keys_have_the_layout_the_descriptor_names() {
        // Report 1: buttons, X and Y as little-endian 16-bit, wheel.
        assert_eq!(mouse(1, -2, 300, -1), vec![1, 0xFE, 0xFF, 0x2C, 0x01, 0xFF]);
        assert_eq!(mouse(0xFF, 0, 0, 0)[0], 7);
        assert_eq!(usage('a'), Some((0x04, false)));
        assert_eq!(usage('Z'), Some((0x1D, true)));
        assert_eq!(usage('1'), Some((0x1E, false)));
        assert_eq!(usage('!'), Some((0x1E, true)));
        assert_eq!(usage(')'), Some((0x27, true)));
        assert_eq!(usage('?'), Some((0x38, true)));
        assert_eq!(usage('ü'), None);
        // The descriptor's collections close.
        let opens = REPORT_MAP.windows(2).filter(|w| w == &[0xA1, 0x01] || w == &[0xA1, 0x00]).count();
        let closes = REPORT_MAP.iter().filter(|b| **b == 0xC0).count();
        assert_eq!(opens, closes);
    }
}
