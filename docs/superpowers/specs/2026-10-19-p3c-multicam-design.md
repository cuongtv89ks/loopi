# Loopi P3c — nhiều camera (thiết kế)

- **Ngày:** 2026-10-19
- **Trạng thái:** chờ chủ dự án duyệt
- **Spec gốc (bắt buộc):** `docs/superpowers/specs/2026-10-14-loopi-design.md`. Tài liệu này thay cho các quyết định tạm ngày 2026-10-18 trong `2026-10-18-p3c-multicam-decisions.md` ở những điểm nó sửa (cầu nối, lưu trữ). Nếu có chỗ mâu thuẫn, spec gốc thắng, trừ các quyết định mới D30–D35 dưới đây; các plan P3c sửa spec gốc cho khớp.
- **Mục tiêu:** ngoài Pi Zero W, thêm 3 camera ở các góc khác trong nhà: 1 camera IP Imou và 2 iPhone cũ. Mọi camera hiện trên timeline, tìm kiếm, recap và vlog như Pi.

## 1. Quyết định của chủ dự án

| # | Chủ đề | Quyết định |
|---|---|---|
| D30 | Cầu nối camera mạng | Một dịch vụ `cameras` trên PC chạy **một agent cho mỗi camera mạng**: cùng agent Rust như Pi (build cho x86_64), thêm nguồn ghi `stream` dùng ffmpeg kéo RTSP. Mỗi camera là một thiết bị riêng (ghép cặp, hàng đợi, heartbeat, pause, bật/tắt âm thanh, giờ yên lặng), protocol v1 giữ nguyên. Thêm và gỡ camera từ trang Thiết bị. Không dùng MediaMTX. Thay quyết định 2026-10-18 "mỗi camera một container + MediaMTX" (2026-10-19) |
| D31 | Báo đang ghi cho camera mạng | Camera mạng không có LED do Loopi điều khiển: dấu hiệu là tín hiệu sẵn có của thiết bị (đèn Imou, màn hình app iPhone) cộng với web — trang Thiết bị ghi rõ camera nào đang ghi, đầu mọi trang có dòng "Đang ghi: N camera". Pause trên web dừng đúng camera đó ở heartbeat kế tiếp (≤ 15 s). Bất biến "không có chuyện đang ghi mà LED tắt" (§4, §7.4) giữ nguyên cho Pi; với camera mạng, agent ghi khi không có LED phần cứng (`led.kind = "none"`), và chỉ được phép như vậy khi nguồn ghi là `stream` |
| D32 | Lưu trữ | Ổ HDD 1 TB hỏng ngày 2026-10-18 (rớt khỏi bus khi ghi liên tục), media ở lại SSD. **Một bản mỗi đoạn video**: MP4 (remux, không mã hoá lại) là bản giữ; H.264 thô bị xoá khi đã có MP4 và phân tích xong. Pi giữ 1080p; camera mạng ghi **720p, 2 fps, khoảng 400 kbps**. **Retention bật, giữ 7 ngày** video và audio thô (D20 đổi từ "mặc định tắt" thành bật). Keyframe, transcript, caption, chương, recap, vlog giữ vĩnh viễn |
| D33 | Chép lời khi nhiều mic cùng nghe | Phát hiện tiếng nói (VAD) chạy trên mọi mic. Audio cắt theo phút đồng hồ nên các đoạn cùng phút khớp nhau giữa các camera. Trong một phút, các khoảng tiếng nói chồng thời gian được gom thành nhóm; **mỗi nhóm chỉ chép lời một lần, từ mic nghe rõ nhất** (tỉ số tiếng nói trên nền nhiễu của chính mic đó cao nhất). Câu chỉ một phòng nghe thấy vẫn được chép. Brain chờ tối đa 3 phút cho audio của phút đó từ mọi camera; camera đến trễ hơn được coi như không có tiếng nói |
| D34 | Hiển thị nhiều camera | Timeline "Hôm nay"/"Ngày" gộp mọi camera theo thời gian, mỗi mục có nhãn vị trí, có bộ lọc "Tất cả" hoặc một camera (lưu trong URL). Mọi thiết bị, kể cả Pi, có **tên vị trí** sửa được; nhãn này dùng ở timeline, tìm kiếm, chat, MCP, recap và vlog |
| D35 | Vlog nhiều camera | Một vlog mỗi ngày. Với mỗi 10 phút có hoạt động, phần tua nhanh lấy camera có nhiều moment nhất trong 10 phút đó; ảnh highlight có thể từ bất kỳ camera nào; mỗi cảnh có nhãn vị trí nhỏ |

Giữ nguyên từ 2026-10-18: iPhone dùng app phát RTSP có sẵn, cắm sạc, bật Guided Access; **cả 4 nguồn đều ghi âm** (chủ dự án đã biết rủi ro theo 통신비밀보호법, §11 spec gốc); khi có BLE presence (P4c), nó giới hạn ghi âm trên **mọi** camera.

Chủ dự án chốt thêm (2026-10-19): **ESP32-S3 chưa làm**, để backlog (ESP32-S3 không mã hoá H.264 bằng phần cứng; nếu sau này muốn, có hai đường: camera mạng phát MJPEG/RTSP qua dịch vụ `cameras`, hoặc firmware Loopi đầy đủ gửi ảnh JPEG — một dự án cỡ P1). Thứ tự: **P3c-1 làm ngay sau P4a**, trước P4b, để SSD không bao giờ đầy; P3c-2 và P3c-3 làm sau P4.

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
  - Video: ffmpeg kéo RTSP (TCP), ghi đoạn 60 s H.264 720p, 2 fps, tối đa khoảng 400 kbps, đặt tên như đoạn của `rpicam-vid`, nên phần hàng đợi và upload không đổi.
  - Âm thanh: ffmpeg xuất PCM 16 kHz mono qua pipe, làm một `AudioSource` mới; mọi thứ sau đó (Ogg Opus, đoạn theo phút) dùng lại. `audio_gain_db` mặc định 0 cho `stream`. Luồng không có audio thì báo như mic bị rút (`mic_missing`).
  - Camera mất kết nối: ffmpeg thoát, agent thử lại với thời gian chờ tăng dần, heartbeat báo `camera_failures` và `last_error`; sai mật khẩu ghi rõ trong `last_error` (không kèm URL có mật khẩu).
- `led.kind = "none"`: không có LED phần cứng; chỉ hợp lệ khi `capture.source = "stream"` (D31). Cấu hình sai thì agent từ chối chạy.
- Phiên bản agent và protocol không đổi.

## 4. Thay đổi ở brain

### 4.1 Thiết bị và camera (P3c-1, P3c-2)
- `device.location` (tên vị trí, sửa được, mặc định là tên hiện tại).
- Bảng `network_camera`: id, `device_id` (sau khi ghép cặp), vị trí, URL (không có mật khẩu), username, trạng thái (đang bật/đã tắt). **Mật khẩu không nằm trong database**, chỉ trong `loopi.toml` của camera (quyền 600). Trang không bao giờ hiện lại mật khẩu.
- Thêm camera: form "Thêm camera mạng" (vị trí, URL RTSP, username, mật khẩu, bật/tắt âm thanh) → tạo token ghép cặp → viết `cameras/<id>/loopi.toml`. Gỡ camera: ghi file `disabled`, thu hồi thiết bị; dữ liệu đã ghi bị xoá theo retention hoặc "Quên".

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

CPU: PC là i5-1135G7 (4 nhân, 8 luồng), RAM 15 GB. Hiện một Pi tốn khoảng 9,4 CPU-giờ/ngày. Với D33, Whisper chỉ chạy cho tiếng nói không trùng; caption vẫn chạy cho keyframe của mọi camera (ngân sách 100–300 keyframe/ngày/camera). Nếu độ trễ vượt 15 phút, giảm ngân sách keyframe của camera mạng trước.

## 6. Quyền riêng tư và bảo mật
- D31 cho camera mạng; Pi giữ nguyên bất biến LED.
- Mật khẩu RTSP (ví dụ mã an toàn của Imou) chỉ nằm trong `loopi.toml` của camera, quyền 600, không vào git, không log, không hiện lại trên web, không vào heartbeat.
- "Quên khoảng thời gian" áp dụng cho mọi camera. Gỡ camera thu hồi thiết bị ngay.
- Ghi âm trên mọi camera theo quyết định của chủ dự án; P4c (BLE presence) sẽ giới hạn ghi âm trên mọi camera.

## 7. Kiểm thử
- Agent: nguồn `stream` với script ffmpeg giả (như `rpicam-vid` giả hiện có) và một file video mẫu phát lặp, không cần RTSP thật; nguồn audio từ pipe; `led.kind = "none"` bị từ chối với nguồn `csi`.
- `loopi-cameras`: chạy và dừng agent khi thư mục thay đổi, khởi động lại khi agent thoát, dừng khi có `disabled`.
- Brain: xoá bản thô sau khi có MP4 và phân tích xong (không xoá sớm hơn); retention bật mặc định; gom nhóm tiếng nói và chọn mic (khoảng tiếng nói giả); chờ tối đa 3 phút; timeline gộp và bộ lọc; form thêm camera (mật khẩu không bao giờ xuất hiện trong trang, log hay database); chọn camera cho vlog.

## 8. Nghiệm thu
1. Thêm Imou và 2 iPhone từ web; mỗi camera ghi trong vòng 2 phút và hiện trên timeline với nhãn vị trí.
2. Một câu nói giữa hai mic cho **một** transcript.
3. Pause một camera trên web: camera đó dừng trong vòng 15 s, dấu hiệu đang ghi đổi theo.
4. Chạy 24 giờ: dung lượng ≤ khoảng 22 GB/ngày; độ trễ trung bình tới caption/transcript ≤ 15 phút trong 08:00–22:00; không agent nào khởi động lại liên tục.
5. Vlog của một ngày có ≥ 2 camera hoạt động dùng ≥ 2 camera.
6. (P3c-1) Sau khi bật một bản mỗi đoạn và retention: dung lượng SSD ổn định, không còn video thô cũ hơn 7 ngày.

## 9. Chia plan
1. **P3c-1 — lưu trữ và hiển thị** (ngay sau P4a): một bản mỗi đoạn, retention bật 7 ngày, `device.location`, timeline gộp có nhãn và bộ lọc, nhãn vị trí trong tìm kiếm/chat/MCP.
2. **P3c-2 — camera mạng** (sau P4): nguồn `stream` và `led.kind = "none"` ở agent, `loopi-cameras`, container `cameras`, form thêm/gỡ camera, "Đang ghi: N camera".
3. **P3c-3 — xử lý nhiều camera** (sau P3c-2): chép lời từ mic rõ nhất, recap có vị trí, vlog nhiều camera.

## 10. Sửa spec gốc
- Thêm D30–D35 vào bảng quyết định và dòng "Sửa đổi".
- §4 nguyên tắc 3 và §7.4: ghi rõ ngoại lệ D31 cho camera mạng.
- §9.5: retention bật mặc định, 7/7 ngày (D32); một bản mỗi đoạn.
- §10.6: Thiết bị (thêm camera mạng, tên vị trí), Hôm nay (gộp, bộ lọc).
- §14: thêm dòng P3c với tiêu chí ở §8.
- §16 Backlog: ESP32-S3 (camera mạng hoặc thiết bị Loopi đầy đủ).

## 11. Ngoài phạm vi
ESP32 (backlog), xem trực tiếp (live view), camera ngoài LAN, phân biệt người nói, nhận diện người, camera tự đẩy luồng (RTMP/SRT) — nếu một app iPhone chỉ đẩy được luồng, thêm MediaMTX ở plan P3c-2.
