// NERVICE desktop — a native Windows shell around the existing HUD (http://127.0.0.1:8765/v3).
//
// WHAT THIS DOES, in plain English (the owner doesn't read Rust):
//  1. On launch it checks whether the Nervice server answers on port 8765. If not, it starts
//     `python run_api.py` silently (no console window) and waits up to 30s for it to answer.
//     If that fails, a native error dialog says WHY — never a silent blank window.
//  2. It then opens one window pointed at the HUD. WebView2 keeps its own storage under
//     AppData, so the token login survives restarts. Closing the window only HIDES it —
//     the app keeps living in the system tray.
//  3. Tray menu: Open/Hide, Restart Server, Quit. Quit stops the python server ONLY if this
//     app was the one that started it; a server Nate started himself is left alone.
//  4. When this app spawns the server, an OS "job object" chains the python process to the
//     app's lifetime — even a crash or Task-Manager kill of the app takes the child down,
//     so orphan servers can never pile up.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Child, Command};
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

/// Start `python run_api.py` headless and chain it to a kill-on-close job object so it can
/// never outlive this app. The child handle is stored for Restart Server / Quit.
fn spawn_server(srv: &Srv) -> Result<(), String> {
    let child = Command::new(PYTHON)
        .arg("run_api.py")
        .current_dir(REPO)
        .creation_flags(CREATE_NO_WINDOW)
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

/// Launch path: do nothing if the server is already up (Nate started it himself);
/// otherwise spawn it and wait until it answers.
fn ensure_server(srv: &Srv) -> Result<(), String> {
    if server_up() {
        return Ok(());
    }
    spawn_server(srv)?;
    if wait_up(30) {
        Ok(())
    } else {
        Err("The server started but did not answer /health within 30 seconds.\n\
             Check C:\\Users\\nateb\\nervice\\logs\\server_console.log"
            .into())
    }
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

            // 1. Server first — the window is only created once /health answers, so the HUD
            //    is never a blank page. On failure: dialog with the real reason, then exit.
            if let Err(e) = ensure_server(&app.state::<Srv>()) {
                error_box(&e);
                std::process::exit(1);
            }

            // 2. The one window, pointed straight at the live HUD.
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(HUD_URL.parse().unwrap()))
                .title("NERVICE")
                .inner_size(1500.0, 900.0)
                .build()?;

            // 3. Tray icon + menu — the app's resting place when the window is closed.
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
                            let res = spawn_server(&srv).and_then(|_| {
                                if wait_up(30) { Ok(()) } else {
                                    Err("Server did not answer within 30s after restart.".into())
                                }
                            });
                            match res {
                                Ok(()) => {
                                    if let Some(w) = app.get_webview_window("main") {
                                        let _ = w.eval("location.reload()"); // fresh page on the fresh server
                                    }
                                }
                                Err(e) => error_box(&e),
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
