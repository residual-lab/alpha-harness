//! Keep a Python and a venv, run the app, apply updates: the launcher, in the desktop app.
//!
//! The heavy half — a `CPython` and ~330 MB of wheels — is installed once by the bundled `uv`
//! into a per-user folder ([`home`]); an update is just this project's wheel, a few megabytes.
//!
//! This process is the *parent* of the app, which is what makes updating safe. Windows keeps a
//! running process's extension modules open, so an install over a live app fails half-way.
//! Here the app writes the version it wants and exits, the install happens with nothing
//! running, and the app starts again.
//!
//! Two venv slots, one live and one spare: an update builds the spare and flips a pointer, so a
//! failed install cannot touch what works, and a version that will not start is stepped back
//! out of. The folder, its files and what the app reads from them are the Python launcher's, so
//! an install it made carries straight over.

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::{Mutex, OnceLock, PoisonError};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value, json};
use tauri::{AppHandle, Emitter, Manager, Url};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

use crate::Status;

/// The release this was built as, stamped by CI from the tag; a local build says 0.0.0.
pub const BUILD_VERSION: &str = match option_env!("ALPHA_HARNESS_VERSION") {
    Some(version) => version,
    None => "0.0.0",
};
const REPOSITORY: &str = "residual-lab/alpha-harness";
const REPOSITORY_VARIABLE: &str = "ALPHA_HARNESS_REPOSITORY";

const HOME_VARIABLE: &str = "ALPHA_HARNESS_HOME";
/// This program's version, handed to the app: the in-app update replaces the wheel and never
/// this program, so without it the app cannot tell the program around it is years older.
const LAUNCHER_VARIABLE: &str = "ALPHA_HARNESS_LAUNCHER";
/// This process's id, so the app can tell when its parent is gone.
const PID_VARIABLE: &str = "ALPHA_HARNESS_LAUNCHER_PID";
/// Tells the app a window of its own is showing it, so it opens no browser.
const SHELL_VARIABLE: &str = "ALPHA_HARNESS_SHELL";

/// The wheel's SHA-256 as CI hashed it while building this program, for its own version: the
/// first install is checked against it, whatever the release's files say by then.
const BUILT_WHEEL_SHA256: Option<&str> = option_env!("ALPHA_HARNESS_WHEEL_SHA256");

const REQUEST_FILE: &str = "update-request.json";
const ERROR_FILE: &str = "update-error.json";
/// Written by the app once it serves, naming its version and the port it serves on.
const SERVED_FILE: &str = "served.json";
/// The version that last installed itself over an older install, so it tries once only.
const OUTGROWN_FILE: &str = "launcher-upgrade.txt";
const ACTIVE_FILE: &str = "active-slot.txt";
const LOCK_FILE: &str = "running.lock";
/// This program's path, for an app the old launcher started to hand over to.
const DESKTOP_FILE: &str = "desktop-app.txt";
/// How long the folder's lock is waited for. An app the old launcher started opens this program
/// and exits, and the old launcher lets go of the lock only once it sees that.
const CLAIM_WAIT: Duration = Duration::from_secs(15);
const LOG_FILE: &str = "launcher.log";
/// Past this the log starts afresh beside one previous file: the app writes to it as it works.
const LOG_LIMIT: u64 = 10 * 1024 * 1024;
/// What a failure box reads from the end of the log: plenty for its last lines.
const LOG_TAIL: u64 = 64 * 1024;
/// How long the app's last lines are waited for once it has exited: a process it started may
/// hold its output open for good.
const DRAIN: Duration = Duration::from_secs(2);
/// Where the app serves unless another program holds it, and where a release from before
/// [`SERVED_FILE`] named a port always serves; kept in step with `alpha_harness.__main__`.
const APP_PORT: u16 = 8000;
const SLOTS: [&str; 2] = ["a", "b"];
/// Long enough for a cold `uv` to fetch `CPython` and ~330 MB of wheels on a slow line.
const INSTALL_TIMEOUT: Duration = Duration::from_mins(30);
/// Tries at the whole install before giving up: a slow line drops a download now and then, and
/// uv's cache keeps whatever already arrived.
const INSTALL_ATTEMPTS: u32 = 3;
/// How long the app is given to close itself before it is terminated. Worth waiting for: a
/// clean exit unwinds uvicorn's lifespan, which releases `DuckDB`'s single-writer lock.
const QUIT_TIMEOUT: Duration = Duration::from_secs(15);
/// How long Quit keeps asking a backend that is still starting, perhaps migrating the database,
/// before terminating it: the request cannot reach it until its port is open.
const START_TIMEOUT: Duration = Duration::from_mins(1);
const POLL: Duration = Duration::from_millis(250);
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Set when the user chooses Quit, so a deliberate exit is told from a crash.
static QUITTING: AtomicBool = AtomicBool::new(false);
/// The running app, so Quit can close it. `None` whenever none is running.
static CHILD: Mutex<Option<Child>> = Mutex::new(None);
static ROOT: OnceLock<PathBuf> = OnceLock::new();
/// Held for the life of the process; the OS drops it however this one ends.
static LOCK: OnceLock<File> = OnceLock::new();
/// The start-up page, to show again while an update installs.
static SPLASH: OnceLock<Url> = OnceLock::new();
/// Set once the supervisor has finished, so Quit can tell it has nothing left to stop.
static DONE: AtomicBool = AtomicBool::new(false);

/// Where everything this program owns lives. Per-user, so updating needs no elevation.
pub fn home() -> PathBuf {
    if let Some(path) = std::env::var_os(HOME_VARIABLE).filter(|v| !v.is_empty()) {
        return path.into();
    }
    let user = std::env::home_dir().unwrap_or_default();
    let base = if cfg!(windows) {
        std::env::var_os("LOCALAPPDATA")
            .filter(|v| !v.is_empty())
            .map_or_else(|| user.join("AppData").join("Local"), PathBuf::from)
    } else if cfg!(target_os = "macos") {
        user.join("Library").join("Application Support")
    } else {
        xdg_data(&user)
    };
    base.join("AlphaHarness")
}

fn xdg_data(user: &Path) -> PathBuf {
    // The XDG spec has a relative one ignored, as it does an empty one.
    std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| user.join(".local").join("share"))
}

/// The applications menu entry the Python launcher wrote, named as this app's own: left behind,
/// the menu shows Alpha Harness twice, and one of them opens the old launcher.
#[cfg(target_os = "linux")]
fn forget_old_launcher() {
    // Without a home, the entry's path would be relative to wherever this was started.
    let Some(user) = std::env::home_dir() else {
        return;
    };
    let entry = xdg_data(&user)
        .join("applications")
        .join("alpha-harness.desktop");
    // Only the one it wrote, known by its comment; an entry someone made by hand stays.
    if fs::read_to_string(&entry)
        .is_ok_and(|text| text.contains("Comment=Alpha generation for WorldQuant BRAIN"))
        && fs::remove_file(&entry).is_ok()
    {
        note("removed the old launcher's applications menu entry");
    }
}

fn root() -> &'static Path {
    ROOT.get_or_init(home)
}

fn download() -> String {
    let repository = std::env::var(REPOSITORY_VARIABLE)
        .ok()
        .filter(|r| !r.is_empty())
        .unwrap_or_else(|| REPOSITORY.to_owned());
    format!("https://github.com/{repository}/releases/download")
}

// --- the log ---------------------------------------------------------------------------------

/// `YYYY-MM-DD HH:MM:SS` in UTC: the date from days since the epoch, as `civil_from_days` does.
fn stamp() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let (days, rest) = (seconds / 86_400, seconds % 86_400);
    let z = days.cast_signed() + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02} {:02}:{:02}:{:02}Z",
        rest / 3600,
        rest / 60 % 60,
        rest % 60
    )
}

/// Append to the log: the window shows the app, so this file is the only account of a run.
pub fn note(message: &str) {
    let line = format!("{} {message}\n", stamp());
    log()
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .append(line.as_bytes());
}

/// The log's one writer, for this program's notes and the app's output alike: starting afresh is
/// then a rename between two whole lines, and nothing written meanwhile is lost.
struct Log {
    path: PathBuf,
    file: Option<File>,
    /// Bytes since it last started afresh, or tried to.
    written: u64,
    limit: u64,
}

impl Log {
    fn new(path: PathBuf, limit: u64) -> Self {
        Self {
            path,
            file: None,
            written: 0,
            limit,
        }
    }

    fn append(&mut self, bytes: &[u8]) {
        if self.file.is_none() {
            self.file = open_log(&self.path);
            self.written = self
                .file
                .as_ref()
                .and_then(|file| file.metadata().ok())
                .map_or(0, |meta| meta.len());
        }
        if let Some(file) = &mut self.file
            && file.write_all(bytes).is_ok()
        {
            self.written += bytes.len() as u64;
        }
        if self.written > self.limit {
            // Closed first: Windows renames no file that is open.
            self.file = None;
            let mut previous = self.path.clone().into_os_string();
            previous.push(".1");
            let _ = fs::rename(&self.path, previous);
            // Counted afresh even if another program holding it kept it from being renamed, so
            // that is tried again a whole log later rather than on every line.
            self.file = open_log(&self.path);
            self.written = 0;
        }
    }
}

fn open_log(path: &Path) -> Option<File> {
    OpenOptions::new().create(true).append(true).open(path).ok()
}

fn log() -> &'static Mutex<Log> {
    static LOG: OnceLock<Mutex<Log>> = OnceLock::new();
    LOG.get_or_init(|| Mutex::new(Log::new(root().join(LOG_FILE), LOG_LIMIT)))
}

/// Copy one of the app's streams into the log a whole line at a time, so a line of its stdout
/// never lands inside one of its stderr, nor the log starts afresh in the middle of one.
fn pump(pipe: impl Read, log: &Mutex<Log>) {
    let mut pipe = BufReader::new(pipe);
    let mut line = Vec::new();
    while pipe.read_until(b'\n', &mut line).is_ok_and(|read| read > 0) {
        // The last one, cut short by the app's exit.
        if !line.ends_with(b"\n") {
            line.push(b'\n');
        }
        log.lock()
            .unwrap_or_else(PoisonError::into_inner)
            .append(&line);
        line.clear();
    }
}

/// The tail of the log, for a message box. A traceback's last lines carry the reason.
fn recent(lines: usize) -> String {
    let read = || -> std::io::Result<String> {
        let mut log = File::open(root().join(LOG_FILE))?;
        let end = log.seek(SeekFrom::End(0))?;
        let start = end.saturating_sub(LOG_TAIL);
        log.seek(SeekFrom::Start(start))?;
        let mut bytes = Vec::new();
        log.read_to_end(&mut bytes)?;
        let text = String::from_utf8_lossy(&bytes).into_owned();
        // Begun mid-line, the first line is a fragment.
        Ok(match text.split_once('\n') {
            Some((_, rest)) if start > 0 => rest.to_owned(),
            _ => text,
        })
    };
    match read() {
        Ok(tail) => {
            let all: Vec<&str> = tail.lines().collect();
            all[all.len().saturating_sub(lines)..].join("\n")
        }
        Err(error) => format!("(the log could not be read: {error})"),
    }
}

/// The current step, on the start-up page and in the log.
fn status(app: &AppHandle, text: &str) {
    note(text);
    if let Ok(mut current) = app.state::<Status>().0.lock() {
        text.clone_into(&mut current);
    }
    let _ = app.emit("startup", text);
}

/// Report a failure the user can act on, and wait for them to read it.
fn fail(app: &AppHandle, message: &str) {
    note(&format!("FATAL {message}"));
    let log = root().join(LOG_FILE);
    app.dialog()
        .message(format!("{message}\n\nDetails: {}", log.display()))
        .title("Alpha Harness")
        .kind(MessageDialogKind::Error)
        .blocking_show();
}

// --- files shared with the app ---------------------------------------------------------------

/// Written beside and renamed over, so a crash mid-write never leaves an empty file behind.
/// Retried a moment: Windows refuses to replace or delete a file someone has open that instant,
/// a virus scanner say.
fn patiently(mut change: impl FnMut() -> std::io::Result<()>) -> std::io::Result<()> {
    let mut tries = 0;
    loop {
        match change() {
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied && tries < 5 => {
                tries += 1;
                thread::sleep(Duration::from_millis(50));
            }
            done => return done,
        }
    }
}

fn write_atomic(path: &Path, contents: impl AsRef<[u8]>) -> std::io::Result<()> {
    let partial = path.with_extension("partial");
    let mut file = File::create(&partial)?;
    file.write_all(contents.as_ref())?;
    // On disk before the rename, or a power cut can leave the name on an empty file.
    file.sync_all()?;
    drop(file);
    patiently(|| fs::rename(&partial, path))
}

fn read_json(path: &Path) -> Map<String, Value> {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default()
}

fn text_of(body: &Map<String, Value>, key: &str) -> Option<String> {
    body.get(key).and_then(Value::as_str).map(str::to_owned)
}

fn other(slot: &str) -> &'static str {
    if slot == "a" { "b" } else { "a" }
}

fn active() -> &'static str {
    let recorded = fs::read_to_string(root().join(ACTIVE_FILE)).unwrap_or_default();
    SLOTS
        .into_iter()
        .find(|s| *s == recorded.trim())
        .unwrap_or("a")
}

/// Point at a slot. One small write, which is what makes the swap atomic enough.
fn activate(slot: &str) {
    if let Err(error) = write_atomic(&root().join(ACTIVE_FILE), slot) {
        note(&format!("could not record the active slot: {error}"));
    }
    note(&format!("active slot -> {slot}"));
}

/// The console interpreter, which is what `uv` expects to be pointed at.
fn venv_python(slot: &str) -> PathBuf {
    let venv = root().join(format!("venv-{slot}"));
    if cfg!(windows) {
        venv.join("Scripts").join("python.exe")
    } else {
        venv.join("bin").join("python")
    }
}

/// The windowed interpreter, so running the app flashes no console. Only Windows has one.
fn venv_pythonw(slot: &str) -> PathBuf {
    if cfg!(windows) {
        root()
            .join(format!("venv-{slot}"))
            .join("Scripts")
            .join("pythonw.exe")
    } else {
        venv_python(slot)
    }
}

fn marker(slot: &str) -> PathBuf {
    root().join(format!("slot-{slot}.json"))
}

/// What is installed in a slot, or `None` when it holds nothing usable.
fn slot_version(slot: &str) -> Option<String> {
    if !venv_python(slot).exists() {
        return None;
    }
    text_of(&read_json(&marker(slot)), "version")
}

/// Whether the slot's version has served the app at least once. Only an install that recorded
/// otherwise is unproven: one installed before this was recorded was already running.
fn proven(slot: &str) -> bool {
    read_json(&marker(slot)).get("served") != Some(&Value::Bool(false))
}

/// Record that the slot's version served, so a later failed exit is not blamed on it.
fn prove(slot: &str) {
    let mut body = read_json(&marker(slot));
    if body.get("served") == Some(&Value::Bool(false)) {
        body.insert("served".into(), Value::Bool(true));
        let _ = write_atomic(&marker(slot), Value::Object(body).to_string());
    }
}

fn requested() -> Option<String> {
    // Only version characters: it goes into a download URL and a folder name.
    text_of(&read_json(&root().join(REQUEST_FILE)), "version").filter(|v| {
        !v.is_empty()
            && v.chars()
                .all(|c| c.is_ascii_alphanumeric() || ".+-".contains(c))
    })
}

/// The wheel URL the app read off the release, rather than one built from the version.
fn requested_wheel(version: &str) -> Option<String> {
    // Of this very release, with no `..` to climb out of it into another repository's.
    let release = format!("{}/v{version}/", download());
    text_of(&read_json(&root().join(REQUEST_FILE)), "wheelUrl")
        .filter(|url| url.starts_with(&release) && !url.contains(".."))
}

/// The wheel's SHA-256 as the app read it off the release, given as GitHub gives an asset's
/// digest: `sha256:` and the hash in hex.
fn requested_digest() -> Option<String> {
    text_of(&read_json(&root().join(REQUEST_FILE)), "wheelDigest")
        .and_then(|digest| digest.strip_prefix("sha256:").map(str::to_ascii_lowercase))
        .filter(|hex| hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit()))
}

/// Orders dated releases and their candidates: `2026.10.1rc1 < 2026.10.1rc2 < 2026.10.1 <
/// 2026.10.1.1`. A candidate is never newer than its release, and the release is newer than it.
fn release_of(version: &str) -> (Vec<u64>, bool, u64) {
    let lead: String = version
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '.')
        .collect();
    let numbers = lead
        .split('.')
        .map_while(|part| part.parse().ok())
        .collect();
    let rest = &version[lead.len()..];
    // Only a bare version is a release: `rc2`, `a1` or `.dev0` after it marks a pre-release.
    let candidate = rest.strip_prefix("rc").and_then(|n| n.parse().ok());
    (numbers, rest.is_empty(), candidate.unwrap_or(0))
}

/// Whether this program is newer than the version installed and has not installed itself
/// yet. An install too broken to start never reaches the in-app update, and downloading the
/// newer program is what a person does next. Once per version: one that fails to start is
/// reverted and not tried again.
fn outgrown(running: Option<&str>) -> bool {
    let Some(running) = running else { return false };
    if release_of(BUILD_VERSION) <= release_of(running) {
        return false;
    }
    let tried = fs::read_to_string(root().join(OUTGROWN_FILE)).unwrap_or_default();
    tried.trim() != BUILD_VERSION
}

/// Leave the app something to say about a version that did not take.
fn note_failure(version: &str, reason: &str) {
    let time = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64());
    let reason: String = reason.chars().take(400).collect();
    let body = json!({"version": version, "error": reason, "time": time});
    let _ = write_atomic(&root().join(ERROR_FILE), body.to_string());
}

// --- installing -------------------------------------------------------------------------------

/// The `uv` bundled beside this program, or the one an earlier launcher left in the folder.
/// Bundled under a name of its own: a .deb puts it in /usr/bin, beside any `uv` already there.
fn find_uv() -> Option<PathBuf> {
    let exe = if cfg!(windows) { ".exe" } else { "" };
    let beside = std::env::current_exe()
        .ok()?
        .parent()?
        .join(format!("alpha-harness-uv{exe}"));
    [beside, root().join(format!("uv{exe}"))]
        .into_iter()
        .find(|path| path.exists())
}

fn quiet(command: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}

/// Outside Windows no job object holds `uv`, so it can outlive a killed parent. It inherits the
/// folder's lock, so a relaunch waits for it rather than installing over it; on Linux it is
/// also killed along with this program. A group of its own lets [`stop`] end what it started.
#[cfg(unix)]
fn contain(command: &mut Command) -> &mut Command {
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;

    let lock = LOCK.get().map(AsRawFd::as_raw_fd);
    #[cfg(target_os = "linux")]
    let parent = std::process::id();
    command.process_group(0);
    // SAFETY: only async-signal-safe calls, between fork and exec.
    unsafe {
        command.pre_exec(move || {
            if let Some(fd) = lock {
                let flags = libc::fcntl(fd, libc::F_GETFD);
                libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC);
            }
            // Checked after: a parent gone before the request was made sends no signal.
            #[cfg(target_os = "linux")]
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) == 0
                && u32::try_from(libc::getppid()).ok() != Some(parent)
            {
                libc::_exit(1);
            }
            Ok(())
        })
    }
}

#[cfg(not(unix))]
fn contain(command: &mut Command) -> &mut Command {
    command
}

/// Kill `uv` and, outside Windows, everything it started, which would hold the folder's lock.
fn stop(child: &mut Child) {
    #[cfg(unix)]
    if let Ok(group) = i32::try_from(child.id()) {
        // SAFETY: a signal to the group `contain` gave uv, numbered by its still unreaped id.
        unsafe {
            libc::kill(-group, libc::SIGKILL);
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// Run `uv`, its progress on the start-up page and its errors into the log; give up after
/// [`INSTALL_TIMEOUT`].
fn run_uv(app: &AppHandle, uv: &Path, arguments: &[&str], downloads: u32) -> Result<(), String> {
    let mut command = Command::new(uv);
    command
        .args(arguments)
        // uv's 30 s default failed first installs on slow lines; two minutes still bounds a
        // stalled connection, and fewer downloads at once do not starve each other.
        .env("UV_HTTP_TIMEOUT", "120")
        .env("UV_HTTP_RETRIES", "5")
        .env("UV_CONCURRENT_DOWNLOADS", downloads.to_string())
        // uv keeps its Python builds and cache inside the folder, so uninstalling is deleting
        // one folder and nothing is left behind in the user's profile.
        .env("UV_PYTHON_INSTALL_DIR", root().join("python"))
        .env("UV_CACHE_DIR", root().join("cache"))
        .env("UV_NO_CONFIG", "1")
        // Never a Python found on the machine: a Homebrew or python.org one is moved or removed
        // by its own upgrades, taking the venv built on it down too.
        .env("UV_PYTHON_PREFERENCE", "only-managed")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    let what = arguments.first().copied().unwrap_or("uv");
    let mut child = contain(quiet(&mut command))
        .spawn()
        .map_err(|e| format!("uv {what} failed: {e}"))?;
    // Or a killed install keeps running, racing the next start over the same venv and cache.
    #[cfg(windows)]
    if !job::adopt(&child) {
        note("could not tie uv to this program's lifetime");
    }
    let stderr = child.stderr.take();
    let page = app.clone();
    let reader = thread::spawn(move || {
        let mut text = String::new();
        let Some(stderr) = stderr else { return text };
        let mut stderr = BufReader::new(stderr);
        let mut line = Vec::new();
        while stderr
            .read_until(b'\n', &mut line)
            .is_ok_and(|read| read > 0)
        {
            let piece = String::from_utf8_lossy(&line);
            // Shown as it arrives, so nobody closes an install that is still moving.
            let shown = piece.trim();
            if ["Download", "Resolved", "Prepared", "Installed"]
                .iter()
                .any(|step| shown.starts_with(step))
            {
                let _ = page.emit("startup-detail", shown);
            }
            text.push_str(&piece);
            line.clear();
        }
        text
    });
    let deadline = Instant::now() + INSTALL_TIMEOUT;
    let status = loop {
        // Quit stops an install at once: its slot's marker was removed first, so a half-built
        // slot never reads as one that works, and the next start builds it again.
        if QUITTING.load(Ordering::SeqCst) {
            stop(&mut child);
            return Err("stopped by Quit".into());
        }
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() < deadline => thread::sleep(POLL),
            _ => {
                stop(&mut child);
                break None;
            }
        }
    };
    // Timed out, a process uv started may still hold its stderr open, so it is not waited for.
    let errors = if status.is_some() {
        reader.join().unwrap_or_default()
    } else {
        String::new()
    };
    let tail = |limit: usize| -> String {
        let trimmed = errors.trim();
        let skip = trimmed.chars().count().saturating_sub(limit);
        trimmed.chars().skip(skip).collect()
    };
    let code = status.and_then(|s| s.code());
    note(&format!("uv {what} -> {code:?} {}", tail(2000)));
    match status {
        Some(s) if s.success() => Ok(()),
        Some(_) => Err(format!("uv {what} failed: {}", tail(400))),
        None => Err(format!(
            "uv {what} did not finish within {} minutes",
            INSTALL_TIMEOUT.as_secs() / 60
        )),
    }
}

/// Build `slot` from scratch at exactly `version`. Always the slot that is *not* running:
/// installing over a live environment is how an interrupted upgrade bricks it.
///
/// Rebuilding rather than upgrading in place costs no download — the wheels are in uv's
/// cache from the last install — and a slot never carries a package left by an old version.
///
/// With the wheel's SHA-256 known, uv refuses a wheel that does not match it and, under
/// `--require-hashes`, anything the release's constraints leave unhashed. Every dependency is
/// checked against those constraints either way.
fn install(
    app: &AppHandle,
    uv: &Path,
    slot: &str,
    version: &str,
    wheel: Option<String>,
    digest: Option<String>,
) -> Result<(), String> {
    let base = download();
    // A development build installs a wheel built from this checkout (`uv build` leaves
    // constraints.txt beside it), since no release exists for its 0.0.0.
    let local = if cfg!(debug_assertions) {
        std::env::var("ALPHA_HARNESS_WHEEL").ok()
    } else {
        None
    };
    let constraints = local.as_deref().map_or_else(
        || format!("{base}/v{version}/constraints.txt"),
        |path| {
            Path::new(path)
                .with_file_name("constraints.txt")
                .to_string_lossy()
                .into_owned()
        },
    );
    // A local wheel is taken as it is: uv reads a hash only off a URL.
    let sha256 = BUILT_WHEEL_SHA256
        .filter(|_| version == BUILD_VERSION)
        .map(str::to_owned)
        .or(digest)
        .filter(|_| local.is_none());
    let wheel = local
        .or(wheel)
        .unwrap_or_else(|| format!("{base}/v{version}/alpha_harness-{version}-py3-none-any.whl"));
    let wheel = match &sha256 {
        Some(sha256) => format!("{wheel}#sha256={sha256}"),
        None => wheel,
    };
    note(&format!("installing {version} from {wheel}"));
    // The marker goes first: a half-built slot must never read as a working one.
    let _ = fs::remove_file(marker(slot));
    let venv = root().join(format!("venv-{slot}"));
    let python = venv_python(slot);
    let python = python.to_string_lossy();
    // Constrained to the versions the release was locked against, so a dependency publishing a
    // breaking version cannot break an install that worked yesterday.
    let mut arguments = vec![
        "pip",
        "install",
        "--python",
        &python,
        "-c",
        &constraints,
        &wheel,
    ];
    if sha256.is_some() {
        arguments.push("--require-hashes");
    }
    let mut attempt = 1;
    loop {
        let _ = fs::remove_dir_all(&venv);
        // One download at a time once a try has failed: on a contended line, two still can.
        let downloads = if attempt == 1 { 2 } else { 1 };
        let tried = run_uv(
            app,
            uv,
            &["venv", "--python", "3.14", &venv.to_string_lossy()],
            downloads,
        )
        .and_then(|()| run_uv(app, uv, &arguments, downloads));
        match tried {
            Err(error) if attempt < INSTALL_ATTEMPTS && !QUITTING.load(Ordering::SeqCst) => {
                note(&format!("install attempt {attempt} failed: {error}"));
                attempt += 1;
                status(
                    app,
                    &format!(
                        "The download was interrupted. Trying again ({attempt} of \
                         {INSTALL_ATTEMPTS}), one file at a time…"
                    ),
                );
            }
            result => break result?,
        }
    }
    let body = json!({"version": version, "served": false});
    write_atomic(&marker(slot), body.to_string())
        .map_err(|e| format!("could not record the install: {e}"))?;
    note(&format!("installed {version} into slot {slot}"));
    Ok(())
}

// --- running ----------------------------------------------------------------------------------

/// The app dies with this program however this program dies: on Windows a job object holds it,
/// so a kill from Task Manager cannot leave it running with `DuckDB`'s lock and its port.
#[cfg(windows)]
mod job {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use std::sync::OnceLock;

    use windows_sys::Win32::Foundation::HANDLE;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_BREAKAWAY_OK,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JobObjectExtendedLimitInformation, SetInformationJobObject,
    };

    /// Never closed: closing the last handle to the job is what kills the app.
    struct Job(HANDLE);
    // SAFETY: a job handle is a kernel object handle, usable from any thread.
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    static JOB: OnceLock<Option<Job>> = OnceLock::new();

    fn create() -> Option<Job> {
        // SAFETY: plain Win32 calls on a zeroed, correctly sized structure.
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return None;
            }
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            // Breakaway lets the browser the app opens for a link outlive Alpha Harness.
            limits.BasicLimitInformation.LimitFlags =
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;
            let size = u32::try_from(size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>()).ok()?;
            let set = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                (&raw const limits).cast(),
                size,
            );
            (set != 0).then_some(Job(job))
        }
    }

    pub fn adopt(child: &Child) -> bool {
        let Some(job) = JOB.get_or_init(create) else {
            return false;
        };
        // SAFETY: both handles are live for the duration of the call.
        unsafe { AssignProcessToJobObject(job.0, child.as_raw_handle()) != 0 }
    }
}

/// What blocks until the app has exited, leaving it for [`Child::wait`] to reap: until then its
/// id is still the app's, so a kill from Quit cannot reach a process that took the id over.
#[cfg(unix)]
fn exit_of(child: &Child) -> impl FnOnce() + Send + 'static {
    let pid = child.id();
    move || {
        let mut info = std::mem::MaybeUninit::<libc::siginfo_t>::uninit();
        loop {
            // SAFETY: waitid only writes the siginfo_t it is pointed at, which outlives the call.
            let waited = unsafe {
                libc::waitid(
                    libc::P_PID,
                    pid,
                    info.as_mut_ptr(),
                    libc::WEXITED | libc::WNOWAIT,
                )
            };
            if waited == 0
                || std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted
            {
                return;
            }
        }
    }
}

#[cfg(windows)]
fn exit_of(child: &Child) -> impl FnOnce() + Send + 'static {
    use std::os::windows::io::{AsHandle, AsRawHandle};

    use windows_sys::Win32::System::Threading::{INFINITE, WaitForSingleObject};

    // A handle of its own, open for as long as the wait, wherever the `Child` is by then.
    let process = child.as_handle().try_clone_to_owned();
    move || {
        if let Ok(process) = process {
            // SAFETY: a live process handle, owned by this closure.
            unsafe { WaitForSingleObject(process.as_raw_handle(), INFINITE) };
        }
    }
}

/// A thread of the supervisor's own, named for a debugger or `top -H`.
fn worker(name: &str, work: impl FnOnce() + Send + 'static) {
    if let Err(error) = thread::Builder::new().name(name.to_owned()).spawn(work) {
        note(&format!("could not start {name}: {error}"));
    }
}

/// Copy one of the app's streams into the log on a thread of its own, which lets go of `done`
/// once the stream ends.
fn copy_to_log(name: &str, pipe: Option<impl Read + Send + 'static>, done: Sender<()>) {
    if let Some(pipe) = pipe {
        worker(name, move || {
            pump(pipe, log());
            drop(done);
        });
    }
}

fn open(app: &AppHandle, url: Url) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.navigate(url);
    }
}

/// Back to the start-up page, which says how an update is getting on; the app's own page
/// would sit on a dead server meanwhile.
fn show_splash(app: &AppHandle) {
    if let Some(url) = SPLASH.get() {
        open(app, url.clone());
    }
}

/// The start-up page's address, as it reports itself once loaded: a window just opened reads
/// about:blank, and Windows serves it from a different origin than macOS and Linux.
pub fn remember_splash(url: Url) {
    let _ = SPLASH.set(url);
}

fn navigate(app: &AppHandle, port: u16) {
    match Url::parse(&format!("http://127.0.0.1:{port}")) {
        Ok(url) => open(app, url),
        Err(error) => note(&format!("could not open the app: {error}")),
    }
}

/// The version the app serves and the port it serves on, once it does.
fn served_at(body: &Map<String, Value>) -> Option<(String, u16)> {
    let version = text_of(body, "version")?;
    let port = body
        .get("port")
        .and_then(Value::as_u64)
        .and_then(|port| u16::try_from(port).ok())
        .filter(|port| *port != 0);
    Some((version, port.unwrap_or(APP_PORT)))
}

/// Give a backend that just exited a moment to let go of port 8000, so the next one serves at
/// the same address and its page keeps what it stored there. Held by any other program, the app
/// serves on a port of its own.
fn await_port() {
    let deadline = Instant::now() + Duration::from_secs(3);
    while TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], APP_PORT))).is_err()
        && Instant::now() < deadline
    {
        thread::sleep(POLL);
    }
}

/// Run the app to completion, its output in the log. Returns its exit code.
fn start(app: &AppHandle, slot: &str) -> i32 {
    note(&format!("starting app from slot {slot}"));
    let served = root().join(SERVED_FILE);
    // Left behind, the last run's would take the app's page to a server not yet listening.
    if let Err(error) = patiently(|| fs::remove_file(&served))
        && error.kind() != std::io::ErrorKind::NotFound
    {
        note(&format!("could not clear {SERVED_FILE}: {error}"));
    }
    let mut command = Command::new(venv_pythonw(slot));
    command
        .args(["-m", "alpha_harness"])
        .env(HOME_VARIABLE, root())
        .env(LAUNCHER_VARIABLE, BUILD_VERSION)
        .env(PID_VARIABLE, std::process::id().to_string())
        .env(SHELL_VARIABLE, "tauri")
        .env("PYTHONUTF8", "1")
        // `-m` puts the working folder first on the import path, so a stray `random.py` there
        // would replace the real one.
        .env("PYTHONSAFEPATH", "1")
        // Set globally, either points the venv's Python at some other standard library.
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .stdin(Stdio::null())
        // Through this program, the log's one writer, so it can start afresh losing nothing.
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = match quiet(&mut command).spawn() {
        Ok(child) => child,
        // Antivirus holding a freshly installed pythonw.exe, say. A failed exit rather than a
        // panic, so the caller can still roll back and say why.
        Err(error) => {
            note(&format!("could not start the app: {error}"));
            return 1;
        }
    };
    #[cfg(windows)]
    if !job::adopt(&child) {
        note("could not tie the app to this program's lifetime");
    }
    // Every sender is gone once the app has exited and its last lines are in the log.
    let (exited, events) = mpsc::channel();
    copy_to_log("app-stdout", child.stdout.take(), exited.clone());
    copy_to_log("app-stderr", child.stderr.take(), exited.clone());
    let exit = exit_of(&child);
    if let Ok(mut held) = CHILD.lock() {
        // Quit chosen while it was starting found nothing to close, so it is closed here.
        if QUITTING.load(Ordering::SeqCst) {
            drop(held);
            let _ = child.kill();
            let _ = child.wait();
            return 0;
        }
        *held = Some(child);
    }
    worker("app-waiter", move || {
        exit();
        let _ = exited.send(());
    });
    // Until it serves, served.json is looked for every so often; after, only the app's exit
    // wakes this thread.
    let mut navigated = false;
    while let Err(RecvTimeoutError::Timeout) =
        events.recv_timeout(if navigated { Duration::MAX } else { POLL })
    {
        // Proven as soon as it serves: a tray app can run for weeks before it ever exits.
        if !QUITTING.load(Ordering::SeqCst)
            && let Some((version, port)) = served_at(&read_json(&served))
        {
            if Some(version) == slot_version(slot) {
                prove(slot);
            }
            navigate(app, port);
            navigated = true;
        }
    }
    let code = reap();
    // Its last lines, a traceback say, belong in the log before anything reads it.
    let _ = events.recv_timeout(DRAIN);
    // Gone with the run it describes, so nothing later reads it as the next one's.
    let _ = fs::remove_file(&served);
    note(&format!("app exited {code}"));
    code
}

/// The exit code of the app, which only this thread reaps: Quit merely kills it. A wait that ended
/// early, as macOS's does for a process that is only stopped, is made up for by looking again
/// every so often, the app still in [`CHILD`] for Quit to reach.
fn reap() -> i32 {
    loop {
        let Ok(mut held) = CHILD.lock() else {
            return -1;
        };
        let Some(child) = held.as_mut() else {
            return 0;
        };
        match child.try_wait() {
            Ok(Some(status)) => {
                *held = None;
                return status.code().unwrap_or(-1);
            }
            Ok(None) => {}
            Err(error) => {
                note(&format!("lost track of the app: {error}"));
                *held = None;
                return -1;
            }
        }
        drop(held);
        thread::sleep(POLL);
    }
}

/// One `POST /api/quit`, and whether the app took it. HTTP because a windowed process on Windows
/// has no graceful signal, and terminating it skips the shutdown that closes `DuckDB`.
fn ask_to_quit(port: u16) -> std::io::Result<bool> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(5))?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    // The app refuses a write without X-Harness-Client; this program is a client like any.
    stream.write_all(
        format!(
            "POST /api/quit HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Harness-Client: 1\r\n\
             Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        .as_bytes(),
    )?;
    // Only the status line: the app may hold the connection open while it shuts down.
    let mut answer = Vec::new();
    Read::by_ref(&mut stream)
        .take(12)
        .read_to_end(&mut answer)?;
    Ok(answer.starts_with(b"HTTP/1.1 2"))
}

/// Ask the app to close itself, and make sure it has. A backend still starting has no port
/// open yet, so it is asked again until it can hear, then given [`QUIT_TIMEOUT`] to finish.
fn close_app() {
    let mut deadline = Instant::now() + START_TIMEOUT;
    let mut asked = false;
    loop {
        // Nothing of ours is running: whatever holds its port is some other program.
        if CHILD.lock().map_or(true, |held| held.is_none()) {
            return;
        }
        if Instant::now() >= deadline {
            break;
        }
        // Only once it serves, at the port it names: before then that port may answer for some
        // other program.
        if !asked
            && let Some((_, port)) = served_at(&read_json(&root().join(SERVED_FILE)))
            && ask_to_quit(port).is_ok_and(|took| took)
        {
            note("asked the app to close");
            asked = true;
            deadline = Instant::now() + QUIT_TIMEOUT;
        }
        thread::sleep(POLL);
    }
    note("the app did not close in time; terminating it");
    // Not reaped here: the supervisor, waiting on it, does that once it has gone.
    if let Ok(mut held) = CHILD.lock()
        && let Some(child) = held.as_mut()
    {
        let _ = child.kill();
    }
}

/// Quit from the tray: close the app, or stop an install, then this program. The supervisor
/// exits by itself once it sees that, unless a message box holds it, so it is not waited on long.
pub fn quit(app: &AppHandle) {
    if QUITTING.swap(true, Ordering::SeqCst) {
        return;
    }
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
    let app = app.clone();
    thread::spawn(move || {
        close_app();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !DONE.load(Ordering::SeqCst) && Instant::now() < deadline {
            thread::sleep(POLL);
        }
        app.exit(0);
    });
}

// --- the loop ---------------------------------------------------------------------------------

/// Whether this process may run, or another launcher already holds the folder.
///
/// Two of them would run `uv` over one venv and open `DuckDB` twice, which is a corrupted install
/// rather than a slow one. A second copy of this program is stopped by the single-instance
/// plugin before it gets here; this catches the older Python launcher.
fn claim() -> bool {
    let file = match OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(root().join(LOCK_FILE))
    {
        Ok(file) => file,
        Err(error) => {
            // A lock we cannot create is not a reason to refuse to start.
            note(&format!(
                "could not open the lock file, running unguarded: {error}"
            ));
            return true;
        }
    };
    match file.try_lock() {
        Ok(()) => {
            let _ = LOCK.set(file);
            true
        }
        Err(fs::TryLockError::WouldBlock) => false,
        Err(fs::TryLockError::Error(error)) => {
            note(&format!(
                "could not lock the folder, running unguarded: {error}"
            ));
            true
        }
    }
}

fn supervise(app: &AppHandle, uv: &Path) -> i32 {
    let mut restarting = false;
    loop {
        let mut slot = active();
        let running = slot_version(slot);
        let asked = requested();
        let mut wanted = asked
            .clone()
            .or_else(|| running.clone())
            .unwrap_or_else(|| BUILD_VERSION.to_owned());
        let upgrading = asked.is_none() && outgrown(running.as_deref());
        if upgrading {
            note(&format!(
                "this program ({BUILD_VERSION}) is newer than {running:?}; installing it"
            ));
            BUILD_VERSION.clone_into(&mut wanted);
        }

        if running.as_deref() != Some(wanted.as_str()) {
            show_splash(app);
            // The empty slot, so the one in use survives a failure untouched. With nothing
            // installed at all, the first slot is the only choice.
            let target = if running.is_some() { other(slot) } else { slot };
            status(
                app,
                &if running.is_some() {
                    format!("Updating to Alpha Harness {wanted}…")
                } else {
                    format!(
                        "Installing Alpha Harness {wanted}. The first start downloads Python and \
                         the app (about 330 MB), which takes a few minutes."
                    )
                },
            );
            let wheel = requested_wheel(&wanted);
            match install(app, uv, target, &wanted, wheel, requested_digest()) {
                Ok(()) => {
                    activate(target);
                    slot = target;
                    // Only once installed: a download that failed tried nothing, and this
                    // program is the way out of an install too broken to update itself.
                    if upgrading {
                        let _ = write_atomic(&root().join(OUTGROWN_FILE), BUILD_VERSION);
                    }
                }
                Err(_) if QUITTING.load(Ordering::SeqCst) => return 0,
                Err(error) => {
                    let Some(running) = running.as_deref() else {
                        fail(
                            app,
                            &format!("Could not install Alpha Harness {wanted}.\n{error}"),
                        );
                        return 1;
                    };
                    note(&format!("install failed, staying on {running}: {error}"));
                    note_failure(&wanted, &error);
                }
            }
        }
        // Cleared whether or not it was applied: left behind, a request that cannot install
        // would retry on every start for good.
        let _ = fs::remove_file(root().join(REQUEST_FILE));
        if QUITTING.load(Ordering::SeqCst) {
            return 0;
        }

        if restarting {
            await_port();
        }
        status(app, "Starting Alpha Harness…");
        let code = start(app, slot);
        restarting = true;
        if QUITTING.load(Ordering::SeqCst) {
            note("closed from the tray");
            return 0;
        }

        // A version that dies on the way up cannot report anything itself: no server, no page.
        // Judged by whether it ever served, not by a timer: a first start copies and migrates
        // the database before serving, which can take any length of time.
        if let Some(back) = slot_version(other(slot)).filter(|_| code != 0 && !proven(slot)) {
            let failed = slot_version(slot).unwrap_or_else(|| wanted.clone());
            note(&format!(
                "{failed} exited {code} before it ever served; reverting to {back}"
            ));
            activate(other(slot));
            note_failure(
                &failed,
                &format!(
                    "It closed before it could start (exit code {code}). Alpha Harness went back \
                     to {back}."
                ),
            );
            continue;
        }

        if requested().is_none() {
            if code != 0 {
                fail(
                    app,
                    &format!(
                        "Alpha Harness stopped unexpectedly (exit code {code}).\n\n{}",
                        recent(8)
                    ),
                );
            }
            return code;
        }
        note("update requested; restarting");
    }
}

fn run(app: &AppHandle) -> i32 {
    if let Err(error) = fs::create_dir_all(root()) {
        fail(
            app,
            &format!("Could not create {}: {error}", root().display()),
        );
        return 1;
    }
    note(&format!("Alpha Harness {BUILD_VERSION} (desktop)"));
    let deadline = Instant::now() + CLAIM_WAIT;
    let mut claimed = claim();
    if !claimed {
        status(app, "Waiting for the previous launcher to close…");
    }
    while !claimed && Instant::now() < deadline {
        thread::sleep(POLL);
        claimed = claim();
    }
    if !claimed {
        let message = if SLOTS.into_iter().any(|s| slot_version(s).is_some()) {
            "Alpha Harness is already running from its previous launcher, or an install it began is \
             still finishing. Quit it from its tray icon or the account menu, or wait a minute, \
             then open Alpha Harness again."
        } else {
            "Alpha Harness is still installing from its previous launcher. Wait for it to open, \
             quit it, then open Alpha Harness again."
        };
        fail(app, message);
        return 0;
    }
    if let Ok(exe) = std::env::current_exe()
        && let Err(error) =
            write_atomic(&root().join(DESKTOP_FILE), exe.to_string_lossy().as_bytes())
    {
        note(&format!("could not record where this program is: {error}"));
    }
    #[cfg(target_os = "linux")]
    forget_old_launcher();
    let Some(uv) = find_uv() else {
        fail(
            app,
            "This copy of Alpha Harness is missing its bundled uv. Download it again.",
        );
        return 1;
    };
    supervise(app, &uv)
}

/// Run the supervisor beside the window; its end is the app's.
pub fn spawn(app: AppHandle) {
    let started = thread::Builder::new()
        .name("supervisor".into())
        .spawn(move || {
            let code = run(&app);
            DONE.store(true, Ordering::SeqCst);
            app.exit(code);
        });
    if let Err(error) = started {
        note(&format!("could not start the supervisor: {error}"));
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    /// The app, as the tests below run it: this test binary again, writing to the pipes they read.
    /// Run by itself, it does nothing.
    #[test]
    fn child() {
        let Ok(task) = std::env::var("ALPHA_HARNESS_TEST_CHILD") else {
            return;
        };
        if let Some(lines) = task.strip_prefix("lines ") {
            // Turn about, so both streams end together and the last lines of each are kept.
            for number in 0..lines.parse().unwrap() {
                std::io::stdout()
                    .write_all(line('o', number).as_bytes())
                    .unwrap();
                std::io::stderr()
                    .write_all(line('e', number).as_bytes())
                    .unwrap();
            }
        } else {
            thread::sleep(Duration::from_mins(1));
        }
        // Gone before the test harness can report into the very pipes being read.
        std::process::exit(0);
    }

    fn line(tag: char, number: usize) -> String {
        format!("{tag} {number:08} {}\n", "x".repeat(number % 97))
    }

    fn run_child(task: &str) -> Child {
        Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "supervisor::tests::child", "--quiet"])
            .env("ALPHA_HARNESS_TEST_CHILD", task)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap()
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("alpha-harness-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A child writing as fast as it can to both pipes, this program's notes in between, and a
    /// log of `limit` bytes: the first and one past the last line kept of each, from the log and
    /// the one before it, having checked that from the first kept none is missing or cut.
    fn stress(name: &str, lines: usize, limit: u64) -> HashMap<String, (usize, usize)> {
        let dir = scratch(name);
        let path = dir.join(LOG_FILE);
        let log = Mutex::new(Log::new(path.clone(), limit));
        let mut child = run_child(&format!("lines {lines}"));
        let (stdout, stderr) = (child.stdout.take().unwrap(), child.stderr.take().unwrap());
        thread::scope(|scope| {
            scope.spawn(|| pump(stdout, &log));
            scope.spawn(|| pump(stderr, &log));
            for number in 0..lines / 10 {
                log.lock().unwrap().append(line('n', number).as_bytes());
            }
        });
        assert!(child.wait().unwrap().success());
        let mut kept = fs::read_to_string(dir.join(format!("{LOG_FILE}.1"))).unwrap();
        kept.push_str(&fs::read_to_string(&path).unwrap());
        fs::remove_dir_all(&dir).unwrap();

        let mut streams = HashMap::<String, (usize, usize)>::new();
        // The test harness's own, before the child's first line.
        let harness = |text: &&str| text.is_empty() || *text == "running 1 test";
        for text in kept.lines().filter(|text| !harness(text)) {
            let (tag, rest) = text.split_once(' ').expect(text);
            let number = rest.split_once(' ').and_then(|(n, _)| n.parse().ok());
            let number = number.expect(text);
            let tag_char = tag.chars().next().unwrap();
            assert_eq!(format!("{text}\n"), line(tag_char, number), "cut or mixed");
            let (_, next) = streams.entry(tag.to_owned()).or_insert((number, number));
            assert_eq!(number, *next, "lost before {text}");
            *next += 1;
        }
        streams
    }

    #[test]
    fn the_log_starts_afresh_losing_nothing() {
        // Small enough to start afresh once: every line is kept, in the log or the one before.
        let streams = stress("log-once", 2000, 160 * 1024);
        assert_eq!(streams["o"], (0, 2000));
        assert_eq!(streams["e"], (0, 2000));
        assert_eq!(streams["n"], (0, 200));
    }

    #[test]
    fn the_log_starts_afresh_many_times_cutting_no_line() {
        // Some 5 MB through a log of 256 kB: older lines go with each file before last, by design.
        let streams = stress("log-many", 40_000, 256 * 1024);
        assert_eq!(streams["o"].1, 40_000);
        assert_eq!(streams["e"].1, 40_000);
        assert!(
            streams["o"].0 > 0 && streams["e"].0 > 0,
            "started afresh only once"
        );
    }

    #[test]
    fn exit_is_seen_and_left_to_reap() {
        let mut child = run_child("sleep");
        let (seen, exited) = mpsc::channel();
        let exit = exit_of(&child);
        thread::spawn(move || {
            exit();
            seen.send(()).unwrap();
        });
        assert!(exited.recv_timeout(Duration::from_millis(300)).is_err());
        child.kill().unwrap();
        exited.recv_timeout(Duration::from_secs(10)).unwrap();
        // Exited but not reaped, Quit's kill still reaches it, and only `Child::wait` reaps it.
        child.kill().unwrap();
        assert!(!child.wait().unwrap().success());
    }

    #[test]
    fn served_names_the_port_or_means_8000() {
        let dir = scratch("served");
        let served = |text: &str| {
            fs::write(dir.join(SERVED_FILE), text).unwrap();
            served_at(&read_json(&dir.join(SERVED_FILE)))
        };
        let at = |version: &str, port| Some((version.to_owned(), port));
        assert_eq!(
            served(r#"{"version": "2026.11.10", "port": 54321}"#),
            at("2026.11.10", 54321)
        );
        // What every release before the port was named writes.
        assert_eq!(
            served(r#"{"version": "2026.10.21"}"#),
            at("2026.10.21", 8000)
        );
        assert_eq!(served(r#"{"version": "1", "port": 0}"#), at("1", 8000));
        assert_eq!(served(r#"{"version": "1", "port": 65536}"#), at("1", 8000));
        assert_eq!(served(r#"{"version": "1", "port": "8001"}"#), at("1", 8000));
        assert_eq!(served(r#"{"port": 8001}"#), None);
        // Read mid-write.
        assert_eq!(served(r#"{"version": "2026.11"#), None);
        assert_eq!(served(""), None);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_app_may_use_ipc_on_any_local_port() {
        let capability: Value =
            serde_json::from_str(include_str!("../capabilities/app.json")).unwrap();
        let patterns: Vec<tauri::utils::acl::RemoteUrlPattern> = capability["remote"]["urls"]
            .as_array()
            .unwrap()
            .iter()
            .map(|url| url.as_str().unwrap().parse().unwrap())
            .collect();
        let allowed = |url: &str| patterns.iter().any(|p| p.test(&url.parse().unwrap()));
        assert!(allowed("http://127.0.0.1:8000/"));
        assert!(allowed("http://127.0.0.1:54321/tasks?id=7#top"));
        assert!(!allowed("https://127.0.0.1:8000/"));
        assert!(!allowed("http://localhost:8000/"));
        assert!(!allowed("http://127.0.0.2:8000/"));
        assert!(!allowed("http://127.0.0.1.example.com:8000/"));
    }
}
