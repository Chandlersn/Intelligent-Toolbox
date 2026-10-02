// 无控制台窗口：这是常驻桌面悬浮球，debug/release 都不该弹黑框。
// （调试需要 stdout 时可临时改回 cfg_attr(not(debug_assertions), ...)。）
#![windows_subsystem = "windows"]

use std::collections::HashMap;
use std::sync::OnceLock;

use serde::Serialize;
use tauri::{Emitter, Manager};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_global_shortcut::GlobalShortcutExt;

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

/// 极简文件日志（秒级时间戳）：壳是无控制台程序，自退时无处可看原因，
/// 关键生命周期事件落盘到 %LOCALAPPDATA%/repo-collector-shell/shell.log。
fn slog(msg: &str) {
    use std::io::Write;
    let base = match std::env::var_os("LOCALAPPDATA") {
        Some(b) => b,
        None => return,
    };
    let p = std::path::Path::new(&base)
        .join("repo-collector-shell")
        .join("shell.log");
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(f, "[{ts}] {msg}");
    }
}

/// 用系统默认浏览器打开收藏主页面（不藏在壳窗口里）。
fn open_main_page_in_browser() {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = std::process::Command::new("cmd")
            .args(["/c", "start", "", &format!("{COLLECTOR_BASE}/")])
            .creation_flags(0x0800_0000) // CREATE_NO_WINDOW，不留黑框
            .spawn();
    }
}

/// 点球打开收藏主页面：自定义命令对远程页走 ACL 会拦（"not allowed. Plugin not found"），
/// 改走事件通道 —— JS emit("ball-tap")，core:default 已含 allow-emit。
#[tauri::command]
fn open_main_page() {
    open_main_page_in_browser();
}

/// 全局快捷键（Alt+Q）：任意界面读剪贴板里的链接 → 甩给收集器，
/// 结果经 collect-result 事件回给悬浮球弹 toast（球是唯一 UI 出口）。
fn collect_clipboard_via_hotkey(app: &tauri::AppHandle) {
    use tauri_plugin_clipboard_manager::ClipboardExt;

    fn emit_result(app: &tauri::AppHandle, ok: bool, dup: bool, error: &str) {
        if let Some(win) = app.get_webview_window("main") {
            let _ = win.emit(
                "collect-result",
                serde_json::json!({ "ok": ok, "dup": dup, "error": error }),
            );
        }
    }

    let text = match app.clipboard().read_text() {
        Ok(t) => t,
        Err(e) => {
            emit_result(app, false, false, &format!("无法读取剪贴板: {e}"));
            return;
        }
    };
    let url = text
        .split_whitespace()
        .find(|s| s.starts_with("http://") || s.starts_with("https://"))
        .unwrap_or("")
        .to_string();
    if url.is_empty() {
        emit_result(app, false, false, "剪贴板里没有链接");
        return;
    }
    let handle = app.clone();
    std::thread::spawn(move || {
        #[derive(Serialize)]
        struct P<'a> {
            url: &'a str,
            text: &'a str,
        }
        let resp = client()
            .post(format!("{COLLECTOR_BASE}/collect"))
            .json(&P { url: &url, text: "" })
            .send();
        let (ok, dup, err) = match resp {
            Ok(r) => r
                .json::<serde_json::Value>()
                .ok()
                .map(|v| {
                    (
                        v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false),
                        v.get("dup").and_then(|x| x.as_bool()).unwrap_or(false),
                        v.get("error")
                            .and_then(|x| x.as_str())
                            .unwrap_or("")
                            .to_string(),
                    )
                })
                .unwrap_or((false, false, "响应异常".into())),
            Err(e) => (false, false, format!("后端未响应: {e}")),
        };
        emit_result(&handle, ok, dup, &err);
    });
}

fn main() {
    // 固定 WebView2 用户数据目录（在 Tauri 初始化前设置）：
    // 1) 不依赖启动方式（双击 / 发送到 / 深链），档案位置恒定；
    // 2) 规避 Windows 11「管理员保护」下提权进程写默认 AppData 档案失败的问题
    //    （tauri-apps/tauri#13926 的官方缓解方案）。
    if std::env::var_os("WEBVIEW2_USER_DATA_FOLDER").is_none() {
        let base = std::env::var_os("LOCALAPPDATA")
            .unwrap_or_else(|| std::env::current_dir().unwrap_or_default().into_os_string());
        let dir = std::path::Path::new(&base).join("repo-collector-shell");
        let _ = std::fs::create_dir_all(&dir);
        std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &dir);
    }

    // 命令行直达：「收藏箱」支持被系统「发送到」快捷方式或脚本以 URL / 深链调用，
    // 启动时把第一个参数直接甩给收集器，再照常弹出常驻悬浮球。
    for arg in std::env::args().skip(1) {
        if !arg.trim().is_empty() {
            forward_to_collector(&arg);
        }
    }

    slog("shell starting");

    tauri::Builder::default()
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    // 只在按下沿触发一次（抬起沿忽略）
                    if event.state() == tauri_plugin_global_shortcut::ShortcutState::Pressed {
                        collect_clipboard_via_hotkey(app);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![open_main_page])
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
            slog("setup done");

            // 全局快捷键 Alt+Q：任意界面一键收剪贴板
            match app.global_shortcut().register("Alt+Q") {
                Ok(_) => slog("hotkey alt+q registered"),
                Err(e) => slog(&format!("hotkey register failed: {e}")),
            }

            // 悬浮球点按 → 打开收藏页（事件通道，见 open_main_page 注释）
            use tauri::Listener;
            app.listen("ball-tap", move |_| {
                slog("ball-tap -> open main page");
                open_main_page_in_browser();
            });

            // 悬浮球菜单「退出」：干净结束进程
            let quit_handle = app.handle().clone();
            app.listen("ball-quit", move |_| {
                slog("quit requested by ball menu");
                quit_handle.exit(0);
            });

            // 悬浮球「单击收藏」：clipboardManager 插件的 JS API 不在全局包里，
            // 统一走事件通道让 Rust 读剪贴板（与快捷键同一条已验证路径）
            let clip_handle = app.handle().clone();
            app.listen("ball-collect-clipboard", move |_| {
                collect_clipboard_via_hotkey(&clip_handle);
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app, event| match event {
            tauri::RunEvent::ExitRequested { code, .. } => {
                slog(&format!("exit requested, code={code:?}"));
            }
            tauri::RunEvent::Exit => {
                slog("exit");
            }
            tauri::RunEvent::WindowEvent { label, event, .. } => match event {
                tauri::WindowEvent::CloseRequested { .. } => {
                    slog(&format!("window close requested: {label}"));
                }
                tauri::WindowEvent::Destroyed => {
                    slog(&format!("window destroyed: {label}"));
                }
                _ => {}
            },
            _ => {}
        });
}
