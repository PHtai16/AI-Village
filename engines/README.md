# Thư mục `engines/`: Lớp Adapter Kết nối AI Engine và Provider

Thư mục `engines/` đóng vai trò tầng chuyển đổi giao thức trung lập giữa `core/` với Antigravity CLI (`engines/antigravity/`) và các nhà cung cấp mô hình AI tùy chọn (`engines/providers/`). Thư mục này không chứa logic phòng ban, không chứa quy tắc điều phối nhóm và không tự cấp quyền cho Agent.

> **Trạng thái hiện tại:** Hợp đồng tài liệu mức thiết kế cho Chặng A. Chưa triển khai mã kết nối và **chưa gọi `agy` CLI** ở bước này. Việc kiểm chứng thực thi thật (Execution Proof) thuộc phạm vi `PHASE 02`.

## 1. Hợp đồng Adapter Tối thiểu (Mức Thiết kế)

Mọi Engine Adapter khi triển khai ở các phase sau phải tuân thủ hợp đồng dữ liệu thống nhất gồm 9 thành phần:

| Thành phần hợp đồng | Mô tả thiết kế |
| :--- | :--- |
| **Engine Request** | Yêu cầu đầu vào gồm `request_id`, `task_id`, `run_id`, `session_id`, `model_tier`, prompt/ngữ cảnh đã chuẩn hóa, danh mục công cụ cho phép, giới hạn `workspace_path` và `timeout_seconds`. |
| **Engine Result** | Kết quả chuẩn hóa gồm `request_id`, `status`, nội dung phản hồi, danh sách lời gọi công cụ ghi nhận được, tham chiếu artifact sinh ra trên đĩa, `error` và `telemetry`. |
| **Execution Status** | Trạng thái kỹ thuật của lượt chạy adapter (`SUCCEEDED`, `FAILED`, `TIMED_OUT`, `CANCELLED`, `BLOCKED`), tách biệt hoàn toàn với trạng thái nghiệp vụ `VERIFIED_PASS` hay Task `COMPLETED`. |
| **Error** | Cấu trúc lỗi chuẩn gồm `error_code` (`PROVIDER_DISCONNECTED`, `QUOTA_EXCEEDED`, `AUTH_ERROR`, `RATE_LIMITED`, `TIMED_OUT`, `UNSUPPORTED_CAPABILITY`), `message` đã lọc bí mật, `retryable` và `exit_code` (nếu là CLI). |
| **Session Reference** | Định danh phiên logic (`session_id`, `provider_session_ref`) để đảm bảo tách biệt ngữ cảnh giữa các phiên A/B và tránh trộn lịch sử làm việc. |
| **Tool Call Information** | Dữ liệu ghi nhận các thao tác công cụ mà Engine đã yêu cầu hoặc thực thi nội sinh, phục vụ việc đối chiếu độc lập trên hệ thống tệp thay vì tin lời mô hình. |
| **Telemetry** | Thời gian thực thi (`duration_ms`), số token thực tế (`prompt_tokens`, `completion_tokens`) và `telemetry_status` (`VERIFIED_MEASURED` hoặc `UNAVAILABLE_FROM_PROVIDER` kèm giá trị `null` khi provider không trả về). Nghiêm cấm bịa đặt số liệu. |
| **Cancel / Timeout** | Hỗ trợ hủy tiến trình an toàn (chấm dứt toàn bộ cây tiến trình con khi hủy hoặc vượt `timeout_seconds`) và không thử lại vô hạn khi gặp lỗi quota/auth. |
| **Capability Discovery** | Cơ chế kiểm tra tính khả dụng thực tế của Engine trên máy (phiên bản CLI, cờ lệnh được hỗ trợ như streaming hay session flags, trạng thái xác thực) trước khi nhận việc. |

## 2. Phân biệt Yêu cầu Thiết kế và Chức năng Đã kiểm chứng

- **Đã xác lập trong Chặng A:** Hợp đồng thiết kế tối thiểu nêu trên và nguyên tắc tách biệt `engines/` khỏi `core/`.
- **Chưa kiểm chứng thực tế:** Phiên bản cài đặt, tập cờ lệnh thực tế của `agy`, khả năng xuất `stream-json`, cô lập đa phiên đồng thời, xử lý exit code và số đo token từ Antigravity CLI. Toàn bộ các mục này phải chờ kiểm chứng bằng thực thi thật trong `PHASE 02` (`P02.01`–`P02.06`).
