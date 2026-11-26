# Báo cáo Kiểm toán Phase 05 / P05.01: Agent Manifest Schema & Safe YAML Loader

**Ngày kiểm toán:** 2026-11-26  
**Nhánh / Repository:** `main` (`PHtai16/AI-Village`)  
**HEAD tham chiếu trước đó:** `dd2de05da1158b59dcced3bf3ffe7edd2efc4faf` (Gate C PASS)  
**Trạng thái nghiệm thu P05.01:** **PASS (Offline Verified)**

---

## 1. Kiến trúc Schema và Ranh giới Tầng

Hợp đồng hồ sơ tĩnh của nhân viên AI và phòng ban được cài đặt bằng Pydantic v2 (`extra="forbid"`) tại gói [`core/agents/`](file:///C:/DoCho/AI-Village/core/agents/__init__.py) mà không cần cài đặt thêm thư viện bên thứ ba ngoài `PyYAML 6.0.3` đã sẵn có trong môi trường. Kiểm thử AST tại [`test_p05_01_agent_manifest.py`](file:///C:/DoCho/AI-Village/tests/agents/test_p05_01_agent_manifest.py) xác nhận không có bất kỳ lệnh import `sqlite3`, `storage`, `sqlalchemy`, `subprocess`, `crewai`, `api`, `ui`, `httpx`, `urllib` hoặc `requests` trong `core/agents/`. Thư mục [`agents/`](file:///C:/DoCho/AI-Village/agents/README.md) tiếp tục chỉ chứa tệp YAML tĩnh và tài liệu, không chứa mã thực thi.

---

## 2. Quy tắc Danh tính (`Identity Rules`)

Mô hình [`AgentManifest`](file:///C:/DoCho/AI-Village/core/agents/manifest.py) chuẩn hóa đầy đủ 9 nhóm trường bắt buộc theo [`AGENT_SPEC.md`](file:///C:/DoCho/AI-Village/docs/standards/AGENT_SPEC.md):

- Định danh `id` tuân thủ `^[a-z0-9_]+\.[a-z0-9_]+$`, cấm thông tin bí mật và ký tự rỗng.
- Ràng buộc tương hỗ ba chiều bảo vệ danh tính Master: `id == "core.master"`, `system_role == "orchestrator"` và `department == "orchestration"` luôn đi cùng nhau. Mọi Worker cố tình khai báo `system_role="orchestrator"` hoặc `department="orchestration"` đều bị từ chối fail-closed.
- Thuộc tính `department` bị cấm trùng với tên nhà cung cấp AI như `antigravity`, `google_gemini`, `openai`, `anthropic`, `ollama`, `openai_compatible`.

---

## 3. Hồ sơ Master (`agents/master/agent.yaml`)

Hồ sơ chính thức [`agents/master/agent.yaml`](file:///C:/DoCho/AI-Village/agents/master/agent.yaml) khai báo `id: "core.master"`, `system_role: "orchestrator"`, `department: "orchestration"` với quyền ghi `permissions.fs_write: []`, mạng `network: "none"`, không khai báo công cụ chưa tồn tại (`tools.allowed: []`, `skills.allowed: []`), và liệt kê tường minh trong `mission.non_goals` các giới hạn cấm chấm QA PASS thay QA, cấm chỉnh Policy Engine, cấm tự nâng quyền hay tự ý đổi Provider trái chính sách.

---

## 4. Hồ sơ Worker (`agents/roles/it_artifact_worker/agent.yaml`)

Hồ sơ Worker tối thiểu [`agents/roles/it_artifact_worker/agent.yaml`](file:///C:/DoCho/AI-Village/agents/roles/it_artifact_worker/agent.yaml) bám sát phạm vi đã kiểm chứng ở Phase 03 cho `it.artifact_worker` (`department: "it"`, `system_role: "worker"`, năng lực `workspace_artifact_authoring`, phạm vi đọc `workspace/{task_id}/inputs/*`, phạm vi ghi `workspace/{task_id}/outputs/*`, `tools.allowed: []`, `skills.allowed: []`). Không tạo hàng loạt nhân viên AI thiếu năng lực thật.

---

## 5. Tham chiếu Phòng ban (`Department References`)

[`DepartmentManifest`](file:///C:/DoCho/AI-Village/core/agents/department.py) hỗ trợ chuẩn schema tại [`DEPARTMENT_SPEC.md`](file:///C:/DoCho/AI-Village/docs/standards/DEPARTMENT_SPEC.md), cấm đặt `department_id="orchestration"` hoặc trùng tên AI Provider, và tái sử dụng tập 9 phòng ban mặc định (`hr`, `resources`, `service`, `marketing`, `it`, `reception`, `process`, `qa`, `vault`) từ [`core/contracts/agent.py`](file:///C:/DoCho/AI-Village/core/contracts/agent.py). Trường `default_tools` được giữ hoàn toàn ở mức gợi ý chuyên môn, không tự cấp quyền cho Agent.

---

## 6. Trường Định tuyến Engine (`Engine Routing Fields`)

`AgentEngineConfig` hỗ trợ `default_type`, `model_tier` (`pro`, `flash`, `code`) cùng các trường tùy chọn `provider_id`, `adapter_id`, `connection_id`, `model_id`, `routing_profile`, `fallback_policy` (`none`, `same_tier_allowed`, `operator_approved_only`) và `parameters`. Các validator cấm tuyệt đối khóa bí mật (`api_key`, `raw_key`, `secret`, `token`, `password`, `credential`, `authorization`) và chuỗi khóa thô ở cả key lẫn giá trị. Cấu hình `engine` chỉ mang tính ưu tiên định tuyến tĩnh, không thay thế quyết định kiểm quyền từ Governance.

---

## 7. Khai báo Kỹ năng và Công cụ (`Skills & Tools`)

- `skills.allowed`: yêu cầu mã `snake_case`, không trùng lặp và không dùng wildcard.
- `tools.allowed`: yêu cầu cú pháp `^tools\.[a-z0-9_]+\.[a-z0-9_]+$`, không trùng lặp và hỗ trợ đối chiếu `registered_tools` qua `validate_agent_manifest(...)`. Các manifest mẫu hiện tại đặt `[]` để không kích hoạt công cụ chưa tồn tại trước Phase 07.

---

## 8. Ranh giới Quyền hạn (`Permission Boundaries`)

- `AgentMemoryConfig`: bắt buộc chứa `"private"`, chỉ chấp nhận `private`, `project`, `global`.
- `AgentPermissionsConfig`: khóa chặt `fs_read` và `fs_write` không chứa `..`, đường dẫn tuyệt đối (`/`, `\`, `C:\`, `~`), wildcard toàn cục (`*`, `**`, `**/*`), tệp bí mật (`.env`, `credentials.json`, `*.pem`, `.ssh/`, `secrets/`, `.runtime/`), và chỉ cho phép `fs_write` trỏ sâu bên trong `workspace/` hoặc `sandbox/` của Worker; Master bắt buộc `fs_write: []`.
- Giới hạn `limits`: `timeout_seconds` trong khoảng `1..3600` và `max_retries` trong khoảng `0..5`.
- Bài kiểm thử xác nhận việc nạp hoặc validate `AgentManifest` không bao giờ tự cấp `ALLOW` khi đi qua [`PolicyEvaluator`](file:///C:/DoCho/AI-Village/core/governance/evaluator.py) và không khởi chạy bất kỳ Engine/CLI nào.

---

## 9. An toàn Nạp YAML (`YAML Security`)

Bộ nạp [`core/agents/loader.py`](file:///C:/DoCho/AI-Village/core/agents/loader.py) áp dụng `_StrictNoDuplicateSafeLoader(yaml.SafeLoader)` kèm kiểm tra hệ thống tệp:

- Chuẩn hóa đường dẫn thực qua `os.path.realpath`, chặn traversal `..` và chặn symlink hoặc NTFS directory junction trỏ ra ngoài `allowed_root` (`PATH_OUTSIDE_ALLOWED_ROOT`).
- Chặn tệp vượt ngưỡng dung lượng (`FILE_TOO_LARGE`, mặc định `65_536` bytes), từ chối khóa YAML trùng lặp (`DUPLICATE_YAML_KEY`), từ chối thẻ thực thi nguy hiểm như `!!python/object/apply:...` (`UNSAFE_YAML_TAG`), và kiểm tra sự khớp nhau giữa tên thư mục và `id` khi bật `enforce_path_convention`.

---

## 10. Tương thích Ngược (`Compatibility`)

- `AgentManifest.to_agent_identity()` ánh xạ thuần sạch sang [`AgentIdentity`](file:///C:/DoCho/AI-Village/core/contracts/agent.py) (`id` -> `agent_id`, `capabilities.skills` -> `capability_ids`, `ConnectionRef` không chứa `credential_ref`) mà không làm vỡ hợp đồng P04.01.
- `manifest_to_spike_agent_spec()` chuyển đổi thuần túy sang [`SpikeAgentSpec`](file:///C:/DoCho/AI-Village/core/orchestration/spike_models.py) mà không thay đổi bất kỳ tệp nào trong `core/orchestration/`.

---

## 11. Kết quả Kiểm thử Đơn vị và Toàn Kho

Chạy thực tế sau chỉnh sửa cuối cùng của `P05.01` trên Python `3.13.13`, pytest `9.1.1`:

- `python -m pytest tests/agents/test_p05_01_agent_manifest.py tests/stage_a tests/standards -v`: **`22 passed in 9.48s`** (9 kiểm thử Manifest/YAML/Boundary trong `test_p05_01_agent_manifest.py` cùng 13 kiểm thử Stage A và Standards).
- `python -m pytest tests -v -rs`: **`174 passed, 24 skipped in 56.57s`** (0 thất bại; 24 mục `SKIPPED` gồm 2 bài CrewAI thiếu dependency `crewai` và 22 bài live CLI yêu cầu `AI_VILLAGE_RUN_LIVE=1`).
- `git diff --check`: thoát mã `0`, không có lỗi khoảng trắng.

---

## 12. Phán quyết Nghiệm thu P05.01

Tiểu phase `P05.01` đạt **PASS (Offline Verified)**. Hồ sơ khai báo tĩnh được chuẩn hóa và cô lập hoàn toàn khỏi quyền thực thi và trạng thái runtime, sẵn sàng chuyển tiếp sang `P05.02 — Agent Registry`.
