# BÁO CÁO NGHIỆM THU PHASE 04 / P04.06: WORKFLOW LIFECYCLE, CANCELLATION, TIMEOUT, FALLBACK & IDEMPOTENCY

- **Giai đoạn:** PHASE 04 / P04.06
- **Mục tiêu:** Triển khai bộ điều phối vòng đời workflow (`WorkflowLifecycleController`), phân tách yêu cầu hủy khỏi xác nhận dừng tiến trình thuộc quyền sở hữu, timeout có bằng chứng, idempotency bền vững trên SQLite, chính sách fallback và đối chiếu sau khởi động lại
- **Trạng thái cuối:** **PASS**
- **Hình thức kiểm chứng:** Offline unit tests (`4 passed`) + SQLite & owned subprocess integration tests (`5 passed`) + toàn bộ suite hồi quy (`149 passed, 24 skipped`), chưa chạy `agy.exe` live trong batch này
- **HEAD tham chiếu:** `57b9a44eef0e98e954c63ba58f7c1c957bae71f7`
- **Commit mục tiêu:** `feat(core): implement workflow lifecycle and idempotency`

---

## 1. Các Chức Năng Đã Kiểm Chứng

- **VERIFIED (SQLite thật + tiến trình con thật trên Windows):**
  - Idempotent run submission với khóa `(scope, idempotency_key)` trong [0002_workflow_lifecycle.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0002_workflow_lifecycle.sql) và giao dịch đơn nguyên tử tại [lifecycle_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/lifecycle_repo.py): `4` luồng gửi đồng thời tạo đúng `1` Run mới và `3` replay; gửi lại sau restart khớp `IDEMPOTENT_REPLAY`; sai khác payload hoặc Task ID trả `CONFLICT_REJECTED`.
  - Hủy tiến trình con thuộc quyền sở hữu qua `OwnedSubprocessHandle` (`Win32JobGuard` + `ProcessHandleController` tại [lifecycle.py](file:///C:/DoCho/AI-Village/engines/antigravity/lifecycle.py)): dừng đúng PID thuộc execution đã đăng ký (`is_pid_alive(pid) == False`), giữ nguyên tiến trình không liên quan (`is_pid_alive(sibling) == True`), từ chối sai `owner_token` hoặc sai `task_id` (`OWNERSHIP_REJECTED`), và tách rõ hai sự kiện `workflow.cancel_requested` trước `run.cancelled`.
  - Kiểm tra timeout có deadline thực, dừng tiến trình trước khi ghi `Run.TIMED_OUT`, xử lý race đồng thời timeout-vs-cancel và completion-vs-cancel chỉ cho phép một kết quả terminal duy nhất.
  - Khôi phục sau restart khi tác vụ đang `RUNNING`: `reconcile_interrupted_task()` trả `NEEDS_RECONCILIATION` (`process_observable=False`) và ghi `workflow.reconciliation_recorded`, không tự biến thành `COMPLETED` hay `FAILED`; yêu cầu cancel sau restart khi mất handle tiến trình trả `PROCESS_NOT_OBSERVABLE`, không giả mạo `CANCELLED`.
- **UNIT_VERIFIED:**
  - Chặn `CANCEL_FAILED` khi handle chưa xác nhận dừng và `ALREADY_TERMINAL` khi execution đã xong trước lúc hủy.
  - Bộ đánh giá fallback [fallback.py](file:///C:/DoCho/AI-Village/core/tasks/fallback.py): chặn xoay vòng API key (`KEY_ROTATION_FORBIDDEN`), vượt `max_retries` (`RETRY_BUDGET_EXHAUSTED`), thao tác không idempotent, target chưa cấu hình, thiếu năng lực, và yêu cầu `APPROVAL_REQUIRED` khi chuyển `local_only -> cloud`, đổi billing scope hoặc vượt ngân sách.
  - Tích hợp `AntigravityOwnedExecutionHandle` với `AntigravityProcessManager` qua `PopenFactory` mô phỏng tiến trình ngủ chuẩn Python trên `tmp_path` (offline, không gọi `agy.exe` live).

---

## 2. Giới Hạn Của Cancellation & Runtime Capability Flags

- `CustomAgentRuntime.get_runtime_info()` tiếp tục giữ **`supports_workflow_cancellation=False`** vì điều phối P03.01 (`run_master_worker_spike`) chạy đồng bộ theo từng lượt; không nâng cờ này lên `True` sai sự thật.
- Ở tầng `WorkflowLifecycleController`, cancellation hoạt động thật tại:
  1. Điểm kiểm tra trước khi bắt đầu bước mới (`check_checkpoint_can_continue()`).
  2. Tiến trình con đang chạy có `OwnedExecutionHandle` đăng ký quyền sở hữu trong cùng tiến trình điều phối.
- Không hỗ trợ OS-level `PAUSE/RESUME` giữa một lệnh CLI đang chạy và không bao giờ hủy tiến trình theo tên toàn cục như `taskkill /IM agy.exe /F`.

---

## 3. Process Termination, Idempotency, Fallback & Recovery Evidence

- **Schema version mới:** `2` (`0002_workflow_lifecycle.sql` bổ sung bảng `idempotency_keys` và `run_executions`; giữ nguyên `0001_initial.sql`).
- **EventType mới tương thích ngược:** `workflow.cancel_requested`, `workflow.fallback_evaluated`, `workflow.reconciliation_recorded` trong [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py).
- **Ranh giới kiến trúc:** kiểm tra AST xác nhận toàn bộ `core/tasks/` không import `sqlite3`, `storage`, `sqlalchemy`, `subprocess` hay `crewai`.

---

## 4. Test Results & Git Diff Summary

- `python -m pytest tests/core/test_p04_06_workflow_lifecycle_unit.py tests/integration/test_p04_06_workflow_lifecycle.py tests/storage -v`: **`29 passed`** (`4` unit P04.06 + `5` integration P04.06 + `20` storage).
- `python -m pytest tests -v -rs`: **`149 passed, 24 skipped`** (`0` failed; `22` ca live chờ `AI_VILLAGE_RUN_LIVE=1`, `2` ca CrewAI bỏ qua).
- `git diff --check`: Thoát mã `0` sau khi sửa dòng trống cuối tệp `engines/antigravity/lifecycle.py`.

---

## 5. Verdict

**PASS.** Giai đoạn `PHASE 04 / P04.06` đạt toàn bộ tiêu chí kiểm chứng, sẵn sàng tạo commit và chuyển tiếp sang `P04.07` Governance & Human Approval Gate.
