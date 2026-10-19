# Loopi P4a — Image prod và OTA (thiết kế)

- **Ngày:** 2026-10-18
- **Trạng thái:** chờ chủ dự án duyệt
- **Spec gốc (bắt buộc):** `docs/superpowers/specs/2026-10-14-loopi-design.md`, gồm §6.3 (phân vùng thẻ SD), §6.5 (hai biến thể image), §6.7 (OTA), §7 (agent), §8 (protocol v1), §12 (xử lý lỗi), §14 (dòng P4). Tài liệu này chỉ chốt những điểm spec gốc còn để ngỏ. Nếu có chỗ mâu thuẫn, spec gốc thắng, trừ các quyết định mới D26–D29 dưới đây; plan P4a sửa spec gốc cho khớp.
- **Nghiệm thu (phần của §14 thuộc P4a):** OTA thành công; boot lỗi thì tự rollback; image prod không có SSH. Thêm theo D29: trang Phiên bản và ghi chú phát hành.

## 1. P4 chia thành ba phần

Chủ dự án chốt ngày 2026-10-18. Mỗi phần có thiết kế, plan và nhánh riêng, làm theo thứ tự:

1. **P4a — image prod và OTA** (tài liệu này).
2. **P4b — cấp WiFi bằng QR**, kèm captive portal dự phòng. Bản cập nhật P4b được đưa xuống thiết bị bằng chính OTA của P4a.
3. **P4c — BLE presence** giới hạn ghi âm. Chủ dự án muốn có **công tắc bật/tắt** tính năng này trong Cài đặt. Spec gốc §11 nói P4 "chỉ ghi âm khi điện thoại ở gần", nên công tắc sẽ được ghi thành một quyết định D mới khi thiết kế P4c. Theo quyết định P3c, BLE presence nên giới hạn ghi âm trên mọi camera chứ không chỉ riêng Pi.

## 2. Quyết định (2026-10-18)

| # | Điểm còn ngỏ | Quyết định |
|---|---|---|
| D26 | Cơ chế A/B và rollback trên Pi Zero W | Dùng **tryboot của firmware Raspberry Pi**, không thêm U-Boot. Phân vùng boot có hai thư mục `a/` và `b/`, mỗi thư mục chứa kernel, device tree, overlays và cmdline của slot đó. `config.txt` luôn trỏ vào slot tốt (`os_prefix=`). Khi thử bản mới, `tryboot.txt` trỏ vào slot mới và thiết bị khởi động lại bằng `systemctl reboot --reboot-argument="0 tryboot"`. Cờ thử chỉ có hiệu lực một lần, nên mọi lần khởi động lại sau đó (watchdog, kernel panic, rút điện, hẹn giờ) đều quay về `config.txt`, tức là slot cũ. Đã thử trên thiết bị thật (§3) |
| D27 | Cách bắt đầu một lần cập nhật và khi nào bản mới được coi là tốt | Chủ dự án chọn bundle rồi bấm "Cập nhật" cho từng thiết bị trên trang Thiết bị; không có cập nhật tự động. Bản mới chỉ được coi là tốt khi agent trên slot mới gửi được một heartbeat (brain trả `200`) **trong vòng 10 phút** kể từ lúc boot; nếu không, một bộ hẹn giờ độc lập với agent khởi động lại bình thường và thiết bị về slot cũ |
| D28 | Biến thể dev/prod và cách gỡ lỗi khi không có SSH | Bundle dev và bundle prod dùng **chung khoá ký và chung `compatible`**, nên cài qua lại được bằng OTA, kể cả bản cũ hơn. Khi thiết bị prod gặp sự cố, chủ dự án cài bản dev từ web để có lại SSH (chỉ bằng key, chỉ trên thiết bị đã có key dev trong `/data/dev/ssh`), rồi cài lại prod. Không thêm chức năng gửi log |
| D29 | Quản lý phiên bản | Chủ dự án thêm vào P4a ngày 2026-10-18: **trang "Phiên bản"** liệt kê mọi bundle (phiên bản, biến thể, ngày build, kích thước, thiết bị đang chạy bản đó) và xoá được bundle cũ; mỗi bundle có **ghi chú phát hành** sinh tự động từ commit, hiện trước khi bấm Cập nhật. Không làm nút "Quay về bản trước" và không dùng số phiên bản semver |

Các điểm nhỏ do người viết thiết kế tự chốt (ghi ở đây để chủ dự án sửa nếu không đồng ý):

- Bundle RAUC định dạng `verity`, tải hết về `/data/ota/` rồi mới cài (không cài kiểu streaming).
- Agent từ chối tải khi thẻ đã dùng quá 80 %, để bundle không đẩy hàng đợi lên ngưỡng xoá video 85 % (§7.2).
- Mỗi thiết bị chỉ có một lần cập nhật đang chạy.
- Phiên bản OS có dạng `<ngày build>-<7 ký tự commit>`, ví dụ `2026.10.21-a1b2c3d`, tính trên máy build từ git. Cây code có thay đổi chưa commit thì phiên bản có thêm hậu tố `-dirty`, và bản đó không được phát hành (§9). Brain không so sánh phiên bản; chủ dự án tự chọn bản muốn cài.
- OTA không cập nhật firmware khởi động (`bootcode.bin`, `start.elf`, `fixup.dat`); đổi firmware vẫn phải flash lại thẻ.

## 3. Kết quả thử tryboot trên thiết bị thật (2026-10-18)

Thiết bị chạy image dev smoke8: kernel 6.12.61, systemd 258, firmware trong `/boot` build ngày 2026-10-15. Chủ dự án đồng ý cho thử qua SSH. Mọi thay đổi đã được gỡ sau khi thử.

| Lần | Cách làm | Kết quả |
|---|---|---|
| 0 | Đọc chuỗi trong firmware | `bootcode.bin` và `start.elf` đều chứa `tryboot`, `tryboot.txt`, `autoboot.txt`, `tryboot_a_b`, `os_prefix` |
| 1 | Mục `[tryboot]` trong `config.txt`, rồi `systemctl reboot --reboot-argument="0 tryboot"` | Không có tác dụng: không có tham số đánh dấu, `/proc/device-tree/chosen/bootloader/tryboot` = 0. Không có `tryboot.txt` thì firmware dùng `config.txt` như bình thường. Mục `[tryboot]` trong `config.txt` chỉ có tác dụng khi `autoboot.txt` đặt `tryboot_a_b=1`, mà cách đó cần mỗi slot một phân vùng FAT riêng |
| 2 | Thêm `tryboot.txt` (bản sao `config.txt` cộng `cmdline=` trỏ tới một cmdline có tham số đánh dấu) | **Firmware boot theo `tryboot.txt`**: tham số đánh dấu có trong `/proc/cmdline`, `chosen/bootloader/tryboot` = 1, agent chạy và gửi heartbeat |
| 3 | `systemctl reboot` bình thường, `tryboot.txt` vẫn còn | **Quay về `config.txt`**: không có tham số đánh dấu, `tryboot` = 0 |

Mỗi lần khởi động lại, thiết bị ngừng ghi khoảng 40–45 s. Lệnh `systemctl reboot "0 tryboot"` (tham số theo vị trí) bị systemd 258 từ chối; phải dùng `--reboot-argument=`. Kernel Raspberry Pi tìm chuỗi `" tryboot"` (có dấu cách phía trước) trong tham số khởi động lại.

## 4. Phân vùng boot và các slot

Bảng phân vùng giữ nguyên như §6.3: `boot` (FAT32 64 MB), `rootfs-A` = `/dev/mmcblk0p2`, `rootfs-B` = `/dev/mmcblk0p3`, `data` = `/dev/mmcblk0p4`. Khác biệt là `rootfs-B` không còn để trống và phân vùng boot có thêm hai thư mục slot:

```
/boot/
  bootcode.bin  start.elf  fixup.dat        # firmware, dùng chung, OTA không đụng tới
  config.txt                                 # trỏ vào slot tốt: os_prefix=a/ (hoặc b/)
  tryboot.txt                                # chỉ có khi đang chờ thử: os_prefix=<slot mới>/
  loopi.toml                                 # cấu hình lần đầu, như P1 (§6.6)
  a/  zImage  bcm2708-rpi-zero-w.dtb  overlays/  cmdline.txt
  b/  zImage  bcm2708-rpi-zero-w.dtb  overlays/  cmdline.txt
```

- `a/cmdline.txt`: `root=/dev/mmcblk0p2 … rauc.slot=A panic=10`; `b/cmdline.txt`: `root=/dev/mmcblk0p3 … rauc.slot=B panic=10`. `panic=10` để kernel panic tự khởi động lại sau 10 s, tức là quay về slot tốt.
- Mỗi rootfs mang sẵn bộ file boot của chính nó ở `/usr/share/loopi/boot/`: kernel, device tree, overlays và mẫu `config.txt` của biến thể đó. Khi cài, các file này được chép sang thư mục slot trên phân vùng boot. Nhờ vậy OTA cập nhật được cả kernel lẫn module của nó.
- Dung lượng: mỗi slot khoảng 8,3 MB (kernel 7,3 MB, overlays 0,95 MB, device tree 32 kB). Firmware khoảng 3 MB. Phân vùng boot 64 MB còn dư nhiều.
- **Ghi file trên FAT:** `/boot` được mount chỉ đọc. Chỉ trong lúc ghi mới remount rw. Mỗi file được ghi ra file tạm, `fsync`, đổi tên đè lên file đích, rồi `fsync` thư mục, sau đó remount lại chỉ đọc. Đổi tên trên FAT không hoàn toàn nguyên tử. Rủi ro còn lại là mất điện đúng lúc đổi tên `config.txt`, mỗi lần cập nhật xảy ra một lần trong vài mili giây; nếu xảy ra, phải sửa thẻ trên máy tính.
- **Chế độ USB của bản dev:** mẫu `config.txt` của dev có dòng `dtoverlay=dwc2,dr_mode=peripheral`. Khi viết `tryboot.txt`, dòng này giữ đúng trạng thái đang có trong `config.txt` hiện tại (bật hoặc đã comment), để cập nhật không làm mất mic. Mẫu của prod không có dòng này.

## 5. RAUC trên thiết bị

- Package `rauc` của Buildroot trên thiết bị, `host-rauc` trên máy build. Kernel bật những gì bundle `verity` cần: `CONFIG_BLK_DEV_LOOP`, `CONFIG_SQUASHFS`, `CONFIG_DM_VERITY` và các tuỳ chọn nó phụ thuộc. `rauc.service` chỉ chạy sau khi `/data` đã mount (`RequiresMountsFor=/data`), vì `/data` mount kiểu `nofail` và không được systemd xếp trước `local-fs.target`.
- `/etc/rauc/system.conf`:

```ini
[system]
compatible=loopi-rpi0w
bootloader=custom
data-directory=/data/rauc
bundle-formats=-plain

[keyring]
path=/etc/rauc/keyring.pem

[handlers]
bootloader-custom-backend=/usr/libexec/loopi/rauc-boot
post-install=/usr/libexec/loopi/rauc-post-install

[slot.rootfs.0]
device=/dev/mmcblk0p2
type=raw
bootname=A

[slot.rootfs.1]
device=/dev/mmcblk0p3
type=raw
bootname=B
```

- `rauc-boot` là script cài vào rootfs, trả lời các câu hỏi của RAUC:
  - `get-primary`: đọc `os_prefix` trong `config.txt`. `config.txt` không có `os_prefix` (bố cục cũ trước P4a, kernel nằm ở gốc `/boot`) được coi là slot A.
  - `set-primary <slot>`: viết `tryboot.txt` trỏ vào slot đó. Script **không** sửa `config.txt`, vì slot mới chưa được thử. RAUC 1.15 gọi `set-primary` **trước** `rauc-post-install`, nên lần cài đầu vào một slot chưa có file boot thì script không viết `tryboot.txt` (xoá bản cũ nếu có) và vẫn trả về thành công; `rauc-post-install` viết `tryboot.txt` sau khi chép file. `tryboot.txt` không bao giờ trỏ vào slot chưa có kernel.
  - `get-state <slot>` / `set-state <slot> good|bad`: lưu trong `/data/rauc/boot-state`. Khi đặt `good` cho slot đang chạy, `config.txt` được thay bằng `tryboot.txt`, rồi `tryboot.txt` bị xoá.
  - `get-current`: lấy từ tham số `rauc.slot=` trong `/proc/cmdline`.
- `rauc-post-install` chạy sau khi RAUC ghi xong rootfs vào slot mới. Script mount slot đó chỉ đọc, chép `/usr/share/loopi/boot/` sang `/boot/<a|b>/`, rồi viết `tryboot.txt` từ mẫu `config.txt` của slot mới (giữ chế độ USB như §4).
- Khoá: image chỉ chứa chứng chỉ CA công khai (`keyring.pem`). RAUC từ chối mọi bundle không được ký bằng một chứng chỉ do CA này cấp, hoặc có `compatible` khác `loopi-rpi0w`.

## 6. Vòng đời một lần cập nhật

Trạng thái agent báo lên brain: `downloading` → `installing` → `rebooting` → `trying` → `succeeded`, hoặc `rolled_back`, hoặc `failed` kèm lý do.

1. **Nhận lệnh.** Heartbeat trả về lệnh `update` (§7). Agent ack lệnh, ghi `/data/ota/pending.json` (id lệnh, bundle, phiên bản, sha256, slot đang chạy) rồi bắt đầu tải. Nếu đã có một lần cập nhật đang chạy, lệnh mới vẫn được ack nhưng bị bỏ qua: agent ghi lý do vào `last_error` và tiếp tục báo lần cập nhật đang chạy trong trường `ota`.
2. **Tải.** Agent tải `GET /v1/ota/{bundle}` qua mTLS vào `/data/ota/<bundle>.part`. Khi mạng rớt, agent tải tiếp bằng header `Range`, thời gian chờ tăng dần như uploader. Tải xong thì kiểm sha256 và kích thước, rồi đổi tên thành `<bundle>`. Sai sha256 thì xoá file và báo `failed`. Thẻ đã dùng quá 80 % thì từ chối ngay từ đầu.
3. **Cài.** `rauc install /data/ota/<bundle>`. RAUC kiểm chữ ký và `compatible`, đánh dấu slot đích `bad`, ghi rootfs vào slot không chạy, gọi `set-primary`, rồi chạy `rauc-post-install` (chép file boot và viết `tryboot.txt`). Nếu `rauc install` lỗi, agent báo `failed` kèm thông báo của RAUC; slot đang chạy không bị đụng tới.
4. **Khởi động lại để thử.** Agent xoá file bundle, ghi trạng thái `rebooting` vào `pending.json`, rồi chạy `systemctl reboot --reboot-argument="0 tryboot"`. Trong lúc tải và cài, thiết bị vẫn ghi bình thường; chỉ ngừng ghi lúc khởi động lại.
5. **Lần boot thử.** Agent đọc `/proc/device-tree/chosen/bootloader/tryboot` (= 1) và thấy slot đang chạy là slot mới trong `pending.json`. Agent chạy bình thường, ghi hình như mọi khi, và báo `trying`. Ngay khi một heartbeat nhận `200`, agent chạy `rauc status mark-good`: `config.txt` chuyển sang slot mới. Sau đó agent báo `succeeded` và xoá `pending.json`. Lệnh `systemctl reboot` thường bị chính lần tắt máy nó yêu cầu giết (SIGTERM): agent không coi đó là lỗi mà giữ trạng thái `rebooting`. Một bản ghi `failed` mà slot và phiên bản đang chạy đúng là slot và phiên bản bản cập nhật đã cài (bản agent cũ ghi lần khởi động lại đó là lỗi) cũng được xác nhận như một lần boot thử.
6. **Bộ hẹn giờ độc lập với agent.** `loopi-tryboot-guard.timer` chạy 10 phút sau khi boot. Nếu lúc đó vẫn là boot thử (`tryboot` = 1) mà slot đang chạy chưa được đánh dấu tốt, nó chạy `systemctl reboot` bình thường. Vì cờ thử chỉ dùng một lần, máy về slot cũ. Bộ hẹn giờ này lo cả trường hợp agent không khởi động được.
7. **Phát hiện đã quay về.** Khi boot bình thường mà `pending.json` cho biết đang thử một slot khác slot đang chạy, agent đánh dấu slot kia là `bad`, báo `rolled_back` kèm phiên bản đã thử, rồi xoá `pending.json`.

Các trường hợp boot lỗi:

| Lỗi trên slot mới | Điều xảy ra |
|---|---|
| Agent không gửi được heartbeat (agent lỗi, WiFi hỏng, brain tắt) | Bộ hẹn giờ 10 phút khởi động lại bình thường → slot cũ |
| Agent hoặc systemd treo | Watchdog phần cứng (`RuntimeWatchdogSec`) reset máy → slot cũ |
| Kernel panic (ví dụ không mount được rootfs) | `panic=10` → khởi động lại sau 10 s → slot cũ |
| Kernel treo trước khi watchdog được bật | Máy đứng yên cho tới khi chủ dự án **rút điện rồi cắm lại** → slot cũ. Rất khó xảy ra, vì kernel và rootfs được build cùng một lần |

Nếu brain tắt đúng lúc thử, bản mới cũng bị quay về dù không hỏng. Như vậy là an toàn; chủ dự án chỉ cần bấm Cập nhật lại.

**Riêng tư:** các bất biến của §7.4 không đổi. Trong lúc tải, cài và boot thử, agent ghi như mọi khi và LED theo đúng trạng thái recorder. Khi khởi động lại, recorder dừng cùng agent.

**Quy tắc cho `/data`:** hai slot dùng chung `/data` (hàng đợi, khoá, cấu hình, `state.json`). Mỗi phiên bản agent phải đọc được dữ liệu do phiên bản liền trước và liền sau ghi. Muốn đổi định dạng theo kiểu không lùi được thì phải có một quyết định riêng.

## 7. Protocol v1 (chỉ thêm, không đổi cái cũ)

`docs/protocol/v1.openapi.yaml`, brain, agent và hai bộ contract test được sửa cùng lúc (CLAUDE.md).

- **Lệnh mới `update`** trong `HeartbeatResponse.commands`:
  `{id, type: "update", payload: {bundle, version, variant: "dev"|"prod", sha256, size}}`.
  `bundle` là tên file, khớp `^loopi-(dev|prod)-[0-9A-Za-z.+-]{1,64}\.raucb$`. Agent cũ không biết lệnh này chỉ báo "lệnh không hỗ trợ" như hiện nay (`control.rs`).
- **Endpoint mới `GET /v1/ota/{bundle}`:** chỉ cho thiết bị đã ghép cặp và chưa bị thu hồi (mTLS, như `PUT /v1/items`). Tên không khớp mẫu hoặc không có trong thư mục bundle thì trả `404`. Có hỗ trợ `Range`, trả `206`. Trả `200` với `Content-Length` và `Content-Type: application/octet-stream`.
- **Heartbeat có thêm các trường tuỳ chọn:** `os_version`, `os_variant` (`dev`|`prod`), `boot_slot` (`A`|`B`), và `ota: {command_id, state, version, error}`. `state` thuộc `downloading`, `installing`, `rebooting`, `trying`, `succeeded`, `rolled_back`, `failed`. `error` dài tối đa 500 ký tự. Brain cũ không nhận các trường này, nên brain luôn được cập nhật trước thiết bị.

## 8. Brain

- **Thư mục bundle:** `LOOPI_OTA_DIR`, mặc định `<LOOPI_DATA_DIR>/ota` (trong container là `/srv/loopi/ota`). Mỗi bundle đi kèm một file mô tả `loopi-<variant>-<version>.json` do bước build và bước phát hành ghi: `{bundle, version, variant, sha256, size, built_at, commit, notes_base, notes}`. `commit` là mã commit đầy đủ của bản build. `notes` là ghi chú phát hành (§8.1), `notes_base` là phiên bản mà ghi chú so sánh với. Brain chỉ liệt kê những bundle có file mô tả hợp lệ và có file `.raucb` đúng kích thước; file hỏng thì bỏ qua và ghi log. Brain không đọc nội dung bundle.
- **Migration 008:**
  - Mở rộng ràng buộc `device_command.type` để nhận thêm `update`.
  - Bảng `ota_update`: `id` (= id lệnh), `device_id`, `bundle`, `version`, `variant`, `requested_at`, `state`, `error`, `finished_at`.
- **Heartbeat:** khi heartbeat có trường `ota` khớp một `ota_update` đang chạy, brain cập nhật `state` và `error`; gặp `succeeded`, `rolled_back` hoặc `failed` thì ghi thêm `finished_at`.
- **Trang Thiết bị:**
  - Mỗi thiết bị hiện phiên bản OS, biến thể (dev/prod), slot đang chạy và trạng thái cập nhật gần nhất, bằng tiếng Việt: "Đang tải", "Đang cài", "Đang khởi động lại", "Đang thử bản mới", "Đã cập nhật", "Đã quay về bản cũ", "Lỗi: …".
  - Có ô chọn bundle và nút "Cập nhật" (POST có `HX-Request`). Chọn một bundle thì ghi chú phát hành của nó hiện ngay bên dưới, trước khi bấm. Nút bị khoá khi thiết bị đang có một lần cập nhật chưa xong, hoặc khi thiết bị đã bị thu hồi.
  - Hiện 5 lần cập nhật gần nhất.
- **Hết hạn:** một lần cập nhật đã gửi lệnh mà 30 phút không có tiến triển nào được đánh dấu `failed` với lý do "thiết bị không phản hồi", để nút Cập nhật không bị khoá mãi. Thiết bị là nguồn sự thật: nếu sau đó heartbeat lại báo tiến triển cho đúng lần cập nhật đó, brain ghi theo thiết bị.

### 8.1 Trang "Phiên bản" và ghi chú phát hành (D29)

- **Trang `/versions`**, có mục "Phiên bản" trên thanh menu. Mỗi dòng là một bundle, mới nhất trước (theo `built_at`):
  - phiên bản, biến thể (dev/prod), ngày build, kích thước, 7 ký tự đầu của commit;
  - các thiết bị đang chạy đúng bản đó (so `os_version` và `os_variant` trong heartbeat gần nhất);
  - ghi chú phát hành, thu gọn được.
- **Bundle không dùng được** (file mô tả hỏng, thiếu `.raucb`, sai kích thước) hiện trong một mục riêng kèm lý do, để chủ dự án xoá được.
- **Xoá bundle:** nút "Xoá", hỏi xác nhận, POST có `HX-Request`. Xoá cả `.raucb` lẫn `.json` khỏi thư mục bundle. Brain từ chối và nói rõ lý do khi:
  - có thiết bị đang chạy đúng bản đó;
  - có một lần cập nhật chưa xong đang dùng bundle đó.

  Tên file phải khớp mẫu ở §7 và nằm ngay trong thư mục bundle. Lịch sử cập nhật (`ota_update`) vẫn giữ tên và phiên bản của bundle đã xoá.
- **Ghi chú phát hành** do `publish-bundle.sh` sinh trên máy build, nơi có git:
  - `notes_base` là phiên bản mới nhất (theo `built_at`) đang có trong thư mục bundle lúc phát hành, không phân biệt dev hay prod, vì hai biến thể dùng chung code. Chưa có bundle nào thì `notes_base` để trống.
  - `notes` là danh sách `{commit, subject}` của các commit từ commit của `notes_base` tới commit của bản này, chỉ tính những commit đụng tới `agent/`, `os/` hoặc `docs/protocol/`, cũ trước mới sau. Tối đa 50 dòng; dài hơn thì giữ 50 dòng mới nhất và ghi `notes_truncated: true`. Chưa có `notes_base` thì lấy 20 commit gần nhất đụng tới các thư mục đó.
  - Ghi chú lấy nguyên tiêu đề commit (tiếng Anh, conventional commits). Trang web ghi rõ "Thay đổi so với <notes_base>", vì thiết bị có thể đang chạy một bản cũ hơn `notes_base`.

## 9. Build và ký

- **Hai biến thể:**
  - `os/configs/dev.fragment` (đã có) và `os/configs/prod.fragment` (mới), dùng chung `loopi.fragment`.
  - `os/build.sh` nhận biến thể (`VARIANT=dev|prod`, mặc định `dev`). Mỗi biến thể build trong một thư mục output riêng trong volume build, vì Buildroot không build lại đúng khi đổi fragment trong cùng một thư mục.
  - Những gì chỉ có ở dev (dropbear, rsync, strace, alsa-utils, `/etc/loopi-dev`, `loopi-devkeys.service`, `loopi-usbgadget.service`, `loopi-usbmode`, dòng `dwc2` trong mẫu `config.txt`) được tách khỏi phần dùng chung.
- **Image prod** (§6.5): không SSH; cổng USB luôn ở chế độ host; công cụ gỡ lỗi chỉ còn `loopi-selftest`; `run-agent` chỉ chạy `/usr/bin/loopi-agent`. Cấp WiFi vẫn qua `boot/loopi.toml` cho tới khi có P4b.
- **Đầu ra của một lần build:** `os/out/loopi-<variant>-<version>.img` (thẻ SD đầy đủ, dùng cho lần flash đầu), `.raucb` (bundle đã ký) và `.json` (file mô tả §8). Script `os/scripts/publish-bundle.sh` thêm ghi chú phát hành vào file mô tả (§8.1), rồi chép bundle và file mô tả vào thư mục bundle của brain; build không tự chép. Script từ chối phát hành bản `-dirty`, bản đã có trong thư mục bundle, và bản có commit không tìm thấy trong git.
- **Khoá ký:**
  - Script `os/scripts/rauc-keys.sh` tạo một lần một CA và một chứng chỉ ký (ECDSA P-256) trong `~/.config/loopi/rauc/`, thư mục quyền 700, file quyền 600.
  - Không bao giờ vào git, không bao giờ in ra. Build mount thư mục này chỉ đọc vào container. Thiếu khoá thì build từ chối tạo bundle.
  - Chứng chỉ CA (công khai) được đưa vào image thành `keyring.pem`.
  - **Chủ dự án nên sao lưu thư mục khoá.** Mất khoá thì không cài được bản cập nhật nào nữa, chỉ còn cách flash lại thẻ.

## 10. Chuyển thiết bị hiện tại sang bố cục A/B (lần OTA đầu, làm bằng tay)

Thiết bị đang chạy image dev cũ: rootfs ở `mmcblk0p2`, kernel ở gốc `/boot`, `mmcblk0p3` để trống, chưa có RAUC. Việc chuyển làm qua SSH, không rút thẻ, nên giữ nguyên ghép cặp và hàng đợi:

1. Chép `/data/keys` và `/data/config` về PC vào một thư mục quyền 700 ngoài repo. Không in ra, không commit. Đây là đường lui: nếu phải flash lại thẻ thì khôi phục được ghép cặp mà không cần token mới.
2. Ghi rootfs dev mới (có RAUC) vào `mmcblk0p3`, chép bộ file boot của nó vào `/boot/b/`, viết `tryboot.txt` trỏ vào `b/`, và ghi `/data/ota/pending.json` như một lần cập nhật thường, để agent mới xử lý lần boot thử theo đúng §6. `config.txt` hiện tại giữ nguyên, vẫn boot kernel cũ ở gốc `/boot` với `mmcblk0p2` (slot A theo §5).
3. Khởi động lại ở chế độ thử. Nếu agent mới gửi được heartbeat, `rauc status mark-good` chuyển `config.txt` sang `b/`. Nếu không, thiết bị tự về bố cục cũ, còn nguyên vẹn, và có thể làm lại.
4. Lần OTA đầu tiên bằng nút trên web sẽ ghi vào slot A (`mmcblk0p2`) và tạo `/boot/a/`. Chỉ sau khi lần đó thành công mới xoá kernel, device tree, overlays và `cmdline.txt` cũ ở gốc `/boot`.

Toàn bộ các bước nằm trong một script (`os/scripts/migrate-to-ab.sh`) để chạy lại được và review được. Script dừng ngay ở bất kỳ bước kiểm tra nào thất bại.

## 11. Xử lý lỗi (bổ sung §12 của spec gốc)

| Tình huống | Cách xử lý |
|---|---|
| Rớt WiFi khi đang tải bundle | Tải tiếp bằng `Range`, thời gian chờ tăng dần; ghi vẫn chạy |
| sha256 hoặc kích thước sai | Xoá file, báo `failed`; slot đang chạy không đổi |
| Chữ ký sai, `compatible` khác | `rauc install` từ chối, báo `failed` kèm lý do của RAUC |
| Thẻ đã dùng quá 80 % | Từ chối trước khi tải, báo `failed` |
| Mất điện khi đang tải hoặc đang cài | Boot lại vào slot cũ (`config.txt` chưa đổi). Agent thấy `pending.json` chưa tới bước khởi động lại thì báo `failed` ("bị ngắt giữa chừng") và xoá file tạm |
| Mất điện trong lúc boot thử | Lần boot sau là boot bình thường → slot cũ → `rolled_back` |
| Slot mới không gửi được heartbeat trong 10 phút | Bộ hẹn giờ khởi động lại → slot cũ → `rolled_back` |
| Thiết bị không phản hồi lệnh trong 30 phút | Brain đánh dấu `failed`, nút Cập nhật mở lại |

## 12. Kiểm thử

TDD, coverage ≥ 80 % (thư viện agent, package brain).

- **Agent:** module `ota` dùng các trait giả cho RAUC (`install`, `mark_good`, `mark_bad`, slot đang chạy), thông tin boot (cờ tryboot) và khởi động lại. Kiểm:
  - đủ các bước từ lệnh tới `succeeded`;
  - tải tiếp sau khi rớt mạng (wiremock trả `206`);
  - sha256 sai; thẻ quá 80 %; lệnh thứ hai khi đang cập nhật;
  - `mark-good` đúng một lần, sau heartbeat `200` đầu tiên của boot thử, không phải trước đó;
  - phát hiện `rolled_back` khi boot bình thường;
  - mất điện ở từng bước (đọc lại `pending.json`).

  Contract test cho lệnh `update`, các trường heartbeat mới và yêu cầu `GET /v1/ota`.
- **Brain:**
  - danh sách bundle (bỏ qua file mô tả hỏng hoặc thiếu `.raucb`);
  - `GET /v1/ota`: không có chứng chỉ, thiết bị đã thu hồi, tên không khớp mẫu, `../`, bundle không tồn tại, `Range` hợp lệ và không hợp lệ;
  - nút Cập nhật tạo đúng lệnh và `ota_update`;
  - nút bị khoá khi đang cập nhật;
  - heartbeat chuyển trạng thái; hết hạn sau 30 phút;
  - trang Phiên bản: thứ tự, thiết bị đang chạy, mục bundle không dùng được, ghi chú thu gọn;
  - xoá bundle: thành công; từ chối khi có thiết bị đang chạy bản đó hoặc đang có lần cập nhật dùng nó; tên lạ hoặc `../` bị từ chối; lịch sử vẫn còn sau khi xoá;
  - contract test phía server.
- **Image:** `os/tests/check-image.sh` kiểm thêm:
  - cả hai biến thể có `system.conf`, `keyring.pem`, `rauc-boot`, `rauc-post-install`, bộ hẹn giờ, `/usr/share/loopi/boot/` đầy đủ;
  - `a/cmdline.txt` và `b/cmdline.txt` có đúng `root=`, `rauc.slot=` và `panic=10`;
  - **prod** không có `dropbear` hay bất kỳ SSH server nào, không có `/etc/loopi-dev`, không có dòng `dwc2`, và `run-agent` không đọc `/data/dev`.
- **`publish-bundle.sh`:** test trên một repo git tạm: ghi chú đúng khoảng commit, chỉ các thư mục `agent/`, `os/`, `docs/protocol/`, cắt ở 50 dòng, trường hợp chưa có bundle nào, và từ chối bản `-dirty`, bản trùng, commit lạ.
- **Script boot (`rauc-boot`, `rauc-post-install`, `migrate-to-ab.sh`):** test chạy trên PC với một thư mục boot giả, kiểm đúng nội dung `config.txt` và `tryboot.txt` sau mỗi thao tác, kể cả giữ chế độ USB của dev.

## 13. Nghiệm thu trên thiết bị thật

Mỗi lần khởi động lại ngừng ghi khoảng 45 s. Bước 3 ngừng ghi khoảng 10 phút.

1. Chuyển thiết bị hiện tại sang bố cục A/B (§10), vẫn là bản dev.
2. Cập nhật dev → dev mới bằng nút trên web: `succeeded`, slot đổi.
3. Cài một **bundle hỏng cố ý**: agent của bundle này thoát ngay khi khởi động. Sau khoảng 10 phút thiết bị tự quay về; web hiện "Đã quay về bản cũ".
4. Cài một **bundle làm kernel panic** (`root=` trỏ sai phân vùng): thiết bị tự khởi động lại và quay về; web hiện "Đã quay về bản cũ".
5. Cập nhật dev → **prod**: cổng 22 đóng (kết nối bị từ chối); thiết bị vẫn ghi, gửi và heartbeat bình thường; web hiện biến thể "prod".
6. Cập nhật prod → dev, để làm tiếp P4b.
7. Trang Phiên bản: các bundle đã phát hành hiện đúng thiết bị đang chạy và ghi chú phát hành; xoá được một bundle cũ; xoá bundle đang chạy thì bị từ chối kèm lý do.

Hai bundle hỏng chỉ dùng cho nghiệm thu, build bằng một tuỳ chọn riêng, không bao giờ nằm trong thư mục bundle sau khi nghiệm thu xong. Ghi lại: kích thước bundle, thời gian tải, thời gian cài, thời gian ngừng ghi thực tế, và RAM/CPU của agent khi đang tải.

## 14. Sửa spec gốc

Plan P4a sửa `2026-10-14-loopi-design.md`:

- thêm D26–D29 vào bảng quyết định và dòng "Sửa đổi";
- §6.3: `rootfs-B` dùng cho OTA; phân vùng boot có `a/`, `b/`;
- §6.7: trỏ tới tài liệu này;
- §8: lệnh `update`, `GET /v1/ota/{bundle}`, các trường heartbeat mới;
- §12: các dòng mới ở §11;
- §10.6: thêm trang "Phiên bản" (D29).

## 15. Ngoài phạm vi P4a

- Cấp WiFi bằng QR và captive portal (P4b).
- BLE presence (P4c).
- Cập nhật firmware khởi động bằng OTA.
- Cập nhật tự động, lịch cập nhật, cập nhật nhiều thiết bị một lúc.
- Nút "Quay về bản trước" (chuyển sang slot còn lại mà không tải lại) và số phiên bản semver; chủ dự án không chọn (D29). Muốn quay về bản trước thì cài lại bundle của bản đó.
- Tải bundle lên qua web; bundle chỉ vào thư mục bằng `publish-bundle.sh`.
- Cập nhật các camera P3c: chúng là container trên PC, cập nhật cùng brain.
