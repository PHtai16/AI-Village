# BÁO CÁO NGHIỆM THU PHASE 04 / P04.03: EVENT JOURNAL, DETERMINISTIC REPLAY & ACTIVITY TIMELINE

- **Giai đoạn:** PHASE 04 / P04.03
- **Mục tiêu:** Xây dựng nhật ký sự kiện chỉ ghi thêm trong bộ nhớ tại `core/events/`, giải quyết khoảng trống `run.status_changed`, triển khai replay xác định và dựng Activity Timeline chỉ đọc
- **Trạng thái cuối:** **PASS**
- **Hình thức kiểm chứng:** Offline unit tests và AST boundary validation, chạy hoàn toàn trong bộ nhớ, chưa nối SQLite hay live `agy.exe`
- **HEAD tham chiếu lúc bắt đầu:** `c45c2ad3955300ba172c09057e17cfa0e2b70964`

---

## 1. Event Journal Architecture

Gói `core/events/` tổ chức theo `4` mô-đun thuần Python không phụ thuộc SQLite:

| Mô-đun | Thành phần chính | Trách nhiệm |
| :--- | :--- | :--- |
| [journal.py](file:///C:/DoCho/AI-Village/core/events/journal.py) | `EventJournal`, `InMemoryEventJournal`, `JournalEntry`, `AppendResult`, `BatchAppendResult`, `JournalError`, các hàm `record_*` | Ghi nối tiếp bất biến trong bộ nhớ, gán `sequence_number` đơn điệu, chống trùng ID và bảo vệ batch |
| [replay.py](file:///C:/DoCho/AI-Village/core/events/replay.py) | `validate_journal_entries`, `replay_task_history`, `replay_run_history`, `TaskReplayResult`, `RunReplayResult`, `ReplayIssue` | Xác minh tính toàn vẹn chuỗi sự kiện, phát hiện lịch sử thiếu hoặc mâu thuẫn và tái dựng khi đủ baseline |
| [projections.py](file:///C:/DoCho/AI-Village/core/events/projections.py) | `TimelineEntry`, `ActivityTimelineProjection`, `project_activity_timeline`, `project_timeline_from_entries` | Dựng góc nhìn Activity Timeline chỉ đọc theo thứ tự ghi nhận với con trỏ `sequence_number` |
| [__init__.py](file:///C:/DoCho/AI-Village/core/events/__init__.py) | `__all__` | Xuất công khai các kiểu dữ liệu và hàm chính của tầng `core.events` |

---

## 2. Append-Only Contract

Giao diện `EventJournal` và lớp `InMemoryEventJournal` khóa chặt mô hình chỉ ghi thêm:

- Chỉ cung cấp `append`, `append_batch`, `read_entries`, `get_by_event_id` và `count`; hoàn toàn không tồn tại API sửa, ghi đè, xóa hay làm rỗng sự kiện đã ghi.
- Lớp `JournalEntry` dùng `ConfigDict(extra="forbid", frozen=True)`.
- Mọi thao tác ghi đều nhân bản sâu bằng vòng lặp JSON `Event.model_validate_json(event.model_dump_json())` và mọi thao tác đọc đều trả bản sao độc lập qua `_clone_entry`, ngăn chặn việc người gọi thay đổi `redacted_payload` từ bên ngoài làm biến dạng lịch sử trong Journal.

---

## 3. Ordering & Sequence Strategy

Thứ tự sự kiện được phân tách rõ ràng giữa thời điểm phát sinh và thứ tự lưu trữ:

- `Event.timestamp` giữ thời điểm thực có múi giờ khi sự kiện xảy ra.
- `JournalEntry.sequence_number` là số nguyên tăng đơn điệu từ `1`, độc lập hoàn toàn với đồng hồ timestamp hay chuỗi `event_id`.
- `InMemoryEventJournal` dùng `threading.RLock()` bảo vệ quá trình cấp phát `sequence_number` và cập nhật `_entries`, bảo đảm an toàn khi nhiều luồng ghi song song trong cùng tiến trình.

---

## 4. Duplicate Event Protection

`InMemoryEventJournal.append` đối chiếu mọi bản ghi mới với chỉ mục `_by_event_id`:

- **Trùng `event_id` kèm nội dung giống hệt:** trả `AppendResult` với `status="IDEMPOTENT_DUPLICATE"`, `appended=False`, `is_duplicate=True` và bản sao `entry` cũ, không tăng bộ đếm thứ tự và không ghi thành bản ghi thứ hai.
- **Trùng `event_id` nhưng sai khác nội dung:** từ chối fail-closed với `status="REJECTED"`, `appended=False`, `is_duplicate=True` và mã lỗi `DUPLICATE_EVENT_CONFLICT`.
- **Sự kiện lỗi schema, thiếu anchor, thiếu múi giờ hoặc lộ secret:** bị chặn trước khi cấp `sequence_number`, trả `MISSING_ATTRIBUTION_ANCHOR`, `INVALID_TIMESTAMP`, `UNSAFE_PAYLOAD` hoặc `INVALID_EVENT`.

---

## 5. Batch Atomicity Trong Bộ Nhớ

Hàm `append_batch` thi hành kiểm tra hai pha trong bộ nhớ:

- Chặn danh sách rỗng bằng mã `EMPTY_BATCH`.
- Kiểm tra toàn bộ schema từng phần tử, trùng `event_id` nội bộ trong batch và va chạm với `_by_event_id` hiện có trước khi cấp `sequence_number`.
- Nếu bất kỳ phần tử nào không hợp lệ, toàn bộ batch bị hủy bỏ, bộ đếm `sequence_number` giữ nguyên không nhảy số và không có bản ghi nửa vời nào xuất hiện trong `_entries`.
- Tính nguyên tử này chỉ áp dụng trong phạm vi bộ nhớ RAM hiện tại, chưa thay thế transaction đĩa của SQLite ở `P04.04` đến `P04.05`.

---

## 6. `run.status_changed` Compatibility

Khoảng trống sự kiện chuyển đổi trung gian của `Run` phát hiện ở `P04.02` đã được khắc phục tối thiểu:

- Bổ sung `"run.status_changed"` vào `EventType` trong [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py), giữ nguyên toàn bộ các loại sự kiện cũ và validator hiện có.
- Cập nhật hàm `transition_run` tại [state_machine.py](file:///C:/DoCho/AI-Village/core/tasks/state_machine.py) để giữ nguyên `run.started`, `run.completed`, `run.failed`, `run.cancelled`, `run.timed_out`, đồng thời phát `run.status_changed` kèm `previous_status` và `new_status` cho `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED` và các bước phục hồi `RUNNING`.
- Cập nhật assertions tương ứng tại [test_p04_02_task_state_machine_unit.py](file:///C:/DoCho/AI-Village/tests/core/test_p04_02_task_state_machine_unit.py) và chạy lại toàn bộ bộ test `P04.01` và `P04.02` để xác nhận tương thích ngược.

---

## 7. Task/Run Event Integration

Bốn hàm cầu nối tại [journal.py](file:///C:/DoCho/AI-Village/core/events/journal.py) liên kết đầu ra của State Machine và AgentRuntime vào Journal:

- `record_task_transition` và `record_run_transition` chỉ ghi khi `result.accepted` đạt `True` và có `Event` hợp lệ; nếu guard từ chối chuyển trạng thái, hàm trả về `MISSING_TRANSITION_EVENT` mà không ghi thêm gì vào Journal.
- `record_paired_transition` ghi trọn bộ sự kiện từ `start_task_run`, `complete_run_only`, `fail_task_run`, `timeout_task_run` hoặc `cancel_task_run` bằng `append_batch`.

---

## 8. Replay Model & Baseline Requirements

Bộ replay trong [replay.py](file:///C:/DoCho/AI-Village/core/events/replay.py) thực thi xác định theo thứ tự `JournalEntry` mà không phát sinh sự kiện phụ trong lúc đọc:

- `replay_task_history` theo dõi chuỗi trạng thái `Task`, vòng đời liên kết `Run`, sự kiện chờ/chặn, hồ sơ QA và công cụ đã quan sát.
- `replay_run_history` theo dõi độc lập chuỗi trạng thái `Run` và các mốc `started_at`, `finished_at`.
- **Nguyên tắc không bịa dữ liệu nền:** khi thiếu `baseline_task` hoặc `baseline_run`, replay không tự bịa `title`, `description` hay `requested_by`, mà đặt `status="INCOMPLETE_HISTORY"`, `can_reconstruct_full_task=False` kèm cảnh báo `MISSING_BASELINE`. Chỉ khi có đủ baseline hợp lệ và không vi phạm tính nhất quán mới trả về `VALID` cùng bản thể tái dựng.

---

## 9. Replay Validation

Hàm `validate_journal_entries`, `replay_task_history` và `replay_run_history` kiểm tra và chặn fail-closed toàn bộ `18` mã lỗi replay khi phát hiện lịch sử hỏng:

- Trình tự vật lý: `DUPLICATE_SEQUENCE`, `NON_MONOTONIC_SEQUENCE`, `SEQUENCE_GAP`, `DUPLICATE_EVENT_ID`.
- Thời gian và cấu trúc: `INVALID_TIMESTAMP`, `TIMESTAMP_ORDER_ANOMALY`, `INVALID_EVENT_SCHEMA`, `UNSAFE_PAYLOAD`.
- Ranh giới sở hữu và máy trạng thái: `TASK_ID_MISMATCH`, `RUN_ID_MISMATCH`, `PREVIOUS_STATUS_MISMATCH`, `UNKNOWN_STATUS`, `INVALID_TRANSITION`, `TERMINAL_STATE_MUTATION`.
- Tính xác thực QA: `UNVERIFIED_QA_PASS` và `UNTRUSTED_SOURCE_OR_ACTOR` khi một sự kiện tự nhận QA PASS hoặc hoàn thành Task từ nguồn `provider`, `agent_runtime`, `llm_self_report` hay thiếu `evidence_id`.

---

## 10. Activity Timeline Projection

Hàm `project_activity_timeline` và `project_timeline_from_entries` trong [projections.py](file:///C:/DoCho/AI-Village/core/events/projections.py) đọc trực tiếp từ `EventJournal`:

- Xuất `TimelineEntry` gồm đủ `event_id`, `sequence_number`, `timestamp`, `event_type`, `task_id`, `run_id`, `session_id`, `actor_id`, `source`, `previous_status`, `new_status`, `artifact_id`, `evidence_id`, `tool_name`, `provider_id`, `connection_id`, `model_id`, `correlation_id`, `legacy_session_ref` và `summary` tạo theo mẫu cố định đã lọc secrets.
- Chỉ trích xuất `tool_name` từ các sự kiện quan sát công cụ có cấu trúc như `tool.call_observed`, `tool.call_completed` và `engine.event_forwarded` thuộc nguồn thực thi, bỏ qua lời đề xuất `tool.call_proposed` và văn bản tự sự của LLM.
- Dùng `last_sequence` làm con trỏ truy vấn ổn định khi có thêm sự kiện mới.

---

## 11. AgentRuntime/Event Mapping

Hàm `record_runtime_event` tái sử dụng trực tiếp `core_event_from_runtime_event` tại [compat.py](file:///C:/DoCho/AI-Village/core/contracts/compat.py) mà không thay đổi `CustomAgentRuntime`:

- Bảo toàn dấu thời gian thật từ `EngineEvent.timestamp` hoặc đối số tường minh; từ chối ghi với mã `INVALID_TIMESTAMP` nếu thiếu mốc thời gian thật.
- Tách bạch `Session.session_id` chuẩn UUID v4 với mã `sess-*` cũ của `P03.01` đến `P03.03` thông qua khóa `legacy_session_ref` trong `redacted_payload`.

---

## 12. QA Independence

- Journal và Replay tách biệt kết quả hoàn thành kỹ thuật `run.completed` khỏi `qa.verification_recorded` và `Task.COMPLETED`: việc ghi nhận `run.completed` đưa `Run` về `SUCCEEDED` trong khi `Task` vẫn giữ trạng thái chưa hoàn thành cho tới khi có sự kiện `Task` hợp lệ gắn `evidence_id` độc lập.
- Sự kiện `qa.verification_recorded` ghi `VERIFIED_PASS` bị đánh dấu `INVALID_HISTORY` ngay nếu thiếu `evidence_id` hoặc không xuất phát từ `source="qa_verifier"`.

---

## 13. Secret Safety

- Toàn bộ `Event` đi qua `validate_safe_payload` khi khởi tạo lẫn khi nạp lại qua `_clone_event` trong Journal và qua `validate_journal_entries` lúc Replay.
- Mọi thông báo lỗi `JournalError` và `ReplayIssue`, cũng như chuỗi `summary` trên `TimelineEntry`, đều đi qua `redact_secrets`.
- Kiểm tra AST tự động xác nhận `core/events/` hoàn toàn không import `sqlite3`, `sqlalchemy`, `storage`, `api`, `ui`, `crewai`, `httpx`, `urllib` hay `subprocess`.

---

## 14. Unit/Regression Tests

- **Unit P04.03:** `12` ca kiểm thử tách bạch trong [test_p04_03_event_journal_unit.py](file:///C:/DoCho/AI-Village/tests/core/test_p04_03_event_journal_unit.py) đạt `12 passed`, bao phủ đủ `40` tiêu chí từ append-only, sequence đơn điệu, concurrency, duplicate/conflict, batch nguyên tử, replay hợp lệ, phát hiện lịch sử hỏng, `run.status_changed`, timeline projection, AgentRuntime bridge, tool attribution, QA independence và secret redaction.
- **Unit P04.02:** `6 passed` sau khi cập nhật `run.status_changed`.
- **Tổng suite offline và standards:** chạy `python -m pytest tests -v -rs` đạt `120 passed, 24 skipped` với `0` failure; `24` ca skipped gồm `22` live tests yêu cầu `AI_VILLAGE_RUN_LIVE=1` và `2` bài CrewAI bỏ qua do `global_crewai: False`.

---

## 15. In-Memory Limitations

1. **Chưa bền vững qua restart:** `InMemoryEventJournal` chỉ tồn tại trong bộ nhớ tiến trình; khi tiến trình kết thúc, dữ liệu sự kiện không được giữ lại.
2. **Không thay thế database ACID:** cơ chế `append_batch` với `RLock` bảo vệ tính nguyên tử giữa các thread trong RAM, nhưng chưa đồng bộ ghi đĩa cùng bảng `Task` và `Run`.
3. **Chưa kiểm chứng live hay Gate C:** P04.03 chỉ xác minh offline trên unit tests, chưa đánh dấu `LIVE_VERIFIED` hay nghiệm thu Gate C.

---

## 16. SQLite Integration Plan cho P04.04–P04.05

1. **Schema migration tại `P04.04`:** tạo bảng `events` với khóa chính `event_id`, cột `sequence_number INTEGER PRIMARY KEY AUTOINCREMENT` hoặc chỉ mục thứ tự toàn cục đơn điệu, khóa ngoại tới `tasks`/`runs`, cùng cơ chế chặn `UPDATE`/`DELETE` hoặc chỉ mở hàm `append`.
2. **Repository implementation tại `P04.05`:** cài đặt lớp `SqliteEventJournal` triển khai cùng giao thức `EventJournal` và ghép một transaction SQLite cho thao tác cập nhật `Task`/`Run` kèm `append_batch(events)`, giúp tái dựng toàn bộ trạng thái từ `baseline` và `Event Journal` sau restart.

---

## 17. Verdict

**PASS.** Giai đoạn `PHASE 04 / P04.03` hoàn thiện `EventJournal` chỉ ghi thêm trong bộ nhớ, khắc phục trọn vẹn khoảng trống `run.status_changed`, cung cấp bộ `replay_task_history` / `replay_run_history` xác định kèm `ActivityTimelineProjection` và vượt qua toàn bộ kiểm thử offline, sẵn sàng bước sang `P04.04` SQLite migrations.
