//! Foreign code lives only in the dedicated, killable `_ocr_engine` child.
//! C signatures/lifetimes follow installed Tesseract5 capi.h and Leptonica.
use super::{MAX_OUTPUT, MAX_REQUEST};
use libc::{c_char, c_int, c_void};
use serde::Deserialize;
use std::{
    ffi::{CStr, CString},
    io::Write,
    os::{fd::AsRawFd, unix::ffi::OsStrExt},
    path::Path,
    time::Duration,
};
type Ptr = *mut c_void;
struct Library(Ptr);
impl Library {
    fn open(name: &[u8]) -> Option<Self> {
        // SAFETY: fixed NUL-terminated SONAME; owned handle remains live while
        // every loaded function/engine is used, and is closed once on drop.
        let handle =
            unsafe { libc::dlopen(name.as_ptr().cast(), libc::RTLD_NOW | libc::RTLD_LOCAL) };
        (!handle.is_null()).then_some(Self(handle))
    }
    fn symbol(&self, name: &[u8]) -> Option<Ptr> {
        // SAFETY: fixed NUL-terminated symbol and valid owned library handle.
        let p = unsafe { libc::dlsym(self.0, name.as_ptr().cast()) };
        (!p.is_null()).then_some(p)
    }
}
impl Drop for Library {
    fn drop(&mut self) {
        unsafe {
            libc::dlclose(self.0);
        }
    }
}

struct Api {
    _tess: Library,
    version: unsafe extern "C" fn() -> *const c_char,
    create: unsafe extern "C" fn() -> Ptr,
    init: unsafe extern "C" fn(Ptr, *const c_char, *const c_char) -> c_int,
    delete: unsafe extern "C" fn(Ptr),
    clear: unsafe extern "C" fn(Ptr),
    reset: unsafe extern "C" fn(Ptr),
    psm: unsafe extern "C" fn(Ptr, c_int),
    set_image: unsafe extern "C" fn(Ptr, Ptr),
    recognize: unsafe extern "C" fn(Ptr, Ptr) -> c_int,
    tsv: unsafe extern "C" fn(Ptr, c_int) -> *mut c_char,
    free_text: unsafe extern "C" fn(*const c_char),
    pix_read: unsafe extern "C" fn(*const c_char) -> Ptr,
    pix_destroy: unsafe extern "C" fn(*mut Ptr),
    monitor_create: unsafe extern "C" fn() -> Ptr,
    monitor_delete: unsafe extern "C" fn(Ptr),
    monitor_deadline: unsafe extern "C" fn(Ptr, c_int),
}
impl Api {
    fn load() -> Option<Self> {
        let t = Library::open(b"libtesseract.so.5\0")?;
        macro_rules! sym {
            ($lib:ident,$name:literal,$ty:ty) => {{
                // SAFETY: this exact C API signature is defined by capi.h / allheaders.h;
                // required symbols must all exist before the API is constructed.
                unsafe {
                    std::mem::transmute::<Ptr, $ty>($lib.symbol(concat!($name, "\0").as_bytes())?)
                }
            }};
        }
        Some(Self {
            version: sym!(t, "TessVersion", unsafe extern "C" fn() -> *const c_char),
            create: sym!(t, "TessBaseAPICreate", unsafe extern "C" fn() -> Ptr),
            init: sym!(
                t,
                "TessBaseAPIInit3",
                unsafe extern "C" fn(Ptr, *const c_char, *const c_char) -> c_int
            ),
            delete: sym!(t, "TessBaseAPIDelete", unsafe extern "C" fn(Ptr)),
            clear: sym!(t, "TessBaseAPIClear", unsafe extern "C" fn(Ptr)),
            reset: sym!(
                t,
                "TessBaseAPIClearAdaptiveClassifier",
                unsafe extern "C" fn(Ptr)
            ),
            psm: sym!(
                t,
                "TessBaseAPISetPageSegMode",
                unsafe extern "C" fn(Ptr, c_int)
            ),
            set_image: sym!(t, "TessBaseAPISetImage2", unsafe extern "C" fn(Ptr, Ptr)),
            recognize: sym!(
                t,
                "TessBaseAPIRecognize",
                unsafe extern "C" fn(Ptr, Ptr) -> c_int
            ),
            tsv: sym!(
                t,
                "TessBaseAPIGetTsvText",
                unsafe extern "C" fn(Ptr, c_int) -> *mut c_char
            ),
            free_text: sym!(t, "TessDeleteText", unsafe extern "C" fn(*const c_char)),
            // Resolve Leptonica through Tesseract's own dependency handle so
            // Pix allocation/destruction cannot mix two installed ABIs.
            pix_read: sym!(t, "pixRead", unsafe extern "C" fn(*const c_char) -> Ptr),
            pix_destroy: sym!(t, "pixDestroy", unsafe extern "C" fn(*mut Ptr)),
            monitor_create: sym!(t, "TessMonitorCreate", unsafe extern "C" fn() -> Ptr),
            monitor_delete: sym!(t, "TessMonitorDelete", unsafe extern "C" fn(Ptr)),
            monitor_deadline: sym!(
                t,
                "TessMonitorSetDeadlineMSecs",
                unsafe extern "C" fn(Ptr, c_int)
            ),
            _tess: t,
        })
    }
    fn version(&self) -> Option<&[u8]> {
        let p = unsafe { (self.version)() };
        if p.is_null() {
            None
        } else {
            Some(unsafe { CStr::from_ptr(p) }.to_bytes())
        }
    }
}
struct Engine<'a> {
    api: &'a Api,
    handle: Ptr,
}
impl<'a> Engine<'a> {
    fn new(api: &'a Api, language: &str) -> Option<Self> {
        if language.is_empty() || language.len() > 256 {
            return None;
        }
        let language = CString::new(language).ok()?;
        let handle = unsafe { (api.create)() };
        if handle.is_null() {
            return None;
        }
        let this = Self { api, handle };
        // A null datapath preserves Tesseract's compiled default and the
        // worker-inherited TESSDATA_PREFIX, rather than guessing a distro path.
        if unsafe { (api.init)(handle, std::ptr::null(), language.as_ptr()) } != 0 {
            return None;
        }
        unsafe { (api.psm)(handle, 11) };
        Some(this)
    }
    fn read(&mut self, path: &Path, timeout: Duration) -> (u8, Vec<u8>) {
        let Ok(name) = CString::new(path.as_os_str().as_bytes()) else {
            return (1, vec![]);
        };
        let api = self.api;
        let started = std::time::Instant::now();
        let mut pix = unsafe { (api.pix_read)(name.as_ptr()) };
        if pix.is_null() {
            return (1, vec![]);
        }
        let monitor = unsafe { (api.monitor_create)() };
        if monitor.is_null() {
            unsafe { (api.pix_destroy)(&mut pix) };
            return (1, vec![]);
        }
        // SAFETY: all pointers belong to the still-live API/engine. SetImage2
        // borrows Pix; Clear releases that borrow before pixDestroy. Monitor
        // stays live for Recognize, and GetTsvText's buffer is freed exactly once.
        let result = unsafe {
            (api.clear)(self.handle);
            (api.reset)(self.handle);
            (api.monitor_deadline)(monitor, timeout.as_millis().clamp(1, 90_000) as c_int);
            (api.set_image)(self.handle, pix);
            if (api.recognize)(self.handle, monitor) != 0 {
                (if started.elapsed() >= timeout { 5 } else { 3 }, vec![])
            } else {
                let text = (api.tsv)(self.handle, 0);
                if text.is_null() {
                    (3, vec![])
                } else {
                    let len = libc::strnlen(text, MAX_OUTPUT + 1);
                    let result = if len > MAX_OUTPUT {
                        (4, vec![])
                    } else {
                        (
                            0,
                            std::slice::from_raw_parts(text.cast::<u8>(), len).to_vec(),
                        )
                    };
                    (api.free_text)(text);
                    result
                }
            }
        };
        unsafe {
            (api.clear)(self.handle);
            (api.pix_destroy)(&mut pix);
            (api.monitor_delete)(monitor);
        }
        result
    }
}
impl Drop for Engine<'_> {
    fn drop(&mut self) {
        unsafe {
            (self.api.delete)(self.handle);
        }
    }
}

#[derive(Deserialize)]
struct Request {
    image: std::path::PathBuf,
    timeout_ms: u64,
}
fn reply(out: &mut impl Write, status: u8, data: &[u8]) -> std::io::Result<()> {
    out.write_all(&[status])?;
    out.write_all(&(data.len() as u32).to_be_bytes())?;
    out.write_all(data)?;
    out.flush()
}

pub(super) fn run(language: &str) -> i32 {
    // Dedicated child only. Enforce address-space allocation limits even if a
    // native decoder/model misbehaves; the parent additionally bounds RSS/time.
    let limit = libc::rlimit {
        rlim_cur: 512 * 1024 * 1024,
        rlim_max: 512 * 1024 * 1024,
    };
    if unsafe { libc::setrlimit(libc::RLIMIT_AS, &limit) } != 0 {
        return 1;
    }
    let input = std::io::stdin().lock();
    if super::nonblocking(input.as_raw_fd()).is_err() {
        return 1;
    }
    let mut output = std::io::stdout().lock();
    let Some(api) = Api::load() else {
        let _ = reply(&mut output, 2, b"Native OCR library unavailable");
        return 0;
    };
    let Some(mut engine) = Engine::new(&api, language) else {
        let _ = reply(&mut output, 1, b"Native OCR model unavailable");
        return 0;
    };
    let Some(version) = api.version() else {
        return 1;
    };
    if version.len() > 128 || reply(&mut output, 0, version).is_err() {
        return 1;
    }
    loop {
        // Release the retained model after one idle minute, including clients
        // that abandon a partial request. Parent checks clean exits and rewarms.
        let deadline = std::time::Instant::now() + super::IDLE_TIME;
        let mut header = [0u8; 4];
        if super::transfer(input.as_raw_fd(), &mut header, false, deadline, None).is_err() {
            return 0;
        }
        let length = u32::from_be_bytes(header) as usize;
        if length == 0 || length > MAX_REQUEST {
            return 1;
        }
        let mut bytes = vec![0; length];
        if super::transfer(input.as_raw_fd(), &mut bytes, false, deadline, None).is_err() {
            return 1;
        }
        let Ok(request) = serde_json::from_slice::<Request>(&bytes) else {
            return 1;
        };
        if !super::supported_image(&request.image)
            || request.timeout_ms == 0
            || request.timeout_ms > 90_000
        {
            if reply(&mut output, 1, b"Unsupported worker input").is_err() {
                return 1;
            }
            continue;
        }
        let (status, text) = engine.read(&request.image, Duration::from_millis(request.timeout_ms));
        if reply(&mut output, status, &text).is_err() {
            return 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reply_is_length_delimited_and_does_not_treat_tsv_as_lines() {
        let mut b = vec![];
        reply(&mut b, 0, b"word\nwith\ttabs").unwrap();
        assert_eq!(b[0], 0);
        assert_eq!(u32::from_be_bytes(b[1..5].try_into().unwrap()), 14);
        assert_eq!(&b[5..], b"word\nwith\ttabs");
    }
}
