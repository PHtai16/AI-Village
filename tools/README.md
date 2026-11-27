# Thư mục `tools/`: Hệ thống Công cụ Thực thi Dùng chung

Thư mục `tools/` chứa các công cụ thực thi độc lập với phòng ban và nhân vật, được tổ chức thành 5 nhóm chức năng (`files/`, `code/`, `browser/`, `research/`, `office/`). Công cụ thuần túy thực hiện thao tác kỹ thuật có kiểm soát, không chứa prompt nhân vật hay logic điều phối của Master.

> **Trạng thái hiện tại:** Đã hoàn thiện `PHASE 07` (`P07.01`–`P07.06`) với 17 công cụ chuẩn hóa trên 5 nhóm `files/`, `code/`, `browser/`, `research/`, `office/` cùng cầu nối `mcp_bridge/` và `core/tools/`, lưu vết vào SQLite migration `0011` (chi tiết kiểm toán tại [docs/reviews/PHASE_07_AUDIT.md](file:///C:/DoCho/AI-Village/docs/reviews/PHASE_07_AUDIT.md)).

## 1. Tài liệu Tham chiếu Bắt buộc

- Đặc tả Công cụ (Tool Manifest Schema 16 trường, `ToolResult` và vòng đời 10 bước): [docs/standards/TOOL_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/TOOL_SPEC.md).
- Đặc tả Quản trị và Phân quyền (8 lớp Rule, Fail-Closed, Approval Gate): [docs/standards/RULE_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/RULE_SPEC.md).

## 2. Thành phần Kiến trúc và Quy chuẩn Định danh

1. **Tool Registry:** Danh mục tập trung quản lý định danh, phiên bản SemVer, `input_schema`, `output_schema`, mức rủi ro và vòng đời khả dụng (`DRAFT`, `REGISTERED`, `AVAILABLE`, `DEPRECATED`, `DISABLED`). Công cụ mới khai báo ở trạng thái `DRAFT` chưa được phép gọi trong runtime.
2. **Tool ID:** Định danh công cụ chuẩn `tools.<category>.<tool_name>` (ví dụ: `tools.files.read`, `tools.code.run_test`), dùng trong `agent.tools.allowed` và `skill.requirements.tools`.
3. **Action ID:** Định danh hành động kỹ thuật chuẩn `<domain>.<verb>` viết bằng `snake_case` (ví dụ: `files.read`, `code.exec_custom`, `network.http`), dùng trong `tool.permissions_required`, `rule.actions` và `agent.permissions.requires_approval`.
4. **Permissions:** Mọi Tool Call phải qua Permission Manager thuộc `core/governance/` để kiểm tra đồng thời quyền Agent, `tools.allowed`, Action ID và đường dẫn chuẩn hóa trong `workspace/{task_id}/`. Khi Policy Engine gặp lỗi, áp dụng mặc định Fail-Closed (`PERMISSION_DENIED`).
5. **ToolResult:** Chuẩn đầu ra thống nhất tách biệt trạng thái kỹ thuật (`EXECUTION_SUCCEEDED`, `EXECUTION_FAILED`, `PERMISSION_DENIED`, `APPROVAL_REQUIRED`, `CANCELLED`, `TIMED_OUT`) với trạng thái kiểm chứng nghiệp vụ (`UNVERIFIED`, `VERIFIED_PASS`, `VERIFIED_FAIL`).
6. **Sandbox Policy:** Các thao tác chạy mã, cài đặt tạm thời hoặc gọi công cụ bên ngoài phải cô lập trong `sandbox/` theo chính sách mạng, giới hạn CPU/RAM và timeout.
7. **Approval Gate:** Công cụ có `approval_policy` là `CONDITIONAL` hoặc `ALWAYS_REQUIRED` khi chạm Action ID nhạy cảm phải tạm dừng ở `APPROVAL_REQUIRED` chờ người vận hành duyệt qua `approval_id`.
8. **Tool Versioning:** Phiên bản hóa theo SemVer bất biến; thay đổi breaking change trên schema phải tăng `MAJOR`, hỗ trợ `DEPRECATED` và rollback nhanh khi phát sinh sự cố.
