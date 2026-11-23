# Thư mục `sandbox/`: Chính sách Cách ly và Môi trường Thực thi An toàn

Thư mục `sandbox/` định nghĩa chính sách cách ly, cấu hình container mẫu và ranh giới bảo vệ hệ điều hành máy chủ khi các Agent chạy mã nguồn, kịch bản kiểm thử hoặc công cụ có rủi ro.

> **Cảnh báo trạng thái:** Tài liệu này mô tả thiết kế cách ly mục tiêu cho Chặng A. **Môi trường sandbox chưa được kiểm thử xâm nhập hay kiểm chứng thực tế** và tuyệt đối chưa được tuyên bố đã an toàn để chạy mã không đáng tin cậy cho đến khi có bài kiểm thử xác minh ở `PHASE 02`, `PHASE 07` và `PHASE 15`.

## 1. Các Thành phần Chính sách Cách ly

1. **Isolation Model (Mô hình cách ly):** Phân tách rõ hai chế độ thực thi theo `TOOL_SPEC.md`: `sandbox_only` (chạy bên trong container cô lập như Docker không có quyền root/host cho nhóm `code` và `research`) và `restricted_local` (thao tác cục bộ khóa chặt canonical path cho nhóm `files` trong workspace).
2. **Workspace Boundaries (Ranh giới không gian làm việc):** Mỗi nhiệm vụ chỉ được ánh xạ đúng thư mục `workspace/{task_id}/` tương ứng. Cấm mount thư mục gốc repository, thư mục `.git/`, `.runtime/` hay các đường dẫn hệ thống của máy chủ vào vùng ghi của sandbox.
3. **Filesystem Permissions (Quyền hệ thống tệp):** Mọi đường dẫn đầu vào phải qua bước chuẩn hóa tuyệt đối (canonical path resolution) để ngăn thoát thư mục (`..`, symlink escape). Hệ thống tệp gốc của container nên đặt ở chế độ chỉ đọc (`read-only rootfs`) ngoại trừ thư mục làm việc của nhiệm vụ.
4. **Network Restrictions (Hạn chế mạng):** Mặc định tắt hoàn toàn kết nối mạng (`--network none` / `permissions.network: "none"`) đối với mọi tác vụ chạy code hoặc xử lý dữ liệu nội bộ. Chỉ cho phép kết nối có kiểm soát tới các đích hợp lệ khi nhiệm vụ được cấp quyền `intranet` hoặc `full` qua Policy Engine.
5. **Process Timeout (Giới hạn thời gian tiến trình):** Mọi tiến trình chạy trong sandbox đều chịu giám sát đồng hồ đếm ngược theo `timeout_seconds` của Tool Manifest và Agent Manifest. Khi quá hạn, hệ thống cưỡng chế kết thúc toàn bộ nhóm tiến trình con (process tree termination).
6. **Resource Limits (Giới hạn tài nguyên):** Áp đặt trần CPU, bộ nhớ RAM, số lượng tiến trình con (`pids-limit`) và dung lượng ghi đĩa để ngăn sự cố cạn kiệt tài nguyên hoặc vòng lặp rẽ nhánh làm treo máy chủ.
7. **Cleanup (Dọn dẹp sau thực thi):** Sau khi công cụ hoàn tất, hủy hoặc quá thời gian chờ, container/tiến trình tạm thời phải được thu hồi sạch sẽ, giữ lại artifact hợp lệ và log đã lọc bí mật theo chính sách lưu vết.
8. **Secret Protection (Bảo vệ bí mật):** Không truyền biến môi trường chứa API key (`.env`), khóa SSH, token cá nhân hay chứng chỉ số vào môi trường biến của container sandbox. Toàn bộ stdout/stderr thu hồi từ sandbox phải qua bộ lọc che giấu bí mật (`redaction`) trước khi ghi vào `.runtime/`.
9. **Failure Handling (Xử lý sự cố):** Khi hạ tầng sandbox không khởi động được, container gặp lỗi hoặc tiến trình thoát bất thường, áp dụng nguyên tắc Fail-Closed: không tự động hạ cấp sang chạy thẳng trên máy chủ host, trả về `ToolResult.status = EXECUTION_FAILED` hoặc đánh dấu nhánh việc `BLOCKED` kèm thông báo lỗi rõ ràng.

## 2. Nguyên tắc Thực thi An toàn

- Nghiêm cấm chạy các script không đáng tin cậy từ bên ngoài hoặc từ thư mục `scripts/` của một `SKILL.md` chưa qua rà soát trên máy chủ thật.
- Mọi cài đặt package phát sinh trong quá trình chạy thử nghiệm phải bị chặn trên host và tuân thủ Approval Gate nếu nạp gói mới từ bên ngoài.
