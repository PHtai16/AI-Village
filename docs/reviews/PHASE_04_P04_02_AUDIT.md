# BÁO CÁO NGHIỆM THU PHASE 04 / P04.02: TASK STATE MACHINE: GUARDED TRANSITIONS & TRUTHFUL PROGRESS

- **Giai đoạn:** PHASE 04 / P04.02
- **Mục tiêu:** Triển khai máy trạng thái có kiểm soát cho `Task` và `Run` tại `core/tasks/` trên nền Core Contracts P04.01, bảo đảm hoàn thành dựa trên bằng chứng và projection trung thực cho Checklist, Kanban và Timing
- **Trạng thái cuối:** **PASS**
- **Hình thức kiểm chứng:** Offline unit tests và AST boundary validation, không chạy live `agy.exe` do thuần logic State Machine Python
- **HEAD tham chiếu lúc bắt đầu:** `00d2de223b97143a24609afb62b50aa6b344f612`

---

## 1. State Machine Architecture

Gói mới tại `core/tasks/` gồm `5` mô-đun thuần túy, bất biến với dữ liệu đầu vào và không phụ thuộc database:

| Mô-đun | Vai trò chính | Thành phần tiêu biểu |
| :--- | :--- | :--- |
| [transitions.py](file:///C:/DoCho/AI-Village/core/tasks/transitions.py) | Định nghĩa ma trận hợp lệ, bằng chứng và kết quả chuyển trạng thái | `TASK_ALLOWED_TRANSITIONS`, `RUN_ALLOWED_TRANSITIONS`, `QaVerificationEvidence`, `ExecutionEvidence`, `TransitionContext`, `TaskTransitionRequest`, `RunTransitionRequest`, `TaskTransitionResult`, `RunTransitionResult`, `PairedTransitionResult` |
| [guards.py](file:///C:/DoCho/AI-Village/core/tasks/guards.py) | Tập quy tắc kiểm tra fail-closed theo trình tự cố định | `evaluate_task_transition_guards`, `evaluate_run_transition_guards` |
| [state_machine.py](file:///C:/DoCho/AI-Village/core/tasks/state_machine.py) | Tạo bản ghi `Task`/`Run` mới bất biến, phát `Event` trong bộ nhớ và cung cấp thao tác ghép cặp nguyên tử | `transition_task`, `transition_run`, `start_task_run`, `complete_run_only`, `fail_task_run`, `timeout_task_run`, `cancel_task_run`, `qa_evidence_from_engine_verification`, `qa_evidence_from_runtime_result` |
| [projections.py](file:///C:/DoCho/AI-Village/core/tasks/projections.py) | Các hàm chiếu chỉ đọc phục vụ Kanban, Checklist và Timing | `project_kanban_column`, `project_kanban_board`, `project_checklist`, `project_task_timing` |
| [__init__.py](file:///C:/DoCho/AI-Village/core/tasks/__init__.py) | Điểm xuất công khai sạch cho tầng `core` | Tập biểu tượng công khai trong `__all__` |

Đường dẫn chính thức đi qua `transition_task`, `transition_run` và các hàm cặp phối hợp trả về bản thể mới sau khi Pydantic xác nhận toàn bộ hợp đồng P04.01, giữ nguyên bản đầu vào khi bị từ chối.

---

## 2. Task transition matrix

Ma trận chuyển trạng thái hợp lệ cho `TaskStatus`:

| Trạng thái nguồn | Đích được phép |
| :--- | :--- |
| `QUEUED` | `ASSIGNED`, `BLOCKED`, `CANCELLED` |
| `ASSIGNED` | `RUNNING`, `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `FAILED`, `CANCELLED` |
| `RUNNING` | `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `WAITING_INPUT` | `RUNNING`, `BLOCKED`, `FAILED`, `CANCELLED` |
| `WAITING_APPROVAL` | `RUNNING`, `BLOCKED`, `FAILED`, `CANCELLED` |
| `BLOCKED` | `QUEUED`, `ASSIGNED`, `RUNNING`, `FAILED`, `CANCELLED` |
| `COMPLETED`, `FAILED`, `CANCELLED` | Không cho phép chuyển tiếp |

Mọi nhảy cóc trái ma trận như `QUEUED -> COMPLETED`, `ASSIGNED -> COMPLETED`, hay mở lại `COMPLETED -> RUNNING` đều bị chặn với mã lỗi cấu trúc `INVALID_TRANSITION` hoặc `TERMINAL_STATE_MUTATION`.

---

## 3. Run transition matrix

Ma trận chuyển trạng thái độc lập cho `RunStatus`:

| Trạng thái nguồn | Đích được phép |
| :--- | :--- |
| `PENDING` | `RUNNING`, `BLOCKED`, `FAILED`, `CANCELLED` |
| `RUNNING` | `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT` |
| `WAITING_INPUT` | `RUNNING`, `BLOCKED`, `FAILED`, `CANCELLED`, `TIMED_OUT` |
| `WAITING_APPROVAL` | `RUNNING`, `BLOCKED`, `FAILED`, `CANCELLED`, `TIMED_OUT` |
| `BLOCKED` | `RUNNING`, `FAILED`, `CANCELLED`, `TIMED_OUT` |
| `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT` | Không cho phép chuyển tiếp |

Hàm `complete_run_only` chứng minh rõ ranh giới hai vòng đời: một `Run` chuyển sang `SUCCEEDED` sẽ kết thúc lượt chạy và giải phóng `active_run_id`, còn `Task` vẫn giữ trạng thái chưa hoàn thành cho tới khi có bước kiểm định QA hợp lệ để chuyển sang `COMPLETED`.

---

## 4. Guard rules

Các lớp bảo vệ được đánh giá tuần tự và thất bại theo hướng fail-closed trong [guards.py](file:///C:/DoCho/AI-Village/core/tasks/guards.py):

1. `timestamp_guard`: từ chối timestamp thiếu múi giờ bằng `INVALID_TIMESTAMP`, từ chối thời điểm quay ngược trước `created_at`, `updated_at`, `started_at`, `finished_at` bằng `INVALID_TIMESTAMP_ORDER`.
2. `payload_safety_guard`: lọc tên khóa bí mật và chuỗi khóa API trong `context.payload` và `context.reason`, trả `UNSAFE_EVENT_PAYLOAD`.
3. `stale_status_guard`: đối chiếu `expected_current_status` để chặn cập nhật lỗi thời với mã `STALE_EXPECTED_STATUS`.
4. `unknown_status_guard` và `duplicate_transition_guard`: chặn tên trạng thái ngoài tập chuẩn bằng `UNKNOWN_STATUS` và chặn chuyển lặp chính trạng thái hiện tại bằng `DUPLICATE_TRANSITION`.
5. `terminal_state_guard` và `transition_matrix_guard`: khóa sửa trạng thái cuối bằng `TERMINAL_STATE_MUTATION` và khóa cạnh không hợp lệ bằng `INVALID_TRANSITION`.
6. `identity_guard`: xác minh `run.task_id`, `task.active_run_id`, `checklist_items[*].task_id` và `artifacts[*].task_id` khớp cùng một `Task`, trả `WRONG_TASK_RUN_RELATIONSHIP`.
7. `source_trust_guard`: từ chối nguồn `llm_self_report` cho mọi chuyển đổi và từ chối `worker_agent` khi đích là `COMPLETED`.
8. `assignment_guard`, `execution_guard`, `cancellation_guard`, `qa_guard`: kiểm tra phân công Agent cho `ASSIGNED`, Run đang chạy có `started_at` cho `RUNNING`, lỗi có `error_reference` cho `Run.FAILED`, cờ xác nhận hủy `confirmed_cancelled=True` kèm `cancellation_source` cho `CANCELLED`, và bằng chứng QA cho `Task.COMPLETED`.

---

## 5. QA completion gate

Chuyển đổi `Task -> COMPLETED` bắt buộc thỏa đồng thời năm điều kiện tại `_check_task_qa_completion_guard`:

- Tồn tại `QaVerificationEvidence` độc lập, nếu thiếu sẽ trả `MISSING_QA_EVIDENCE`.
- `qa_evidence.verification_status` phải là `VERIFIED_PASS` với mã băm SHA-256 kỳ vọng và thực tế không mâu thuẫn.
- Không chấp nhận `actor_kind` là `llm_self_report` hay `worker_agent`, đồng thời cấm `verifier_actor_id` trùng với `owner_agent_id` của artifact đang nghiệm thu.
- Nếu đính kèm `Run` trong context thì `run.status` bắt buộc là `SUCCEEDED`; nếu `task.active_run_id` còn tồn tại mà thiếu `Run` cũng bị từ chối với `MISSING_RUN`.
- Mọi `ChecklistItem` không thuộc `SKIPPED`/`CANCELLED` đều phải đạt `COMPLETED` kèm `evidence_reference` và `completed_at`; mọi `Artifact` đính kèm đều phải đạt `VERIFIED_PASS` và có `evidence_reference`.

---

## 6. Active Run consistency

Tính nhất quán giữa `Task.active_run_id` và `Run` được duy trì bằng các hàm ghép cặp trong [state_machine.py](file:///C:/DoCho/AI-Village/core/tasks/state_machine.py):

- `start_task_run` đưa `Run` từ `PENDING -> RUNNING` để có `started_at` hợp lệ trước khi đặt `Task.status="RUNNING"` cùng `Task.active_run_id = run.run_id`.
- `complete_run_only` kết thúc `Run` sang `SUCCEEDED` đồng thời đặt `Task.active_run_id = None` mà không đóng `Task`.
- `fail_task_run`, `timeout_task_run` và `cancel_task_run` kết thúc đồng thời `Run` và `Task`, luôn giải phóng `active_run_id = None`.
- Nếu bất kỳ bước nào trong cặp thất bại, `PairedTransitionResult` trả về `accepted=False`, không cập nhật nửa vời và bảo đảm không vi phạm invariant của P04.01 `Task`. Cơ chế atomic SQLite transaction sẽ hoàn thiện tại `P04.04` đến `P04.05`.

---

## 7. Event emission

Khi chuyển đổi hợp lệ trong bộ nhớ:

- Chuyển đổi `Task` phát `Event` chuẩn [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py) mang `event_type="task.status_changed"`, `source` ánh xạ chính xác theo `actor_kind`, `task_id`, `run_id`, `timestamp`, `artifact_id`, `evidence_id`, và `redacted_payload` chứa `previous_status`, `new_status`, `actor_kind`, `reason`.
- Chuyển đổi `Run` chỉ phát `run.started`, `run.completed`, `run.failed`, `run.cancelled`, `run.timed_out` đúng bản chất sự kiện thật.
- **Ghi nhận khoảng trống hợp đồng Event:** P04.01 `EventType` chưa định nghĩa loại `run.status_changed` cho các chuyển đổi trung gian của `Run` sang `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED` hoặc phục hồi về `RUNNING`. Thay vì gắn sai `run.completed` hay `run.failed`, `transition_run` đặt `event=None` và ghi rõ lý do trong `event_omitted_reason`. Kiến nghị bổ sung giá trị `"run.status_changed"` vào `EventType` theo hướng tương thích ngược ở đầu `P04.03`.

---

## 8. Checklist projection

Hàm `project_checklist` tại [projections.py](file:///C:/DoCho/AI-Village/core/tasks/projections.py) chỉ đọc danh sách `ChecklistItem` thuộc đúng `task_id`:

- Chỉ tính vào `verified_completed_items` những bước mang `status="COMPLETED"` kèm đồng thời `evidence_reference` và `completed_at`.
- Xuất `ratio_display` dạng `"số bước đạt / tổng số bước"` cùng `all_required_items_verified`, khóa cứng `implies_task_completion = False` và không sinh trường phần trăm tiến độ giả.

---

## 9. Kanban mapping

Hàm `project_kanban_column` và `project_kanban_board` ánh xạ read-only `9` giá trị `TaskStatus` vào đúng `6` cột chuẩn trong mục `4.2` của [MISSION_CONTROL_UX.md](file:///C:/DoCho/AI-Village/docs/architecture/MISSION_CONTROL_UX.md):

- `queued` tương ứng nhãn `"Chờ"`: `QUEUED`, `ASSIGNED`.
- `in_progress` tương ứng nhãn `"Đang thực hiện"`: `RUNNING`, `WAITING_INPUT`.
- `waiting_approval` tương ứng nhãn `"Chờ phê duyệt"`: `WAITING_APPROVAL`.
- `blocked` tương ứng nhãn `"Bị chặn"`: `BLOCKED`.
- `failed` tương ứng nhãn `"Thất bại"`: `FAILED`, `CANCELLED`.
- `completed` tương ứng nhãn `"Hoàn thành"`: `COMPLETED`.

Mỗi `KanbanTaskCard` và `counts_by_status` giữ nguyên giá trị `TaskStatus` gốc để không đánh đồng các trạng thái khác nghĩa.

---

## 10. Timing calculations

Hàm `project_task_timing` tính toàn bộ số đo từ `task.created_at`, `run.started_at`, `run.finished_at`, chuỗi `task.status_changed` trong `events` và mốc `observed_at` có múi giờ truyền tường minh:

- Khi `Task` chưa khởi chạy bất kỳ `Run` nào, `first_started_at`, `elapsed_ms` và `total_completed_duration_ms` đều là `None`.
- Khi `Task` đang chạy ở một pha chờ như `WAITING_APPROVAL`, `WAITING_INPUT` hoặc `BLOCKED`, hàm cộng dồn thời gian từ các sự kiện thật tới sự kiện đổi trạng thái tiếp theo hoặc `observed_at`.
- Chỉ ghi `total_completed_duration_ms` khi `Task` ở trạng thái kết thúc và có đủ mốc bắt đầu với kết thúc thật. Khóa cứng `estimated_completion_at = None` và `eta_available = False`.

---

## 11. Invalid transition handling

Mọi tình huống lỗi gồm `INVALID_TRANSITION`, `UNKNOWN_STATUS`, `TERMINAL_STATE_MUTATION`, `MISSING_RUN`, `WRONG_TASK_RUN_RELATIONSHIP`, `MISSING_ASSIGNMENT`, `MISSING_QA_EVIDENCE`, `UNVERIFIED_COMPLETION`, `UNTRUSTED_TRANSITION_SOURCE`, `INVALID_TIMESTAMP`, `INVALID_TIMESTAMP_ORDER`, `MISSING_EXECUTION_EVIDENCE`, `CANCELLATION_NOT_CONFIRMED`, `DUPLICATE_TRANSITION`, `STALE_EXPECTED_STATUS`, `UNSAFE_EVENT_PAYLOAD` và `CONTRACT_VALIDATION_ERROR` đều trả về đối tượng `TransitionResult` với `accepted=False`, `task=None`/`run=None`, `event=None`, giữ nguyên hoàn toàn đối tượng gốc và không phát sinh sự kiện thành công giả.

---

## 12. AgentRuntime compatibility

Các thành phần PHASE 03 tại `core/runtime/` và Core Contracts tại `core/contracts/` được giữ nguyên hoàn toàn. Hai hàm thuần `qa_evidence_from_engine_verification` và `qa_evidence_from_runtime_result` tại [state_machine.py](file:///C:/DoCho/AI-Village/core/tasks/state_machine.py) kết nối `EngineArtifactVerificationResult` và `AgentRuntimeResult` sang `QaVerificationEvidence`, đồng thời chặn fail-closed nếu `AgentRuntimeResult` thiếu `qa_verification` hoặc thiếu `business_verified=True` với `status="DELIVERED"`.

---

## 13. Security boundaries

- `validate_safe_payload` và `ensure_not_secret` ngăn lọt chuỗi khóa API hoặc khóa từ điển nhạy cảm vào `Event` chuyển trạng thái; mọi thông báo lỗi trong `TransitionError` đều đi qua `redact_secrets`.
- Kiểm tra AST tự động xác nhận `core/tasks/` không import `sqlite3`, `sqlalchemy`, `storage`, `api`, `ui`, `crewai`, `httpx`, `urllib` hay `subprocess`.
- `tests/stage_a/test_stage_a_baseline.py` chỉ mở allowlist thêm đúng `core/tasks`, giữ nguyên khóa với `storage/`, `api/`, `agents/`, `tools/`, `skills/` và `sandbox/`.

---

## 14. Unit/regression results

- **Bộ unit test P04.02:** `test_p04_02_task_state_machine_unit.py` gồm `6` ca kiểm thử offline bao phủ `34` hạng mục yêu cầu bằng fixture Python thuần trong [test_p04_02_task_state_machine_unit.py](file:///C:/DoCho/AI-Village/tests/core/test_p04_02_task_state_machine_unit.py), đạt `6 passed`.
- **Bộ kiểm thử hồi quy toàn hệ thống:** `python -m pytest tests -v -rs` đạt `108 passed, 24 skipped` với `0` failure và `0` error; `24` ca skipped gồm `22` live tests cần `AI_VILLAGE_RUN_LIVE=1` và `2` bài CrewAI bỏ qua do `global_crewai: False`.
- **Phạm vi bằng chứng:** mọi quy tắc ma trận chuyển đổi, guard, QA completion gate, active_run consistency và projection đều đã được kiểm chứng bằng unit test offline; chưa đánh dấu `LIVE_VERIFIED` vì chưa nối SQLite persistence hay tiến trình `agy.exe` dài hạn.

---

## 15. Những phần để lại cho P04.03–P04.07

1. **`P04.03` Event Journal & Replay:** ghi nhận append-only các `Event` do State Machine phát sinh và xem xét mở rộng thêm `run.status_changed` vào `EventType`.
2. **`P04.04`–`P04.05` SQLite Migrations & Repositories:** bảo đảm cập nhật đồng bộ `Task`, `Run`, `ChecklistItem` và `Event` qua transaction nguyên tử bền vững sau restart.
3. **`P04.06` Cancellation, Timeout, Retry & Fallback:** nối tín hiệu hủy thực tế từ `AntigravityProcessManager` và cơ chế tạo `Run` thử lại hợp lệ sau `FAILED` hoặc `TIMED_OUT`.
4. **`P04.07` Permission Manager & Governance:** xác thực quyền người gọi cho chuyển cột Kanban, hủy tác vụ và phê duyệt hành động nhạy cảm trước khi gọi State Machine.

---

## 16. Verdict

**PASS.** Máy trạng thái `Task` và `Run` tại `core/tasks/` đáp ứng đầy đủ tiêu chí kiểm chứng offline cho `PHASE 04 / P04.02`, bảo toàn `P04.01` và `AgentRuntime` của `PHASE 03`, sẵn sàng làm nền trực tiếp cho Event Journal tại `P04.03`.
