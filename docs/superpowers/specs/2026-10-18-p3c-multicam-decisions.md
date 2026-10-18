# Loopi P3c — nhiều camera (quyết định đã chốt, thiết kế chi tiết làm sau P4)

- **Ngày:** 2026-10-18
- **Trạng thái:** mới chốt các quyết định lớn. Thiết kế chi tiết, spec và plan sẽ làm **sau P4**, theo lựa chọn của chủ dự án.
- **Spec gốc (bắt buộc):** `docs/superpowers/specs/2026-10-14-loopi-design.md`. Các quyết định dưới đây sẽ được đánh số D chính thức khi viết thiết kế P3c.
- **Mục tiêu:** ngoài Pi Zero W, thêm 3 camera đặt ở các góc khác nhau trong nhà: 1 camera IP Imou và 2 iPhone.

## Quyết định của chủ dự án (2026-10-18)

| Chủ đề | Quyết định |
|---|---|
| Cầu nối | **Phương án A.** Mỗi camera chạy một container agent Rust riêng (build x86) với nguồn ghi `rtsp`, coi như một thiết bị riêng. Pairing mTLS, hàng đợi bền, heartbeat, pause và bật/tắt âm thanh đều dùng lại, protocol v1 giữ nguyên. Một container **MediaMTX** làm cổng camera: kéo luồng RTSP từ Imou và nhận hoặc kéo luồng từ iPhone. |
| iPhone | Dùng **app camera có sẵn** (phát RTSP hoặc đẩy RTMP/SRT trong LAN). Máy cắm sạc, bật Guided Access. Không viết app iOS riêng. |
| Âm thanh | **Cả 4 nguồn đều thu âm.** Chủ dự án chọn sau khi đã biết rủi ro theo 통신비밀보호법 (§11) và tải ASR tăng. Pause và công tắc audio riêng cho từng camera vẫn có. Thiết kế P3c phải tính tới việc gộp transcript trùng khi nhiều mic cùng nghe một câu. BLE presence của P4 nên giới hạn ghi âm cho mọi camera, không chỉ riêng Pi. |
| Báo đang ghi | Thay cho đèn LED của Pi: dùng **dấu hiệu có sẵn của từng thiết bị** (màn hình app iPhone, đèn camera Imou nếu có), cộng với **trang Thiết bị** hiển thị rõ camera nào đang ghi. Pause trên web dừng đúng camera đó ngay. Đây là thay đổi bất biến quyền riêng tư của spec gốc, cần ghi thành một D mới. |
| Vlog | **Một vlog mỗi ngày.** Trong mỗi khoảng thời gian, phần tua nhanh lấy camera có nhiều hoạt động nhất; ảnh highlight có thể đến từ bất kỳ camera nào. |
| Lưu trữ | **Media chuyển sang ổ HDD 1 TB** (Seagate ST1000LM035, format lại thành ext4, nhãn `loopi-media`; chủ dự án đã xác nhận xoá dữ liệu BitLocker cũ). Database ở lại SSD. Brain phải từ chối ghi nếu ổ chưa mount, để không lặng lẽ ghi vào SSD. |
| Thứ tự | **P4 trước, P3c sau.** |

## Số đo làm căn cứ (2026-10-18)

- Pi (1080p, 2 fps) mỗi ngày tốn khoảng 15 GB, vì mỗi đoạn được lưu hai lần: H.264 thô khoảng 7,5 GB và bản MP4 để phát khoảng 7,3 GB. Âm thanh khoảng 240 MB.
- 4 camera ở mức này là khoảng 60 GB/ngày.
- SSD còn trống 204 GB, media hiện chiếm 56 GB.
- Camera Imou: RTSP dùng mã an toàn in dưới camera làm mật khẩu. Mã này là bí mật, không bao giờ được commit.
