# Báo cáo Kiểm toán Phase 05 / P05.02: Agent Registry & Versioned Personnel Management

**Ngày kiểm toán:** 2026-11-26  
**Nhánh / Repository:** `main` (`PHtai16/AI-Village`)  
**Commit tham chiếu `P05.01`:** [`5895cea5e0bf84386fc59ff7c0e79ef72986c27f`](file:///C:/DoCho/AI-Village/docs/reviews/PHASE_05_P05_01_AUDIT.md) (`feat(agents): establish validated agent manifests`)  
**Trạng thái nghiệm thu `P05.02`:** **PASS (Offline End-to-End Verified; không gán `LIVE_VERIFIED`)**

---

## 1. Kiến trúc Registry (`Registry Architecture`)

Hệ thống quản trị nhân sự AI được xây dựng theo chuỗi tách tầng `Manifest -> Validation -> Governance -> Agent Registry -> Persistent Storage`:

- Tầng nghiệp vụ thuần tại [`core/agents/registry.py`](file:///C:/DoCho/AI-Village/core/agents/registry.py) giao tiếp với SQLite thông qua Protocol `AgentRegistryRepository` và [`PermissionManager`](file:///C:/DoCho/AI-Village/core/governance/manager.py). Kiểm thử AST khẳng định toàn gói `core/agents/` không import `sqlite3`, `storage`, `sqlalchemy`, `subprocess`, `crewai`, `api`, `ui`, `httpx`, `urllib` hay `requests`.
- Tầng lưu trữ bền vững đặt tại [`storage/repositories/agent_registry_repo.py`](file:///C:/DoCho/AI-Village/storage/repositories/agent_registry_repo.py), gắn trực tiếp vào [`SQLiteRepositoryStore`](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py) dưới thuộc tính `store.agents`.
- Tầng khai báo [`agents/`](file:///C:/DoCho/AI-Village/agents/README.md) giữ nguyên vai trò tệp YAML tĩnh, không đặt logic Python hay truy vấn DB bên trong `agents/`.

---

## 2. Vòng đời Đăng ký (`Registration Lifecycle`)

Trạng thái đăng ký `AgentRegistrationStatus` gồm `DRAFT`, `VALIDATED`, `ACTIVE`, `DISABLED` và `REJECTED`, tách biệt với trạng thái thực thi `AgentRuntimeStateStatus`:

- Một Agent nằm trong YAML trên đĩa được xem là **Agent được khai báo**.
- Sau khi `register_agent` lưu vào SQLite, thực thể trở thành **Agent đã được đăng ký** với mã bản ghi và revision xác định.
- Chỉ bản ghi ở trạng thái `ACTIVE` mới thỏa mãn `is_eligible_for_assignment()` để lọt vào danh sách **Agent được phép nhận việc** cho P05.03 và P05.04.
- Quá trình đăng ký không mở phiên, không phát sinh `Run`, không kích hoạt `AgentRuntime` hay CLI tiến trình con nào.

---

## 3. Tạo, Cập nhật và Vô hiệu hóa (`Create / Update / Disable / Enable`)

[`AgentRegistryService`](file:///C:/DoCho/AI-Village/core/agents/registry.py) thực thi đầy đủ các thao tác quản trị:

- `register_agent`: kiểm tra schema, phòng ban, nhà cung cấp `known_providers`, chống trùng `agent_id` (`DuplicateAgentError`), và yêu cầu Approval Gate nếu bản ghi `ACTIVE` mang quyền nhạy cảm ban đầu (`network != "none"`, `tools.allowed` khác rỗng, `skills.allowed` khác rỗng hoặc `global` trong `memory.scopes`).
- `update_agent`: đối chiếu `expected_record_version`, kiểm tra khớp `agent_id`, phân tích thay đổi qua `analyze_manifest_diff` để phát hiện tăng đặc quyền, đổi phòng ban, đổi provider/connection, tăng `timeout_seconds`/`max_retries`, hoặc thêm capability mới chưa xác minh. Lịch sử phiên bản cũ được bảo toàn hoàn toàn, tạo nền tảng để Run tương lai chốt đúng `(agent_id, manifest_revision)` mà không bị ảnh hưởng khi hồ sơ lên revision mới.
- `disable_agent` và `enable_agent`: từ chối vô hiệu hóa lặp lại (`ALREADY_DISABLED`), từ chối xóa vật lý, không xóa lịch sử `Task`/`Artifact`/`Session`/`Event`, không tự hủy các `Run` đang `RUNNING` mà chỉ báo cáo danh sách `active_run_ids_not_cancelled`, đồng thời chặn `enable_agent` đối với bản ghi `REJECTED`.

---

## 4. Tìm kiếm và Đối chiếu Năng lực (`Search by Capabilities`)

`search_agents(AgentSearchQuery)` truy vấn trực tiếp từ hồ sơ thật trong Registry theo `agent_id`, `name_contains`, `role_contains`, `department`, `capability`, `input_format`, `output_format`, `registration_status`, `eligible_only`, `provider_id` và `default_engine_type`. Cấu trúc `CapabilityVerificationEntry` tách rõ ba thuộc tính `declared`, `validated` và `observed_in_execution`: mặc định mọi năng lực mới khai báo có `observed_in_execution=False`, ngăn việc coi danh sách nhãn YAML như bằng chứng đã chạy đạt.

---

## 5. Quản lý Phiên bản (`Versioning`)

Hệ thống tách bạch 4 khái niệm phiên bản: `spec_version` của chuẩn YAML, `active_revision` của nội dung `AgentManifest`, `record_version` dùng cho optimistic concurrency chống ghi đè đồng thời (`StaleAgentVersionError`), và `Session` khi chạy thật ở runtime sau này. Toàn bộ bảng `agent_manifest_versions` được bảo vệ bằng trigger SQLite `trg_agent_manifest_versions_no_update` và `trg_agent_manifest_versions_no_delete`, chặn sửa hoặc xóa lịch sử ở cả mức SQL trực tiếp.

---

## 6. Schema Migration `0004` (`SQLite Migration`)

Tệp [`0004_agent_registry.sql`](file:///C:/DoCho/AI-Village/storage/db/migrations/0004_agent_registry.sql) nâng phiên bản schema từ `3` lên `4`, bổ sung hai bảng `agent_registry` và `agent_manifest_versions` cùng ba chỉ mục `idx_agent_registry_status`, `idx_agent_registry_dept`, `idx_agent_versions_agent` và hai trigger bất biến lịch sử. Toàn bộ migration `0001`–`0003` giữ nguyên vẹn không sửa đổi; hai bài test P04 cũ kiểm tra version tuyệt đối `== 3` đã cập nhật sang kiểm tra `>= 3` kết hợp `== get_latest_migration_version()` theo đúng mẫu `test_p04_04_sqlite_migrations.py`. Không có cột trạng thái runtime giả nào xuất hiện trong bảng Registry.

---

## 7. Tích hợp Governance (`Governance Enforcement`)

[`core/governance/evaluator.py`](file:///C:/DoCho/AI-Village/core/governance/evaluator.py) bổ sung đầy đủ các action vào `PERMISSION_MATRIX_ACTIONS` và `build_default_policy_rules()`:

- Hành động chuẩn: `agent.read`, `agent.create`, `agent.update`, `agent.disable`, `agent.enable` được cấp cho `human_operator` khi thao tác không leo thang quyền.
- Hành động nhạy cảm bắt buộc Approval Gate: `agent.change_permissions`, `agent.change_department`, `agent.change_connection` và thao tác vô hiệu hóa `core.master`.
- Bất biến hệ thống (`system_deny=True`): chặn mọi `worker_agent` thực hiện đột biến Registry (`PRIVILEGE_ESCALATION_DENIED`), chặn cả `master_agent` lẫn `worker_agent` tự sửa hoặc tự tắt hồ sơ của chính mình (`SELF_MODIFICATION_FORBIDDEN`), và chặn mọi actor không phải người vận hành vô hiệu hóa `core.master`.

---

## 8. Sự kiện Kiểm toán và Giao dịch Nguyên tử (`Audit Event`)

[`core/contracts/event.py`](file:///C:/DoCho/AI-Village/core/contracts/event.py) bổ sung các loại sự kiện `agent.registered`, `agent.updated`, `agent.disabled`, `agent.enabled` và `agent.drift_detected`. Cả bước đánh giá chính sách (`governance.decision_recorded`), bước tiêu thụ phê duyệt một lần (`APPROVAL_CONSUMED`), và bước ghi bản ghi SQLite kèm sự kiện `agent.*` diễn ra an toàn dưới transaction SQLite duy nhất. Khi mô phỏng lỗi ở cả tầng nhật ký Governance bên ngoài lẫn tầng `append_on_connection` nội bộ bên trong [`SQLiteAgentRegistryRepository`](file:///C:/DoCho/AI-Village/storage/repositories/agent_registry_repo.py), SQLite rollback hoàn toàn cả `agent_registry` lẫn `agent_manifest_versions`.

---

## 9. Bền vững Sau Khởi động Lại (`Restart Persistence`)

Sau khi đóng toàn bộ kết nối `SQLiteRepositoryStore` và mở lại phiên bản mới trên cùng tệp `.db` trong `tmp_path`, toàn bộ trạng thái `ACTIVE`/`DISABLED`, `disabled_reason`, lịch sử phiên bản `v1 -> v2`, khóa phiên bản `record_version`, chuỗi sự kiện kiểm toán và bản ghi tiêu thụ phê duyệt một lần đều giữ nguyên vẹn mà không có dữ liệu `Run` hay `Session` giả nào được sinh ra.

---

## 10. Phát hiện Sai lệch Tệp Cấu hình (`Manifest Drift`)

Phương thức `detect_manifest_drift()` đối chiếu SHA-256 chuẩn hóa của tệp YAML hiện tại trên đĩa qua bộ nạp an toàn với `manifest_sha256` đang lưu trong SQLite, trả về các trạng thái rạch ròi `IN_SYNC`, `DRIFT_DETECTED`, `SOURCE_MISSING`, `UNREGISTERED_MANIFEST` hoặc `INVALID_MANIFEST` mà không âm thầm đồng bộ hay ghi đè một chiều nào.

---

## 11. Dữ liệu Cho Agent Explorer (`Agent Explorer Summary`)

`export_explorer_summaries()` trả về danh sách `AgentExplorerSummary` chỉ đọc gồm thông tin định danh, phòng ban, trạng thái đăng ký, `active_revision`, `record_version`, bảng đánh giá capability, cấu hình định tuyến đã loại bỏ `parameters` và danh mục quyền an toàn. Trường `runtime_state_source` giữ hằng số trung thực `"NOT_OBSERVED_BY_REGISTRY"`, không giả lập `is_running`, `active_session_count`, token usage, cost hay CPU/RAM.

---

## 12. Kiểm thử Phủ định và Bảo mật (`Security Negative Tests`)

Bộ kiểm thử [`tests/agents/test_p05_02_agent_registry.py`](file:///C:/DoCho/AI-Village/tests/agents/test_p05_02_agent_registry.py) xác minh đầy đủ các tình huống từ chối Fail-Closed:

- Trùng lặp `agent_id`, cấu hình sai phòng ban hoặc nhà cung cấp lạ, thiếu ngữ cảnh xác thực, giả mạo vai trò.
- Tự sửa hồ sơ bản thân, `worker_agent` cố thay đổi `core.master`, vô hiệu hóa `core.master` thiếu duyệt, leo thang `network`/`timeout` chưa qua Approval Gate, Approval bị từ chối hoặc tái sử dụng lần hai.
- Cập nhật với `expected_record_version` cũ bị chặn `STALE_RECORD_VERSION`, cấm vô hiệu hóa Agent đã `DISABLED`, trigger SQLite chặn `UPDATE`/`DELETE` bảng lịch sử phiên bản, và rollback toàn phần khi ghi sự kiện kiểm toán lỗi.

---

## 13. Kết quả Kiểm thử Tích hợp và Hồi quy (Sau Chỉnh sửa Cuối cùng)

Toàn bộ lệnh chạy trên Python `3.13.13`, SQLite `3.50.4`, pytest `9.1.1`:

1. `python -m pytest tests/agents -v`: **`14 passed in 4.35s`** (gồm 9 bài `P05.01` và 5 bài tổng hợp 32 kịch bản + Acceptance 21 bước `P05.02`).
2. `python -m pytest tests/storage -v`: **`20 passed in 9.18s`**
3. `python -m pytest tests/core -q`: **`79 passed in 7.52s`**
4. `python -m pytest tests/stage_a tests/standards -v`: **`13 passed in 0.85s`**
5. `python -m pytest tests -v -rs`: **`179 passed, 24 skipped in 28.14s`** (0 thất bại; 24 mục `SKIPPED` gồm 2 bài CrewAI thiếu gói `crewai` ngoài phạm vi ADR-0002 và 22 bài live provider CLI chỉ chạy khi đặt `AI_VILLAGE_RUN_LIVE=1`).
6. `git diff --check`: thoát mã `0`.

---

## 14. Phán quyết Nghiệm thu P05.02

Tiểu phase `P05.02` đạt **PASS (Offline End-to-End Verified)**. Không gắn nhãn `LIVE_VERIFIED` do không gọi `agy.exe` trực tiếp trong phạm vi quản trị Registry. Chưa triển khai P05.03–P05.07 và chưa đánh dấu `PHASE 05 DONE`, giữ ranh giới sạch cho bước lập kế hoạch Master và Team Builder kế tiếp.
