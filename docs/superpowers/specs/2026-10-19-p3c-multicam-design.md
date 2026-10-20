# Loopi P3c — nhiều camera (thiết kế)

- **Ngày:** 2026-10-19
- **Trạng thái:** chủ dự án duyệt ngày 2026-10-19; plan của từng phase (P3c-1 … P3c-4) viết từ ngày 2026-10-19, P3c-1 trước (viết ngày 2026-10-19, sau khi P4 kết thúc theo D37: `plans/2026-10-19-p3c1-storage-display.md`)
- **Spec gốc (bắt buộc):** `docs/superpowers/specs/2026-10-14-loopi-design.md`. Tài liệu này thay cho các quyết định tạm ngày 2026-10-18 trong `2026-10-18-p3c-multicam-decisions.md` ở những điểm nó sửa (cầu nối, lưu trữ). Nếu có chỗ mâu thuẫn, spec gốc thắng, trừ các quyết định mới D30–D36 dưới đây; các plan P3c sửa spec gốc cho khớp.
- **Mục tiêu:** thêm được **camera bất kỳ trong LAN** vào brain — camera IP, điện thoại (iPhone, Android) chạy app phát luồng, bo ESP32 có camera — và **mọi camera đã thêm đều đi qua cùng luồng phân tích như Pi**: keyframe, caption, chép lời (khi camera có âm thanh), moment, chương, recap, tìm kiếm, hỏi đáp, MCP và vlog. Bộ camera đầu tiên của chủ dự án: 1 camera IP Imou và 2 iPhone cũ; sau đó có thể thêm điện thoại Android và ESP32.

## 1. Quyết định của chủ dự án

| # | Chủ đề | Quyết định |
|---|---|---|
| D30 | Cầu nối camera mạng | Một dịch vụ `cameras` trên PC chạy **một agent cho mỗi camera mạng**: cùng agent Rust như Pi (build cho x86_64), thêm nguồn ghi `stream` dùng ffmpeg kéo luồng **RTSP hoặc MJPEG qua HTTP**. Mỗi camera là một thiết bị riêng (ghép cặp, hàng đợi, heartbeat, pause, bật/tắt âm thanh, giờ yên lặng), protocol v1 giữ nguyên. Thêm và gỡ camera từ trang Thiết bị. Không dùng MediaMTX. Thay quyết định 2026-10-18 "mỗi camera một container + MediaMTX" (2026-10-19) |
| D31 | Báo đang ghi cho camera mạng | Camera mạng không có LED do Loopi điều khiển: dấu hiệu là tín hiệu sẵn có của thiết bị (đèn Imou, màn hình app iPhone) cộng với web — trang Thiết bị ghi rõ camera nào đang ghi, đầu mọi trang có dòng "Đang ghi: N camera". Pause trên web dừng đúng camera đó ở heartbeat kế tiếp (≤ 15 s). ESP32 chạy firmware có sẵn không có đèn gắn với việc ghi: dấu hiệu chỉ là web; firmware Loopi (P3c-4) bật đèn đúng lúc luồng đang được kéo. Bất biến "không có chuyện đang ghi mà LED tắt" (§4, §7.4) giữ nguyên cho Pi; với camera mạng, agent ghi khi không có LED phần cứng (`led.kind = "none"`), và chỉ được phép như vậy khi nguồn ghi là `stream` |
| D32 | Lưu trữ | Ổ HDD 1 TB hỏng ngày 2026-10-18 (rớt khỏi bus khi ghi liên tục), media ở lại SSD. **Một bản mỗi đoạn video**: MP4 (remux, không mã hoá lại) là bản giữ; H.264 thô bị xoá khi đã có MP4 và phân tích xong. Pi giữ 1080p; camera mạng ghi **720p, 2 fps, khoảng 400 kbps**. **Retention bật, giữ 7 ngày** video và audio thô (D20 đổi từ "mặc định tắt" thành bật). Keyframe, transcript, caption, chương, recap, vlog giữ vĩnh viễn |
| D33 | Chép lời khi nhiều mic cùng nghe | Phát hiện tiếng nói (VAD) chạy trên mọi mic. Audio cắt theo phút đồng hồ nên các đoạn cùng phút khớp nhau giữa các camera. Trong một phút, các khoảng tiếng nói chồng thời gian được gom thành nhóm; **mỗi nhóm chỉ chép lời một lần, từ mic nghe rõ nhất** (tỉ số tiếng nói trên nền nhiễu của chính mic đó cao nhất). Câu chỉ một phòng nghe thấy vẫn được chép. Brain chờ tối đa 3 phút cho audio của phút đó từ mọi camera; camera đến trễ hơn được coi như không có tiếng nói |
| D34 | Hiển thị nhiều camera | Timeline "Hôm nay"/"Ngày" gộp mọi camera theo thời gian, mỗi mục có nhãn vị trí, có bộ lọc "Tất cả" hoặc một camera (lưu trong URL). Mọi thiết bị, kể cả Pi, có **tên vị trí** sửa được; nhãn này dùng ở timeline, tìm kiếm, chat, MCP, recap và vlog |
| D36 | Thêm camera bất kỳ | Form "Thêm camera" có **loại camera**: *Camera IP (RTSP)*, *Điện thoại — iPhone/Android (app phát RTSP hoặc MJPEG)*, *ESP32 có camera (MJPEG qua HTTP)*, *Khác (URL RTSP/HTTP tuỳ ý)*. Mỗi loại điền sẵn mẫu URL và mặc định (có âm thanh hay không). Nút **"Thử kết nối"** kiểm tra luồng trước khi lưu và cho biết độ phân giải, số khung hình/giây, có âm thanh hay không. Mọi camera đã thêm được phân tích như nhau; camera nào không có luồng âm thanh thì chỉ được phân tích hình. Một camera có thể có **URL âm thanh riêng** (luồng WAV/PCM qua HTTP) bên cạnh URL hình — đó là cách ESP32 gửi tiếng từ mic của nó. Không giới hạn cứng số camera; trang Thiết bị cho biết dung lượng 7 ngày ước tính so với chỗ trống trên SSD và cảnh báo khi vượt (2026-10-19) |
| D35 | Vlog nhiều camera | Một vlog mỗi ngày. Với mỗi 10 phút có hoạt động, phần tua nhanh lấy camera có nhiều moment nhất trong 10 phút đó; ảnh highlight có thể từ bất kỳ camera nào; mỗi cảnh có nhãn vị trí nhỏ |

Giữ nguyên từ 2026-10-18: điện thoại dùng app phát luồng có sẵn (không viết app riêng), cắm sạc, bật Guided Access (iPhone) hoặc ghim màn hình (Android); **mọi camera có âm thanh đều ghi âm** (chủ dự án đã biết rủi ro theo 통신비밀보호법, §11 spec gốc); khi có BLE presence (P4c), nó giới hạn ghi âm trên **mọi** camera. BLE presence đã chuyển vào backlog (D37, 2026-10-19), nên hiện không có giới hạn này.

Chủ dự án chốt thêm (2026-10-19, sửa cùng ngày): **ESP32 có camera và mic được thêm như một camera mạng** (D36). ESP32-S3 không mã hoá H.264 bằng phần cứng, nên nó phát hình dạng MJPEG qua HTTP và PC chuyển sang H.264 như mọi camera mạng. Luồng MJPEG không mang âm thanh, nên tiếng từ mic của ESP32 đi qua một **luồng âm thanh riêng** (WAV 16 kHz mono qua HTTP). Firmware có sẵn (ví dụ `CameraWebServer` của Espressif, cổng 81, `/stream`) chỉ có hình: camera đó được phân tích hình. Để có tiếng và đèn báo đúng lúc ghi, Loopi có **firmware ESP32 riêng (P3c-4)**: MJPEG `/stream` và WAV `/audio` có mật khẩu, đèn sáng đúng lúc PC đang kéo luồng, cấu hình WiFi lần đầu. Thiết bị Loopi đầy đủ chạy trên ESP32 (mTLS, hàng đợi trên thẻ, gửi thẳng lên brain) vẫn ở backlog.

Thứ tự (chủ dự án chốt lại 2026-10-19): **plan của các phase P3c được viết từ ngày 2026-10-19, P3c-1 trước** (viết ngày 2026-10-19, sau khi P4 kết thúc theo D37).

## 2. Dịch vụ `cameras` (D30)

- Một container mới trong `deploy/docker-compose.yml`, image build từ crate `agent/` cho x86_64, kèm `ffmpeg`. Không cần quyền Docker, không chạy bằng root.
- Thư mục cấu hình dùng chung với brain: `<LOOPI_DATA_DIR>/cameras/<camera_id>/` (trong container là `/srv/loopi/cameras/…`). Mỗi camera có `loopi.toml` (brain viết, quyền 600) và `data/` (hàng đợi, khoá, `state.json` của agent đó).
- Bên trong là một bộ giám sát nhỏ (`loopi-cameras`, binary thứ hai trong crate agent): đọc thư mục, chạy `loopi-agent run --config <dir>/loopi.toml` cho mỗi camera, khởi động lại khi agent thoát (thời gian chờ tăng dần, như systemd làm cho Pi), dừng agent khi thư mục camera bị xoá hoặc có file `disabled`.
- Agent của camera mạng nói chuyện với brain đúng như Pi: qua Caddy, mTLS, cùng URL brain trong LAN; ghép cặp bằng token một lần mà brain ghi vào `loopi.toml`.
- Giờ: container dùng đồng hồ của PC (đã đồng bộ NTP); agent được cấu hình coi đồng hồ là đã đồng bộ.
- Hàng đợi của camera mạng nằm trên SSD của PC. Ngưỡng 85 %/95 % của agent áp dụng cho đĩa đó.

## 3. Thay đổi ở agent (P3c-2)

- `capture.source = "csi"` (mặc định, Pi) hoặc `"stream"`. Với `stream`:
  - `capture.stream_url` (không chứa mật khẩu), `capture.stream_username`, `capture.stream_password` (chỉ trong file, không bao giờ log, không vào heartbeat).
  - `capture.stream_url` có thể là `rtsp://` (camera IP, app điện thoại) hoặc `http(s)://` phát MJPEG (ESP32, nhiều app Android). RTSP kéo qua TCP; MJPEG không mang mốc thời gian nên dùng giờ lúc nhận khung.
  - Video: ffmpeg ghi đoạn 60 s H.264, 2 fps, tối đa khoảng 400 kbps, cạnh dài tối đa 1280 (720p; nguồn nhỏ hơn giữ nguyên kích thước), đặt tên như đoạn của `rpicam-vid`, nên phần hàng đợi và upload không đổi.
  - `capture.audio_url` (tuỳ chọn): luồng âm thanh riêng qua HTTP (WAV hoặc PCM 16 kHz mono), dùng khi URL hình không mang âm thanh (ESP32). Có `audio_url` thì âm thanh lấy từ đó, không lấy từ URL hình.
  - Âm thanh: khi luồng (hoặc `audio_url`) có âm thanh và camera bật ghi âm, ffmpeg xuất PCM 16 kHz mono qua pipe, làm một `AudioSource` mới; mọi thứ sau đó (Ogg Opus, đoạn theo phút) dùng lại. `audio_gain_db` mặc định 0 cho `stream`. Camera được thêm với "không có âm thanh" (mặc định cho ESP32 chạy firmware có sẵn) thì không mở nguồn audio; camera đáng lẽ có âm thanh mà luồng không có thì báo như mic bị rút (`mic_missing`).
  - Camera mất kết nối: ffmpeg thoát, agent thử lại với thời gian chờ tăng dần, heartbeat báo `camera_failures` và `last_error`; sai mật khẩu ghi rõ trong `last_error` (không kèm URL có mật khẩu).
- `led.kind = "none"`: không có LED phần cứng; chỉ hợp lệ khi `capture.source = "stream"` (D31). Cấu hình sai thì agent từ chối chạy.
- Phiên bản agent và protocol không đổi.

## 4. Thay đổi ở brain

### 4.1 Thiết bị và camera (P3c-1, P3c-2)
- `device.location` (tên vị trí, sửa được, mặc định là tên hiện tại).
- Bảng `network_camera`: id, `device_id` (sau khi ghép cặp), vị trí, URL (không có mật khẩu), username, trạng thái (đang bật/đã tắt). **Mật khẩu không nằm trong database**, chỉ trong `loopi.toml` của camera (quyền 600). Trang không bao giờ hiện lại mật khẩu.
- Thêm camera (D36): form "Thêm camera" (loại camera, vị trí, URL hình, URL âm thanh nếu có, username, mật khẩu, có âm thanh hay không). Loại camera điền sẵn mẫu URL: Imou `rtsp://<IP>:554/cam/realmonitor?channel=1&subtype=0`; ESP32 firmware Loopi `http://<IP>/stream` và `http://<IP>/audio`, ESP32 firmware có sẵn `http://<IP>:81/stream` (không âm thanh); app Android "IP Webcam" `http://<IP>:8080/video`; loại *Khác* để trống.
- **Thử kết nối:** brain (container api, đã có ffmpeg) chạy `ffprobe` tối đa 10 s với URL và thông tin đăng nhập vừa nhập, rồi hiện độ phân giải, fps, codec và có âm thanh hay không (kiểm cả URL âm thanh nếu có). Thông tin đăng nhập không bao giờ vào log hay thông báo lỗi.
- Lưu → tạo token ghép cặp → viết `cameras/<id>/loopi.toml`. Gỡ camera: ghi file `disabled`, thu hồi thiết bị; dữ liệu đã ghi bị xoá theo retention hoặc "Quên".
- Ước tính dung lượng: mỗi camera mạng thêm khoảng 4,4 GB/ngày (≈ 31 GB cho 7 ngày). Trang Thiết bị hiện tổng ước tính 7 ngày so với chỗ trống trên SSD; form thêm camera cảnh báo khi camera mới làm vượt 85 % chỗ trống.

### 4.2 Một bản mỗi đoạn và retention (P3c-1, D32)
- Sau khi đoạn video có MP4 và phân tích (keyframe) xong, brain xoá file H.264 thô, ghi lại thời điểm xoá. Xử lý lại theo ngày và vlog đọc MP4.
- Retention mặc định bật, 7/7 ngày; migration bật nó trên brain đang chạy. Lần chạy đầu xoá theo lô những gì đã quá 7 ngày (như job hiện có).
- Đo lại dung lượng mỗi ngày sau khi bật.

### 4.3 Chép lời nhiều mic (P3c-3, D33)
- Một bước mới giữa VAD và Whisper, theo từng phút đồng hồ:
  - đợi audio của phút đó từ mọi camera đang ghi âm, tối đa 3 phút;
  - gom các khoảng tiếng nói chồng thời gian giữa các mic thành nhóm;
  - mỗi nhóm: chọn mic nghe rõ nhất, tức là có **tỉ số tiếng nói trên nền nhiễu của chính mic đó** cao nhất (đo trên audio thô, trong các khoảng có tiếng nói), chỉ đoạn đó qua Whisper. So sánh mức âm thanh tuyệt đối không công bằng, vì mỗi nguồn có khuếch đại khác nhau (Pi +20 dB, D19) và sau chuẩn hoá mọi mic gần như bằng nhau;
  - các đoạn khác của nhóm được đánh dấu "đã chép ở mic khác" và trỏ tới transcript được chọn.
- Audio của mọi mic vẫn giữ 7 ngày để nghe lại.

### 4.4 Chương, recap, tìm kiếm, vlog (P3c-1, P3c-3)
- Moment mang `device_id`; văn bản đưa cho model có nhãn vị trí (ví dụ `[Phòng khách] …`), nên chương và recap nói được ở đâu.
- Tìm kiếm, nguồn của chat và kết quả MCP có nhãn vị trí.
- Vlog (D35): chia ngày thành khối 10 phút; mỗi khối có hoạt động chọn camera có nhiều moment nhất; ảnh highlight từ mọi camera; nhãn vị trí trên từng cảnh.

### 4.5 Web (P3c-1, P3c-2, D31, D34)
- Timeline gộp có nhãn và bộ lọc (P3c-1, có ích ngay cả khi chỉ có Pi vì đặt được tên vị trí).
- Trang Thiết bị: form thêm camera mạng, sửa tên vị trí, trạng thái từng camera (đang ghi hình/ghi âm, lần báo cuối), nút pause, bật/tắt âm thanh, gỡ.
- Đầu mọi trang: "Đang ghi: N camera" (liên kết tới Thiết bị).

## 5. Dung lượng và tải (ước tính, đo lại khi nghiệm thu)

| | Mỗi ngày |
|---|---|
| Pi 1080p, một bản (đo 2026-10-18: 8,6 GB H.264 thô) | ≈ 8,6 GB |
| Mỗi camera mạng 720p, 2 fps, ≈ 400 kbps | ≈ 4,3 GB |
| Audio Opus, 4 nguồn | ≈ 1 GB |
| **Tổng** | **≈ 22 GB/ngày, ≈ 150 GB cho 7 ngày** |

Mỗi camera mạng thêm sau đó (kể cả ESP32 có mic): ≈ 4,3 GB video + ≈ 0,1 GB audio mỗi ngày (≈ 31 GB cho 7 ngày). Với SSD hiện tại, khoảng 6–7 camera là mức nên dừng; con số thật đo ở nghiệm thu.

CPU: PC là i5-1135G7 (4 nhân, 8 luồng), RAM 15 GB. Hiện một Pi tốn khoảng 9,4 CPU-giờ/ngày. Với D33, Whisper chỉ chạy cho tiếng nói không trùng; caption vẫn chạy cho keyframe của mọi camera (ngân sách 100–300 keyframe/ngày/camera). Nếu độ trễ vượt 15 phút, giảm ngân sách keyframe của camera mạng trước.

## 6. Quyền riêng tư và bảo mật
- D31 cho camera mạng; Pi giữ nguyên bất biến LED.
- Mật khẩu RTSP (ví dụ mã an toàn của Imou) chỉ nằm trong `loopi.toml` của camera, quyền 600, không vào git, không log, không hiện lại trên web, không vào heartbeat.
- "Quên khoảng thời gian" áp dụng cho mọi camera. Gỡ camera thu hồi thiết bị ngay.
- Ghi âm trên mọi camera theo quyết định của chủ dự án. BLE presence (P4c) đã chuyển vào backlog (D37), nên ghi âm chỉ bị giới hạn bằng lịch, nút Pause và công tắc audio.

## 7. Kiểm thử
- Agent: nguồn `stream` với script ffmpeg giả (như `rpicam-vid` giả hiện có) và file mẫu phát lặp — một video H.264, một luồng MJPEG và một luồng WAV qua HTTP làm URL âm thanh riêng — không cần RTSP hay ESP32 thật; nguồn audio từ pipe; camera không có âm thanh không mở nguồn audio; `led.kind = "none"` bị từ chối với nguồn `csi`.
- `loopi-cameras`: chạy và dừng agent khi thư mục thay đổi, khởi động lại khi agent thoát, dừng khi có `disabled`.
- Brain: xoá bản thô sau khi có MP4 và phân tích xong (không xoá sớm hơn); retention bật mặc định; gom nhóm tiếng nói và chọn mic (khoảng tiếng nói giả); chờ tối đa 3 phút; timeline gộp và bộ lọc; form thêm camera theo từng loại (mẫu URL đúng; mật khẩu không bao giờ xuất hiện trong trang, log hay database); "Thử kết nối" với ffprobe giả (thành công, sai mật khẩu, quá 10 s); cảnh báo dung lượng; camera không có âm thanh vẫn có keyframe, caption, moment; chọn camera cho vlog.

## 8. Nghiệm thu
1. Thêm Imou và 2 iPhone từ web; mỗi camera ghi trong vòng 2 phút và hiện trên timeline với nhãn vị trí.
2. Một câu nói giữa hai mic cho **một** transcript.
3. Pause một camera trên web: camera đó dừng trong vòng 15 s, dấu hiệu đang ghi đổi theo.
4. Chạy 24 giờ: dung lượng ≤ khoảng 22 GB/ngày; độ trễ trung bình tới caption/transcript ≤ 15 phút trong 08:00–22:00; không agent nào khởi động lại liên tục.
5. Vlog của một ngày có ≥ 2 camera hoạt động dùng ≥ 2 camera.
6. (P3c-1) Sau khi bật một bản mỗi đoạn và retention: dung lượng SSD ổn định, không còn video thô cũ hơn 7 ngày.
7. (Khi có thiết bị) Một điện thoại Android và/hoặc một ESP32 thêm từ web bằng loại tương ứng: "Thử kết nối" cho đúng thông số, camera hiện trên timeline, có keyframe và caption, có mặt trong tìm kiếm và vlog; ESP32 chạy firmware Loopi (P3c-4) có transcript từ mic của nó, đèn sáng khi PC đang kéo luồng.

## 9. Chia plan
1. **P3c-1 — lưu trữ và hiển thị**: một bản mỗi đoạn, retention bật 7 ngày, `device.location`, timeline gộp có nhãn và bộ lọc, nhãn vị trí trong tìm kiếm/chat/MCP.
2. **P3c-2 — thêm camera bất kỳ**: nguồn `stream` (RTSP, MJPEG, URL âm thanh riêng) và `led.kind = "none"` ở agent, `loopi-cameras`, container `cameras`, form thêm/gỡ camera theo loại, "Thử kết nối", cảnh báo dung lượng, "Đang ghi: N camera".
3. **P3c-3 — xử lý nhiều camera** (sau P3c-2): chép lời từ mic rõ nhất, recap có vị trí, vlog nhiều camera.
4. **P3c-4 — firmware ESP32 của Loopi** (sau P3c-2, khi chủ dự án có bo ESP32 có camera và mic): MJPEG `/stream` và WAV `/audio` có mật khẩu, đèn sáng đúng lúc có máy đang kéo luồng (D31 cho ESP32), cấu hình WiFi lần đầu. Đây là cách duy nhất có tiếng từ mic ESP32.

Plan của các phase P3c được viết từ ngày 2026-10-19, P3c-1 trước, theo thứ tự trên.

## 10. Sửa spec gốc
- Thêm D30–D36 vào bảng quyết định và dòng "Sửa đổi".
- §4 nguyên tắc 3 và §7.4: ghi rõ ngoại lệ D31 cho camera mạng.
- §9.5: retention bật mặc định, 7/7 ngày (D32); một bản mỗi đoạn.
- §10.6: Thiết bị (thêm camera theo loại, thử kết nối, tên vị trí, ước tính dung lượng), Hôm nay (gộp, bộ lọc).
- §14: thêm dòng P3c với tiêu chí ở §8.
- §16 Backlog: thiết bị Loopi đầy đủ chạy trên ESP32 (firmware mTLS, hàng đợi trên thẻ, gửi ảnh JPEG lên brain).

## 11. Ngoài phạm vi
Firmware Loopi đầy đủ cho ESP32 (backlog), app điện thoại riêng của Loopi, xem trực tiếp (live view), camera ngoài LAN, phân biệt người nói, nhận diện người, camera tự đẩy luồng (RTMP/SRT) — nếu một app điện thoại chỉ đẩy được luồng, thêm MediaMTX ở plan P3c-2.
