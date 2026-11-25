# BÁO CÁO NGHIỆM THU PHASE 04 / P04.05: PERSISTENT REPOSITORIES & RESTART RECOVERY

- **Giai đoạn:** PHASE 04 / P04.05
- **Mục tiêu:** Triển khai tầng Repository bền vững trên SQLite, `SQLiteEventJournal`, dịch vụ giao dịch nguyên tử cho State Machine và cơ chế khôi phục/đối chiếu lịch sử sau khởi động lại
- **Trạng thái cuối:** **PASS**
- **Hình thức kiểm chứng:** Offline unit tests, concurrency tests, end-to-end persistence acceptance và 3 kịch bản fault injection trên SQLite `3.50.4` thật trong `tmp_path` (`20 passed` trong `tests/storage/`, `67 passed` trong `tests/core/`, tổng toàn bộ suite `140 passed, 24 skipped`), chưa chạy Antigravity live hay Gate C
- **HEAD tham chiếu lúc bắt đầu P04.05:** `759558e12722eb9732757194d5c58f9b79e25f41` (`feat(storage): establish versioned SQLite schema and migrations`)
- **Commit mục tiêu P04.05:** `feat(storage): implement persistent repositories and recovery`

---

## 1. Repository Architecture

- Gói `storage/repositories/` tách biệt hoàn toàn với `core/`: kiểm tra AST tự động xác nhận toàn bộ các tệp trong `core/` không có bất kỳ lệnh `import sqlite3`, `storage` hay `sqlalchemy` nào.
- Tầng ứng dụng chỉ giao tiếp qua Protocol hoặc dịch vụ giao dịch mà không thao tác SQL trực tiếp.
- Mọi thao tác đọc/ghi trên `SQLiteRepositoryStore` kiểm tra tương thích phiên bản schema (`expected_schema_version = 1`) khi khởi tạo và đóng kết nối sạch sẽ qua `close()` / context manager `__enter__`/`__exit__`.

---

## 2. Interfaces & Implementations

| Giao diện / Mô-đun | Thành phần triển khai | Vai trò chính |
| :--- | :--- | :--- |
| [interfaces.py](file:///C:/DoCho/AI-Village/storage/repositories/interfaces.py) | `TaskRepository`, `RunRepository`, `SessionRepository`, `MetadataRepository` | Hợp đồng `@runtime_checkable` Protocol tách tầng nghiệp vụ khỏi SQLite |
| [errors.py](file:///C:/DoCho/AI-Village/storage/repositories/errors.py) | `RepositoryError`, `StaleStateError`, `OwnershipMismatchError`, `SecretViolationError`, `SchemaCompatibilityError`, `StoredTask`, `StoredRun` | Ngoại lệ có cấu trúc và phong bì lưu trữ giữ `version` + `baseline` tách khỏi `core/contracts/` |
| [event_journal.py](file:///C:/DoCho/AI-Village/storage/repositories/event_journal.py) | `SQLiteEventJournal` | Triển khai `core.events.journal.EventJournal` bền vững trên SQLite |
| [sqlite_store.py](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py) | `SQLiteRepositoryStore`, `SQLiteTaskRepository`, `SQLiteRunRepository`, `SQLiteSessionRepository`, `SQLiteMetadataRepository` | Lưu/đọc có kiểm soát, giữ `NULL` telemetry, kiểm tra khóa ngoại và revision |
| [service.py](file:///C:/DoCho/AI-Village/storage/repositories/service.py) | `PersistentWorkflowService`, `PersistentTransitionOutcome` | Ghép State Machine (`core/tasks/`) với cập nhật Task/Run và Event Journal trong 1 transaction |
| [recovery.py](file:///C:/DoCho/AI-Village/storage/repositories/recovery.py) | `recover_task`, `recover_run`, `TaskRecoveryReport`, `RunRecoveryReport`, `InterruptedWorkAssessment` | Tái dựng sau restart, đối chiếu snapshot với `replay_task_history` và đánh dấu `RUNNING` bị gián đoạn |

---

## 3. SQLiteEventJournal Compatibility

- `isinstance(store.events, EventJournal)` đạt `True`; hỗ trợ đầy đủ `append()`, `append_batch()`, `read_entries()`, `get_by_event_id()` và `count()`.
- Bảo toàn đủ toàn bộ trường của `Event` và `JournalEntry`: `sequence_number`, `event_id`, `timestamp` (UTC/offset nguyên gốc), `event_type`, `source`, `task_id`, `run_id`, `session_id`, `actor_id`, `provider_id`, `connection_id`, `model_id`, `artifact_id`, `evidence_id`, `correlation_id`, `redacted_payload` và `batch_id`.
- Cơ chế chống trùng ID kiểm tra trực tiếp trên bảng `events` của SQLite (không dựa vào cache RAM), bảo đảm idempotent (`IDEMPOTENT_DUPLICATE`) và chặn xung đột nội dung (`DUPLICATE_EVENT_CONFLICT`) ngay cả sau khi đóng và mở lại database.
- `append_batch()` kiểm tra toàn lô trong giao dịch `BEGIN IMMEDIATE`; nếu phần tử cuối xung đột hoặc lỗi schema, toàn lô bị hủy mà không nhảy bộ đếm `sequence_number`.

---

## 4. Task/Run Persistence

- Không cung cấp hàm xóa hoặc sửa tự do lịch sử qua CRUD chung; mọi thay đổi trạng thái chuẩn đi qua `PersistentWorkflowService` và State Machine thật tại `core/tasks/state_machine.py`.
- `create_task()` lưu nguyên bản gốc vào cột `baseline_json` (bất biến qua mọi lần `save_task_state()` kế tiếp) và phiên bản khởi tạo `version = 1`.
- Một `Task` hỗ trợ nhiều `Run` qua `list_runs_for_task()`; ràng buộc sở hữu khóa chặt `Task A` không thể gắn `active_run_id` của `Run` thuộc `Task B` (`OwnershipMismatchError`) và `Run` không thể đổi `task_id` cha.
- Các chỉ số telemetry trong `runs` giữ nguyên `None` qua SQL `NULL` khi `telemetry_status = 'UNAVAILABLE_FROM_PROVIDER'`, tuyệt đối không chuyển thành `0`.

---

## 5. Session Persistence

- `SQLiteSessionRepository` lưu `Session` theo khóa chính UUID v4 `session_id`, tách biệt rạch ròi khỏi `provider_conversation_id`, `invocation_id` và `legacy_session_ref`.
- Không cho phép đổi `task_id` hay `run_id` chủ quản của một phiên đã gắn kết (`OwnershipMismatchError`).

---

## 6. Artifact/Checklist/Connection Persistence

- `ChecklistItem` lưu thứ tự `order_index` cùng `evidence_reference` và `completed_at`; giữ nguyên các ràng buộc chưa hoàn thành nếu thiếu bằng chứng.
- `Artifact` bảo toàn `relative_path`, `sha256`, `version`, `verification_status` và `evidence_reference`; quá trình lưu/đọc không bao giờ tự đổi trạng thái thành `VERIFIED_PASS`.
- `ConnectionRef` lưu metadata `provider_id`, `connection_id`, `model_id`, `project_ref` và `credential_ref` đã kiểm tra sạch secret, từ chối bằng `SecretViolationError` nếu phát hiện khóa API thô.
- `NotificationRef` liên kết chặt tới `event_id` có thật trong bảng `events`.

---

## 7. Transactional State Changes

- `PersistentWorkflowService.apply_task_transition()`, `apply_run_transition()` và `apply_paired_transition()` bọc trọn quy trình đọc `StoredTask`/`StoredRun` → gọi State Machine → lưu Task/Run mới với khóa lạc quan `version` → append Event/Batch trong **một giao dịch SQLite `BEGIN IMMEDIATE` duy nhất**.
- Nếu bất kỳ bước nào sau khi ghi Task/Run thất bại hoặc `EventJournal` từ chối ghi, `sqlite_transaction` thực hiện `ROLLBACK` toàn bộ, bảo đảm không có trạng thái cập nhật nửa vời.

---

## 8. Concurrency & Stale Updates

- Mọi thao tác cập nhật dùng `UPDATE ... SET ..., version = version + 1 WHERE ... AND version = ?`. Khi hai worker cùng đọc một snapshot `version` cũ và cùng cố gắng hoàn thành `Run` hoặc cập nhật `Task`, đúng một luồng ghi thành công (`COMMITTED`) và luồng còn lại bị chặn fail-closed với `StaleStateError` (`STALE_STATE_UPDATE`).
- Kiểm thử ghi đồng thời đa luồng (`6` threads, `12` sự kiện) qua `SQLiteEventJournal` thu được dãy `sequence_number` liên tục không trùng lặp và không mất bản ghi.

---

## 9. Restart Recovery

Kết quả đo trực tiếp trên kịch bản chấp nhận hoàn chỉnh (`test_offline_end_to_end_persistence_acceptance_across_restart` và đo kiểm chứng trên SQLite `3.50.4`):

- **Schema version:** `1` (`PRAGMA integrity_check`: `integrity_ok=True`, `PRAGMA foreign_key_check`: `fk_ok=True`).
- **Trước khi đóng database (`store1`):** `Task.status = COMPLETED` (`active_run_id = None`), `Run.status = SUCCEEDED` (kèm telemetry giữ `input_tokens=None`), `events.count() = 7`, dãy sequence từ `1` đến `7`, `Session` và `ChecklistItem` và `Artifact` (`VERIFIED_PASS` với `sha256` khớp tệp fixture) đã commit.
- **Sau khi đóng và mở lại instance mới (`store2`):** `events.count() = 7`, `Task.status = COMPLETED`, `Run.status = SUCCEEDED`, `recovery_status = VERIFIED_CLEAN`, `snapshot_matches_replay = True`, `timeline.total_returned = 7`, `timeline.last_sequence = 7`.
- **Với tác vụ đang `RUNNING` khi ứng dụng dừng (`test_restart_recovery_timeline_and_interrupted_running_work`):** `recover_task()` và `recover_run()` tuyệt đối không tự chuyển sang `COMPLETED` hay `FAILED`, mà đánh dấu `recovery_status = NEEDS_RECONCILIATION` kèm `InterruptedWorkAssessment(observable_process_status="UNOBSERVABLE_AFTER_RESTART", reconciliation_required=True)`.

---

## 10. Event Replay

- `recover_task()` đối chiếu bản snapshot lưu trong bảng `tasks` với kết quả `replay_task_history()` dựng từ cột `baseline_json` ban đầu và chuỗi sự kiện trong `events`.
- Khi `baseline_json` bị hư hỏng/thiếu (`Fault C`) hoặc không có hàng Task nền tảng, replay trả về `INCOMPLETE_HISTORY` (`can_reconstruct_full_task=False`, có mã `MISSING_BASELINE`) thay vì tự điền dữ liệu giả.
- Khi lịch sử sự kiện chứa bước nhảy trái phép (như `QUEUED -> COMPLETED` từ nguồn `provider`), replay trả về `INVALID_HISTORY`.

---

## 11. Activity Timeline

- `project_activity_timeline(store.events, task_id=...)` truy vấn trực tiếp bảng `events` qua `SQLiteEventJournal` sau khi restart mà không cần bảng timeline riêng.
- Các sự kiện từ cầu nối `record_runtime_event()` (`AgentRuntimeEvent`) giữ nguyên `tool_name` và `legacy_session_ref` trên `TimelineEntry`.

---

## 12. QA Independence

- Repository chỉ lưu metadata xác minh thật; không có phương thức nào tự nâng `Artifact.verification_status` lên `VERIFIED_PASS` hay ép `Task` sang `COMPLETED` khi thiếu `QaVerificationEvidence`.
- Khi tệp artifact thật trên đĩa bị sửa đổi sau khi lưu DB (`test_offline_end_to_end_persistence_acceptance_across_restart`), gọi lại `verify_engine_artifact()` vẫn phát hiện sai lệch SHA-256 và trả về `VERIFIED_FAIL`.

---

## 13. Crash/Rollback Tests

Ba kịch bản tiêm lỗi trong [test_p04_05_persistence_acceptance.py](file:///C:/DoCho/AI-Village/tests/storage/test_p04_05_persistence_acceptance.py) đều PASS:
- **Fault A (Crash/Rollback trước khi ghi Event):** hook ném `sqlite3.OperationalError` ngay sau bước SQL `UPDATE tasks`; kiểm tra lại trên Store mới xác nhận `Task` giữ nguyên `QUEUED`, `version = 1`, `events.count() = 1`.
- **Fault B (Duplicate Event sau restart):** mở lại Store mới và ghi lặp cùng `event_id` trả `IDEMPOTENT_DUPLICATE`, tổng số event vẫn bằng `1`.
- **Fault C (Broken Baseline/History):** làm hỏng `baseline_json` thành `{}` trên bản sao fixture thử nghiệm; `recover_task()` trả `INCOMPLETE_HISTORY`, `baseline_task = None`, `can_reconstruct_full_task = False` và có `MISSING_BASELINE`.

---

## 14. Security

- Chặn `SecretViolationError` khi `ConnectionRef` chứa khóa thô hoặc chuỗi secret (`AIza...`, `sk-...`, `=`).
- `SQLiteEventJournal` chặn `UNSAFE_PAYLOAD` ngay cả khi caller truyền đối tượng `Event` dựng qua `model_construct`.
- Mọi câu truy vấn SQL dùng câu lệnh tĩnh tham số hóa (`?`), không nối chuỗi tên bảng/cột từ dữ liệu ngoài.
- Không có tệp `.db`/`.sqlite` nào bị tạo ở root, `storage/` hay đưa vào lịch sử Git.

---

## 15. Regression Results

- `python -m pytest tests/storage -v`: **`20 passed`** (`7` ca P04.04 + `9` ca unit/integration P04.05 + `4` ca acceptance/fault injection P04.05).
- `python -m pytest tests/core -v`: **`67 passed`** (không hồi quy `P03.01`–`P03.03` và `P04.01`–`P04.03`).
- `python -m pytest tests -v -rs`: **`140 passed, 24 skipped`** (`0 failed`; `22` ca live chờ `AI_VILLAGE_RUN_LIVE=1` và `2` ca CrewAI skipped theo `global_crewai: False`).
- `git diff --check`: Thoát mã `0`, không lỗi định dạng dòng.

---

## 16. Remaining Limitations

1. **Chưa có xử lý timeout, cancellation runtime và fallback (`P04.06`):** các tác vụ `RUNNING` sau restart được phát hiện trung thực ở trạng thái `NEEDS_RECONCILIATION`, nhưng việc dò PID tiến trình thực tế hay chuyển tiếp trạng thái hủy/hết giờ thuộc phạm vi `P04.06`.
2. **Chưa có Governance policy engine (`P04.07`):** các quyết định phân quyền workspace, routing và approval gate cho Mission Control chưa được triển khai.
3. **Chưa chạy Antigravity live trong batch này và chưa nghiệm thu Gate C:** toàn bộ kiểm chứng P04.04–P04.05 thực hiện offline trên SQLite thật trong `tmp_path`.

---

## 17. Verdict

**PASS.** Tầng Repository bền vững, `SQLiteEventJournal`, dịch vụ chuyển đổi nguyên tử và cơ chế khôi phục sau restart của `PHASE 04 / P04.05` đạt toàn bộ tiêu chí nghiệm thu kỹ thuật, an toàn và hồi quy, sẵn sàng để `P04.06` triển khai tiếp quản lý cancellation, timeout, fallback và idempotency trên cùng nền tảng database.
