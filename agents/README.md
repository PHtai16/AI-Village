# Thư mục `agents/`: Định nghĩa Nhân sự AI và Phòng ban

Thư mục `agents/` chỉ lưu trữ các tệp khai báo cấu hình tĩnh (manifest, vai trò, năng lực và ranh giới quyền hạn) của các AI Agent, Master Agent và các phòng ban chức năng. Tuyệt đối không đưa logic điều phối nghiệp vụ, mã gọi API Engine hay dữ liệu trạng thái runtime vào thư mục này.

> **Trạng thái hiện tại (P05.01):** Chỉ lưu trữ hồ sơ YAML tĩnh đã chuẩn hóa (`agents/master/agent.yaml`, `agents/roles/it_artifact_worker/agent.yaml`) và tài liệu cấu trúc; toàn bộ mã Pydantic/Loader/Registry nằm tại `core/agents/` và `storage/repositories/`. Tuyệt đối không đặt file Python hoặc trạng thái runtime vào thư mục `agents/`.

## 1. Cấu trúc Dự kiến

- `agents/master/`: Hồ sơ cấu hình tĩnh của Master Agent (`agent.yaml` với `id: "core.master"`, `department: "orchestration"`, `system_role: "orchestrator"`).
- `agents/departments/`: Department Registry chứa tệp `department.yaml` cho 9 phòng ban mặc định (`hr`, `resources`, `service`, `marketing`, `it`, `reception`, `process`, `qa`, `vault`) và các phòng ban mở rộng sau này.
- `agents/roles/`: Danh mục hồ sơ tĩnh của các Worker Agent (`system_role: "worker"`).
- `agents/registry/`: Schema validation và tài liệu quản trị vòng đời đăng ký động.

## 2. Tài liệu Chuẩn Tham chiếu

- Đặc tả Agent (16 khía cạnh, Static Manifest Schema và Dynamic Runtime State Schema): [docs/standards/AGENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/AGENT_SPEC.md).
- Đặc tả Phòng ban (8 thành phần, 9 bộ phận mặc định và mở rộng): [docs/standards/DEPARTMENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/DEPARTMENT_SPEC.md).
- Mẫu tham chiếu cấu hình: [docs/templates/agent/agent.yaml](file:///C:/DoCho/AI-Village/docs/templates/agent/agent.yaml) và [docs/templates/department/department.yaml](file:///C:/DoCho/AI-Village/docs/templates/department/department.yaml).

## 3. Vai trò của Các Thành phần Quản trị Nhân sự

1. **Agent Registry:** Cung cấp danh mục tĩnh đã kiểm tra hợp lệ theo schema để Master tìm kiếm nhân sự theo `capabilities.skills` và `department`, đồng thời hỗ trợ tạo, cập nhật, vô hiệu hóa có kiểm soát ở Phase 05.
2. **Agent Manifest:** Khai báo tĩnh gồm danh tính, sứ mệnh, năng lực, cấu hình engine ưu tiên, phạm vi bộ nhớ, danh mục `skills.allowed`, `tools.allowed`, giới hạn đường dẫn `permissions.fs_read`/`fs_write` và danh sách Action ID cần duyệt `requires_approval`.
3. **Master Agent:** Thực thể điều phối trung tâm không thuộc 9 phòng ban chuyên môn, chịu trách nhiệm tiếp nhận yêu cầu, lập nhóm công tác và bàn giao cuối cùng. Master không được tự gán trạng thái `COMPLETED` thay cho QA.
4. **Department Registry:** Lưu trữ cấu hình năng lực bảo trợ của từng phòng ban tại `agents/departments/{dept_id}/department.yaml`. Danh mục `default_tools` trong phòng ban chỉ mang tính khuyến nghị chuyên môn, không tự cấp quyền cho Agent.
5. **Dynamic Worker Registration:** Khi cần nhân sự mới, bộ phận HR hoặc Master đề xuất manifest hợp lệ và phải vượt qua validation schema trước khi kích hoạt, tuyệt đối không để Agent tự sửa manifest để tự nâng quyền.

## 4. Ranh giới Quyền hạn

- Không tự cấp quyền thực thi thông qua `system_role` hoặc `department.default_tools`; mọi quyền thực thi thật do Permission Manager tại `core/governance/` đánh giá dựa trên `RULE_SPEC.md`.
- Không trộn lẫn dữ liệu phiên chạy, token usage hay lịch sử hội thoại từ `.runtime/` vào các tệp cấu hình tĩnh trong `agents/`.
