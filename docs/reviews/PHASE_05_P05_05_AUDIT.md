# Báo cáo Kiểm toán Phase 05 / P05.05: Worker Session Lifecycle

**Ngày kiểm toán:** 2026-11-27
**Nhánh / Repository:** `main` trên `PHtai16/AI-Village`
**HEAD tham chiếu đầu vào:** `a8f9d2d97629dc84cbe259aa518dad6a80aec05f`
**Trạng thái nghiệm thu `P05.05`:** **PASS — `OFFLINE_INTEGRATION_VERIFIED`**

---

## 1. Kiến trúc Phiên Làm việc Worker

[`core/orchestration/worker_session_models.py`](file:///C:/DoCho/AI-Village/core/orchestration/worker_session_models.py) và [`core/orchestration/worker_sessions.py`](file:///C:/DoCho/AI-Village/core/orchestration/worker_sessions.py) triển khai `WorkerSessionLifecycleService` tách biệt hoàn toàn khỏi tầng UI và tầng lưu trữ SQL. Mọi hợp đồng Pydantic v2 sử dụng `extra="forbid"`, kết nối với lưu trữ qua Protocol `WorkerSessionRepository`, và giữ tương thích nguyên vẹn cho đường spike `P03.01` lẫn Protocol `AgentRuntime`. Kiểm thử AST trong [`tests/orchestration/test_p05_05_worker_session_lifecycle.py`](file:///C:/DoCho/AI-Village/tests/orchestration/test_p05_05_worker_session_lifecycle.py) xác nhận hai mô-đun này không import `sqlite3`, `storage`, `sqlalchemy`, `subprocess`, `crewai`, `api`, `ui`, `httpx`, `urllib` hay `requests`.

## 2. Kiểm tra Trước Thực thi Preflight 18 Điểm

Phương thức `run_execution_preflight` kiểm tra tuần tự 18 điều kiện fail-closed trước khi đụng tới Engine Adapter:

1. Tồn tại `Task` nguồn.
2. Trạng thái `Task` hợp lệ và thuộc quyền sở hữu của `Run` mục tiêu nếu có `run_id`.
3. `MasterWorkPlan` tồn tại, khớp Task, đang ở `APPLIED` và chứa `step_id` hợp lệ.
4. `TeamProposal` tồn tại, khớp Task/Plan và đang `ASSIGNED`.
5. `TeamProposal` không `STALE` và chưa trôi phiên bản so với Registry sống.
6. Nhân sự tồn tại trong Registry.
7. Nhân sự có trạng thái `ACTIVE`.
8. Phiên bản Manifest đã ghim tồn tại trong lịch sử `AgentRegistryService`.
9. Mã băm SHA-256 của bản Manifest bất biến khớp tuyệt đối với bản ghi ghim.
10. Nhân sự được phân công đúng `step_id` yêu cầu.
11. Mọi bước tiền đề trong `depends_on` đã có phiên `CLOSED` hoặc `ChecklistItem` trạng thái `COMPLETED` kèm bằng chứng, cùng handoff nếu chính sách bật yêu cầu.
12. `Provider` và `Connection` không xung đột với Manifest ghim hoặc trạng thái kích hoạt của `ConnectionRegistry`.
13. Adapter hợp lệ trong `EngineRegistry` và đáp ứng `local_only` / năng lực `EngineCapabilities`.
14. Phạm vi thư mục làm việc đi qua `validate_workspace_path` đồng thời nằm gọn trong `permissions.fs_write`/`fs_read` và `step.allowed_write_paths`.
15. `PermissionManager` cho phép hành động `agent.execute` cho `actor_id`/`actor_kind` hợp lệ, chặn Worker tự cấp quyền thực thi.
16. Bản ghi phê duyệt `ApprovalService` hợp lệ và chưa hết hạn khi `REQUIRE_APPROVAL` kích hoạt.
17. Không có cờ hủy từ `WorkflowLifecycleController`, không vượt giới hạn `manifest.limits.timeout_seconds`.
18. Khóa `idempotency_key` không đụng fingerprint khác biệt; trường hợp trùng khớp fingerprint trả nhánh phát lại `IDEMPOTENT_REPLAY`.

## 3. Tích hợp Runtime và Thực thi qua EngineRegistry

`execute_worker_step` khởi tạo phiên `INITIALIZED`, xây dựng `EngineRequest` chỉ gồm mục tiêu bước, phạm vi workspace được duyệt, thông số Manifest đã ghim và bối cảnh riêng của đúng Worker đó. Cuộc gọi thực thi đi qua `EngineRegistry.resolve` và `adapter.execute` chuẩn của `P02.06`, giữ `EngineResult.business_verification_status == "UNVERIFIED"` và không tự động đổi sang nhà cung cấp hoặc mô hình dự phòng khi phát sinh lỗi.

## 4. Thực thi Phiên bản Manifest Đã Ghim

Khi thực thi hoặc khởi tạo phiên, hệ thống truy xuất đúng phiên bản lịch sử qua `AgentRegistryService.get_manifest_version` và đối chiếu `canonical_sha256()` với `PinnedAgentAssignment.manifest_sha256`. Mọi thay đổi Manifest hoặc thao tác vô hiệu hóa Agent sau lúc lập đội hình khiến `STALE_MANIFEST_REVISION`, `MANIFEST_HASH_DRIFT` hoặc `WORKER_DISABLED` chặn khởi chạy bước mới thay vì tự động nâng cấp ngầm.

## 5. Cô lập Phiên Logical và Cô lập Cuộc hội thoại Provider

Mỗi phiên sử dụng định danh UUID v4 riêng trên mô hình [`Session`](file:///C:/DoCho/AI-Village/core/contracts/session.py) kèm không gian ngữ cảnh `private_context_namespace` duy nhất theo tổ hợp Task, Run, Agent và Session ID. Hai Worker dùng chung `provider_id` và `connection_id` vẫn tạo hai `EngineRequest` độc lập hoàn toàn không lộ `additional_context` sang nhau. Trước khi lưu `provider_conversation_id` do Engine trả về, kho lưu trữ thực thi truy vấn `find_provider_conversation_conflict` ngay trong giao dịch `BEGIN IMMEDIATE` để chặn dùng trùng chuỗi hội thoại giữa các Agent hoặc giữa các Task bằng mã `PROVIDER_CONVERSATION_COLLISION`. Nếu Adapter trả `None`, hệ thống giữ `None` và không bịa mã hội thoại.

## 6. Sở hữu Run, Ma trận Chuyển Trạng thái và Giao dịch SQLite

Kiến trúc giữ quan hệ một `Run` cho mỗi lượt chạy của `Task` chứa nhiều `Session` UUID v4 trong `Run.session_ids`, bảo toàn ràng buộc khóa ngoại `Task.active_run_id` của [`0001_initial.sql`](file:///C:/DoCho/AI-Village/storage/db/migrations/0001_initial.sql). Ma trận `VALID_SESSION_TRANSITIONS` chỉ cho phép chuyển đổi hợp lệ giữa `INITIALIZED`, `ACTIVE`, `IDLE` và ba trạng thái kết thúc `CLOSED`, `TERMINATED`, `FAILED`. Các sự kiện hợp lệ của State Machine được tạo qua `start_task_run` và `transition_run` để [`replay_task_history`](file:///C:/DoCho/AI-Village/core/events/replay.py) giữ nguyên `VALID`. Migration [`storage/db/migrations/0007_worker_sessions.sql`](file:///C:/DoCho/AI-Village/storage/db/migrations/0007_worker_sessions.sql) và [`SQLiteWorkerSessionRepository`](file:///C:/DoCho/AI-Village/storage/repositories/worker_session_repo.py) gộp `tasks`, `runs`, `sessions`, `worker_session_bindings`, `worker_session_transitions` có trigger khóa sửa xóa và `events` vào chung một `transaction_scope()`, tự động rollback toàn phần nếu ghi Event lỗi.

## 7. Gắn Nguồn Sự kiện, Khôi phục Sau Khởi động lại, Hủy và Timeout

`core/contracts/event.py` mở rộng hai loại sự kiện `session.initialized` và `session.status_changed`, kết hợp `session.started`, `session.closed`, `engine.event_forwarded` và `workflow.reconciliation_recorded`. Khi Engine đang chạy trên luồng riêng và bị hủy đồng thời qua `terminate_session` kèm `OwnedExecutionHandle` xác nhận dừng, khóa theo từng phiên đảm bảo callback hoàn tất đến sau không ghi đè trạng thái `TERMINATED`. Sau khi đóng và mở lại SQLite mà tiến trình Worker trước đó đang `ACTIVE` hoặc `IDLE` nhưng không còn handle sống quan sát được, `reconcile_worker_sessions` gán `NEEDS_RECONCILIATION` cùng `UNOBSERVABLE_AFTER_RESTART` và không tự chạy lại tác vụ hay báo `observable_running=True`. Các kết quả timeout ánh xạ thành `FAILED` với mã `TIMED_OUT`.

## 8. Bằng chứng Kiểm thử Unit, Integration và Giới hạn Live

Bộ kiểm thử [`tests/orchestration/test_p05_05_worker_session_lifecycle.py`](file:///C:/DoCho/AI-Village/tests/orchestration/test_p05_05_worker_session_lifecycle.py) kiểm chứng 7 nhóm kịch bản bao phủ đủ 36 mục kiểm tra của đặc tả bằng fake `EngineAdapter` chuẩn hóa qua `EngineRegistry` trên tệp SQLite thật đóng mở lại. Kết quả thực thi quan sát được:

1. `python -m pytest tests/orchestration/test_p05_05_worker_session_lifecycle.py -v`: **7 passed in 12.09s**.
2. `python -m pytest tests/orchestration -v`: **22 passed in 16.00s**.
3. `python -m pytest tests/agents tests/storage tests/core tests/stage_a tests/standards -v`: **126 passed in 11.04s**.
4. `python -m pytest tests -v -rs`: **201 passed, 24 skipped in 28.34s**, toàn bộ 24 mục bỏ qua thuộc bộ live `AI_VILLAGE_RUN_LIVE=1` và benchmark `crewai` tùy chọn.
5. `git diff --check`: thoát mã `0`.

Chưa chạy smoke test live tới `agy.exe` trong phạm vi `P05.05`, do đó kết quả chỉ công bố ở mức `OFFLINE_INTEGRATION_VERIFIED`. Các phiên bản tương lai khi có lệnh chạy live có thể nối thẳng `AntigravityEngineAdapter` đã đăng ký sẵn vào cùng `WorkerSessionLifecycleService` mà không đổi hợp đồng cốt lõi.

## 9. Phán quyết Gate 1 (`P05.05`)

Tiểu phase `P05.05` đạt **PASS** ở mức **`OFFLINE_INTEGRATION_VERIFIED`** và đủ điều kiện mở đường cho `P05.06`.
