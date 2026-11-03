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
    Connection, ObjectPath, OwnedObjectPath, OwnedVariant as OwnedValue, Variant, fdo, interface,
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
/// keys (Home, volume), report 4 an absolute three-button pointer. A phone
/// paired before report 4 existed keeps the old map: forget "Tomacco" in its
/// Bluetooth settings and pair again.
const REPORT_MAP: &[u8] = &[
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x01, 0x09, 0x01, 0xA1, 0x00, // mouse, pointer
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81,
    0x02, // 3 buttons
    0x95, 0x01, 0x75, 0x05, 0x81, 0x03, // padding
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x16, 0x01, 0x80, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02,
    0x81, 0x06, // X, Y
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
    0xC0, //
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x04, 0x09, 0x01, 0xA1,
    0x00, // absolute pointer
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81,
    0x02, // 3 buttons
    0x95, 0x01, 0x75, 0x05, 0x81, 0x01, // padding
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x16, 0x00, 0x00, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02,
    0x81, 0x02, // X, Y 0..32767 across the screen
    0xC0, 0xC0,
];
const MOUSE: u8 = 1;
const KEYBOARD: u8 = 2;
const CONSUMER: u8 = 3;
/// The absolute pointer. With AssistiveTouch on, iOS puts its pointer exactly
/// at the reported point, with no acceleration (measured by Jalrux's
/// iphone-control on iOS 26.5.2, MIT). With two mouse collections iOS takes
/// the buttons from this report, not from the relative one.
const POINTER: u8 = 4;
const ABS_MAX: f64 = 32767.0;

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

/// Just Works pairing while pairing is open: in the window after the start,
/// or while a phone mirrors to this receiver, which a stranger cannot do
/// unseen. Every request and answer goes to the session's Bluetooth log.
struct Agent {
    until: Instant,
    open: Arc<AtomicBool>,
    log: PathBuf,
}
impl Agent {
    fn note(&self, line: &str) {
        use std::io::Write;
        if let Ok(mut file) = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.log)
        {
            let _ = writeln!(file, "{:.3} {line}", now());
        }
    }
    fn allow(&self, request: &str, device: &OwnedObjectPath) -> fdo::Result<()> {
        if Instant::now() < self.until || self.open.load(Ordering::Relaxed) {
            self.note(&format!("{request} {}: accepted", device.as_str()));
            Ok(())
        } else {
            self.note(&format!(
                "{request} {}: refused, pairing is closed",
                device.as_str()
            ));
            Err(fdo::Error::AccessDenied(
                "Pair while the iPhone mirrors to Tomacco".into(),
            ))
        }
    }
}
#[interface(name = "org.bluez.Agent1")]
impl Agent {
    fn release(&self) {
        self.note("agent released");
    }
    fn request_pin_code(&self, device: OwnedObjectPath) -> fdo::Result<String> {
        self.note(&format!("PIN requested by {}: refused", device.as_str()));
        Err(fdo::Error::NotSupported("no PIN".into()))
    }
    fn display_pin_code(&self, _device: OwnedObjectPath, _code: String) {}
    fn request_passkey(&self, device: OwnedObjectPath) -> fdo::Result<u32> {
        self.note(&format!(
            "passkey requested by {}: refused",
            device.as_str()
        ));
        Err(fdo::Error::NotSupported("no passkey".into()))
    }
    fn display_passkey(&self, _device: OwnedObjectPath, _passkey: u32, _entered: u16) {}
    fn request_confirmation(&self, device: OwnedObjectPath, _passkey: u32) -> fdo::Result<()> {
        self.allow("confirmation", &device)
    }
    fn request_authorization(&self, device: OwnedObjectPath) -> fdo::Result<()> {
        self.allow("authorization", &device)
    }
    fn authorize_service(&self, device: OwnedObjectPath, uuid: String) -> fdo::Result<()> {
        self.note(&format!("service {uuid} for {}: accepted", device.as_str()));
        Ok(())
    }
    fn cancel(&self) {
        self.note("pairing cancelled");
    }
}

/// The served pointer and keyboard. Reports go out only while the phone
/// listens to them.
pub struct Hid {
    rt: tokio::runtime::Runtime,
    connection: Connection,
    reports: HashMap<u8, (OwnedObjectPath, Arc<AtomicBool>)>,
    buttons: u8,
    /// The last absolute report: iOS ignores one equal to the one before.
    last_point: Option<(u8, u16, u16)>,
}

impl Hid {
    /// Serves the device; `pairing` opens pairing beyond the first window,
    /// and `log` receives the pairing requests.
    pub fn start(pairing: Arc<AtomicBool>, log: PathBuf) -> Result<Hid> {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()?;
        let (connection, reports) = rt.block_on(serve(pairing, log))?;
        Ok(Hid {
            rt,
            connection,
            reports,
            buttons: 0,
            last_point: None,
        })
    }
    /// Whether the phone listens to the pointer, so input reaches it.
    pub fn listening(&self) -> bool {
        self.reports
            .get(&POINTER)
            .is_some_and(|(_, on)| on.load(Ordering::Relaxed))
    }
    /// Puts the phone's pointer at (x, y), 0..1 of the screen as the phone
    /// shows it, with `buttons` held. A press lands where the pointer was
    /// before the report, so a caller moves first and presses after a pause.
    pub fn point(&mut self, x: f64, y: f64, buttons: u8) -> Result<bool> {
        let (x, y) = (absolute(x), absolute(y));
        let report = nudged(self.last_point, (buttons & 7, x, y));
        self.last_point = Some(report);
        self.buttons = report.0;
        self.send(POINTER, point_report(report))
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
                    (
                        "org.bluez.LEAdvertisingManager1",
                        "UnregisterAdvertisement",
                        ADVERTISEMENT,
                    ),
                    ("org.bluez.GattManager1", "UnregisterApplication", APP),
                ] {
                    if let Ok(adapter) = adapter(&connection).await {
                        let _ = connection
                            .call_method(
                                Some("org.bluez"),
                                adapter.as_str(),
                                Some(iface),
                                method,
                                &(path(at),),
                            )
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

/// 0..1 of the screen to the absolute pointer's 0..32767.
fn absolute(v: f64) -> u16 {
    if v.is_finite() {
        (v.clamp(0.0, 1.0) * ABS_MAX).round() as u16
    } else {
        0
    }
}
/// The report to send for `next`: one unit aside when it equals the last,
/// since iOS drops a repeated absolute report.
fn nudged(last: Option<(u8, u16, u16)>, next: (u8, u16, u16)) -> (u8, u16, u16) {
    if last != Some(next) {
        return next;
    }
    let (b, x, y) = next;
    (b, if x < ABS_MAX as u16 { x + 1 } else { x - 1 }, y)
}
fn point_report((buttons, x, y): (u8, u16, u16)) -> Vec<u8> {
    let (x, y) = (x.to_le_bytes(), y.to_le_bytes());
    vec![buttons & 7, x[0], x[1], y[0], y[1]]
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
        .path("/")
        .build()
        .await
        .map_err(bus)?;
    let objects = manager
        .get_managed_objects()
        .await
        .map_err(|e| bus(e.into()))?;
    let mut adapters: Vec<_> = objects
        .iter()
        .filter_map(|(at, ifaces)| {
            ifaces.get("org.bluez.Adapter1").map(|props| {
                (
                    at.clone(),
                    props
                        .get("Powered")
                        .and_then(|v| bool::try_from(v).ok())
                        .unwrap_or(false),
                )
            })
        })
        .collect();
    adapters.sort_by(|a, b| a.0.as_str().cmp(b.0.as_str()));
    match adapters.iter().find(|(_, powered)| *powered) {
        Some((at, _)) => Ok(at.clone()),
        None if adapters.is_empty() => Err(err("No Bluetooth adapter found")),
        None => Err(err("Bluetooth is off: turn it on to control the phone")),
    }
}

fn log_line(log: &Path, line: &str) {
    use std::io::Write;
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(log) {
        let _ = writeln!(file, "{:.3} {line}", now());
    }
}
async fn serve(
    pairing: Arc<AtomicBool>,
    log: PathBuf,
) -> Result<(Connection, HashMap<u8, (OwnedObjectPath, Arc<AtomicBool>)>)> {
    let log_path = log.clone();
    let connection = Connection::system().await.map_err(bus)?;
    let adapter = adapter(&connection).await?;
    let server = connection.object_server();
    server.at(APP, fdo::ObjectManager).await.map_err(bus)?;
    let mut reports = HashMap::new();
    // (service UUID, [(characteristic UUID, flags, value, report reference)])
    type CharacteristicSpec<'a> = (u16, &'a [&'a str], Vec<u8>, Option<[u8; 2]>);
    let services: [(u16, Vec<CharacteristicSpec<'_>>); 3] = [
        (
            0x180A,
            vec![(
                0x2A50,
                &["read"],
                vec![0x02, 0x6B, 0x1D, 0x46, 0x02, 0x01, 0x00],
                None,
            )],
        ),
        (0x180F, vec![(0x2A19, &["read", "notify"], vec![100], None)]),
        (
            0x1812,
            vec![
                (0x2A4A, &["read"], vec![0x11, 0x01, 0x00, 0x02], None),
                (0x2A4B, &["encrypt-read"], REPORT_MAP.to_vec(), None),
                (0x2A4C, &["write-without-response"], vec![0], None),
                (0x2A4E, &["read", "write-without-response"], vec![1], None),
                (
                    0x2A4D,
                    &["encrypt-read", "notify"],
                    mouse(0, 0, 0, 0),
                    Some([MOUSE, 1]),
                ),
                (
                    0x2A4D,
                    &["encrypt-read", "notify"],
                    vec![0; 8],
                    Some([KEYBOARD, 1]),
                ),
                (
                    0x2A4D,
                    &["encrypt-read", "notify"],
                    vec![0, 0],
                    Some([CONSUMER, 1]),
                ),
                (
                    0x2A4D,
                    &["encrypt-read", "notify"],
                    vec![0; 5],
                    Some([POINTER, 1]),
                ),
            ],
        ),
    ];
    for (s, (service_uuid, characteristics)) in services.into_iter().enumerate() {
        let service_path = path(&format!("{APP}/service{s}"));
        server
            .at(
                &service_path,
                Service {
                    uuid: uuid(service_uuid),
                },
            )
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
        .at(
            AGENT,
            Agent {
                until: Instant::now() + PAIRING_WINDOW,
                open: pairing.clone(),
                log: log.clone(),
            },
        )
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
    let default = connection
        .call_method(
            Some("org.bluez"),
            "/org/bluez",
            Some("org.bluez.AgentManager1"),
            "RequestDefaultAgent",
            &(ObjectPath::try_from(AGENT).unwrap(),),
        )
        .await;
    log_line(
        &log_path,
        &format!(
            "default agent: {}",
            default
                .map(|_| "Tomacco".to_string())
                .unwrap_or_else(|e| format!("refused ({})", bus(e).message))
        ),
    );
    // What decides whether the phone may pair at all.
    {
        let properties = fdo::PropertiesProxy::builder(&connection)
            .destination("org.bluez")
            .path(adapter.as_str());
        if let Ok(proxy) = properties.build().await {
            if let Ok(name) = zbus::names::InterfaceName::try_from("org.bluez.Adapter1") {
                if let Ok(all) = proxy.get_all(name).await {
                    let mut shown: Vec<String> = [
                        "Address",
                        "Alias",
                        "Powered",
                        "Pairable",
                        "PairableTimeout",
                        "Discoverable",
                        "Roles",
                    ]
                    .iter()
                    .filter_map(|key| all.get(*key).map(|v| format!("{key}={v:?}")))
                    .collect();
                    shown.sort();
                    log_line(
                        &log_path,
                        &format!("adapter {}: {}", adapter.as_str(), shown.join(" ")),
                    );
                }
            }
        }
    }
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
    // BlueZ ends pairability after PairableTimeout, and then the kernel refuses
    // a phone before any agent hears of it. A mirror that starts makes the
    // adapter pairable again, for that timeout.
    let watch = connection.clone();
    let at = adapter.clone();
    tokio::spawn(async move {
        let mut was = false;
        loop {
            let now = pairing.load(Ordering::Relaxed);
            if now && !was {
                let set = watch
                    .call_method(
                        Some("org.bluez"),
                        at.as_str(),
                        Some("org.freedesktop.DBus.Properties"),
                        "Set",
                        &("org.bluez.Adapter1", "Pairable", Variant::from(true)),
                    )
                    .await;
                log_line(
                    &log_path,
                    &format!(
                        "mirror started: pairable {}",
                        set.map(|_| "on".to_string())
                            .unwrap_or_else(|e| format!("not set ({})", bus(e).message))
                    ),
                );
            }
            was = now;
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    });
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
        let opens = REPORT_MAP
            .windows(2)
            .filter(|w| w == &[0xA1, 0x01] || w == &[0xA1, 0x00])
            .count();
        let closes = REPORT_MAP.iter().filter(|b| **b == 0xC0).count();
        assert_eq!(opens, closes);
    }
    #[test]
    fn the_absolute_pointer_spans_the_screen_and_is_never_repeated() {
        assert_eq!(absolute(0.0), 0);
        assert_eq!(absolute(1.0), 32767);
        assert_eq!(absolute(0.5), 16384);
        assert_eq!(absolute(-3.0), 0);
        assert_eq!(absolute(7.0), 32767);
        assert_eq!(absolute(f64::NAN), 0);
        // Report 4: buttons, then X and Y as little-endian 16-bit.
        assert_eq!(
            point_report((1, 0x1234, 32767)),
            vec![1, 0x34, 0x12, 0xFF, 0x7F]
        );
        assert_eq!(point_report((0xFF, 0, 0))[0], 7);
        assert_eq!(nudged(None, (0, 10, 10)), (0, 10, 10));
        assert_eq!(nudged(Some((0, 10, 10)), (1, 10, 10)), (1, 10, 10));
        assert_eq!(nudged(Some((1, 10, 10)), (1, 10, 10)), (1, 11, 10));
        assert_eq!(nudged(Some((1, 32767, 0)), (1, 32767, 0)), (1, 32766, 0));
        // The descriptor names report 4.
        assert!(REPORT_MAP.windows(2).any(|w| w == [0x85, POINTER]));
    }
}
