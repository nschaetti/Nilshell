#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use tauri::{Emitter, Manager, State};
use tauri_plugin_shell::{process::{CommandChild, CommandEvent}, ShellExt};

struct Engine { child: Mutex<Option<CommandChild>> }

#[tauri::command]
fn engine_send(request: serde_json::Value, engine: State<'_, Engine>) -> Result<(), String> {
    let mut child = engine.child.lock().map_err(|_| "Engine lock unavailable")?;
    let child = child.as_mut().ok_or("Engine is not running")?;
    let mut data = serde_json::to_vec(&request).map_err(|_| "Invalid request")?;
    data.push(b'\n');
    child.write(&data).map_err(|_| "Could not contact the local engine".into())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .manage(Engine { child: Mutex::new(None) })
        .invoke_handler(tauri::generate_handler![engine_send])
        .setup(|app| {
            let data_dir = app.path().app_data_dir()?;
            let (mut receiver, child) = app.shell().sidecar("nilshell-engine")?
                .env("NILSHELL_DATA_DIR", data_dir)
                .env("PYTHONIOENCODING", "utf-8")
                .env("PYTHONUTF8", "1")
                .spawn()?;
            *app.state::<Engine>().child.lock().unwrap() = Some(child);
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = receiver.recv().await {
                    match event {
                        CommandEvent::Stdout(bytes) => {
                            if let Ok(value) = serde_json::from_slice::<serde_json::Value>(&bytes) {
                                let _ = handle.emit("engine", value);
                            }
                        }
                        CommandEvent::Terminated(_) | CommandEvent::Error(_) => {
                            let _ = handle.emit("engine", serde_json::json!({"event":"engine.stopped"}));
                        }
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Could not start Nilshell")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Ok(mut child) = app.state::<Engine>().child.lock() {
                    if let Some(child) = child.take() { let _ = child.kill(); }
                }
            }
        });
}
