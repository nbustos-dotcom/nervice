// NERVICE desktop — a native Windows shell around the existing HUD (http://127.0.0.1:8765/v3).
//
// WHAT THIS DOES, in plain English (the owner doesn't read Rust):
//  1. On launch it checks whether the Nervice server answers on port 8765. If it already does,
//     it ATTACHES (never spawns a second one — no port fight). If not, it starts
//     `python run_api.py` silently (no console window).
//  2. The tray icon and a window come up IMMEDIATELY — even while the server is still warming.
//     The window starts on a dark splash and flips to the live HUD the moment /health is green.
//     If the server never answers, a native error dialog says WHY — never a silent blank window,
//     and the tray stays put so Quit / Restart Server are always reachable.
//  3. Tray menu: Open/Hide, Restart Server, Quit. Quit stops the python server ONLY if this
//     app was the one that started it; a server Nate started himself is left alone.
//  4. When this app spawns the server, an OS "job object" chains the python process to the
//     app's lifetime — even a crash or Task-Manager kill of the app takes the child down,
//     so orphan servers can never pile up. The child's console is captured to logs/server_console.log
//     and every launch decision is appended to logs/server.log.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const HEALTH_URL: &str = "http://127.0.0.1:8765/health";
const HUD_URL: &str = "http://127.0.0.1:8765/v3";
const PYTHON: &str = r"C:\Users\nateb\nervice\.venv\Scripts\python.exe";
const REPO: &str = r"C:\Users\nateb\nervice";
const CREATE_NO_WINDOW: u32 = 0x0800_0000; // spawn python without flashing a console window

/// Shared state: the python child IF WE spawned it (stays None when the server was already
/// running), plus the job object that ties the child's lifetime to this app's.
struct Srv {
    child: Mutex<Option<Child>>,
    job: Mutex<Option<win32job::Job>>,
}

/// One cheap HTTP poke. ANY response proves the server is alive — /health requires auth, so
/// an unauthenticated request gets 401, and that status code alone is the liveness signal.
/// Only a connection error (nothing listening) counts as "down".
fn server_up() -> bool {
    minreq::get(HEALTH_URL).with_timeout(2).send().is_ok()
}

/// Block until the server answers, or give up after `secs`.
fn wait_up(secs: u64) -> bool {
    let end = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < end {
        if server_up() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

/// Append one launch-decision line to logs/server.log (epoch-stamped). run_api.py writes the
/// human-readable boot line on an actual boot; this is the launcher's-eye view (attach / spawn /
/// fail). A log write must never block the app.
fn log_launch(msg: &str) {
    use std::io::Write;
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let _ = std::fs::create_dir_all(format!(r"{REPO}\logs"));
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(format!(r"{REPO}\logs\server.log"))
    {
        let _ = writeln!(f, "[tauri @{secs}] {msg}");
    }
}

/// Start `python run_api.py` headless and chain it to a kill-on-close job object so it can
/// never outlive this app. The child handle is stored for Restart Server / Quit. The child's
/// stdout/stderr go to logs/server_console.log (the file the failure dialog points at), and
/// NERVICE_LAUNCHER tells run_api.py to tag its boot line "spawned by tauri".
fn spawn_server(srv: &Srv) -> Result<(), String> {
    let _ = std::fs::create_dir_all(format!(r"{REPO}\logs"));
    let (out, err) = match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(format!(r"{REPO}\logs\server_console.log"))
    {
        Ok(f) => match f.try_clone() {
            Ok(f2) => (Stdio::from(f), Stdio::from(f2)),
            Err(_) => (Stdio::null(), Stdio::null()),
        },
        Err(_) => (Stdio::null(), Stdio::null()),
    };
    let child = Command::new(PYTHON)
        .arg("run_api.py")
        .current_dir(REPO)
        .env("NERVICE_LAUNCHER", "tauri")
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(out)
        .stderr(err)
        .spawn()
        .map_err(|e| format!("Could not start the server:\n{PYTHON}\n{e}"))?;
    let job = win32job::Job::create().map_err(|e| format!("job object create: {e}"))?;
    let mut info = job
        .query_extended_limit_info()
        .map_err(|e| format!("job object info: {e}"))?;
    info.limit_kill_on_job_close(); // "when the app's handle goes away, kill the child"
    job.set_extended_limit_info(&info)
        .map_err(|e| format!("job object limit: {e}"))?;
    job.assign_process(child.as_raw_handle() as isize)
        .map_err(|e| format!("job object assign: {e}"))?;
    *srv.child.lock().unwrap() = Some(child);
    *srv.job.lock().unwrap() = Some(job); // keep the handle alive for the app's lifetime
    Ok(())
}

fn error_box(msg: &str) {
    rfd::MessageDialog::new()
        .set_level(rfd::MessageLevel::Error)
        .set_title("NERVICE")
        .set_description(msg)
        .show();
}

/// Kill the python child ONLY if this app spawned it. External servers are never touched.
fn kill_our_child(srv: &Srv) {
    if let Some(mut c) = srv.child.lock().unwrap().take() {
        let _ = c.kill();
        let _ = c.wait();
    }
}

fn main() {
    tauri::Builder::default()
        // A second launch of the exe never opens a second window — it just surfaces this one.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .setup(|app| {
            app.manage(Srv { child: Mutex::new(None), job: Mutex::new(None) });

            // 1. Attach-or-spawn, WITHOUT blocking this thread. If the server is already up we leave
            //    it alone (Nate started it). Otherwise we spawn ours and remember to wait for it.
            //    The tray + window below come up immediately either way.
            let already_up = server_up();
            let mut warming = false; // true = we spawned; the splash must wait for /health
            if already_up {
                log_launch("attached to an already-running server (left as-is)");
            } else {
                match spawn_server(&app.state::<Srv>()) {
                    Ok(()) => {
                        log_launch("spawned the server child");
                        warming = true;
                    }
                    Err(e) => {
                        // Hard spawn failure (e.g. python missing): say why now, but keep the app
                        // and tray alive so Restart Server / Quit still work.
                        log_launch(&format!("spawn FAILED: {e}"));
                        error_box(&e);
                    }
                }
            }

            // 2. The one window. Straight to the HUD if the server already answers; otherwise a
            //    dark splash (dist/index.html) that the background thread flips to the HUD once
            //    /health is green. Never a blank page, never a "can't connect" error page.
            let initial = if already_up {
                WebviewUrl::External(HUD_URL.parse().unwrap())
            } else {
                WebviewUrl::App("index.html".into())
            };
            WebviewWindowBuilder::new(app, "main", initial)
                .title("NERVICE")
                .inner_size(1500.0, 900.0)
                .build()?;

            // 3. Tray icon + menu — built on EVERY launch, before any health wait, so it is present
            //    the entire time the server warms (and even if the spawn failed above).
            let open = MenuItemBuilder::with_id("open", "Open / Hide").build(app)?;
            let restart = MenuItemBuilder::with_id("restart", "Restart Server").build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
            let menu = MenuBuilder::new(app).items(&[&open, &restart, &quit]).build()?;
            TrayIconBuilder::with_id("tray")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("NERVICE")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| {
                    let srv = app.state::<Srv>();
                    match event.id().as_ref() {
                        "open" => {
                            if let Some(w) = app.get_webview_window("main") {
                                if w.is_visible().unwrap_or(false) {
                                    let _ = w.hide();
                                } else {
                                    let _ = w.show();
                                    let _ = w.set_focus();
                                }
                            }
                        }
                        "restart" => {
                            // Ours? kill + respawn. Still up after that = an EXTERNAL server
                            // we refuse to kill — instruct Nate instead of guessing.
                            kill_our_child(&srv);
                            if server_up() {
                                error_box(
                                    "The server was started outside this app, so I won't kill it.\n\
                                     Stop it yourself, then click Restart Server again.",
                                );
                                return;
                            }
                            log_launch("restart: respawning the server child");
                            let res = spawn_server(&srv).and_then(|_| {
                                if wait_up(30) {
                                    Ok(())
                                } else {
                                    Err("Server did not answer within 30s after restart.".into())
                                }
                            });
                            match res {
                                Ok(()) => {
                                    log_launch("restart: server healthy again");
                                    if let Some(w) = app.get_webview_window("main") {
                                        // navigate (not reload) so this works even if the window
                                        // was still sitting on the splash.
                                        let _ = w.navigate(HUD_URL.parse().unwrap());
                                    }
                                }
                                Err(e) => {
                                    log_launch(&format!("restart FAILED: {e}"));
                                    error_box(&e);
                                }
                            }
                        }
                        "quit" => {
                            kill_our_child(&srv); // ONLY ours; an external server keeps running
                            app.exit(0);
                        }
                        _ => {}
                    }
                })
                .build(app)?;

            // 4. Off-thread health wait: flip the splash to the live HUD once /health answers, or
            //    raise a real error dialog on timeout. Runs only when WE spawned the server; the
            //    tray (built above) is already interactive while this waits.
            if warming {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    let ok = wait_up(30);
                    let h = handle.clone();
                    let _ = handle.run_on_main_thread(move || {
                        if ok {
                            log_launch("server healthy; window -> HUD");
                            if let Some(w) = h.get_webview_window("main") {
                                let _ = w.navigate(HUD_URL.parse().unwrap());
                            }
                        } else {
                            log_launch("server did NOT answer /health within 30s");
                            error_box(
                                "The server started but did not answer /health within 30 seconds.\n\
                                 Check C:\\Users\\nateb\\nervice\\logs\\server_console.log",
                            );
                        }
                    });
                });
            }
            Ok(())
        })
        // Closing the window = hide to tray. The app (and any spawned server) lives on.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building NERVICE desktop")
        .run(|app, event| {
            // Belt-and-suspenders: every exit path takes our child down cleanly. (The job
            // object would catch a crash anyway; this just avoids a zombie Task Manager row.)
            if let RunEvent::Exit = event {
                kill_our_child(&app.state::<Srv>());
            }
        });
}
