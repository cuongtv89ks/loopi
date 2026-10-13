# Loopi — Thiết kế hệ thống v1

- **Ngày:** 2026-10-14
- **Trạng thái:** Draft, chờ review
- **Sửa đổi:** 2026-10-14: đổi sang camera CSI đi kèm kit và USB mic qua OTG (D16)
- **Phạm vi tài liệu:** kiến trúc tổng thể và thiết kế cho cả 3 dự án con (`os/`, `agent/`, `brain/`). Mỗi phase sẽ có implementation plan riêng.

---

## 0. Tóm tắt

Loopi là một **"second brain" cá nhân, local-first**, lấy cảm hứng từ Looki L1. Phần thu là một **Raspberry Pi Zero W đặt cố định**, dùng camera CSI đi kèm kit và USB mic cắm qua cổng OTG, cắm điện trực tiếp. Thiết bị **ghi video tốc độ thấp và âm thanh liên tục**, rồi đẩy về một **Linux PC chỉ có CPU** (gọi là *brain*). Brain lo toàn bộ phần AI: caption khung hình, nhận dạng giọng nói Việt/Hàn/Anh, chia ngày thành chương, recap cuối ngày kèm việc cần làm, tìm kiếm và hỏi đáp có trích nguồn, vlog timelapse, và một MCP server để Claude hoặc agent khác truy vấn ký ức. Thiết bị chạy một **image Buildroot tối giản** (rootfs chỉ đọc, phân vùng A/B cho OTA) cùng một agent viết bằng Rust.

---

## 1. Bối cảnh và nghiên cứu

### 1.1 Looki L1 (sản phẩm tham chiếu)
- Giá $249, không bắt buộc subscription. Nặng 32g, IP67, pin 375mAh. Ảnh 4K, video 1080p30, FOV 109°, có EIS và HDR. 3 mic, loa, gyro 6 trục, bộ nhớ 32GB, BT 5.0, WiFi 2.4/5GHz.
- Không quay liên tục mà chụp clip ngắn theo chu kỳ: 5s mỗi phút chạy được ~9h, 11s mỗi 3 phút chạy được ~13h. Quay liên tục chỉ được 1–3h.
- Pipeline: thiết bị → app điện thoại → cloud (Gemini, ChatGPT, Claude) → recap chia chương, auto-vlog, truyện tranh, tìm kiếm ký ức, hỏi đáp, gợi ý chủ động.
- Công ty Looki Tech (Toronto), gọi vốn Series A $20M (07/2026), hơn 40K người dùng.
- Điểm yếu reviewer hay nhắc: ảnh thiếu sáng kém, góc đeo ở ngực, phụ thuộc cloud, **có thể tắt đèn báo ghi hình**, thông báo AI gây phiền và hay sai. Techspective tóm lại là *"captures more than it understands"*.
- **Insight rút ra:** giá trị nằm ở pipeline AI phía sau. Phần cứng chỉ là "con mắt".

### 1.2 Thị trường
- Meta mua Limitless và đã ngừng bán. Amazon mua Bee (07/2025). Humane AI Pin đã bị khai tử.
- Omi ($89) là open-source, có omiGlass dùng ESP32-S3.
- OpenGlass (ACL 2026) chứng minh mô hình **tách cảm biến khỏi xử lý**: ESP32 chụp, một máy ở gần chạy MLLM local, độ trễ trung vị khoảng 1 giây.
- **Khoảng trống:** chưa có thiết bị lifelog nào vừa local-first, vừa tự host, vừa mở.

### 1.3 Ràng buộc Pi Zero W
- BCM2835: ARMv6 một nhân 1GHz, RAM 512MB, WiFi chỉ 2.4GHz, BT 4.1/BLE. Không chạy được ML trên thiết bị.
- Có encoder phần cứng H.264 tới 1080p30 trên VideoCore IV.
- ARMv6 đang bị bỏ rơi dần: Debian đã bỏ, chỉ còn Raspbian 32-bit giữ. `rpi-image-gen` chủ yếu nhắm arm64. Buildroot vẫn có `raspberrypi0w_defconfig`.
- Các camera CSI chính hãng và loại tương thích (OV5647, IMX219, IMX708) đều chạy trên Zero W qua libcamera.
- Zero W chỉ có **một cổng micro-USB OTG** cho dữ liệu. WiFi đi qua SDIO nên không tranh băng thông với USB.
- Nếu sau này muốn nâng cấp, Pi Zero 2 W có cùng kích thước. Thiết kế giữ agent portable, chỉ cần đổi target triple và defconfig.

---

## 2. Mục tiêu và phạm vi

**Mục tiêu:** một hệ thống "second brain" dùng thật mỗi ngày. Dữ liệu nằm trên máy của người dùng, thiết bị cắm vào là chạy.

**Trong phạm vi v1 (MVP):**
- Timeline chia chương kèm recap cuối ngày, có trích việc cần làm hoặc đã hứa
- Tìm kiếm và hỏi đáp ký ức, câu trả lời có trích nguồn
- MCP server chỉ đọc
- Vlog timelapse hằng ngày
- Image Buildroot plug-and-play, có OTA

**Ngoài phạm vi v1** (đưa vào backlog, mục 16): truyện tranh, burst 30fps, tìm kiếm bằng hình, phân biệt người nói, tính năng Bluetooth ngoài BLE presence, chế độ đeo người và chạy pin.

---

## 3. Các quyết định đã chốt

| # | Chủ đề | Quyết định |
|---|---|---|
| D1 | Mục đích | Second brain cá nhân, dùng thật mỗi ngày |
| D2 | Board | Pi Zero W (ARMv6). Giữ có chủ đích, không đổi sang Zero 2 W |
| D3 | Brain | Linux PC chỉ có CPU. Cloud chỉ là tuỳ chọn, đi qua provider router của brain. Thiết bị **không** gọi cloud trực tiếp |
| D4 | Vị trí đặt | Cố định (phòng hoặc bàn làm việc), không đeo người |
| D5 | Nguồn điện | Cắm sạc thẳng. Không có pin, không tính điện năng |
| D6 | Dữ liệu thu | Video và âm thanh. Ảnh tĩnh và clip được brain cắt ra từ video |
| D7 | Cách thu | **Video liên tục tốc độ thấp** (H.264 1080p, 2fps), cắt đoạn 60s. Brain quyết định mọi thứ |
| D8 | Đồng bộ | Gửi liên tục khi brain liên lạc được. Hàng đợi trên SD để chịu lúc mất mạng |
| D9 | Ngôn ngữ hội thoại | Tiếng Việt, tiếng Hàn, tiếng Anh, có thể trộn lẫn |
| D10 | Kiến trúc thiết bị | Buildroot + agent viết bằng Rust |
| D11 | Lộ trình | Không có phase spike riêng. Làm image Buildroot ngay từ P1. Các ẩn số được đo trong tiêu chí nghiệm thu |
| D12 | Bluetooth | Image kèm BlueZ. Agent có event bus để mở rộng. BLE presence để giới hạn ghi âm làm ở P4 |
| D13 | Web UI | FastAPI + Jinja + HTMX, toàn bộ là một codebase Python |
| D14 | Embedding MVP | Chỉ embed text (caption, transcript, summary) |
| D15 | Vlog MVP | Kiểu timelapse, vì video gốc chỉ có 2fps |
| D16 | Phần cứng thu | Camera CSI đi kèm kit (nối bằng cáp CSI của Zero). Mic là USB mic cắm qua cổng OTG, nên cổng OTG luôn chạy chế độ host và không dùng được USB gadget mode |

---

## 4. Kiến trúc tổng thể

```
 THIẾT BỊ  (Pi Zero W · Buildroot · loopi-agent viết bằng Rust)
 ┌─────────────────────────────────────────────────┐
 │ Cam CSI (kit) · USB mic (OTG) · Nút · LED · BT   │
 │  rpicam-vid (H.264 HW) ─┐                        │
 │  ALSA → Opus ───────────┼─► hàng đợi /data ─► uploader
 │  nút/lịch/brain ─► event bus (Mark/Pause/Resume) │
 └───────────────┬─────────────────────────────────┘
                 │ HTTPS + mTLS (sync protocol v1), LAN
 BRAIN  (Linux PC chỉ CPU · Docker Compose)
 ┌───────────────▼─────────────────────────────────┐
 │ Caddy (mTLS) → FastAPI (ingest · web · MCP)      │
 │ Procrastinate workers:                           │
 │   video → chuyển cảnh/dHash → MiniCPM-V caption   │
 │   audio → Silero VAD → faster-whisper             │
 │   → moments → embedding → chương → recap → vlog   │
 │ Postgres 17 + pgvector + pg_trgm · Ollama         │
 │ Provider router (local ⇄ cloud, cloud chỉ nhận text)│
 └─────────────────────────────────────────────────┘
```

**Nguyên tắc:**
1. **Thiết bị chỉ làm việc tối thiểu, có chủ đích:** ghi, xếp hàng, gửi. Nâng cấp AI không phải động vào thiết bị.
2. **Local-first:** ảnh và âm thanh thô không bao giờ rời brain, trừ khi người dùng bật rõ ràng. Cloud chỉ nhận text.
3. **Bất biến riêng tư:** không có chuyện đang ghi mà LED lại tắt. Không có tuỳ chọn nào để tắt LED.
4. **Không âm thầm làm mất dữ liệu:** mọi lần drop hoặc lỗi đều được báo về brain và hiện trên UI.

---

## 5. Phần cứng

| Linh kiện | Lựa chọn | Ghi chú |
|---|---|---|
| Board | Raspberry Pi Zero W (bộ kit) | Đã có. Tắt HDMI và audio onboard |
| Nguồn | Sạc 5V micro-USB ≥2A, cắm vào cổng PWR | Đã có. Nuôi luôn USB mic qua cổng OTG |
| Camera | Camera CSI **đi kèm kit**, nối bằng cáp CSI của Zero | Đã có. Thường là OV5647 (5MP) hoặc Camera Module v2 (IMX219). Image dùng `camera_auto_detect`, không gắn chặt vào sensor nào; thay module CSI khác về sau không phải sửa code |
| Mic | **USB mic** (USB Audio Class) | Cắm vào cổng micro-USB **OTG** (cổng `USB`, không phải cổng `PWR`). Mic đầu USB-A thì thêm adapter OTG micro-B → USB-A. Không phải hàn, không cần overlay |
| Điều khiển | 1 nút bấm (GPIO) + 1 LED đỏ (GPIO) | |
| Lưu trữ | microSD 32–64GB loại High Endurance | Ghi liên tục |
| Vỏ | Case Pi Zero có lỗ camera + chân đế hoặc ngàm tripod 1/4" | Chỉnh được góc |
| Bluetooth | Có sẵn trên board (CYW43438) | Dùng chung chip với WiFi |

**Không dùng:** pin, mạch sạc/boost, chip đo pin, RTC, mic I2S. Thời gian lấy qua NTP; agent chỉ ghi khi đồng hồ đã đồng bộ.

**Hệ quả của phần cứng kit:**
- **Cổng OTG bị USB mic chiếm ở chế độ host**, nên không có đường cấu hình qua USB gadget (Ethernet-over-USB). Cấp WiFi vẫn đi theo `loopi.toml` ở P1 và QR, captive portal hoặc Improv BLE ở P4.
- **Chất lượng hình phụ thuộc camera kit.** Với ống kính tiêu chuẩn, góc nhìn ngang chỉ khoảng 54–62°, chụp thiếu sáng kém và không có autofocus. Nên đặt máy ở góc bao quát được khu vực chính và đủ sáng. Nếu sau này cần nâng cấp, chỉ cần thay module CSI (ví dụ Camera Module 3 Wide).

**Băng thông và dung lượng ước tính:** H.264 1080p@2fps khoảng 0,5–1,5Mbps, tuỳ cảnh thay đổi nhiều hay ít. Video thô khoảng 5–10GB/ngày, cộng audio Opus khoảng 260MB/ngày nếu ghi 24h ở 24kbps. Các con số này đo thật ở P1.

---

## 6. `loopi-os`: image Buildroot

### 6.1 Nền tảng
- Buildroot LTS mới nhất, xuất phát từ `raspberrypi0w_defconfig`, nằm trong cây **`BR2_EXTERNAL`** ở `os/`.
- Thư viện C là glibc. Kernel lấy từ cây `raspberrypi/linux` mà Buildroot dùng cho rpi0w.
- Init là **systemd**: hardware watchdog, giám sát service, `systemd-timesyncd` + `systemd-time-wait-sync`, D-Bus cho BlueZ. journald chỉ lưu trong RAM.

### 6.2 Các gói chính
`libcamera` (pipeline rpi/vc4), `rpicam-apps`, `alsa-lib` (kèm kernel module `snd-usb-audio`, nạp tự động qua udev của systemd), `libopus`, `wpa_supplicant`, `bluez5_utils`, `dbus`, `ca-certificates`, `loopi-agent` (một package dùng hạ tầng cargo của Buildroot).

### 6.3 Phân vùng thẻ SD (chốt từ P1)

| Phân vùng | FS | Kích thước | Nội dung |
|---|---|---|---|
| `boot` | FAT32 | 64MB | Firmware, kernel, DTB, `config.txt`, `cmdline.txt`, file `loopi.toml` để cấu hình lần đầu |
| `rootfs-A` | squashfs | 128MB | Rootfs chỉ đọc. `/var` và `/tmp` nằm trên tmpfs |
| `rootfs-B` | squashfs | 128MB | Để trống cho tới P4 (RAUC) |
| `data` | ext4 | Phần còn lại | `/data/queue`, `/data/config`, `/data/keys`, `/data/dev` (chỉ có ở dev) |

### 6.4 `config.txt` (định hướng)
- `camera_auto_detect=1` để tự nhận sensor của kit (OV5647/IMX219/IMX708). Chỉ khi module bên thứ ba không tự nhận mới khai báo `dtoverlay=<sensor>` cụ thể
- Cổng OTG chạy chế độ host bằng driver `dwc_otg` mặc định của kernel Raspberry Pi. **Không** bật `dtoverlay=dwc2` ở chế độ peripheral
- `dtparam=audio=off`, không cần framebuffer hay HDMI
- Tinh chỉnh CMA cho 512MB RAM
- Tắt LED ACT. LED trạng thái là LED ngoài do agent điều khiển

### 6.5 Hai biến thể image

| | `loopi_dev_defconfig` | `loopi_prod_defconfig` |
|---|---|---|
| SSH | dropbear (chỉ xác thực bằng key) | Không |
| Công cụ debug | strace, rpicam-hello, arecord, `loopi-selftest` | Chỉ có `loopi-selftest` |
| Agent | Ưu tiên `/data/dev/loopi-agent` nếu file tồn tại | Chỉ chạy bản nằm trong rootfs |
| Dùng ở | P1–P3 | Từ P4 |

### 6.6 Cấu hình lần đầu
- **P1:** người dùng sửa `boot/loopi.toml` trên máy tính. Lần boot đầu, agent chép file vào `/data/config/`, ghép cặp với brain, rồi **xoá mật khẩu WiFi và token khỏi `boot`**.
- **P4:** camera quét mã QR do web brain sinh ra (chứa WiFi, URL brain và token). Nếu không quét được thì thiết bị phát một access point có captive portal. Có thể thêm Improv BLE.

```toml
# boot/loopi.toml (ví dụ)
[wifi]
ssid = "HomeWiFi"
psk  = "********"

[brain]
url        = "https://brain.lan:8443"
pair_token = "ABCD-EFGH"

[capture]
video_width     = 1920
video_height    = 1080
video_fps       = 2
segment_seconds = 60
audio_enabled   = true
audio_device    = "auto"        # thiết bị ALSA; "auto" = thiết bị USB capture đầu tiên

[schedule]
quiet_hours = ["00:00-07:00"]   # không ghi trong khoảng này
```

### 6.7 OTA (P4)
RAUC với bundle được ký, chuyển slot A/B. Bản mới phải boot được, agent chạy lên và gửi được heartbeat thì mới tính là thành công; nếu không, hệ thống tự rollback. Bundle do brain host.

---

## 7. `loopi-agent` (Rust)

### 7.1 Runtime
- Target `arm-unknown-linux-gnueabihf`, ra một binary duy nhất.
- tokio `current_thread`. Mục tiêu RSS dưới 20MB, chưa tính `rpicam-vid`.
- Dùng `sd_notify` để báo watchdog (`WatchdogSec=30`).

### 7.2 Các module

| Module | Nhiệm vụ | Phụ thuộc (trait) |
|---|---|---|
| `recorder::video` | Spawn và giám sát `rpicam-vid -t 0 -n --codec h264 --inline --framerate <fps> --width/--height --segment <ms> -o /data/queue/incoming/v_%06d.h264`. Dùng inotify bắt đoạn vừa đóng, gắn `started_at`/`ended_at` theo wall clock, chuyển vào hàng đợi | `Camera`, `Clock` |
| `recorder::audio` | Mở USB mic qua ALSA theo `audio_device` (`auto` = thiết bị USB capture đầu tiên). Thu mono ở 16kHz nếu mic hỗ trợ, nếu không thì thu ở rate gốc (thường 48kHz) rồi resample trong agent. Encode Opus ~24kbps thành file Ogg 60s căn theo phút đồng hồ. Mic bị rút thì thử mở lại 10s một lần và báo `mic_missing`, video vẫn ghi bình thường | `AudioSource`, `Clock` |
| `queue` | Lưu file kèm sidecar JSON (`id` ULID, `kind`, `started_at`, `ended_at`, `sha256`, `size`). Ghi atomic (tmp → fsync → rename). Thẻ trên 85% thì xoá video chưa gửi cũ nhất cho tới khi xuống dưới 80%, mỗi lần xoá đều ghi sự kiện `dropped` để báo về brain; audio không bị xoá. Trên 95% thì dừng ghi, LED báo lỗi, gửi sự kiện `disk_full` | `Clock` |
| `uploader` | Gửi tuần tự, cũ nhất trước, `PUT /v1/items/{id}`. rustls + mTLS, **ưu tiên ChaCha20-Poly1305** vì ARMv6 không có AES phần cứng. Thời gian chờ khi lỗi tăng dần từ 1s tới 5 phút. Xoá file local sau khi nhận `200`/`201` | `Transport` |
| `control` | Event bus (`tokio::sync::broadcast`) với các lệnh `Mark`, `Pause`, `Resume`, `SetConfig`. Nguồn lệnh: nút bấm (`gpiocdev`), lệnh từ heartbeat, lịch `quiet_hours`. **Đây là điểm cắm cho các extension Bluetooth** | — |
| `led` | Sáng liên tục: đang ghi. Nháy chậm: tạm dừng hoặc trong giờ yên lặng. Nháy nhanh: lỗi (chưa NTP, thẻ đầy, camera lỗi, chưa ghép cặp). Tắt: agent không chạy | — |
| `health` | Heartbeat 15s một lần: phiên bản, uptime, nhiệt độ SoC, dung lượng thẻ, độ dài hàng đợi, RSSI, trạng thái NTP, trạng thái pause, lỗi gần nhất | `Transport` |
| `pairing` | Sinh khoá Ed25519 và CSR (`rcgen`), gọi `POST /v1/pair` kèm token, lưu cert đã ký, `device_id` và fingerprint CA của brain vào `/data/keys` | `Transport` |
| `config` | Đọc và validate `/data/config/loopi.toml`, áp dụng `SetConfig` từ brain. Cấu hình sai thì giữ cấu hình cũ và báo lỗi | — |

### 7.3 Nút bấm
- Bấm nhanh (dưới 1s): `Mark`, tạo item `marker` mang timestamp.
- Giữ 2s: bật/tắt `Pause`.

### 7.4 Bất biến riêng tư
- Recorder là tiến trình con có `PR_SET_PDEATHSIG(SIGTERM)`. Service dùng `KillMode=control-group`.
- LED "đang ghi" lấy trực tiếp từ trạng thái tiến trình recorder đang chạy, không lấy từ cấu hình.
- Không có key cấu hình nào để tắt LED.
- `audio_enabled=false` phải dừng hẳn recorder audio. Brain cũng hiển thị trạng thái này.

---

## 8. Sync protocol v1

Nguồn sự thật duy nhất là `docs/protocol/v1.openapi.yaml`, tạo ở P1. Cả agent lẫn brain đều có contract test chạy theo file này.

| Endpoint | Xác thực | Request | Response |
|---|---|---|---|
| `POST /v1/pair` | Token dùng một lần | `{token, csr_pem, hw_info{model, serial, agent_version}}` | `201 {device_id, cert_pem, ca_fingerprint}`; token sai hoặc hết hạn trả `401` |
| `PUT /v1/items/{ulid}` | mTLS | Header `X-Loopi-Kind: video\|audio\|marker`, `X-Loopi-Start`, `X-Loopi-End` (RFC 3339 UTC), `X-Loopi-Sha256`, `Content-Type`. Body là bytes thô; marker thì body là JSON | `201` đã nhận mới · `200` đã có (cùng sha) · `409` cùng id khác sha · `422` header sai · `507` brain hết dung lượng |
| `POST /v1/heartbeat` | mTLS | Trạng thái thiết bị (mục 7.2) và `acked_command_ids[]` | `200 {commands: [{id, type: pause\|resume\|set_config, payload}]}` |

- Phiên bản protocol nằm ở path. Agent không tương thích nhận `426`.
- Brain có CA riêng, ký CSR lúc ghép cặp. Caddy kiểm tra mTLS (`verify_if_given`, trust pool là CA của brain) rồi chuyển subject của cert vào header cho app. Riêng `/v1/pair` không cần cert. Thu hồi thiết bị bằng deny list trong DB.

---

## 9. `loopi-brain`

### 9.1 Các service (Docker Compose, ở `deploy/`)

| Service | Công nghệ | Vai trò |
|---|---|---|
| `proxy` | Caddy | TLS/mTLS, reverse proxy |
| `api` | FastAPI, Python 3.13, `uv` | Endpoint cho thiết bị, web UI (Jinja + HTMX, SSE), MCP (`/mcp`) |
| `worker` | Procrastinate (job queue trên Postgres) | Các stage của pipeline, job định kỳ |
| `db` | Postgres 17 + pgvector + pg_trgm | Metadata, vector, tìm kiếm theo trigram |
| `models` | Ollama | VLM, LLM và model embedding |

Media lưu trên filesystem (`/srv/loopi/media/{raw,keyframes,vlog}/YYYY/MM/DD/`). DB chỉ lưu đường dẫn.

### 9.2 Pipeline

**Mỗi đoạn video:**
1. Ingest: kiểm sha256, lưu file thô, tạo bản ghi `item`, đẩy job vào hàng đợi.
2. Remux sang MP4 (`ffmpeg -c copy`).
3. Giải mã khung hình ở độ phân giải thấp, chấm điểm chuyển cảnh, tính dHash so với keyframe gần nhất của thiết bị.
4. Chỉ những khung hình **thực sự mới** mới được lưu thành keyframe JPEG (độ phân giải đầy đủ) và đưa vào caption.
5. MiniCPM-V 4.6 trả JSON `{scene, activity, people_count, objects[], text_seen, notable: bool}`. Caption viết bằng tiếng Anh.

**Mỗi đoạn audio:**
1. Silero VAD tìm các đoạn có tiếng nói.
2. faster-whisper large-v3-turbo (int8, CPU) tự nhận ngôn ngữ cho từng đoạn và trả transcript kèm timestamp.
3. Xoá audio thô sau khi có transcript (mặc định; chỉnh được).

**Hợp nhất:** mỗi caption hoặc đoạn transcript thành một `moment` (khoảng thời gian, loại, text, liên kết tới keyframe/transcript). Sau đó embed text bằng Qwen3-Embedding-0.6B.

**Job định kỳ:**
- Mỗi giờ: chia chương cho phần dữ liệu mới.
- 23:30: recap cuối ngày, trích việc cần làm, dựng vlog.
- 03:00: áp dụng chính sách lưu trữ.

### 9.3 Provider router

```yaml
# deploy/providers.yaml (ví dụ)
privacy:
  allow_cloud_images: false
  allow_cloud_audio: false
tasks:
  caption:         { provider: ollama, model: minicpm-v4.6 }
  asr:             { provider: local,  model: faster-whisper/large-v3-turbo-int8 }
  embed:           { provider: ollama, model: qwen3-embedding:0.6b }
  chapter_summary: { provider: ollama, model: qwen3:4b }
  recap:           { provider: ollama, model: qwen3:8b }
  chat:            { provider: ollama, model: qwen3:8b }
  # Tuỳ chọn, chỉ khi đã đặt ANTHROPIC_API_KEY:
  # recap: { provider: ollama, model: qwen3:8b, fallback: { provider: anthropic, model: claude-haiku-5-5 } }
  # chat:  { provider: anthropic, model: claude-sonnet-5-5, fallback: { provider: ollama, model: qwen3:8b } }
```

- Mặc định mọi tác vụ chạy local. Cloud chỉ được dùng khi người dùng tự cấu hình provider và API key (D3).
- Router chặn việc gửi ảnh hay audio lên provider cloud trừ khi flag privacy tương ứng là `true`.
- Khi provider chính lỗi thì chuyển sang `fallback`.
- Khi xử lý bị tồn đọng thì **không** tự đẩy lên cloud. UI hiện độ trễ, và tồn đọng được xử lý dần, chủ yếu vào ban đêm.

### 9.4 Mô hình dữ liệu (các bảng chính)

| Bảng | Trường chính |
|---|---|
| `device` | id, name, cert_fingerprint, revoked, last_heartbeat (jsonb), paused, audio_enabled |
| `item` | id (ULID), device_id, kind, started_at, ended_at, sha256, path, status, error |
| `keyframe` | id, item_id, ts, path, dhash, caption (jsonb), model_version |
| `transcript_segment` | id, item_id, start_ts, end_ts, lang, text, model_version |
| `moment` | id, ts_start, ts_end, kind (visual\|speech\|marker), text, keyframe_id?, transcript_segment_id?, embedding vector, model_version |
| `chapter` | id, date, ts_start, ts_end, title, summary, model_version |
| `day_recap` | date, markdown, model_version, generated_at |
| `commitment` | id, date, text, source_moment_id, status (open\|done\|dismissed) |
| `vlog` | date, path, duration, script (jsonb) |
| `device_command` | id, device_id, type, payload, created_at, acked_at |

### 9.5 Chính sách lưu trữ (mặc định, chỉnh được)
- Video thô giữ 7 ngày. Keyframe, transcript, caption, chương, recap và vlog giữ vĩnh viễn.
- Audio thô xoá ngay sau khi có transcript.
- **"Quên khoảng thời gian"**: xoá hẳn item, keyframe, transcript, moment (kể cả embedding), đồng thời đánh dấu chương, recap và vlog của khoảng đó cần sinh lại.

### 9.6 Ước tính sức chịu (nghiệm thu ở P2)
- 100–300 keyframe mới mỗi ngày × 5–15s/keyframe, tổng dưới 1h CPU.
- 1–3h tiếng nói mỗi ngày, Whisper turbo int8 xử lý khoảng 0,5–3h.
- Cấu hình khuyến nghị: RAM 16GB (32GB thì thoải mái), đĩa trống khoảng 100GB.

---

## 10. Tính năng người dùng

### 10.1 Chia chương
Ranh giới chương dựa trên ba tín hiệu: khoảng trống (pause, không có thay đổi), độ lệch embedding giữa các caption liên tiếp, và chuyển chủ đề trong transcript. LLM đặt tiêu đề và viết tóm tắt 2–3 câu cho từng chương.

### 10.2 Recap cuối ngày và việc cần làm
LLM tổng hợp từ các chương, các marker và transcript thành markdown tiếng Việt gồm:
- diễn biến chính
- khoảnh khắc đáng nhớ
- **việc cần làm hoặc đã hứa** được nhắc tới trong hội thoại

Việc cần làm được lưu vào bảng `commitment` để theo dõi trạng thái.

### 10.3 Vlog timelapse
- Ứng viên highlight gồm: marker ±30s, keyframe có `notable=true`, và những đoạn hội thoại dày đặc.
- LLM viết kịch bản dưới dạng JSON. ffmpeg dựng: cả ngày tua nhanh 60–90s, chèn ảnh tĩnh highlight có hiệu ứng Ken Burns, phụ đề lấy từ transcript, kèm vài đoạn âm thanh gốc.

### 10.4 Tìm kiếm và hỏi đáp
1. LLM trích bộ lọc thời gian từ câu hỏi (structured output).
2. Hybrid retrieval: pgvector cosine kết hợp pg_trgm, lọc theo thời gian.
3. LLM trả lời, **bắt buộc trích nguồn** bằng `moment_id`. UI hiển thị thumbnail, giờ, và link tới đoạn tương ứng.
4. Không có nguồn nào liên quan thì nói "không tìm thấy", không bịa.

### 10.5 MCP server (chỉ đọc)
- FastMCP, Streamable HTTP tại `/mcp`, xác thực bằng bearer token tạo trên trang Cài đặt.
- Các tool:
  - `search_memories(query: str, start?: datetime, end?: datetime, limit?: int = 10)`: trả về moment kèm trích dẫn
  - `get_timeline(date: date)`: trả về chương và các mốc
  - `get_day_recap(date: date)`
  - `get_moment(moment_id: str)`: trả text kèm ảnh keyframe (MCP image content)
  - `list_commitments(status?: open|done|dismissed)`

### 10.6 Web UI (Jinja + HTMX, chat stream qua SSE)

| Trang | Nội dung |
|---|---|
| Hôm nay / Ngày | Timeline theo chương, keyframe, recap, việc cần làm |
| Tìm kiếm & Chat | Hỏi đáp có trích nguồn |
| Vlog | Xem và tải từng ngày |
| Thiết bị | Trạng thái lấy từ heartbeat, nút Pause/Resume, bật/tắt audio, mã QR ghép cặp, thu hồi thiết bị |
| Cài đặt | Provider, chính sách lưu trữ, "Quên khoảng thời gian", token MCP, job lỗi và nút xử lý lại |

---

## 11. Quyền riêng tư và pháp lý
- LED báo ghi hình luôn sáng khi đang ghi (bất biến ở mục 7.4). Có nút Pause trên thiết bị và trên web, có giờ yên lặng, và có công tắc audio.
- Cloud chỉ nhận text theo mặc định (mục 9.3). Có chức năng "Quên khoảng thời gian".
- **Ghi âm khi vắng nhà:** ở Hàn Quốc, 통신비밀보호법 cấm ghi âm cuộc trò chuyện giữa người khác mà mình không tham gia. Trước P4, rủi ro này được kiểm soát bằng lịch, nút Pause và công tắc audio. Ở P4, **chỉ ghi âm khi điện thoại của người dùng ở gần (BLE presence)**. Nên thông báo cho người trong nhà biết về thiết bị. *(Đây là lưu ý thiết kế, không phải tư vấn pháp lý.)*

---

## 12. Xử lý lỗi

| Tình huống | Cách xử lý |
|---|---|
| `rpicam-vid` crash | Supervisor khởi động lại, thời gian chờ tăng dần. Sau 5 lần thì LED báo lỗi và heartbeat gửi `last_error` |
| USB mic bị rút hoặc lỗi | Video vẫn ghi tiếp. Agent thử mở lại mic 10s một lần, heartbeat báo `mic_missing`, UI hiện cảnh báo |
| Chưa có NTP | Không ghi, LED nháy nhanh |
| Mất WiFi hoặc brain tắt | Dữ liệu dồn vào hàng đợi, áp dụng ngưỡng 85%/95% |
| Mất điện | Rootfs chỉ đọc, ghi atomic. Đoạn dở dang trong `incoming/` vẫn được đưa vào hàng đợi lúc boot |
| Agent bị treo | Watchdog systemd khởi động lại |
| Cấu hình sai | Giữ cấu hình cũ và báo lỗi |
| Upload trùng hoặc xung đột | `200` khi trùng; `409` thì ghi log và giữ file để điều tra |
| Job lỗi | Thử lại 3 lần, sau đó chuyển sang dead-letter hiện trên UI, có nút xử lý lại |
| Ollama hoặc cloud lỗi | Job tự thử lại; router chuyển sang fallback; ingest không bị ảnh hưởng |
| Đổi model | Mỗi kết quả lưu `model_version`; có lệnh xử lý lại theo ngày |
| Đĩa brain đầy | Ingest trả `507`, thiết bị giữ hàng đợi, UI cảnh báo, job lưu trữ dọn dẹp |

---

## 13. Kiểm thử
- **Coverage:** ≥80% cho logic của agent và brain (TDD theo quy ước của người dùng).
- **Contract:** `docs/protocol/v1.openapi.yaml` dùng chung. Agent test client theo schema, brain test server theo schema.
- **Agent:** unit test với các trait `Camera`, `AudioSource`, `Clock`, `Transport`, chạy được trên máy dev. Integration test dùng script `rpicam-vid` giả phát đoạn H.264 mẫu, gửi tới brain giả (wiremock). Trên thiết bị có `loopi-selftest`: in ra sensor camera nhận được, kiểm USB mic có mặt và mức âm thanh, NTP, một vòng upload và LED.
- **OS:** CI build cả hai defconfig (có ccache). Smoke test boot trên phần cứng thật mỗi khi phát hành.
- **Brain:** pytest cho từng stage với fixture, integration test với Postgres thật (testcontainers), và bài E2E "phát lại một ngày".
- **Đánh giá chất lượng AI (golden set, lưu ở `brain/eval/`):**
  - 30 keyframe có nhãn (caption chấm thủ công)
  - 20 đoạn audio vi/ko/en kèm transcript chuẩn (WER/CER)
  - 25 câu hỏi kèm moment đúng (recall@5)

---

## 14. Lộ trình và tiêu chí nghiệm thu

| Phase | Nội dung | Hoàn thành khi |
|---|---|---|
| **P1** | Image dev, agent v0 (video, audio, hàng đợi, uploader, nút, LED, heartbeat, pairing qua `loopi.toml`), protocol v1, brain ingest, timeline video thô, pause/resume từ web | Flash, sửa `loopi.toml`, boot lên là tự ghép cặp. Đoạn ghi hiện trên timeline trong vòng 2 phút. Chạy 24h không crash. Ngắt WiFi 1h, có mạng lại thì hàng đợi đẩy hết, không mất gì. Rút điện 10 lần vẫn boot bình thường. Kill agent thì việc ghi dừng và LED tắt. **Có số đo** CPU%, RAM, bitrate thực tế và dung lượng mỗi ngày |
| **P2** | Pipeline AI: lọc keyframe, caption, ASR, moment, embedding, chương, recap, việc cần làm, tìm kiếm và hỏi đáp, provider router | Độ trễ trung bình từ lúc ghi tới khi có caption/transcript **≤ 15 phút** trong khung 08:00–22:00. Recap tự sinh mỗi tối. Có baseline WER/CER cho vi/ko/en; WER trên 30% thì đổi model hoặc cấu hình. Recall@5 ≥ 70%. Có số đo thời gian CPU mỗi ngày |
| **P3** | MCP server, vlog timelapse | Gọi được 5 tool từ Claude Code. Vlog tự sinh mỗi tối |
| **P4** | Image prod, cấp WiFi bằng QR (kèm captive portal dự phòng), OTA qua RAUC, BLE presence để giới hạn ghi âm | Cấp WiFi không phải sửa file. OTA thành công, boot lỗi thì tự rollback. Image prod không có SSH. Điện thoại ra khỏi tầm thì audio dừng trong vòng 60s |

---

## 15. Rủi ro và giả định cần xác nhận

| # | Rủi ro / giả định | Xác nhận ở | Phương án dự phòng |
|---|---|---|---|
| R1 | `libcamera`/`rpicam-apps` trong Buildroot chạy được trên ARMv6 và nhận được camera của kit | Đầu P1 | Tự đóng gói `rpicam-apps` trong `BR2_EXTERNAL`, ghim phiên bản libcamera |
| R2 | CPU của Zero W kham nổi cùng lúc rpicam-vid, Opus và upload TLS | P1 | Giảm fps hoặc độ phân giải, giảm độ phức tạp Opus, giới hạn tốc độ upload |
| R3 | WiFi 2.4GHz đông và chung chip với BT | P1 | Giới hạn bitrate, upload theo lô |
| R4 | Chất lượng Whisper khi nói trộn vi/ko | P2 | PhoWhisper cho tiếng Việt, model fine-tune cho tiếng Hàn, ASR cloud nếu người dùng tự bật |
| R5 | VLM trên CPU quá chậm | P2 | Lọc trùng mạnh hơn, model nhỏ hơn, dồn xử lý vào ban đêm |
| R6 | Hệ sinh thái ARMv6 tiếp tục bị bỏ rơi | Liên tục | Buildroot tự build từ source; đường nâng cấp sang Zero 2 W cùng kích thước |
| R7 | Mốc giờ của từng đoạn chỉ chính xác ở mức giây (lấy theo wall clock lúc đóng đoạn) | P1 | Đủ cho lifelog. Cần chính xác hơn thì thêm `--save-pts` |
| R8 | Driver `dwc_otg` của Pi Zero sinh nhiều ngắt khi chạy USB audio, cộng dồn với tải CPU ở R2 | P1 | Giữ FIQ của `dwc_otg` bật (mặc định); ưu tiên mic hỗ trợ 16kHz để agent khỏi phải resample; đo tải ngắt và CPU trong bài chạy 24h |
| R9 | Camera kit hẹp góc và yếu sáng làm giảm chất lượng caption | P2 | Đặt máy ở vị trí bao quát và đủ sáng; thay module CSI khác (thiết kế không phụ thuộc sensor) |

---

## 16. Backlog (sau v1)
Truyện tranh (sinh ảnh qua cloud) · Burst 30fps khi bấm Mark · Tìm kiếm bằng hình (Qwen3-VL-Embedding/Reranker) · Phân biệt người nói · Remote BLE · Cảm biến BLE · Live view · Chế độ đeo người kèm pin

---

## 17. Cấu trúc repo

```
loopi/
├── agent/            # Rust (crate loopi-agent), target arm-unknown-linux-gnueabihf
├── brain/            # Python (uv): api/, worker/, pipeline/, web/, mcp/, eval/
├── os/               # BR2_EXTERNAL: configs/, board/loopi/, package/loopi-agent/, rootfs-overlay/
├── hw/               # BOM, sơ đồ nối dây, STL vỏ
├── deploy/           # docker-compose.yml, Caddyfile, providers.yaml.example
└── docs/
    ├── protocol/v1.openapi.yaml
    └── superpowers/specs/
```

---

## Nguồn tham khảo
- Looki L1: https://www.looki.ai/pages/looki-l1 · https://www.looki.ai/products/looki-l1
- Tracxn Looki: https://platform.tracxn.com/a/d/company/695c962efc8da554cfe61722/looki
- Review: https://techspective.net/2026/04/15/looki-l1-review-a-wearable-ai-camera-that-captures-more-than-it-understands/ · https://www.91mobiles.com/reviews/looki-l1-review/ · https://mightygadget.com/?p=72545 · https://www.galaxus.de/en/page/when-ai-films-and-analyses-every-minute-of-your-life-40990
- Thị trường: https://www.plaud.ai/blogs/articles/9-life-changing-ai-wearable-devices-in-2026 · http://techcrunch.com/2025/07/22/amazon-acquires-bee-the-ai-wearable-that-records-everything-you-say · https://github.com/BasedHardware/Omi · https://arxiv.org/abs/2607.03213
- Pi Zero W / OS: https://raspberrypi.stackexchange.com/questions/63519/power-consumption-of-pi-zero-w · https://forums.raspberrypi.com/viewtopic.php?t=391459 · https://github.com/raspberrypi/rpi-image-gen · https://github.com/buildroot/buildroot/tree/master/board/raspberrypi · https://wiki.alpinelinux.org/wiki/Raspberry_Pi · https://orionrobots.co.uk/2024/06/04/raspberry-pi-camera-compatibility.html · https://www.raspberrypi.com/documentation/computers/camera_software.html
- OTA / provisioning: https://rauc.io/ · https://toolbox.openhomefoundation.org/improv · https://github.com/balena-os/wifi-connect
- Model: https://ollama.com/openbmb/minicpm-v4.6%3A1b · https://www.alphaxiv.org/abs/2601.04720
