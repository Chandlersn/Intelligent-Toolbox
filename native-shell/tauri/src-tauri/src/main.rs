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

/// 最小 percent-decode（含 query 的 `+`→空格），兼容中文链接、含 `&`/`%2F` 的 URL。
fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'%' && i + 2 < bytes.len() {
            let h = (bytes[i + 1] as char).to_digit(16);
            let l = (bytes[i + 2] as char).to_digit(16);
            if let (Some(h), Some(l)) = (h, l) {
                out.push((h * 16 + l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(if b == b'+' { b' ' } else { b });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// 把深链 / 命令行给的链接或文本转给收集器后端完成收藏。
/// 支持：repocollector://collect?url=...&text=...、裸 `url=/text=`、裸 URL 三种形态。
/// 组装约定与服务端 /collect 一致：给了 url 直接收；只给 text 时由服务端从文本抽链接。
fn forward_to_collector(raw: &str) {
    let trimmed = raw.trim();
    // 只剥自有 scheme 前缀，避免误伤 https:// 等真实链接。
    let rest = trimmed.strip_prefix("repocollector://").unwrap_or(trimmed);

    let (url, text) = if rest.contains("url=") || rest.contains("text=") {
        // 有参数：取 '?' 之后为 query（无 '?' 时整段即 query）
        let query = rest.split_once('?').map(|(_, q)| q).unwrap_or(rest);
        let mut params: HashMap<String, String> = HashMap::new();
        for pair in query.split('&') {
            if let Some((k, v)) = pair.split_once('=') {
                params.insert(k.to_string(), percent_decode(v));
            }
        }
        (
            params.get("url").cloned().unwrap_or_default(),
            params.get("text").cloned().unwrap_or_default(),
        )
    } else {
        (rest.to_string(), String::new())
    };

    // 放后台线程发，避免阻塞 Tauri 事件循环与深链回调。
    std::thread::spawn(move || {
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
    });
}

fn main() {
    // 命令行直达：「收藏箱」支持被系统「发送到」快捷方式或脚本以 URL / 深链调用，
    // 启动时把第一个参数直接甩给收集器，再照常弹出常驻悬浮球。
    for arg in std::env::args().skip(1) {
        if !arg.trim().is_empty() {
            forward_to_collector(&arg);
        }
    }

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
