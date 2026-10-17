# ESP32-C3 Web Compiler

Web-based Arduino compiler for ESP32-C3 (tested on ESP32-C3 Super Mini).
Flask backend + single-file frontend — edit code in the browser, compile on the
server with `arduino-cli`, download the `.bin` files, flash with any tool
(esptool, Arduino IDE, etc).

## Features

- **Multi-file projects** — any `.ino` / `.h` / `.hpp` / `.cpp` / `.c` filenames,
  selectable main sketch (`.ino`)
- **Compile** (cached, parallel `--jobs`) and **Erase & Compile** (`--clean` full rebuild)
- **Download all artifacts** — bootloader, partitions, sketch `.bin`, merged `.bin`
- **Library manager** — list installed, search + install from Arduino Library
  Manager, install from `.zip`, uninstall
- **Dark IDE-style UI**, mobile friendly

---

## Full install guide (Ubuntu/Debian)

It's not just `arduino-cli` — you need the ESP32 core, libraries, an optional
WiFi patch, Python/Flask, and (optionally) a service + public tunnel.

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-pip curl git
```

### 2. Install arduino-cli

```bash
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sudo sh
# installs to /usr/local/bin/arduino-cli
arduino-cli version
```

### 3. Add the ESP32 board index and install the core

```bash
arduino-cli config init   # creates ~/.arduino15/arduino-cli.yaml (skip if exists)
arduino-cli config add board_manager.additional_urls \
  https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
```

Verify:

```bash
arduino-cli core list
# esp32:esp32 3.3.12 ...
```

### 4. Install libraries you need

```bash
arduino-cli lib install "U8g2"
# add more as needed, e.g.:
# arduino-cli lib install "PubSubClient"
```

You can also install/remove libraries later from the web UI (Library tab).

### 5. (Optional) Patch `libnet80211.a` for raw WiFi frames

The stock ESP-IDF `libnet80211.a` blocks raw 802.11 frame injection
(`ieee80211_raw_frame_sanity_check`). If your sketches need it (e.g. deauther
tools), weaken the symbol. **Use the matching objcopy for the chip
architecture** — RISC-V objcopy for C3/C5/C6, Xtensa objcopy for S2/S3.

For ESP32-C3 (RISC-V):

```bash
LIBDIR=~/.arduino15/packages/esp32/tools/esp32c3-libs/3.3.12/lib
OBJCOPY=~/.arduino15/packages/esp32/tools/esp-rv32/2601/bin/riscv32-esp-elf-objcopy

cp "$LIBDIR/libnet80211.a" "$LIBDIR/libnet80211.a.bak"
"$OBJCOPY" --weaken-symbol ieee80211_raw_frame_sanity_check "$LIBDIR/libnet80211.a"

# verify: symbol type must be "W" (weak), not "T"
~/.arduino15/packages/esp32/tools/esp-rv32/2601/bin/riscv32-esp-elf-nm \
  "$LIBDIR/libnet80211.a" | grep ieee80211_raw_frame_sanity_check
```

Repeat for `esp32c5-libs` / `esp32c6-libs` if you target those chips.
The web UI shows the patch status on the Compile tab.

> If you update the ESP32 core later, the patch is lost — re-apply it.

### 6. Install the web app

```bash
git clone https://github.com/nexops993/esp32-webcompiler.git ~/esp32-webcompiler
cd ~/esp32-webcompiler
pip install flask          # or: pip install --break-system-packages flask
python3 app.py             # serves on http://127.0.0.1:8765
```

The app only needs `app.py` + `index.html` — nothing else.

### 7. (Optional) Run as a systemd service

```bash
sudo tee /etc/systemd/system/esp32compiler.service > /dev/null <<'EOF'
[Unit]
Description=ESP32-C3 Web Compiler
After=network.target

[Service]
Type=simple
User=agentuser
WorkingDirectory=/home/agentuser/esp32-webcompiler
ExecStart=/usr/bin/python3 /home/agentuser/esp32-webcompiler/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now esp32compiler
```

### 8. (Optional) Expose publicly via Cloudflare Tunnel

```bash
# install cloudflared, then:
cloudflared tunnel login
cloudflared tunnel create esp32compiler-vm
cloudflared tunnel route dns esp32compiler-vm esp32.example.com
cloudflared tunnel --config /etc/cloudflared/esp32-config.yml run
```

Example `/etc/cloudflared/esp32-config.yml`:

```yaml
tunnel: <tunnel-uuid>
credentials-file: /etc/cloudflared/esp32-tunnel.json
ingress:
  - hostname: esp32.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

> The app has **no login** — anyone with the URL can compile. Put it behind
> Cloudflare Access, basic auth, or a firewall if you expose it.

---

## Configuration

Compile settings live at the top of `app.py`:

```python
FQBN = "esp32:esp32:esp32c3:CDCOnBoot=cdc,PartitionScheme=huge_app"
```

- `CDCOnBoot=cdc` — USB serial on the C3 Super Mini (prevents "bricked" boards
  with no serial port)
- `PartitionScheme=huge_app` — ~3 MB app partition for large sketches
  (WiFi + WebServer + displays). Change to `default` for small sketches.

## API summary

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/info` | board, FQBN, jobs, patch status |
| GET | `/api/files` | project file list |
| GET/POST/DELETE | `/api/file` | read / save / delete a file (`?name=`) |
| POST | `/api/file/new` | create file |
| POST | `/api/file/rename` | rename (`old`, `new`) |
| POST | `/api/file/main` | set main `.ino` |
| POST | `/api/upload` | upload files (multipart) |
| POST | `/api/compile` | start compile (`{"clean": true}` for `--clean`) |
| GET | `/api/compile/<job_id>` | poll status + log |
| GET | `/api/download` | main `.bin` |
| GET | `/api/download/<name>` | any build artifact `.bin` |
| GET | `/api/libraries` | installed libraries |
| GET | `/api/libraries/search?q=` | search Library Manager |
| POST | `/api/libraries/install` | install by name (JSON) or `.zip` (multipart) |
| POST | `/api/libraries/uninstall` | uninstall by name |
| GET | `/api/libtask/<id>` | poll install/uninstall task |
