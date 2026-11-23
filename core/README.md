# Thư mục `core/`: Lõi Điều phối và Quản trị Hệ thống

Thư mục `core/` chứa toàn bộ thuật toán điều phối, máy trạng thái nhiệm vụ, quản lý phiên làm việc, luồng sự kiện và chính sách kiểm soát an ninh của AI Village.

> **Trạng thái hiện tại:** Chỉ xác lập ranh giới thiết kế cho Chặng A. Chưa triển khai bất kỳ module nghiệp vụ Python nào thuộc `PHASE 03`–`11`.

## 1. Các Nhóm Logic Dự kiến

| Thành phần dự kiến | Trách nhiệm chính |
| :--- | :--- |
| **Orchestration (`core/orchestration/`)** | Tiếp nhận mục tiêu qua Master, lập nhóm công tác động theo năng lực (`capabilities`), quản lý handoff có định danh và điều phối phiên họp phản biện giới hạn lượt. |
| **Tasks (`core/tasks/`)** | Máy trạng thái nhiệm vụ (`QUEUED`, `ASSIGNED`, `RUNNING`, `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED`) có guard chuyển trạng thái, phụ thuộc nhiệm vụ, timeout và cancellation idempotent. |
| **Events (`core/events/`)** | Event Bus nội bộ và Event Journal ghi nối tiếp, đảm bảo lọc bỏ thông tin nhạy cảm (`redaction`) trước khi lưu và hỗ trợ replay khi khôi phục. |
| **Sessions (`core/sessions/`)** | Cô lập ngữ cảnh phiên làm việc logic cho từng Agent và từng nhiệm vụ, quản lý vòng đời phiên và ngăn rò rỉ lịch sử giữa các run. |
| **Governance (`core/governance/`)** | Tập hợp quy tắc quản trị 8 lớp (`Rule System`), kiểm soát chính sách phân quyền (`Permission Manager`) và cổng phê duyệt con người (`Human Approval Gate`). |
| **Permissions** | Bộ đánh giá quyền xác định (`Policy Evaluator`) trả về `PolicyDecision` (`ALLOW`, `DENY`, `APPROVAL_REQUIRED`, `ERROR`) theo nguyên tắc `Deny-Overrides` và `Fail-Closed`. |
| **Approvals** | Quản lý vòng đời `ApprovalRequest` gồm `approval_id`, thời hạn hiệu lực `expires_at`, khớp tham số gốc và lưu vết kiểm toán quyết định `APPROVED`/`REJECTED`. |
| **QA Integration** | Ràng buộc bắt buộc xác minh độc lập trước khi cho phép chuyển trạng thái Task sang `COMPLETED`; từ chối nghiệm thu nếu thiếu bằng chứng từ QA hoặc test runner. |
| **Resource Coordination (`core/optimization/`)** | Theo dõi ngưỡng tài nguyên, điều phối hàng đợi theo concurrency/quota, tiếp nhận ngắt đa cấp từ Reception và thu thập dữ liệu thực thi cho quy trình tự cải tiến Phương án C. |

## 2. Ranh giới Phụ thuộc (Dependency Boundaries)

1. **`core/` không phụ thuộc vào `ui/` hoặc `api/`:** Tuyệt đối không import bất kỳ thư viện giao diện, HTTP framework hay component hiển thị nào từ `ui/` hoặc `api/` vào trong `core/`.
2. **`core/` độc lập với AI Framework cụ thể:** Giao tiếp với tầng thực thi thông qua giao diện trừu tượng `AgentRuntime` và hợp đồng từ `engines/`, không phụ thuộc cứng vào API nội bộ của CrewAI hay một nhà cung cấp mô hình duy nhất.
3. **`core/` không truy cập trực tiếp tệp cấu hình bí mật để gửi ra ngoài:** Việc lưu trữ bền vững đi qua giao diện repository tại `storage/`, còn việc thực thi công cụ đi qua Tool Runtime tại `tools/`.
4. **Tham chiếu hợp đồng kỹ thuật:** [SYSTEM_BLUEPRINT.md](file:///C:/DoCho/AI-Village/docs/architecture/SYSTEM_BLUEPRINT.md), [RULE_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/RULE_SPEC.md) và [AGENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/AGENT_SPEC.md).
