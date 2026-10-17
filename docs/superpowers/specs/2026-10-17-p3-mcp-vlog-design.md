# Loopi P3 — MCP server và vlog timelapse (thiết kế)

- **Ngày:** 2026-10-17
- **Trạng thái:** chờ chủ dự án duyệt
- **Spec gốc (bắt buộc):** `docs/superpowers/specs/2026-10-14-loopi-design.md`, gồm §9.4 (bảng `vlog`), §9.5, §10.3, §10.5, §10.6, §14 (dòng P3), và các quyết định D15, D21, D23, D24, D25. Tài liệu này chỉ chốt những điểm spec gốc còn để ngỏ. Nếu có chỗ mâu thuẫn, spec gốc thắng.
- **Nghiệm thu P3 (§14):** gọi được 5 tool từ Claude Code; vlog tự sinh mỗi tối.

## 1. Quyết định của chủ dự án (2026-10-17)

| # | Điểm còn ngỏ | Quyết định |
|---|---|---|
| D24 | `get_moment` gửi ảnh keyframe cho client MCP chạy trên cloud | Mặc định tắt. Công tắc riêng trong Cài đặt; khi bật thì ảnh được thu nhỏ còn 320 px. Ảnh của mẫu đánh giá không bao giờ gửi |
| D25 | "Cả ngày tua nhanh" trong khi camera đứng yên phần lớn thời gian | Chỉ tua nhanh những giờ có hoạt động; 60–90 s, tối thiểu 20 s; ngày không có hoạt động thì không dựng vlog |
| — | Kiến trúc MCP | FastMCP gắn vào app FastAPI hiện có tại `/mcp`, không tách service riêng (để tiết kiệm RAM) |
| — | Cách dựng vlog | Lấy mẫu khung hình từ các đoạn MP4 của những giờ có hoạt động, kèm ảnh highlight có hiệu ứng Ken Burns. Ngày đã hết video thô thì dựng từ keyframe |

Các điểm nhỏ do người viết thiết kế tự chốt (ghi ở đây để chủ dự án sửa nếu không đồng ý):

- Token MCP có tên, chỉ hiện một lần, lưu dạng hash, có thể thu hồi.
- Phụ đề vlog giữ nguyên ngôn ngữ gốc của câu nói.
- Có thể dựng vlog cho những ngày cũ còn video thô.

## 2. Chia thành hai plan

Hai hệ thống con độc lập với nhau, nên mỗi cái có plan và nhánh riêng, làm theo thứ tự:

1. **P3a — MCP** (nhỏ, xong trước): nhánh `feat/p3a`, tách từ `feat/p3`.
2. **P3b — vlog** (lớn hơn): nhánh `feat/p3b`, tách từ nhánh có P3a.

Cả hai đi qua `feat/p3`, rồi tới `feat/phase2`, rồi deploy lên live như P2.

## 3. P3a — MCP server (§10.5)

### 3.1 Đường vào và xác thực

- **Endpoint:** `https://<brain>:8443/mcp`, giao thức MCP Streamable HTTP ở chế độ stateless.
  - Chạy cùng process `api` (uvicorn), không thêm container.
  - SDK là `mcp` chính thức (FastMCP), pin đúng bản ≥ 2.3 trong `uv.lock`. Plan phải đọc API của bản đã cài, không dựa vào trí nhớ.
- **Caddy:** khối `handle /mcp*` đặt trước khối web có basic auth.
  - Không có basic auth; brain tự kiểm bearer token.
  - `request_body max_size 64KB`.
  - Xoá header `X-Loopi-Client-Fingerprint` (`header_up -X-Loopi-Client-Fingerprint`), như khối web.
  - Caddy tự flush `text/event-stream`, không cần cấu hình thêm.
- **Token:**
  - Bảng `mcp_token`: `id` (ULID), `name`, `token_sha256` (duy nhất), `created_at`, `last_used_at`, `revoked_at`.
  - Token có dạng `loopi_` cộng 32 byte ngẫu nhiên base64url. Tạo trên Cài đặt, chỉ hiện đúng một lần.
  - Danh sách trên Cài đặt hiện tên, ngày tạo, lần dùng cuối và nút "Thu hồi".
  - So sánh bằng hash, thời gian so sánh không phụ thuộc nội dung (constant-time).
  - Thiếu token, sai token hoặc token đã thu hồi đều trả 401 với cùng một thông báo cố định.
  - Không bao giờ ghi token hay hash của nó vào log.
  - `last_used_at` cập nhật tối đa mỗi phút một lần.
- **Công tắc D24:** dòng `setting` có key `mcp`, giá trị `{"images": false}` (mặc định). Cài đặt ghi rõ:
  - "Chữ trả về qua MCP sẽ được client (ví dụ Claude Code) gửi lên model trên cloud."
  - "Ảnh chỉ gửi khi bật công tắc này."

### 3.2 Năm tool (chỉ đọc)

Quy ước chung:
- Giờ hiển thị theo `LOOPI_TZ`, định dạng ISO 8601 có offset. Ngày dạng `YYYY-MM-DD`, tính theo giờ địa phương.
- Chữ của moment và tóm tắt chương được cắt ở 500 ký tự, recap ở 8000.
- Kết quả là JSON có cấu trúc (structured content), kèm bản text.
- Mỗi moment đi kèm `moment_id` để client trích nguồn.

| Tool | Vào | Ra |
|---|---|---|
| `search_memories(query, start?, end?, limit=10)` | `query` 1–500 ký tự; `start`/`end` là datetime có offset, hoặc ngày; `limit` 1–50 | Các moment của hybrid search P2b (`search.search_moments`, cùng ngưỡng với trang Tìm kiếm): `moment_id`, `kind`, `ts_start`, `ts_end`, `text`, `lang`, `day`. Không có gì liên quan thì trả danh sách rỗng kèm `"not_found": true`; không gọi được model embedding thì vẫn tìm theo chữ và trả `"semantic": false` (như trang Tìm kiếm) |
| `get_timeline(date)` | ngày | Các chương (`id`, `ts_start`, `ts_end`, `title`, `summary`, `closed`), các marker trong ngày, và số moment theo loại |
| `get_day_recap(date)` | ngày | `markdown`, `generated_at`, `stale`; chưa có recap thì trả `"exists": false` |
| `get_moment(moment_id)` | ULID | `text`, `kind`, giờ, `lang`. Moment hình có thêm caption (`scene`, `activity`, `objects`, `people_count`). Khi D24 bật và keyframe không thuộc bộ đánh giá: thêm ảnh JPEG 320 px (MCP image content) |
| `list_commitments(status?)` | `open` \| `done` \| `dismissed`, bỏ trống là tất cả | `id`, `day`, `text`, `status`, `source_moment_id`, mới nhất trước, tối đa 200 |

**Khi có lỗi:**
- Ngày sai, id không tồn tại hoặc `status` lạ: tool trả lỗi (`isError`) với thông báo tiếng Anh cố định, không nhắc lại dữ liệu người dùng gửi.
- Dữ liệu đã bị "Quên" thì không còn nữa, tool trả như không có.

**Log:** chỉ ghi tên tool, thời gian chạy và số kết quả. Không bao giờ ghi query hay nội dung trả về.

### 3.3 Kiểm thử P3a

- Dùng client MCP thật của SDK (`streamable_http_client`) gọi vào app trong pytest: liệt kê đủ 5 tool, gọi từng tool.
- 401 cho từng trường hợp: thiếu token, sai token, đã thu hồi.
- Công tắc ảnh: tắt thì không có ảnh; bật thì có ảnh rộng 320 px; keyframe thuộc bộ đánh giá thì không có ảnh.
- Test log: query và nội dung không có trong log.
- Test Caddy (`test_deploy.py`): `/mcp` đi qua mà không có basic auth.

**Nghiệm thu P3a:** đăng ký server từ Claude Code trên máy brain và gọi đủ 5 tool. Kết quả ghi vào ledger, chỉ tên tool và OK. README hướng dẫn:
- `claude mcp add --transport http loopi https://localhost:8443/mcp --header "Authorization: Bearer <token>"`;
- đặt `NODE_EXTRA_CA_CERTS` trỏ tới CA của Loopi.

## 4. P3b — vlog timelapse (§10.3, D15, D25)

### 4.1 Lịch chạy và job

- **`plan_vlogs`** (mỗi 15 phút, giống planner của recap):
  - Hôm nay: khi recap 23:30 của hôm nay đã lưu mà chưa có vlog.
  - Ngày trước đó: khi đã có recap mà chưa có vlog. Mỗi lượt chỉ bắt kịp một ngày, ngày mới nhất trước.
  - Dựng lại đúng một lần nếu recap của ngày được viết lại sau vlog (`day_recap.generated_at > vlog.generated_at`).
  - Không bao giờ hẹn lại một ngày mà job của nó đang chờ, đang chạy hoặc đã lỗi.
- **`write_vlog_script(day)`:** khoá `llm`, priority −10, model `qwen3:8b` (D23; recap vừa nạp model này nên không tốn thêm RAM). Viết xong thì hẹn `render_vlog`.
- **`render_vlog(day)`:** khoá `vlog`, priority −10, `ffmpeg -threads 2`, timeout 20 phút. Không giữ khoá `llm`, nên không chặn việc chia chương.
- **"Dựng lại vlog"** (trang Ngày và trang Vlog, POST qua HTMX): đánh dấu `stale`; planner làm phần còn lại.

### 4.2 Ứng viên highlight

- Marker: ±30 s, lấy keyframe gần nhất trong khoảng đó.
- Keyframe có `caption.notable = true`.
- Quãng hội thoại dày: cửa sổ 5 phút có ≥ 6 moment lời nói. Moment lời nói đã qua các luật lọc hallucination đang bật của P2c. Lấy keyframe gần tâm cửa sổ.
- Ứng viên nào không có ảnh thì bỏ.
- **Chấm điểm:** marker 3, notable 2, hội thoại 1 cộng mật độ. Gộp các ứng viên cách nhau dưới 2 phút. Gửi tối đa 40 ứng viên cho LLM, mỗi ứng viên có mã ngắn (`H1` …), giờ, loại và chữ (≤ 200 ký tự). Prompt ≤ 9000 ký tự.

### 4.3 Kịch bản (JSON, structured output)

- **LLM trả về:**
  - `{"title": ≤ 80 ký tự, "highlights": [{"ref": "H3", "line": ≤ 120 ký tự, "audio": bool}]}`, gồm 6–10 highlight (ngày ít ứng viên thì ít hơn).
- **Brain xử lý lại:**
  - Bỏ ref lạ hoặc trùng.
  - Xếp theo thời gian.
  - Giữ tối đa 3 `audio: true`, và chỉ cho highlight còn audio thô.
  - Cắt chữ theo giới hạn.
- **Dự phòng** (model không chạy được, hoặc câu trả lời không dùng được):
  - Brain chọn 8 ứng viên điểm cao nhất.
  - Tiêu đề là "Ngày dd/mm".
  - Dòng mô tả lấy từ caption hoặc chữ của ứng viên.
  - Audio cho 2 ứng viên hội thoại đầu.
  - `model_version` là `rule`.
- Nhờ phần dự phòng, tối nào cũng có vlog.

### 4.4 Dựng video (ffmpeg, worker)

- **Đoạn có hoạt động:** đoạn video của ngày, đã phân tích xong, còn MP4, có moment trong vòng 5 phút quanh nó.
- **Thời lượng:**
  - Tua nhanh `T = clamp(0.25 s × số đoạn, 10 s, 60 s)`.
  - Ảnh highlight 3 s mỗi cái.
  - Tổng dưới 20 s thì kéo dài ảnh lên tối đa 5 s mỗi cái.
  - Không có đoạn nào thì không dựng; vlog ghi `empty` và trang hiện "Không có hoạt động".
- **Tua nhanh:**
  - Một lệnh ffmpeg đọc các MP4 bằng concat demuxer.
  - `select` lấy đều `T × 24` khung.
  - `scale=1280:-2`, `fps=24`.
  - Mã hoá libx264 `-preset veryfast -crf 26`.
- **Ảnh highlight:** mỗi ảnh là JPEG keyframe chạy qua `zoompan` (Ken Burns, phóng nhẹ 1.0 → 1.1), 1280×720, 24 fps, cùng thông số mã hoá. Ảnh được chèn đúng vị trí thời gian giữa các phần tua nhanh.
- **Ghép:** concat demuxer giữa các phần (cùng codec), rồi mux một lần cuối với:
  - **Audio AAC:** im lặng, trừ dưới các ảnh có `audio: true`. Ở đó phát tối đa 8 s audio quanh thời điểm highlight, lấy từ bản đã chuẩn hoá (D19, `media.audio_view_path`), fade 0.3 s ở hai đầu.
  - **Phụ đề mềm `mov_text`** từ file SRT do brain viết:
    - tiêu đề ở 0–3 s;
    - dòng mô tả trong lúc mỗi ảnh hiện;
    - câu nói của transcript ở các ảnh hội thoại, giữ ngôn ngữ gốc.
- **Ra file:**
  - MP4 720p 24 fps.
  - Ghi vào file tạm dưới `vlog/.tmp/`, rồi đổi tên nguyên tử (atomic rename) thành `vlog/YYYY/MM/DD/<YYYY-MM-DD>.mp4` (cấu trúc theo ngày như §9.4).
  - Thêm file WebVTT cùng tên `.vtt` cho trình phát web, vì trình duyệt không hiện mov_text nằm trong MP4.
- **Không còn video thô** (retention đã xoá): phần tua nhanh dựng từ JPEG keyframe của ngày theo thứ tự thời gian, mỗi ảnh 0.25 s. Ghi `source: keyframes` trong `script`.

### 4.5 Dữ liệu, lưu trữ, "Quên"

- **Bảng `vlog`:** `day` (khoá chính), `path`, `duration_s`, `script` (jsonb: tiêu đề, highlight, nguồn, các tham số), `model_version`, `generated_at`, `stale`, `error`, `error_at`.
- **Giữ vĩnh viễn (§9.5):** retention không bao giờ đụng tới `vlog/`.
- **"Quên khoảng thời gian"** (§9.5 "đánh dấu … vlog … cần sinh lại"):
  - Xoá dòng `vlog` và cả hai file của mọi ngày bị chạm tới, vì vlog có thể chứa hình, tiếng và chữ của khoảng bị quên. Thứ tự giống recap ở P2b (R24): xoá file sau khi commit, qua `media.remove_files` và `pending_files`.
  - Planner sẽ dựng lại từ phần còn lại.

### 4.6 Web

- **`/vlog`:** danh sách theo ngày (tiêu đề, thời lượng, lúc dựng, trạng thái).
- **`/vlog/{day}`:** `<video controls>` cùng `<track>` WebVTT, và nút "Tải về" (`Content-Disposition: attachment`). File MP4 phục vụ có hỗ trợ Range để tua được.
- **Trang Ngày:** thẻ vlog của ngày, có nút "Dựng lại vlog".
- **Trang "Xử lý":** thẻ "Hiểu ngày" thêm vlog gần nhất và vlog lỗi.
- **Menu:** thêm "Vlog".

### 4.7 Lỗi, riêng tư, tài nguyên

- Viết kịch bản lỗi thì chuyển sang dự phòng.
- Dựng video lỗi thì thử lại 2 lần, sau đó hiện "Dựng vlog lỗi" trên trang Ngày và trang Xử lý. Không bao giờ lỗi im lặng.
- Thông báo lỗi chỉ ghi lớp lỗi và mã thoát của ffmpeg, không có chữ của transcript hay caption.
- Vlog không bao giờ lên cloud. Tải về chỉ qua trang web có basic auth.
- **Ước tính tài nguyên:**
  - ffmpeg 720p dùng khoảng 300–500 MB RAM.
  - Một ngày khoảng 8 giờ hoạt động tốn chừng 3–8 phút CPU với 2 thread.
  - File 10–20 MB mỗi ngày.
  - Plan đo lại khi deploy, cùng ngưỡng bộ nhớ của P2b Task 14.

### 4.8 Kiểm thử P3b

- **Video tổng hợp:** `helpers.make_h264` ghép nhiều đoạn, cùng keyframe và audio tổng hợp.
- **`ffprobe`** kiểm tra: thời lượng trong khoảng mong đợi, có 1 stream video, 1 audio và 1 phụ đề, độ phân giải 1280×720.
- **Kịch bản:** câu trả lời đúng; ref lạ hoặc trùng; quá 3 audio; model không chạy được (thì dùng dự phòng).
- **Planner:** hôm nay chỉ dựng sau recap; bắt kịp mỗi lượt một ngày; dựng lại sau khi recap đổi; không hẹn lại ngày đã lỗi.
- **Nguồn dựng:** ngày không có hoạt động; ngày chỉ còn keyframe.
- **"Quên"** xoá vlog và cả hai file.
- **Privacy:** chữ của transcript và caption không có trong log hay trong lỗi.
- **Web:** Range request, nút Tải về, track VTT, nút "Dựng lại" có CSRF/HTMX.

**Nghiệm thu P3b:**
- Vlog tự sinh 3 tối liền mà không ai bấm gì.
- Đo thời gian dựng, CPU và dung lượng file.
- Chủ dự án xem một vlog và cho nhận xét (ghi trong ledger, không trích nội dung).

## 5. Ngoài phạm vi P3

- Tool MCP có ghi dữ liệu.
- MCP truy cập ngoài LAN.
- Nhạc nền.
- Phụ đề đốt cứng vào hình.
- Làm mờ khuôn mặt.
- Vlog trên cloud.
- Chọn highlight bằng nhìn ảnh (vision).
