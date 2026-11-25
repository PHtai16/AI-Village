# BÁO CÁO NGHIỆM THU PHASE 04 / P04.04: SQLITE SCHEMA & VERSIONED MIGRATIONS

- **Giai đoạn:** PHASE 04 / P04.04
- **Mục tiêu:** Thiết lập tầng kết nối SQLite chuẩn thư viện Python (`sqlite3`), lược đồ cơ sở dữ liệu tương thích Core Contracts và bộ chạy migration có phiên bản cùng rollback nguyên tử
- **Trạng thái cuối:** **PASS**
- **Hình thức kiểm chứng:** Kiểm thử SQLite thật trong `tmp_path` (`7 passed`), kiểm toán ranh giới cấu trúc và hồi quy toàn suite offline (`127 passed, 24 skipped`), chưa chạy Antigravity live
- **HEAD tham chiếu lúc bắt đầu:** `b5dad2da6ac9269d5a240ccd047fc7259efaa028`
- **Commit mục tiêu:** `feat(storage): establish versioned SQLite schema and migrations`

---

## 1. SQLite Architecture

- Tầng cơ sở dữ liệu đặt gọn trong `storage/db/` gồm [__init__.py](file:///C:/DoCho/AI-Village/storage/db/__init__.py), [connection.py](file:///C:/DoCho/AI-Village/storage/db/connection.py), [migrations.py](file:///C:/DoCho/AI-Village/storage/db/migrations.py) và thư mục script [0001_initial.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0001_initial.sql).
- Chỉ dùng `sqlite3` chuẩn của Python `3.13.13` (SQLite `3.50.4`), `pathlib`, `hashlib`, `uuid` và `pydantic` v2 sẵn có; không thêm SQLAlchemy, Alembic hay ORM ngoài.
- Đường dẫn mặc định là `.runtime/ai_village.db` hoặc đọc từ biến môi trường `AI_VILLAGE_DB_PATH` qua `resolve_database_path()`. Quá trình `import storage.db` hoàn toàn không tạo thư mục hay tệp database trên đĩa.
- `open_sqlite_connection()` bật `isolation_level=None`, `row_factory = sqlite3.Row`, `PRAGMA busy_timeout`, `PRAGMA foreign_keys = ON` (xác minh lại giá trị trả về `1` theo nguyên tắc fail-closed) và chế độ `WAL` kèm `synchronous = FULL` cho database trên đĩa. Tầng `core/` tiếp tục không import `sqlite3` hay `storage`.

---

## 2. Database Schema

Lược đồ phiên bản `1` trong [0001_initial.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0001_initial.sql) được suy xuất trực tiếp từ các hợp đồng trong `core/contracts/`, không tạo Project Runtime hay Task Runtime thứ hai:

- Toàn bộ các danh sách phụ trợ (`assigned_agent_ids_json`, `checklist_item_ids_json`, `artifact_ids_json`, `acceptance_criteria_json`, `metadata_json`, `session_ids_json`, `error_reference_json`, `redacted_payload_json`) đều gắn ràng buộc `CHECK (json_valid(...))`.
- Thời gian lưu nguyên chuỗi ISO-8601 có múi giờ và độ chính xác microsecond (`TEXT`), không ép thành epoch làm mất offset hay cắt ngắn mili-giây.
- Mọi chỉ số telemetry trong bảng `runs` giữ `NULL` khi thiếu dữ liệu từ provider (`CHECK` cấm kết hợp `telemetry_status = 'VERIFIED_MEASURED'` với `telemetry_source = 'unavailable'`), tuyệt đối không gán mặc định thành `0`.

---

## 3. Tables & Relationships

| Bảng | Khóa chính | Vai trò & Ranh giới |
| :--- | :--- | :--- |
| `schema_migrations` | `version` | Theo dõi phiên bản schema, tên script, SHA-256 checksum và thời điểm áp dụng |
| `projects` | `project_id` | Nhóm tổ chức `ProjectRef`, không chứa trạng thái thực thi riêng |
| `connections` | `(provider_id, connection_id)` | Metadata kết nối `ConnectionRef`, cấm chứa raw key hoặc chuỗi `=`/khoảng trắng trong `credential_ref` |
| `tasks` | `task_id` | Lưu `Task`, cột `version` cho kiểm soát đồng thời lạc quan (`P04.05`) và `baseline_json` bất biến cho replay |
| `runs` | `run_id` | Lưu `Run`, `version`, `baseline_json`, ràng buộc `UNIQUE (run_id, task_id)` và telemetry nullable |
| `sessions` | `session_id` | Lưu `Session` UUID v4 tách biệt khỏi `provider_conversation_id` và `legacy_session_ref` |
| `checklist_items` | `checklist_item_id` | Lưu `ChecklistItem`, ép `evidence_reference` và `completed_at` khi ở trạng thái `COMPLETED` |
| `artifacts` | `artifact_id` | Lưu `Artifact`, ràng buộc `VERIFIED_PASS` phải có đủ `evidence_reference` và `sha256` 64 ký tự |
| `events` | `sequence_number` (`AUTOINCREMENT`) | Nhật ký sự kiện chỉ ghi thêm với `event_id` duy nhất và ràng buộc có ít nhất 1 anchor |
| `notifications` | `notification_id` | Lưu `NotificationRef` liên kết bắt buộc tới `events(event_id)` và `tasks`/`runs` khi có |

---

## 4. Foreign Keys

- Mọi kết nối do `open_sqlite_connection()` mở đều ép `PRAGMA foreign_keys = ON`.
- Ràng buộc `tasks(active_run_id, task_id) -> runs(run_id, task_id) ON DELETE RESTRICT` kết hợp `UNIQUE (run_id, task_id)` trên `runs` ngăn tuyệt đối tình trạng Task A tham chiếu `active_run_id` của Run thuộc Task B tại tầng SQL, đồng thời cho phép `active_run_id IS NULL` theo chuẩn `MATCH SIMPLE` của SQLite.
- `sessions(run_id, task_id)` và `artifacts(run_id, task_id)` và `notifications(run_id, task_id)` đều tham chiếu cặp `(run_id, task_id)` trên `runs` để khóa chặt quyền sở hữu giữa Run và Task.
- **Ranh giới chưa dùng Foreign Key:**
  - `assigned_agent_id` / `owner_agent_id` / `actor_id` giữ dạng `TEXT` thay vì FK cứng vì Agent Registry thuộc `PHASE 05` chưa được triển khai (không tạo bảng giả).
  - Bảng `events` cố ý không đặt FK cứng lên `task_id`, `run_id`, `session_id`, `artifact_id` để giữ nguyên tính tương thích với giao thức `EventJournal` (`P04.03`) vốn cho phép ghi sự kiện hợp lệ chỉ neo theo `correlation_id` hoặc `actor_id` (tính đồng bộ thực thể được dịch vụ giao dịch tại `P04.05` bảo đảm).

---

## 5. Migration Versions

- Bộ phát hiện đọc các tệp `NNNN_snake_name.sql` trong `storage/db/migrations/` theo thứ tự tăng dần, kiểm tra tính liên tục phiên bản và băm SHA-256 toàn nội dung tệp.
- Hiện tại tồn tại 1 migration chuẩn: `0001_initial.sql` (phiên bản mục tiêu `1`).
- Chạy lại `apply_migrations()` trên database đã nâng cấp trả về `applied_versions=[]`, `skipped_versions=[1]` và bảo toàn nguyên vẹn dữ liệu hiện có.
- `verify_schema_compatibility()` và `apply_migrations()` chặn fail-closed với `CHECKSUM_MISMATCH`, `DOWNGRADE_FORBIDDEN` và `SCHEMA_VERSION_AHEAD`.

---

## 6. Migration Rollback

- Trình chạy migration trong [migrations.py](file:///C:/DoCho/AI-Village/storage/db/migrations.py) **không** sử dụng `sqlite3.Connection.executescript()` vì phương thức này tự động `COMMIT` ngầm trước khi thực thi script và phá vỡ cơ chế rollback nhiều câu lệnh.
- Thay vào đó, `split_sql_statements()` dùng `sqlite3.complete_statement()` để tách an toàn từng câu lệnh (bao gồm khối `CREATE TRIGGER ... BEGIN ... END;`) và chạy từng câu lệnh trong khối `with sqlite_transaction(conn, immediate=True):`.
- Kiểm thử thực tế đã chứng minh khi câu lệnh thứ hai trong một script migration lỗi, bảng vừa tạo ở câu lệnh đầu bị rollback hoàn toàn, `schema_migrations` giữ nguyên phiên bản `1` và dữ liệu có sẵn trong `tasks` còn nguyên vẹn.

---

## 7. Transaction Integrity

- `sqlite_transaction(conn, immediate=True)` dùng `BEGIN IMMEDIATE` (hoặc `SAVEPOINT` có định danh ngẫu nhiên khi lồng nhau), thực thi `COMMIT` khi thoát sạch và `ROLLBACK` khi phát sinh bất kỳ ngoại lệ nào.
- Kiểm thử chứng minh khi một giao dịch nhiều câu lệnh chèn `Task` mới rồi gặp lỗi `CHECK` ở câu lệnh cập nhật `Run` kế tiếp, `Task` mới bị hoàn tác hoàn toàn khỏi bảng `tasks`.

---

## 8. Event Append-Only

- Bảng `events` sử dụng `sequence_number INTEGER PRIMARY KEY AUTOINCREMENT` giúp bộ đếm tăng đơn điệu bền vững qua `sqlite_sequence` ngay cả khi đóng và mở lại kết nối database, kèm `event_id TEXT NOT NULL UNIQUE` và `CHECK` bắt buộc ít nhất một anchor.
- Hai trigger `trg_events_no_update` và `trg_events_no_delete` chặn mọi câu lệnh `UPDATE` và `DELETE` thông qua `RAISE(ABORT, ...)` ở mức SQL thông thường.
- **Lưu ý giới hạn bảo mật:** Trigger ngăn thao tác sửa/xóa qua đường SQL thông thường trên kết nối ứng dụng, nhưng không thể bảo vệ tuyệt đối trước chủ thể có quyền truy cập trực tiếp file `.db` trên hệ điều hành hoặc quyền `DROP TRIGGER`.

---

## 9. Indexes

`0001_initial.sql` tạo sẵn `16` chỉ mục phục vụ các truy vấn đọc cốt lõi của P04.05 và Mission Control về sau:
- `idx_tasks_status_priority`, `idx_tasks_project_id`
- `idx_runs_task_id_status`, `idx_runs_correlation_id`
- `idx_sessions_task_id`, `idx_sessions_run_id`, `idx_sessions_provider_conv`
- `idx_checklist_task_order`, `idx_artifacts_task_run`
- `idx_events_task_seq`, `idx_events_run_seq`, `idx_events_session_seq`, `idx_events_corr_seq`, `idx_events_type_seq`
- `idx_notifications_task_created`, `idx_notifications_event_id`

---

## 10. Metadata Security

- Bảng `connections` chỉ lưu `provider_id`, `connection_id`, `model_id`, `project_ref` và tay cầm `credential_ref`; ràng buộc `CHECK` trong SQL chặn các giá trị chứa `=`, khoảng trắng hoặc tiền tố khóa thô (`AIza`, `sk-`, `ghp_`, `gho_`, `github_pat_`, `bearer `, `-----BEGIN`).
- Toàn bộ bài kiểm thử chạy trong `tmp_path` của pytest; không có tệp `.db` thật nào được tạo trong root hay thư mục `storage/`.
- Kiểm tra ranh giới Chặng A tại [test_stage_a_baseline.py](file:///C:/DoCho/AI-Village/tests/stage_a/test_stage_a_baseline.py) giữ nguyên việc cấm code trong `storage/repositories/`, `storage/artifacts/`, `storage/knowledge/`, `agents/`, `tools/`, `skills/`, `api/`, `sandbox/`.

---

## 11. SQL Integration Tests

- **P04.04 suite (`python -m pytest tests/storage/test_p04_04_sqlite_migrations.py -v`):** **`7 passed`** (0 failed) với SQLite `3.50.4` thật trên `tmp_path`, kiểm chứng đủ: import sạch không ghi đĩa, đóng kết nối giải phóng transaction treo, fresh migration, `PRAGMA integrity_check` & `PRAGMA foreign_key_check`, idempotency, rollback migration lỗi giữa chừng, chặn downgrade/checksum mismatch/schema ahead, foreign key và ownership `Task.active_run_id <-> Run.task_id`, `events` append-only triggers & monotonic sequence sau reconnect, ràng buộc `Session`/`Artifact`/`Checklist`/`Notification`/`Connection`, bảo toàn `NULL` telemetry và độ chính xác timestamp.
- **Toàn suite offline (`python -m pytest tests -v -rs`):** **`127 passed, 24 skipped`** (0 failed; `22` ca live yêu cầu `AI_VILLAGE_RUN_LIVE=1` và `2` bài CrewAI skipped theo `global_crewai: False`).
- **`git diff --check`:** Thoát mã `0`, không lỗi khoảng trắng hoặc cuối dòng.

---

## 12. Limitations

1. **Chưa có tầng Repository và Service giao dịch (`P04.05`):** Hiện chỉ có tầng kết nối và migration trong `storage/db/`, chưa có `SQLiteRepositoryStore`, `SQLiteEventJournal` hay luồng khôi phục sau restart.
2. **Bảo vệ SQL chưa thay thế kiểm soát tệp OS:** Ràng buộc `CHECK` và `TRIGGER` hoạt động trong SQLite khi `foreign_keys=ON`, không ngăn được việc sửa trực tiếp file nhị phân `.db` từ ngoài ứng dụng.
3. **Chưa kiểm chứng live hay Gate C:** Toàn bộ bài kiểm thử P04.04 chạy offline với SQLite thật trong `tmp_path`.

---

## 13. Verdict

**PASS.** Toàn bộ thành phần nền tảng SQLite và migration có phiên bản của `PHASE 04 / P04.04` đáp ứng đầy đủ tiêu chí kỹ thuật, an toàn dữ liệu và vượt qua toàn bộ kiểm thử bắt buộc, đủ điều kiện chốt commit P04.04 và chuyển tiếp sang `P04.05`.
