#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::sync::OnceLock;

use serde::Serialize;
use tauri::{Emitter, Manager};
use tauri_plugin_deep_link::DeepLinkExt;

// 收集器后端地址（与 Android 的 COLLECTOR_BASE 对应）。
// 开发期本机；发布期应指向可访问地址。
const COLLECTOR_BASE: &str = "http://127.0.0.1:8732";

// 单例 HTTP client（用于把深链里的链接 POST 给收集器）
fn client() -> &'static reqwest::blocking::Client {
    static CLIENT: OnceLock<reqwest::blocking::Client> = OnceLock::new();
    CLIENT.get_or_init(|| reqwest::blocking::Client::new())
}

/// 把深链里的链接转给收集器后端完成收藏。
/// 支持 repocollector://collect?url=... 或 ?text=...（整段文本由服务端抽取）。
fn forward_to_collector(raw: &str) {
    // raw 形如 repocollector://collect?url=https://github.com/owner/repo
    let query = match raw.split_once('?') {
        Some((_, q)) => q,
        None => raw, // 没有 ?，整段当作 url
    };

    let mut params: HashMap<String, String> = HashMap::new();
    for pair in query.split('&') {
        if let Some((k, v)) = pair.split_once('=') {
            params.insert(k.to_string(), v.to_string());
        }
    }
    let url = params.get("url").cloned().unwrap_or_default();
    let text = params.get("text").cloned().unwrap_or_default();

    // 服务端 /collect 的 POST 逻辑：给了 url 直接收；只给 text 时从文本抽链接。
    // 这里按同一约定组包：有 url 走 url，否则把整段 text 作为 text 字段发出去。
    #[derive(Serialize)]
    struct Payload<'a> {
        url: &'a str,
        text: &'a str,
    }
    let payload = Payload {
        url: &url,
        text: &text,
    };
    let _ = client()
        .post(format!("{}/collect", COLLECTOR_BASE))
        .json(&payload)
        .send();
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_deep_link::init())
        .setup(|app| {
            // 注册本 App 能处理的协议 scheme
            #[cfg(desktop)]
            {
                use tauri_plugin_deep_link::DeepLinkExt;
                let _ = app.deep_link().register("repocollector");
            }

            // 处理深链：Tauri v2 在「冷启动带链接进入」和「运行中再次被唤起」时都会触发
            let handle = app.handle().clone();
            app.deep_link().on_open_url(move |event| {
                for url in event.urls() {
                    let raw = url.to_string();
                    forward_to_collector(&raw);
                    // 顺手在悬浮球窗口弹个提示（若窗口在）
                    if let Some(win) = handle.get_webview_window("main") {
                        let _ = win.emit("deep-link-received", raw);
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
