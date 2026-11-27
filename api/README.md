# Thư mục `api/`: Hợp đồng Giao tiếp HTTP và Realtime Events

Thư mục `api/` đóng vai trò lớp biên giao tiếp giữa các ứng dụng khách (`ui/`) với Core Runtime (`core/`) và tầng lưu trữ (`storage/`). Tầng này chịu trách nhiệm xác thực đầu vào DTO, định tuyến yêu cầu và truyền phát sự kiện thời gian thực, tuyệt đối không chứa logic điều phối nghiệp vụ hay tự quyết định trạng thái nhiệm vụ.

> **Trạng thái hiện tại:** Đã mở khóa riêng cụm endpoint đọc-chỉ phục vụ `Gate E` tại [api/artifacts.py](file:///C:/DoCho/AI-Village/api/artifacts.py) để tra cứu metadata và tải Artifact qua FastAPI sau khi kiểm tra quyền và mã băm SHA-256. Toàn bộ các endpoint điều phối, tác vụ, hàng đợi và SSE khác tiếp tục giữ nguyên kế hoạch triển khai ở `PHASE 12`.

## 1. Hợp đồng API Dự kiến (Mức Thiết kế)

| Nhóm chức năng | Phạm vi hợp đồng dự kiến | Ràng buộc bảo mật & ranh giới |
| :--- | :--- | :--- |
| **Task Submission** | Tiếp nhận yêu cầu nhiệm vụ mới, mô tả mục tiêu, tham chiếu tệp đầu vào hợp lệ và chuyển tới Core Runtime khởi tạo ở trạng thái `QUEUED`. | Xác thực schema đầu vào và giới hạn kích thước gói tin; mọi tệp đính kèm được nạp an toàn vào `workspace/{task_id}/inputs/`. |
| **Task Status** | Truy vấn trạng thái nhiệm vụ hiện tại, danh sách run, các bước thực thi ghi nhận trong Event Journal và danh sách nhân sự được phân công. | Chỉ đọc từ trạng thái thật do Core Runtime và Storage quản lý; không tự suy diễn tiến độ giả. |
| **Task Cancellation** | Tiếp nhận lệnh hủy hoặc tạm dừng khẩn cấp nhiệm vụ từ người dùng và chuyển tín hiệu xuống Core Runtime xử lý idempotent. | Xác nhận hoàn tất hủy chỉ sau khi Runtime chấm dứt tiến trình con thực tế. |
| **Agent Discovery** | Trả về danh mục Agent, phòng ban, nhãn năng lực và trạng thái phiên hiện tại từ Agent Registry và Runtime State. | Không trả về bí mật hệ thống hoặc cấu hình nhạy cảm của Engine. |
| **Tool / Skill Discovery** | Trả về danh mục công cụ khả dụng và kỹ năng đã duyệt (`APPROVED`) phục vụ tra cứu hiển thị trên UI. | Không cho phép client sửa đổi trực tiếp quyền thực thi công cụ hoặc trạng thái duyệt kỹ năng qua HTTP không kiểm soát. |
| **Approval Requests** | Liệt kê các `ApprovalRequest` đang chờ ở trạng thái `WAITING_APPROVAL` và tiếp nhận quyết định `APPROVED` / `REJECTED` từ người vận hành. | Kiểm tra hiệu lực `approval_id`, `expires_at` và khớp nguyên vẹn tham số hành động trước khi chuyển tiếp xuống Core Runtime. |
| **Artifacts** | Liệt kê, xem metadata (kèm SHA-256, SemVer, `task_id`) và tải về sản phẩm đã nghiệm thu từ Vault/Output. | Kiểm tra canonical path để chặn path traversal và chỉ cho tải tệp trong phạm vi cho phép. |
| **Event Streaming** | Cung cấp luồng sự kiện thời gian thực qua SSE hoặc WebSocket dựa trên Event Journal, hỗ trợ `Last-Event-ID` / reconnect và chống trùng lặp (dedupe). | Lọc sạch secrets, khóa API, đường dẫn nội bộ nhạy cảm trước khi đẩy sự kiện ra trình duyệt. |

## 2. Ranh giới Triển khai

- Theo `ADR-0001`, tầng `api/` dự kiến triển khai bằng **FastAPI** tại `PHASE 12` (`P12.01`–`P12.03`), sau khi lõi Runtime và Storage đã vượt qua các Gate tiền điều kiện.
- Tuyệt đối không đặt logic lập kế hoạch của Master, kiểm tra quyền hay kết nối trực tiếp AI Provider vào mã định tuyến của `api/`.
