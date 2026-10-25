#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{WebviewUrl, WebviewWindowBuilder};

fn main() {
    // No shell, filesystem, or general IPC commands are exposed to web content.
    // The same authenticated loopback service works in Tauri and a web browser.
    let mut arguments = std::env::args().skip(1);
    if arguments.next().as_deref() != Some("--url") {
        eprintln!("Launch with: wcfi audio emulator launch");
        std::process::exit(2);
    }
    let endpoint = arguments.next().expect("Missing local emulator URL");
    let url = url::Url::parse(&endpoint).expect("Invalid emulator URL");
    if url.scheme() != "http" || url.host_str() != Some("127.0.0.1")
        || url.port().is_none() || url.fragment().is_none()
        || !url.username().is_empty() || url.password().is_some()
    {
        eprintln!("Only a local authenticated emulator URL is accepted");
        std::process::exit(2);
    }
    tauri::Builder::default()
        .setup(move |app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.clone()))
                .title("WCFI Audio · Emulator")
                .inner_size(1440.0, 940.0)
                .min_inner_size(980.0, 680.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Could not start WCFI Audio");
}
