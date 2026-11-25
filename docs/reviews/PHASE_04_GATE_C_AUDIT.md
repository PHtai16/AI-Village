# Báo cáo Kiểm toán Nghiệm thu Gate C — Phase 04 (Agent Runtime, State Machine, Persistence & Governance)

**Ngày kiểm toán:** 2026-11-26  
**Nhánh / Repository:** `main` (`PHtai16/AI-Village`)  
**Các commit Phase 04 đã kiểm chứng:**
- `P04.04`: `759558e` (`feat(storage): establish versioned SQLite schema and migrations`)
- `P04.05`: `57b9a44` (`feat(storage): implement persistent repositories and recovery`)
- `P04.06`: [`984f1c963b119736a8f5981a1b582d9744ebcd74`](file:///C:/DoCho/AI-Village/docs/reviews/PHASE_04_P04_06_AUDIT.md) (`feat(core): implement workflow lifecycle and idempotency`)
- `P04.07`: [`37a333941624ee576c763d7b74ba3c1a1ff2aca1`](file:///C:/DoCho/AI-Village/docs/reviews/PHASE_04_P04_07_AUDIT.md) (`feat(governance): implement permission and approval gates`)

**Kết quả nghiệm thu Gate C:** **PASS (Offline End-to-End Verified)**

---

## 1. Phân loại Trung thực Offline và Live Verification

- **Chế độ Offline (`OFFLINE_VERIFIED_PASS`):** Toàn bộ 165 bài kiểm thử đơn vị, lưu trữ SQLite thật, tiến trình con hệ điều hành thật qua `OwnedSubprocessHandle`, kiểm quyền Governance, tiêu thụ phê duyệt một lần, xác minh mã băm SHA-256 độc lập qua `verify_engine_artifact`, và tái dựng trạng thái sau restart đều đã chạy thực thi và đạt 100% với 0 thất bại.
- **Chế độ Live Provider CLI (`NOT_EXECUTED_IN_THIS_SESSION`):** Không gán nhãn `LIVE_VERIFIED` cho Gate C trong phiên làm việc này. Toàn bộ 22 bài kiểm thử `*live*.py` (yêu cầu cờ `AI_VILLAGE_RUN_LIVE=1`) cùng 2 bài CrewAI benchmark nằm trong trạng thái `SKIPPED` có chủ đích nhằm giữ quy trình kiểm chứng tất định, không phụ thuộc mạng hay hạn mức API bên ngoài.

---

## 2. Ma trận Nghiệm thu 14 Tiêu chí Phase 04 & Gate C

| STT | Tiêu chí Nghiệm thu | Thành phần Mã nguồn Xác minh | Bằng chứng Kiểm thử Thực tế | Trạng thái |
| :--- | :--- | :--- | :--- | :--- |
| 1 | Hợp đồng Core `Task`, `Run`, `Session`, `Event`, `Artifact`, `ChecklistItem`, `ConnectionRef` tách biệt khỏi UI/DB | [`core/contracts/`](file:///C:/DoCho/AI-Village/core/contracts/__init__.py) | `tests/core/test_p04_01_core_contracts_unit.py` | **PASS** |
| 2 | Máy trạng thái `Task` & `Run` có guard chống bỏ qua QA và chống ghi đè trạng thái terminal | [`core/tasks/state_machine.py`](file:///C:/DoCho/AI-Village/core/tasks/state_machine.py) | `tests/core/test_p04_02_task_state_machine_unit.py` | **PASS** |
| 3 | Nhật ký sự kiện append-only gắn `sequence_number` đơn điệu, chống trùng Event ID và hỗ trợ replay | [`core/events/journal.py`](file:///C:/DoCho/AI-Village/core/events/journal.py), [`replay.py`](file:///C:/DoCho/AI-Village/core/events/replay.py) | `tests/core/test_p04_03_event_journal_unit.py` | **PASS** |
| 4 | Schema SQLite có version (`0001`–`0003`), trigger chống sửa/xóa `events` và khóa ngoại sở hữu | [`storage/db/migrations/`](file:///C:/DoCho/AI-Village/storage/db/migrations.py) | `tests/storage/test_p04_04_sqlite_migrations.py` (`v3`) | **PASS** |
| 5 | Repository bền vững giao dịch nguyên tử cùng `EventJournal`, rollback sạch khi phát sinh lỗi ghi | [`storage/repositories/sqlite_store.py`](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py) | `tests/storage/test_p04_05_persistent_repositories.py` | **PASS** |
| 6 | Telemetry không có sẵn từ provider được giữ `None` (`UNAVAILABLE_FROM_PROVIDER`), không bịa số token | [`core/contracts/run.py`](file:///C:/DoCho/AI-Village/core/contracts/run.py), [`test_phase_04_gate_c_acceptance.py`](file:///C:/DoCho/AI-Village/tests/integration/test_phase_04_gate_c_acceptance.py) | `test_gate_c_01` kiểm tra `input_tokens is None` | **PASS** |
| 7 | Chạy một Task đơn hoàn chỉnh từ `QUEUED -> ASSIGNED -> RUNNING -> WAITING_APPROVAL -> RUNNING -> COMPLETED` | [`service.py`](file:///C:/DoCho/AI-Village/storage/repositories/service.py), [`lifecycle.py`](file:///C:/DoCho/AI-Village/core/tasks/lifecycle.py) | `test_gate_c_01` trong Gate C Acceptance Suite | **PASS** |
| 8 | Tạm dừng tại điểm kiểm tra chờ duyệt và khôi phục tiếp tục chạy an toàn sau khi khởi động lại kho lưu trữ | [`approvals.py`](file:///C:/DoCho/AI-Village/core/governance/approvals.py), [`recovery.py`](file:///C:/DoCho/AI-Village/storage/repositories/recovery.py) | `test_gate_c_01` dừng ở `WAITING_APPROVAL`, đóng mở lại SQLite và resume thành công | **PASS** |
| 9 | Hủy Run sở hữu dừng đúng tiến trình con qua `owner_token`, cô lập tiến trình anh em không liên quan | [`engines/antigravity/lifecycle.py`](file:///C:/DoCho/AI-Village/engines/antigravity/lifecycle.py) | `test_gate_c_02` xác nhận `CANCEL_CONFIRMED` và sibling PID vẫn sống | **PASS** |
| 10 | Xử lý quá hạn `handle_timeout` chờ đủ deadline, dừng tiến trình sở hữu trước khi ghi nhận `TIMED_OUT` | [`WorkflowLifecycleController.handle_timeout`](file:///C:/DoCho/AI-Village/core/tasks/lifecycle.py) | `test_gate_c_03` xác nhận `NOT_ELAPSED` rồi `TIMED_OUT_CONFIRMED` | **PASS** |
| 11 | Chống gửi trùng bằng idempotency fingerprint bền vững trong SQLite, từ chối xung đột tham số | [`lifecycle_repo.py`](file:///C:/DoCho/AI-Village/storage/repositories/lifecycle_repo.py) | `test_gate_c_04` & `test_p04_06_workflow_lifecycle.py` | **PASS** |
| 12 | Phục hồi sau restart đối với tác vụ `RUNNING` mất liên kết gắn cờ `NEEDS_RECONCILIATION`, không bịa trạng thái | [`reconcile_interrupted_task`](file:///C:/DoCho/AI-Village/core/tasks/lifecycle.py) | `test_gate_c_04` ghi nhận `UNOBSERVABLE` và `NEEDS_RECONCILIATION` | **PASS** |
| 13 | Kiểm soát Governance `PolicyEvaluator` (DENY > REQUIRE_APPROVAL > ALLOW), cô lập workspace/symlink, Tool allowlist & Approval dùng một lần | [`core/governance/`](file:///C:/DoCho/AI-Village/core/governance/__init__.py), [`approval_repo.py`](file:///C:/DoCho/AI-Village/storage/repositories/approval_repo.py) | `tests/core/test_p04_07_governance_unit.py` + `test_p04_07_governance_persistence.py` | **PASS** |
| 14 | Truy vết xuyên suốt `Event`, `ChecklistItem`, `Artifact` và `recover_task` tái dựng khớp 100% sau restart | [`recovery.py`](file:///C:/DoCho/AI-Village/storage/repositories/recovery.py), [`verification.py`](file:///C:/DoCho/AI-Village/engines/verification.py) | `test_gate_c_01` xác minh SHA-256 trên đĩa, `VERIFIED_CLEAN`, `snapshot_matches_replay=True` | **PASS** |

---

## 3. Bằng chứng Chạy Kiểm thử Thực tế (Sau Chỉnh sửa Cuối cùng)

Toàn bộ các lệnh kiểm thử chạy độc lập trên Python `3.13.13`, SQLite `3.50.4`, pytest `9.1.1`:

1. Nghiệm thu Gate C chuyên biệt:
   - `python -m pytest tests/integration/test_phase_04_gate_c_acceptance.py -v` -> **`4 passed in 1.99s`**
2. Tầng Core (`core/`):
   - `python -m pytest tests/core -q` -> **`79 passed in 4.71s`**
3. Tầng Storage (`storage/`):
   - `python -m pytest tests/storage -q` -> **`20 passed in 3.53s`**
4. Tầng Integration (`tests/integration/`):
   - `python -m pytest tests/integration -v -rs` -> **`13 passed, 23 skipped in 7.81s`**
5. Kiến trúc, Tiêu chuẩn và Liên kết tài liệu:
   - `python -m pytest tests/stage_a tests/standards -v` -> **`13 passed in 0.63s`**
6. Hồi quy toàn bộ kho lưu trữ:
   - `python -m pytest tests -v -rs` -> **`165 passed, 24 skipped in 16.62s`**
   - `git diff --check` -> thoát với mã `0` (không lỗi khoảng trắng).

Chi tiết 24 kiểm thử `SKIPPED`:
- 2 bài CrewAI benchmark/unit tại `tests/benchmarks/p03_02/` và `tests/integration/test_p03_02_crewai_benchmark_live.py` thiếu dependency tùy chọn `crewai` ngoài phạm vi ADR-0002.
- 22 bài live provider CLI tại `tests/integration/test_*_live.py` yêu cầu `AI_VILLAGE_RUN_LIVE=1`.

---

## 4. Bằng chứng SQLite, Tiến trình Con và QA Độc lập

- **SQLite Schema v3 & Recovery:** `test_phase_04_gate_c_acceptance.py` đóng mở liên tiếp ba phiên bản `SQLiteRepositoryStore` trên cùng tệp DB vật lý (`store1 -> store2 -> store3`). Phiên sau kiểm tra lại toàn bộ `Task`, `Run`, `Session`, `ChecklistItem`, `Artifact`, `ApprovalRequest` (`CONSUMED`) và `ActivityTimelineProjection`, khẳng định `recovery_status == "VERIFIED_CLEAN"` và `snapshot_matches_replay is True`.
- **Quản lý Tiến trình Sở hữu:** `OwnedSubprocessHandle` chấm dứt đúng child PID thuộc `Run` khi hủy hoặc quá hạn thông qua `owner_token` kiểm chứng chéo với bảng `run_executions`, đồng thời xác minh bằng `is_pid_alive()` rằng tiến trình anh em không thuộc `Run` vẫn tiếp tục sống mà không bị tác động bởi các lệnh dọn tiến trình diện rộng.
- **QA Độc lập (`verify_engine_artifact`):** Trong lần chạy thử nghiệm ban đầu trên Windows, `verify_engine_artifact` đã phát hiện ngay sự sai lệch mã băm SHA-256 giữa `write_text` ghi CRLF mặc định trên Windows với chuỗi UTF-8 LF mong đợi. Việc chuyển sang ghi byte chuẩn LF khôi phục xác minh SHA-256 `VERIFIED_PASS`, minh chứng cơ chế đối chiếu byte độc lập trên đĩa không phụ thuộc lời tự báo cáo.

---

## 5. Tổng hợp Bảo mật và Governance

- Mọi hành động nhạy cảm từ tác nhân hoặc ranh giới vận hành đều đi qua `PolicyEvaluator` và `PermissionManager` với ghi nhận nhật ký bắt buộc `governance.decision_recorded` (Fail-Closed khi append lỗi).
- Tác nhân `worker_agent` bị chặn nâng quyền (`PRIVILEGE_ESCALATION_DENIED`), chặn đọc/ghi đường dẫn traversal, vượt symlink/junction hoặc chạm vào tệp nhạy cảm (`.env`, `*.pem`, `.ssh/`, `secrets/`, `.runtime/`). Cả `worker_agent` lẫn `master_agent` đều bị chặn tự chuyển `Task` sang `COMPLETED` (`QA_BYPASS_DENIED`).
- Phê duyệt con người ràng buộc bằng SHA-256 `request_fingerprint`, cấm tự phê duyệt, cấm tài khoản giả mạo `agent_claimed`, cấm ghi đè `system_deny=True`, kiểm tra quá hạn ở cả trạng thái `PENDING` lẫn `APPROVED`, tiêu thụ một lần duy nhất và rollback nguyên tử trong `SQLiteApprovalRepository.consume_and_execute`.

---

## 6. Giới hạn đã Biết

1. **`CustomAgentRuntime.get_runtime_info().supports_workflow_cancellation == False`:** Runtime điều phối đồng bộ từ P03.03 giữ nguyên khai báo trung thực `False` vì chưa hỗ trợ cắt ngang giữa nhịp gọi provider API đồng bộ; hủy cấp quy trình dài hạn được điều khiển qua `WorkflowLifecycleController`, `OwnedSubprocessHandle` và checkpoint giữa các bước.
2. **Checkpoint Pause/Resume vs OS Process Freeze:** Trạng thái `WAITING_APPROVAL` thực hiện tạm dừng có kiểm soát tại ranh giới checkpoint lưu trữ trên SQLite để tái khởi động an toàn giữa các phiên tiến trình, không sử dụng cơ chế đóng băng bộ nhớ tiến trình hệ điều hành (`SIGSTOP`/Debug Suspend).
3. **Biên giới UI / HTTP API:** Logic kiểm quyền cho các thao tác Mission Control đã hoàn thiện tại `core/governance/` và `storage/`; lớp kết nối HTTP API và giao diện điều khiển trực quan thuộc phạm vi các Phase tiếp theo.

---

## 7. Phán quyết Gate C

Toàn bộ 14 tiêu chí nghiệm thu Phase 04 và Gate C đều có bằng chứng kiểm chứng thực tế trên mã nguồn và cơ sở dữ liệu SQLite (`165 passed, 24 skipped`, 0 thất bại, 0 lỗi hồi quy). **Gate C đạt trạng thái PASS (Offline End-to-End Verified)**, hoàn tất **PHASE 04** và sẵn sàng chuyển giao sang Phase 05 theo lộ trình [`docs/PROGRESS.md`](file:///C:/DoCho/AI-Village/docs/PROGRESS.md).
