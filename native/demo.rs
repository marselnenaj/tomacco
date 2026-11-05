//! Local, revisioned Demo Studio scenes and independently validated render jobs.
use crate::{core::*, jobs, toolchain as tc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

const PRESETS: &[&str] = &[
    "portrait-orbit",
    "landscape-orbit",
    "square-float",
    "portrait-still",
];
const MAX_MEDIA: u64 = 16 * 1024 * 1024 * 1024;
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Source {
    path: String,
    sha256: String,
    width: u32,
    height: u32,
    duration: f64,
    fps: f64,
    rotation: u32,
    has_audio: bool,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Clip {
    start: f64,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Output {
    width: u32,
    height: u32,
    fps: u32,
    duration: f64,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Device {
    model: String,
    finish: String,
    bezel: f64,
    thickness: f64,
    corner_radius: f64,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Background {
    color: String,
    color2: String,
    style: String,
    image: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    image_sha256: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Shadow {
    enabled: bool,
    opacity: f64,
    blur: f64,
    offset_x: f64,
    offset_y: f64,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Camera {
    distance: f64,
    fov: f64,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Keyframe {
    time: f64,
    yaw: f64,
    pitch: f64,
    roll: f64,
    scale: f64,
    x: f64,
    y: f64,
    easing: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Scene {
    format: String,
    version: u32,
    revision: u64,
    id: String,
    title: String,
    source: Source,
    clip: Clip,
    output: Output,
    device: Device,
    background: Background,
    shadow: Shadow,
    camera: Camera,
    keyframes: Vec<Keyframe>,
}
fn invalid(message: impl Into<String>) -> Error {
    Error::new("demo_invalid", message)
}
fn bounded(value: f64, min: f64, max: f64, name: &str) -> Result<()> {
    if !value.is_finite() || value < min || value > max {
        return Err(invalid(format!("{name} must be between {min} and {max}")));
    }
    Ok(())
}
fn color(value: &str) -> bool {
    value.len() == 7 && value.starts_with('#') && value[1..].bytes().all(|b| b.is_ascii_hexdigit())
}
fn regular_file(p: &Path, limit: u64) -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(p)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > limit {
        return Err(invalid("Expected a bounded regular local file"));
    }
    Ok(file)
}
fn read_json(p: &Path) -> Result<Value> {
    let file = regular_file(p, JSON_LIMIT)?;
    if !file.metadata()?.is_file() || file.metadata()?.len() > JSON_LIMIT {
        return Err(invalid("Scene or request exceeds the 2 MiB JSON limit"));
    }
    Ok(serde_json::from_reader(file.take(JSON_LIMIT + 1))?)
}
fn hash_file(p: &Path, limit: u64) -> Result<String> {
    let mut file = regular_file(p, limit)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > limit {
        return Err(invalid(
            "Media must be a nonempty regular file within the size limit",
        ));
    }
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    let mut total = 0;
    loop {
        check_cancelled()?;
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > limit {
            return Err(invalid("Media exceeds the size limit"));
        }
        hash.update(&buffer[..n]);
    }
    Ok(hash
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>())
}
fn verify(source: &Source) -> Result<()> {
    if hash_file(Path::new(&source.path), MAX_MEDIA)? != source.sha256 {
        return Err(Error::new(
            "demo_source_changed",
            "Source media changed since scene creation",
        ));
    }
    Ok(())
}
fn tool(name: &str) -> Result<String> {
    tc::find_optional(name)?
        .ok_or_else(|| Error::new("demo_tool_missing", format!("Demo Studio requires {name}")))
}
fn renderer() -> Result<String> {
    let sibling = own_executable()?
        .parent()
        .unwrap()
        .join("tomacco-demo-render");
    if tc::executable(&sibling) {
        return Ok(sibling.to_string_lossy().into_owned());
    }
    tool("tomacco-demo-render")
}
fn probe(p: &Path, count: bool) -> Result<Value> {
    let mut args = vec![
        tool("ffprobe")?,
        "-v".into(),
        "error".into(),
        "-protocol_whitelist".into(),
        "file,pipe".into(),
        "-show_streams".into(),
        "-show_format".into(),
        "-of".into(),
        "json".into(),
    ];
    if count {
        args.push("-count_frames".into());
    }
    args.push(p.to_string_lossy().into_owned());
    let out = tc::run(&args, if count { 900 } else { 30 }, None, true)?;
    Ok(serde_json::from_str(s(&out, "stdout"))?)
}
fn video(probe: &Value) -> Result<&Value> {
    probe["streams"]
        .as_array()
        .and_then(|a| a.iter().find(|s| s["codec_type"] == "video"))
        .ok_or_else(|| invalid("No video stream in local media"))
}
fn number(v: &Value) -> Option<f64> {
    v.as_f64()
        .or_else(|| v.as_str()?.parse().ok())
        .filter(|f: &f64| f.is_finite())
}
fn rate(v: &Value) -> Option<f64> {
    let text = v.as_str()?;
    if let Some((a, b)) = text.split_once('/') {
        let b: f64 = b.parse().ok()?;
        if b == 0.0 {
            return None;
        }
        Some(a.parse::<f64>().ok()? / b)
    } else {
        text.parse().ok()
    }
}
fn inspect(p: &Path) -> Result<Source> {
    let before = hash_file(p, MAX_MEDIA)?;
    let metadata = probe(p, false)?;
    let v = video(&metadata)?;
    let raw = number(&v["side_data_list"][0]["rotation"])
        .or_else(|| number(&v["tags"]["rotate"]))
        .unwrap_or(0.0);
    let rotation = ((raw.round() as i64).rem_euclid(360)) as u32;
    if ![0, 90, 180, 270].contains(&rotation) {
        return Err(invalid("Only right-angle source rotation is supported"));
    }
    let mut width = v["width"].as_u64().unwrap_or(0) as u32;
    let mut height = v["height"].as_u64().unwrap_or(0) as u32;
    if [90, 270].contains(&rotation) {
        std::mem::swap(&mut width, &mut height);
    }
    let duration = number(&v["duration"])
        .or_else(|| number(&metadata["format"]["duration"]))
        .unwrap_or(0.0);
    let fps = rate(&v["avg_frame_rate"])
        .or_else(|| rate(&v["r_frame_rate"]))
        .unwrap_or(0.0);
    bounded(duration, 0.1, 86400.0, "Source duration")?;
    bounded(fps, 0.01, 1000.0, "Source frame rate")?;
    if width == 0
        || height == 0
        || width > 8192
        || height > 8192
        || width as u64 * height as u64 > 32_000_000
    {
        return Err(invalid(
            "Source dimensions exceed the 32 MP / 8192 px decode limit",
        ));
    }
    if hash_file(p, MAX_MEDIA)? != before {
        return Err(Error::new(
            "demo_source_changed",
            "Source changed during inspection",
        ));
    }
    Ok(Source {
        path: p.to_string_lossy().into_owned(),
        sha256: before,
        width,
        height,
        duration,
        fps,
        rotation,
        has_audio: metadata["streams"]
            .as_array()
            .unwrap()
            .iter()
            .any(|s| s["codec_type"] == "audio"),
    })
}
impl Scene {
    fn validate(&self) -> Result<()> {
        if self.format != "tomacco.demo"
            || self.version != 1
            || self.revision == 0
            || self.revision > 9_007_199_254_740_991
            || self.title.chars().count() > 200
            || self.id.is_empty()
        {
            return Err(invalid(
                "Unsupported scene format, revision, title or identity",
            ));
        }
        identifier(&self.id, "scene identity", Some("[a-f0-9]{32}"))?;
        if !Path::new(&self.source.path).is_absolute()
            || self.source.sha256.len() != 64
            || !self.source.sha256.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err(invalid(
                "Source path and digest must be frozen inspection values",
            ));
        }
        if self.source.width == 0
            || self.source.height == 0
            || self.source.width > 8192
            || self.source.height > 8192
            || self.source.width as u64 * self.source.height as u64 > 32_000_000
            || ![0, 90, 180, 270].contains(&self.source.rotation)
        {
            return Err(invalid("Invalid source geometry"));
        }
        bounded(self.source.duration, 0.1, 86400.0, "Source duration")?;
        bounded(self.source.fps, 0.01, 1000.0, "Source FPS")?;
        for d in [self.output.width, self.output.height] {
            if !(320..=3840).contains(&d) || d % 2 != 0 {
                return Err(invalid("Output dimensions must be even, 320..3840"));
            }
        }
        if ![24, 25, 30, 60].contains(&self.output.fps) {
            return Err(invalid("Output FPS must be 24, 25, 30 or 60"));
        }
        bounded(self.output.duration, 0.1, 60.0, "Output duration")?;
        bounded(self.clip.start, 0.0, self.source.duration, "Clip start")?;
        if self.clip.start + self.output.duration > self.source.duration + 0.000_001 {
            return Err(invalid("Clip extends beyond the inspected source duration"));
        }
        if !["phone", "tablet"].contains(&self.device.model.as_str()) || !color(&self.device.finish)
        {
            return Err(invalid("Invalid device model or finish"));
        }
        bounded(self.device.bezel, 0.005, 0.12, "Bezel")?;
        bounded(self.device.thickness, 0.01, 0.2, "Thickness")?;
        bounded(self.device.corner_radius, 0.01, 0.2, "Corner radius")?;
        if !["solid", "gradient", "image"].contains(&self.background.style.as_str())
            || !color(&self.background.color)
            || !color(&self.background.color2)
        {
            return Err(invalid("Invalid background style or colors"));
        }
        if self.background.style == "image"
            && (!Path::new(&self.background.image).is_absolute()
                || self.background.image_sha256.len() != 64)
        {
            return Err(invalid("Image background requires a verified local PNG"));
        }
        bounded(self.shadow.opacity, 0.0, 1.0, "Shadow opacity")?;
        bounded(self.shadow.blur, 0.0, 100.0, "Shadow blur")?;
        bounded(self.shadow.offset_x, -500.0, 500.0, "Shadow X")?;
        bounded(self.shadow.offset_y, -500.0, 500.0, "Shadow Y")?;
        bounded(self.camera.distance, 2.0, 12.0, "Camera distance")?;
        bounded(self.camera.fov, 15.0, 75.0, "Camera field of view")?;
        if self.keyframes.is_empty() || self.keyframes.len() > 120 {
            return Err(invalid("Scene requires 1..120 keyframes"));
        }
        let mut previous = -1.0;
        for k in &self.keyframes {
            bounded(k.time, 0.0, self.output.duration, "Keyframe time")?;
            if k.time <= previous {
                return Err(invalid("Keyframe times must be strictly increasing"));
            }
            previous = k.time;
            bounded(k.yaw, -75.0, 75.0, "Yaw")?;
            bounded(k.pitch, -75.0, 75.0, "Pitch")?;
            bounded(k.roll, -180.0, 180.0, "Roll")?;
            bounded(k.scale, 0.1, 1.5, "Scale")?;
            bounded(k.x, 0.0, 1.0, "X")?;
            bounded(k.y, 0.0, 1.0, "Y")?;
            if !["linear", "smooth", "ease-in", "ease-out"].contains(&k.easing.as_str()) {
                return Err(invalid("Unknown keyframe easing"));
            }
        }
        Ok(())
    }
    fn verify(&self) -> Result<()> {
        self.validate()?;
        verify(&self.source)?;
        if self.background.style == "image"
            && hash_file(Path::new(&self.background.image), 64 * 1024 * 1024)?
                != self.background.image_sha256
        {
            return Err(Error::new(
                "demo_source_changed",
                "Background image changed",
            ));
        }
        Ok(())
    }
}
fn load(p: &Path) -> Result<Scene> {
    let scene: Scene = serde_json::from_value(read_json(p)?).map_err(|e| invalid(e.to_string()))?;
    scene.validate()?;
    Ok(scene)
}
fn scene_lock(p: &Path) -> Result<Lock> {
    let lock = Lock::open(&home()?.join("locks").join(format!(
        "demo-scene-{}.lock",
        digest(p.as_os_str().as_encoded_bytes())
    )))?;
    lock.acquire(false)?;
    Ok(lock)
}
fn response(p: &Path, scene: &Scene) -> Value {
    json!({"path":p,"revision":scene.revision,"scene":scene})
}
fn absent(p: &Path) -> Result<()> {
    if fs::symlink_metadata(p).is_ok() {
        return Err(Error::new(
            "demo_conflict",
            format!("Destination already exists: {}", p.display()),
        ));
    }
    Ok(())
}
fn publish(from: &Path, to: &Path) -> Result<()> {
    File::open(from)?.sync_all()?;
    fs::hard_link(from, to).map_err(|e| {
        Error::new(
            "demo_conflict",
            format!("Cannot publish {} without overwriting: {e}", to.display()),
        )
    })?;
    File::open(to.parent().unwrap())?.sync_all()?;
    Ok(())
}
fn create(a: &Value) -> Result {
    let p = output_path(s(a, "path"))?;
    let _lock = scene_lock(&p)?;
    absent(&p)?;
    let source = inspect(&path(s(a, "source"), false, true)?)?;
    let preset = a["preset"].as_str().unwrap_or("portrait-orbit");
    if !PRESETS.contains(&preset) {
        return Err(invalid("Unknown scene preset"));
    }
    let (w, h) = match preset {
        "landscape-orbit" => (1920, 1080),
        "square-float" => (1080, 1080),
        _ => (1080, 1920),
    };
    let duration = source.duration.min(8.0);
    let still = preset == "portrait-still";
    let value = json!({"format":"tomacco.demo","version":1,"revision":1,"id":uuid::Uuid::new_v4().simple().to_string(),"title":a["title"].as_str().unwrap_or("Tomacco app demo"),"source":source,"clip":{"start":0.0},"output":{"width":w,"height":h,"fps":30,"duration":duration},"device":{"model":"phone","finish":"#242421","bezel":0.035,"thickness":0.065,"corner_radius":0.09},"background":{"color":"#f4f2ec","color2":"#e7e4da","style":"solid","image":""},"shadow":{"enabled":true,"opacity":0.22,"blur":24.0,"offset_x":0.0,"offset_y":40.0},"camera":{"distance":4.0,"fov":35.0},"keyframes":[{"time":0.0,"yaw":if still {0.0}else{-18.0},"pitch":8.0,"roll":if still {0.0}else{-4.0},"scale":0.72,"x":0.5,"y":0.5,"easing":"smooth"},{"time":duration,"yaw":if still {0.0}else{18.0},"pitch":8.0,"roll":if still {0.0}else{4.0},"scale":0.72,"x":0.5,"y":0.5,"easing":"smooth"}]});
    let scene: Scene = serde_json::from_value(value)?;
    scene.validate()?;
    let parent = p.parent().unwrap();
    if !parent.is_dir() {
        return Err(invalid("Scene parent directory must already exist"));
    }
    let temp = tempfile::NamedTempFile::new_in(parent)?;
    atomic_json(temp.path(), &serde_json::to_value(&scene)?)?;
    publish(temp.path(), &p)?;
    Ok(response(&p, &scene))
}
fn merge(target: &mut Value, changes: &Value) {
    if let (Some(t), Some(c)) = (target.as_object_mut(), changes.as_object()) {
        for (k, v) in c {
            if v.is_object() && t.get(k).is_some_and(Value::is_object) {
                merge(t.get_mut(k).unwrap(), v);
            } else {
                t.insert(k.clone(), v.clone());
            }
        }
    } else {
        *target = changes.clone();
    }
}
fn revision(scene: &Scene, a: &Value) -> Result<()> {
    if a["if_revision"].as_u64() != Some(scene.revision) {
        return Err(Error::new(
            "demo_conflict",
            "Scene revision changed; reload before saving or rendering",
        )
        .details(json!({"current_revision":scene.revision})));
    }
    Ok(())
}
fn update(a: &Value) -> Result {
    let p = output_path(s(a, "path"))?;
    let _lock = scene_lock(&p)?;
    let old = load(&p)?;
    revision(&old, a)?;
    let changes = a["changes"]
        .as_object()
        .filter(|o| !o.is_empty())
        .ok_or_else(|| invalid("Changes must be a nonempty object"))?;
    if changes.keys().any(|k| {
        ![
            "title",
            "clip",
            "output",
            "device",
            "background",
            "shadow",
            "camera",
            "keyframes",
        ]
        .contains(&k.as_str())
    }) {
        return Err(invalid(
            "Source, format, identity and revision cannot be edited",
        ));
    }
    let mut value = serde_json::to_value(&old)?;
    merge(&mut value, &a["changes"]);
    value["revision"] = json!(old.revision + 1);
    let mut scene: Scene = serde_json::from_value(value).map_err(|e| invalid(e.to_string()))?;
    if scene.background.style == "image" {
        let p = path(&scene.background.image, false, true)?;
        validate_png(&p, None)?;
        scene.background.image = p.to_string_lossy().into_owned();
        scene.background.image_sha256 = hash_file(&p, 64 * 1024 * 1024)?;
    }
    scene.verify()?;
    atomic_json(&p, &serde_json::to_value(&scene)?)?;
    Ok(response(&p, &scene))
}
fn h264_encoder(ffmpeg: &str) -> bool {
    tc::run(
        &[
            ffmpeg.to_string(),
            "-hide_banner".into(),
            "-encoders".into(),
        ],
        10,
        None,
        true,
    )
    .is_ok_and(|v| {
        s(&v, "stdout")
            .lines()
            .any(|line| line.split_whitespace().nth(1) == Some("libx264"))
    })
}
fn capabilities() -> Result {
    let r = renderer().ok();
    let f = tc::find_optional("ffmpeg")?;
    let p = tc::find_optional("ffprobe")?;
    let encoder = f.as_deref().is_some_and(h264_encoder);
    let mut missing = Vec::new();
    if r.is_none() {
        missing.push("tomacco-demo-render");
    }
    if f.is_none() {
        missing.push("ffmpeg");
    }
    if p.is_none() {
        missing.push("ffprobe");
    }
    if !encoder {
        missing.push("ffmpeg encoder libx264");
    }
    Ok(
        json!({"ready":r.is_some()&&f.is_some()&&p.is_some()&&encoder,"export_ready":r.is_some()&&f.is_some()&&p.is_some()&&encoder,"h264_encoder":{"name":"libx264","available":encoder},"missing":missing,"renderer":{"available":r.is_some(),"path":r},"ffmpeg":{"available":f.is_some(),"path":f},"ffprobe":{"available":p.is_some(),"path":p},"presets":PRESETS,"models":["phone","tablet"],"easings":["linear","smooth","ease-in","ease-out"],"backgrounds":["solid","gradient","image"],"limits":{"width_min":320,"width_max":3840,"height_min":320,"height_max":3840,"even_dimensions":true,"fps":[24,25,30,60],"duration_min":0.1,"duration_max":60,"keyframes_max":120,"source_bytes_max":MAX_MEDIA,"source_pixels_max":32000000,"background_format":"PNG","source_frame_rate_preserved":false},"silent":true,"timestamp_behavior":"Output frame i samples clip.start + i/fps; ffmpeg selects the source frame at that time. Source display dimensions include autorotation. Upscaling does not add source detail or frame rate."}),
    )
}
fn job_result(mut job: Value, request: &Value) -> Value {
    for key in [
        "scene_path",
        "scene_revision",
        "output",
        "poster",
        "manifest",
        "time",
    ] {
        if let Some(v) = request.get(key) {
            job[key] = v.clone();
        }
    }
    job
}
fn render(a: &Value, preview: bool) -> Result {
    let p = path(s(a, "path"), false, true)?;
    let _scene_lock = scene_lock(&p)?;
    let scene = load(&p)?;
    revision(&scene, a)?;
    scene.verify()?;
    let output = output_path(s(a, "output"))?;
    if !output.parent().unwrap().is_dir() {
        return Err(invalid("Output parent directory must already exist"));
    }
    let extension = output
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if extension != if preview { "png" } else { "mp4" } {
        return Err(invalid(
            "Preview output requires .png; export output requires .mp4",
        ));
    }
    let time = if preview {
        let t = a["time"]
            .as_f64()
            .ok_or_else(|| invalid("Preview requires time"))?;
        bounded(t, 0.0, scene.output.duration, "Preview time")?;
        Some(t)
    } else {
        None
    };
    let helper = renderer()?;
    let ffmpeg = tool("ffmpeg")?;
    if !preview && !h264_encoder(&ffmpeg) {
        return Err(Error::new(
            "demo_tool_missing",
            "The local ffmpeg does not provide the required libx264 encoder",
        ));
    }
    tool("ffprobe")?;
    let exclusive = format!("demo-output:{}", output.display());
    let lock = Lock::open(
        &home()?
            .join("locks")
            .join(format!("demo-admit-{}.lock", digest(exclusive.as_bytes()))),
    )?;
    lock.acquire(false)?;
    let identity = json!({"kind":if preview{"preview"}else{"export"},"scene_path":p,"scene_revision":scene.revision,"scene_digest":digest(&serde_json::to_vec(&scene)?),"output":output,"time":time});
    let token = if let Some(id) = a["request_id"].as_str() {
        identifier(id, "request ID", Some("[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"))?;
        format!("explicit:{id}")
    } else {
        identity.to_string()
    };
    let root = home()?.join("demo");
    private_dir(&root)?;
    let registry = root.join(format!("request-{}.json", digest(token.as_bytes())));
    let request_lock =
        Lock::open(&root.join(format!("request-{}.lock", digest(token.as_bytes()))))?;
    request_lock.acquire(false)?;
    if registry.exists() {
        let previous = read_json(&registry)?;
        if previous["identity"] != identity {
            return Err(Error::new(
                "demo_conflict",
                "Request ID was already used for a different render",
            ));
        }
        if previous["job_id"].as_str().is_some() {
            let job = jobs::get(s(&previous, "job_id"))?;
            return Ok(job_result(job, &previous["request"]));
        }
        if let Some(active) = jobs::active_exclusive(&exclusive)? {
            if active["plan"]["operation_key"] == identity {
                atomic_json(
                    &registry,
                    &json!({"identity":identity,"job_id":active["id"],"request":previous["request"]}),
                )?;
                return Ok(job_result(active, &previous["request"]));
            }
        }
        return Err(Error::new(
            "demo_conflict",
            "A previous admission for this request was interrupted; inspect job.list and use a new request ID if no matching job exists",
        ));
    }
    if let Some(active) = jobs::active_exclusive(&exclusive)? {
        return Err(
            Error::new("demo_conflict", "Output already has an active render")
                .details(json!({"job":active})),
        );
    }
    let poster = PathBuf::from(format!("{}.poster.png", output.display()));
    let manifest = PathBuf::from(format!("{}.manifest.json", output.display()));
    absent(&output)?;
    if !preview {
        absent(&poster)?;
        absent(&manifest)?;
    }
    let scratch = root.join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&scratch)?;
    let temporary = scratch.join(if preview { "render.png" } else { "render.mp4" });
    let mut request = json!({"kind":if preview{"preview"}else{"export"},"scene_path":p,"scene_revision":scene.revision,"scene":scene,"output":output,"scratch":scratch,"temporary":temporary,"time":time,"created_at":now()});
    if !preview {
        request["poster"] = json!(poster);
        request["manifest"] = json!(manifest);
    }
    let request_path = scratch.join("request.json");
    atomic_json(&request_path, &request)?;
    let own = own_executable()?;
    let render_scene = scratch.join("render-scene.json");
    let mut args = vec![
        helper,
        "--scene".into(),
        render_scene.to_string_lossy().into_owned(),
        "--output".into(),
        temporary.to_string_lossy().into_owned(),
    ];
    if let Some(t) = time {
        args.extend(["--time".into(), t.to_string()]);
    }
    let mut artifacts = vec![output.clone(), scratch.join("validation.json")];
    if !preview {
        artifacts.extend([poster, manifest]);
    }
    let spec = json!({"cwd":scratch,"timeout":7200,"retry":{"count":0},"exclusive":exclusive,"single_active":true,"operation_key":identity,"metadata":{"demo":request},"steps":[{"phase":"verifying_source","argv":[own,"_demo_prepare",request_path]},{"phase":"rendering","argv":args},{"phase":"validating_output","argv":[own,"_demo_finalize",request_path],"artifacts":artifacts}]});
    // Reserve request identity durably before spawning: a crash cannot permit
    // the same explicit ID to start a different output concurrently.
    atomic_json(
        &registry,
        &json!({"identity":identity,"job_id":null,"request":request}),
    )?;
    let job = jobs::submit(
        if preview {
            "Demo preview"
        } else {
            "Demo export"
        },
        spec,
    )?;
    atomic_json(
        &registry,
        &json!({"identity":identity,"job_id":job["id"],"request":request}),
    )?;
    Ok(job_result(job, &request))
}
fn copy_verified(from: &Path, to: &Path, expected: &str, limit: u64) -> Result<()> {
    let mut input = regular_file(from, limit)?;
    let metadata = input.metadata()?;
    if !metadata.is_file() || metadata.len() > limit {
        return Err(invalid("Source copy exceeds limit"));
    }
    let mut output = private_file(to, false)?;
    let mut hash = Sha256::new();
    let mut buf = [0; 65536];
    let mut total = 0;
    loop {
        check_cancelled()?;
        let n = input.read(&mut buf)?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > limit {
            return Err(invalid("Source grew past size limit"));
        }
        output.write_all(&buf[..n])?;
        hash.update(&buf[..n]);
    }
    output.sync_all()?;
    if hash
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>()
        != expected
    {
        return Err(Error::new(
            "demo_source_changed",
            "Source changed while preparing immutable render input",
        ));
    }
    Ok(())
}
pub fn prepare(request_path: &str) -> Result<()> {
    let r = read_json(Path::new(request_path))?;
    let mut scene: Scene = serde_json::from_value(r["scene"].clone())?;
    scene.verify()?;
    let scratch = Path::new(s(&r, "scratch"));
    let source = scratch.join("source.media");
    copy_verified(
        Path::new(&scene.source.path),
        &source,
        &scene.source.sha256,
        MAX_MEDIA,
    )?;
    let actual = inspect(&source)?;
    if actual.width != scene.source.width
        || actual.height != scene.source.height
        || (actual.duration - scene.source.duration).abs() > 0.001
        || actual.rotation != scene.source.rotation
        || (actual.fps - scene.source.fps).abs() > 0.0001
        || actual.has_audio != scene.source.has_audio
    {
        return Err(Error::new(
            "demo_source_changed",
            "Source metadata no longer matches the scene",
        ));
    }
    scene.source.path = source.to_string_lossy().into_owned();
    if scene.background.style == "image" {
        let image = scratch.join("background.png");
        copy_verified(
            Path::new(&scene.background.image),
            &image,
            &scene.background.image_sha256,
            64 * 1024 * 1024,
        )?;
        scene.background.image = image.to_string_lossy().into_owned();
    }
    atomic_json(
        &scratch.join("render-scene.json"),
        &serde_json::to_value(&scene)?,
    )?;
    Ok(())
}
fn validate_png(p: &Path, dimensions: Option<(u32, u32)>) -> Result<Value> {
    let mut file = regular_file(p, 64 * 1024 * 1024)?;
    if !file.metadata()?.is_file() || file.metadata()?.len() > 64 * 1024 * 1024 {
        return Err(Error::new("demo_output_invalid", "PNG exceeds 64 MiB"));
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(64 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    let image = crate::visual::decode(&bytes)
        .map_err(|e| Error::new("demo_output_invalid", format!("Cannot decode PNG: {e}")))?;
    if dimensions.is_some_and(|d| d != (image.width, image.height)) {
        return Err(Error::new(
            "demo_output_invalid",
            "PNG dimensions do not match requested output",
        ));
    }
    Ok(
        json!({"width":image.width,"height":image.height,"sha256":digest(&bytes),"bytes":bytes.len()}),
    )
}
fn validate_video(p: &Path, expected: Option<&Output>) -> Result<Value> {
    let metadata = probe(p, true)?;
    let v = video(&metadata)?;
    let width = v["width"].as_u64().unwrap_or(0);
    let height = v["height"].as_u64().unwrap_or(0);
    let fps = rate(&v["avg_frame_rate"]).unwrap_or(0.0);
    let frames = number(&v["nb_read_frames"]).unwrap_or(0.0) as u64;
    let duration = number(&v["duration"])
        .or_else(|| number(&metadata["format"]["duration"]))
        .unwrap_or(0.0);
    let audio = metadata["streams"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| s["codec_type"] == "audio");
    if width == 0
        || height == 0
        || width > 3840
        || height > 3840
        || frames == 0
        || frames > 3600
        || duration <= 0.0
        || duration > 61.0
    {
        return Err(Error::new(
            "demo_output_invalid",
            "Output media exceeds the supported dimensions, frames or duration",
        ));
    }
    if let Some(e) = expected {
        let requested_frames = (e.duration * e.fps as f64).ceil() as u64;
        if width != e.width as u64
            || height != e.height as u64
            || frames != requested_frames
            || (fps - e.fps as f64).abs() > 0.0001
            || (duration - frames as f64 / e.fps as f64).abs() > 0.002
            || audio
            || v["codec_name"] != "h264"
            || v["pix_fmt"] != "yuv420p"
        {
            return Err(Error::new("demo_output_invalid","Encoded output does not match requested H.264 dimensions, FPS, frames, duration or silent audio policy").details(metadata));
        }
    }
    tc::run(
        &[
            tool("ffmpeg")?,
            "-nostdin".into(),
            "-v".into(),
            "error".into(),
            "-xerror".into(),
            "-protocol_whitelist".into(),
            "file,pipe".into(),
            "-i".into(),
            p.to_string_lossy().into_owned(),
            "-map".into(),
            "0:v:0".into(),
            "-an".into(),
            "-f".into(),
            "null".into(),
            "-".into(),
        ],
        900,
        None,
        true,
    )?;
    Ok(
        json!({"path":p,"width":width,"height":height,"fps":fps,"frames":frames,"duration":duration,"has_audio":audio,"codec":v["codec_name"],"pixel_format":v["pix_fmt"],"sha256":hash_file(p,MAX_MEDIA)?,"decoded":true}),
    )
}
/// Recorded hardlinks let the durable job supervisor clean a killed finalizer.
/// Only exact expected destinations that still name our recorded inode qualify.
pub fn cleanup_failed_artifacts(spec: &Value) -> Result<()> {
    let request = &spec["metadata"]["demo"];
    if request["kind"] != "preview" && request["kind"] != "export" {
        return Ok(());
    }
    let record = Path::new(s(request, "scratch")).join("publication.json");
    if !record.exists() {
        return Ok(());
    }
    cleanup_publication(request, &read_json(&record)?)
}
fn cleanup_publication(request: &Value, record: &Value) -> Result<()> {
    let temporary = s(request, "temporary");
    let allowed = [
        (temporary.to_string(), s(request, "output").to_string()),
        (
            format!("{temporary}.poster.png"),
            s(request, "poster").to_string(),
        ),
        (
            Path::new(s(request, "scratch"))
                .join("checked-manifest.json")
                .to_string_lossy()
                .into_owned(),
            s(request, "manifest").to_string(),
        ),
    ];
    let entries = record
        .as_array()
        .filter(|a| a.len() <= 3)
        .ok_or_else(|| invalid("Invalid publication recovery record"))?;
    for entry in entries.iter().rev() {
        let source = s(entry, "source");
        let target = s(entry, "destination");
        if target.is_empty() || !allowed.iter().any(|(a, b)| a == source && b == target) {
            return Err(invalid(
                "Publication recovery path does not match its render request",
            ));
        }
        if let Ok(metadata) = fs::symlink_metadata(target) {
            if metadata.is_file()
                && Some(metadata.dev()) == entry["device"].as_u64()
                && Some(metadata.ino()) == entry["inode"].as_u64()
            {
                fs::remove_file(target)?;
                File::open(Path::new(target).parent().unwrap())?.sync_all()?;
            }
        }
    }
    Ok(())
}
struct Publication {
    request: Value,
    record: Value,
    committed: bool,
}
impl Publication {
    fn new(request: &Value, pairs: &[(PathBuf, PathBuf)]) -> Result<Self> {
        let mut entries = Vec::new();
        for (source, destination) in pairs {
            absent(destination)?;
            let m = fs::metadata(source)?;
            entries.push(
                json!({"source":source,"destination":destination,"device":m.dev(),"inode":m.ino()}),
            );
        }
        let record = json!(entries);
        atomic_json(
            &Path::new(s(request, "scratch")).join("publication.json"),
            &record,
        )?;
        Ok(Self {
            request: request.clone(),
            record,
            committed: false,
        })
    }
}
impl Drop for Publication {
    fn drop(&mut self) {
        if !self.committed {
            if let Err(e) = cleanup_publication(&self.request, &self.record) {
                eprintln!("Publication rollback failed: {e}");
            }
        }
    }
}
pub fn finalize(request_path: &str) -> Result<()> {
    let r = read_json(Path::new(request_path))?;
    let scene: Scene = serde_json::from_value(r["scene"].clone())?;
    scene.verify()?;
    let scratch = Path::new(s(&r, "scratch"));
    let copied = load(&scratch.join("render-scene.json"))?;
    copied.verify()?;
    let temp = Path::new(s(&r, "temporary"));
    let out = Path::new(s(&r, "output"));
    absent(out)?;
    let preview = r["kind"] == "preview";
    let actual = if preview {
        validate_png(temp, Some((scene.output.width, scene.output.height)))?
    } else {
        validate_video(temp, Some(&scene.output))?
    };
    let mut report = json!({"format":"tomacco.demo.validation","version":1,"validated_at":now(),"scene_revision":scene.revision,"source_sha256":scene.source.sha256,"source_verified":true,"media":actual,"silent":true,"output":out});
    report["media"]["path"] = json!(out);
    let mut pairs = Vec::new();
    if preview {
        report["time"] = r["time"].clone();
        report["frame_index"] = json!(
            ((r["time"].as_f64().unwrap() * scene.output.fps as f64).floor() as u64)
                .min((scene.output.duration * scene.output.fps as f64).ceil() as u64 - 1)
        );
    } else {
        let temp_poster = PathBuf::from(format!("{}.poster.png", temp.display()));
        let renderer_manifest =
            read_json(&PathBuf::from(format!("{}.manifest.json", temp.display())))?;
        report["poster"] = validate_png(
            &temp_poster,
            Some((scene.output.width, scene.output.height)),
        )?;
        if renderer_manifest["format"] != "tomacco.demo.render"
            || renderer_manifest["version"] != 1
            || renderer_manifest["width"] != actual["width"]
            || renderer_manifest["height"] != actual["height"]
            || renderer_manifest["frames"] != actual["frames"]
            || number(&renderer_manifest["fps"])
                .is_none_or(|f| (f - scene.output.fps as f64).abs() > 0.0001)
            || number(&renderer_manifest["duration"])
                .is_none_or(|d| (d - number(&actual["duration"]).unwrap()).abs() > 0.002)
            || number(&renderer_manifest["requested_duration"])
                .is_none_or(|d| (d - scene.output.duration).abs() > 0.000001)
            || renderer_manifest["silent"] != true
        {
            return Err(Error::new(
                "demo_output_invalid",
                "Renderer manifest disagrees with independently decoded output",
            )
            .details(renderer_manifest));
        }
        report["renderer"] = renderer_manifest;
        let manifest = json!({"format":"tomacco.demo.export","version":1,"scene":scene,"scene_path":r["scene_path"],"output":out,"poster":r["poster"],"validation":report});
        let checked = scratch.join("checked-manifest.json");
        atomic_json(&checked, &manifest)?;
        pairs.push((temp_poster, PathBuf::from(s(&r, "poster"))));
        pairs.push((checked, PathBuf::from(s(&r, "manifest"))));
    }
    // Finish all expensive validation before exposing any public artifact.
    scene.verify()?;
    copied.verify()?;
    check_cancelled()?;
    atomic_json(&scratch.join("validation.json"), &report)?;
    pairs.push((temp.to_path_buf(), out.to_path_buf()));
    let mut publication = Publication::new(&r, &pairs)?;
    // MP4 is last. Drop rolls back errors; the job supervisor handles SIGTERM.
    for (source, destination) in &pairs {
        check_cancelled()?;
        publish(source, destination)?;
    }
    publication.committed = true;
    println!("{}", report);
    Ok(())
}
pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "demo.capabilities" => capabilities(),
        "demo.media.inspect" => Ok(serde_json::to_value(inspect(&path(
            s(a, "path"),
            false,
            true,
        )?)?)?),
        "demo.scene.create" => create(a),
        "demo.scene.get" => {
            let p = path(s(a, "path"), false, true)?;
            let _lock = scene_lock(&p)?;
            let scene = load(&p)?;
            scene.verify()?;
            Ok(response(&p, &scene))
        }
        "demo.scene.update" => update(a),
        "demo.preview" => render(a, true),
        "demo.export" => render(a, false),
        "demo.export.inspect" => {
            let p = path(s(a, "path"), false, true)?;
            let mut actual = validate_video(&p, None)?;
            let manifest = PathBuf::from(format!("{}.manifest.json", p.display()));
            if manifest.exists() {
                let m = read_json(&manifest)?;
                if m["validation"]["media"]["sha256"] != actual["sha256"] {
                    return Err(Error::new(
                        "demo_output_invalid",
                        "Export manifest digest does not match media",
                    ));
                }
                actual["manifest"] = m;
            }
            Ok(actual)
        }
        _ => Err(Error::new("unknown_operation", op)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn scene() -> Scene {
        serde_json::from_value(json!({"format":"tomacco.demo","version":1,"revision":1,"id":"1234567890abcdef1234567890abcdef","title":"Fixture","source":{"path":"/tmp/demo-source.mp4","sha256":"0".repeat(64),"width":1080,"height":2400,"duration":8.0,"fps":30.0,"rotation":0,"has_audio":false},"clip":{"start":0.0},"output":{"width":1080,"height":1920,"fps":30,"duration":8.0},"device":{"model":"phone","finish":"#242421","bezel":0.035,"thickness":0.065,"corner_radius":0.09},"background":{"color":"#f4f2ec","color2":"#f4f2ec","style":"solid","image":""},"shadow":{"enabled":true,"opacity":0.22,"blur":24.0,"offset_x":0.0,"offset_y":40.0},"camera":{"distance":4.0,"fov":35.0},"keyframes":[{"time":0.0,"yaw":-18.0,"pitch":8.0,"roll":-4.0,"scale":0.72,"x":0.5,"y":0.5,"easing":"smooth"},{"time":8.0,"yaw":18.0,"pitch":8.0,"roll":4.0,"scale":0.72,"x":0.5,"y":0.5,"easing":"smooth"}]})).unwrap()
    }
    #[test]
    fn rejects_unordered_keyframes_and_unknown_nested_fields() {
        let mut s = scene();
        s.validate().unwrap();
        s.keyframes[1].time = 0.0;
        assert!(s.validate().is_err());
        let mut v = serde_json::to_value(scene()).unwrap();
        v["device"]["shell"] = json!("ignored?");
        assert!(serde_json::from_value::<Scene>(v).is_err());
    }
    #[test]
    fn rejects_invalid_geometry_clip_and_nonfinite_numbers() {
        let mut s = scene();
        s.output.width = 1081;
        assert!(s.validate().is_err());
        s = scene();
        s.clip.start = 0.1;
        assert!(s.validate().is_err());
        s = scene();
        s.camera.fov = f64::NAN;
        assert!(s.validate().is_err());
        s = scene();
        s.keyframes[0].scale = 1.51;
        assert!(s.validate().is_err());
    }
    #[test]
    fn partial_edit_keeps_unedited_timeline_and_rejects_stale_revision() {
        let mut v = serde_json::to_value(scene()).unwrap();
        let frames = v["keyframes"].clone();
        merge(
            &mut v,
            &json!({"device":{"finish":"#ff0000"},"output":{"width":1920}}),
        );
        let s: Scene = serde_json::from_value(v).unwrap();
        assert_eq!(s.device.finish, "#ff0000");
        assert_eq!(s.device.bezel, 0.035);
        assert_eq!(serde_json::to_value(&s.keyframes).unwrap(), frames);
        assert_eq!(
            revision(&s, &json!({"if_revision":2})).unwrap_err().code,
            "demo_conflict"
        );
    }
    #[test]
    fn source_copy_is_hashed_not_merely_copied() {
        let temp = tempfile::tempdir().unwrap();
        let src = temp.path().join("source");
        let dst = temp.path().join("copy");
        fs::write(&src, b"original").unwrap();
        let hash = hash_file(&src, 1024).unwrap();
        fs::write(&src, b"modified").unwrap();
        assert_eq!(
            copy_verified(&src, &dst, &hash, 1024).unwrap_err().code,
            "demo_source_changed"
        );
        copy_verified(&src, &dst, &digest(b"modified"), 1024).unwrap();
        assert_eq!(fs::read(dst).unwrap(), b"modified");
    }
    #[test]
    fn publication_never_overwrites_existing_regular_file_or_symlink() {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("new");
        let target = temp.path().join("existing");
        fs::write(&source, b"new").unwrap();
        fs::write(&target, b"old").unwrap();
        assert_eq!(publish(&source, &target).unwrap_err().code, "demo_conflict");
        assert_eq!(fs::read(&target).unwrap(), b"old");
        let link = temp.path().join("link");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(publish(&source, &link).is_err());
        assert_eq!(fs::read(target).unwrap(), b"old");
    }
    #[test]
    fn publication_rollback_removes_only_its_own_unchanged_hardlinks() {
        let temp = tempfile::tempdir().unwrap();
        let scratch = temp.path().join("scratch");
        fs::create_dir(&scratch).unwrap();
        let source = scratch.join("render.png");
        let target = temp.path().join("preview.png");
        fs::write(&source, b"render").unwrap();
        let request =
            json!({"kind":"preview","scratch":scratch,"temporary":source,"output":target});
        {
            let _publication =
                Publication::new(&request, &[(source.clone(), target.clone())]).unwrap();
            publish(&source, &target).unwrap();
        }
        assert!(!target.exists());
        {
            let _publication =
                Publication::new(&request, &[(source.clone(), target.clone())]).unwrap();
            publish(&source, &target).unwrap();
            fs::remove_file(&target).unwrap();
            fs::write(&target, b"other writer").unwrap();
        }
        assert_eq!(fs::read(&target).unwrap(), b"other writer");
        cleanup_failed_artifacts(&json!({"metadata":{"demo":request}})).unwrap();
        assert_eq!(fs::read(target).unwrap(), b"other writer");
    }
    #[test]
    fn recovery_record_cannot_redirect_cleanup_to_unrelated_files() {
        let temp = tempfile::tempdir().unwrap();
        let target = temp.path().join("unrelated");
        fs::write(&target, b"preserve").unwrap();
        let metadata = fs::metadata(&target).unwrap();
        let request = json!({"kind":"preview","scratch":temp.path(),"temporary":temp.path().join("render.png"),"output":temp.path().join("preview.png")});
        let record = json!([{"source":temp.path().join("render.png"),"destination":target,"device":metadata.dev(),"inode":metadata.ino()}]);
        assert!(cleanup_publication(&request, &record).is_err());
        assert_eq!(fs::read(target).unwrap(), b"preserve");
    }
    #[test]
    fn png_validator_requires_complete_decodable_requested_geometry() {
        let temp = tempfile::tempdir().unwrap();
        let p = temp.path().join("frame.png");
        let image = crate::visual::Image {
            width: 2,
            height: 2,
            pixels: vec![255; 16],
        };
        fs::write(&p, crate::visual::encode(&image).unwrap()).unwrap();
        validate_png(&p, Some((2, 2))).unwrap();
        assert!(validate_png(&p, Some((4, 4))).is_err());
        fs::write(&p, b"\x89PNG\r\n\x1a\n").unwrap();
        assert!(validate_png(&p, None).is_err());
    }
}
