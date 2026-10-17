# ESP32-C3 Web Compiler

Web-based Arduino compiler for ESP32-C3 (Super Mini). Flask backend + single-file frontend.

## Features
- Multi-file project editor (`.ino` / `.h` / `.cpp` / `.c`), selectable main sketch
- Compile with cache or full clean rebuild (`--clean`), parallel jobs
- Download all build artifacts (bootloader, partitions, sketch `.bin`, merged)
- Arduino Library Manager: browse, install (name or `.zip`), uninstall
- Claymorphism UI, mobile friendly

## Requirements (server)
- `arduino-cli` with `esp32:esp32` core installed
- Python 3 + Flask

## Run
```bash
pip install flask
python app.py   # serves on 127.0.0.1:8765
```

## Deploy notes
- Run behind a reverse proxy / Cloudflare Tunnel for public HTTPS.
- FQBN used: `esp32:esp32:esp32c3:CDCOnBoot=cdc,PartitionScheme=huge_app`
- Optional: weaken `ieee80211_raw_frame_sanity_check` in `libnet80211.a` for raw WiFi frames.
