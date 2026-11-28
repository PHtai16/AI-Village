# Báo cáo kiểm toán hoàn thành PHASE 11 / P11.01

Tài liệu này ghi nhận kết quả phân tích kiến trúc, thay đổi cơ sở dữ liệu, luồng tiếp nhận đồng thời, kiểm soát phân quyền bảo mật và bằng chứng kiểm thử cho hạng mục `P11.01` về tiếp nhận yêu cầu mới khi Task cũ đang chạy mà không làm mất dữ liệu hay nhầm ngữ cảnh hội thoại.

## 1. Executive Summary

Hạng mục `P11.01` đạt mức nghiệm thu `OFFLINE_INTEGRATION_VERIFIED` tại commit `22accc0bf27156b0c1da6a7ab731943cd8515de3` với nhãn rút gọn `22accc0`. Hệ thống bổ sung cơ chế cô lập phiên hội thoại, ghi nhận bền vững mọi lượt tiếp nhận và bổ sung phạm vi cho Task đang chạy trong một giao dịch `BEGIN IMMEDIATE` thống nhất cùng `EventJournal` và `NotificationStorePort`.

Mọi yêu cầu mới đến trong lúc Task A đang ở trạng thái `RUNNING` đều giữ nguyên Task A cùng `baseline_json` không suy suyển. Yêu cầu độc lập tạo Task B mới ở trạng thái `QUEUED` chờ Master xử lý, phần bổ sung hợp lệ ghi thành `TaskRefinementRecord` nối đuôi bất biến, và mọi tín hiệu mơ hồ hoặc trỏ vào Task đã kết thúc bị giữ lại ở trạng thái `CLARIFICATION_REQUIRED` thay vì tự ý gắn nhầm hoặc đoán ý người dùng.

## 2. Root Causes Discovered & Fixed

Kiểm tra trực tiếp mã nguồn trước khi triển khai xác định năm nguyên nhân gốc:

1. `ReceptionDepartmentService` trước đây lưu toàn bộ `_inbox` và `_dedup_state` trong bộ nhớ tiến trình Python nên toàn bộ hàng đợi tiếp nhận và bộ đếm khử trùng lặp bị mất trắng khi khởi động lại.
2. Hàm `_dedup_token_for_signal` băm tín hiệu `QUEUED` và `APPENDED` theo `summary[:80].lower()` khi không có `dedup_key`, dẫn đến hai yêu cầu hoàn toàn độc lập nhưng giống câu chữ bị gộp thành trùng lặp và không tạo `NotificationRef` riêng.
3. Chưa có khái niệm `conversation_id`, bảng `conversation_turns`, bảng `conversation_task_links` hay `task_refinements`, nên nhiều Task và nhiều luồng trao đổi đồng thời có nguy cơ trộn lẫn ngữ cảnh.
4. Chưa có tầng khóa `idempotency_key` gắn với `request_fingerprint` trên SQLite hoặc kiểm soát phiên bản lạc quan `record_version` cho bước Master xác nhận và giải quyết làm rõ.
5. Chưa có cơ chế chặn fail-closed khi Worker thử giả mạo tác nhân khác, can thiệp Task không sở hữu, trỏ sai `conversation_id`/`run_id`, hoặc gửi yêu cầu bổ sung nhắm vào Task đã terminal.

## 3. Architecture & Implementation Details

Phạm vi triển khai tuân thủ ranh giới tầng của [SYSTEM_BLUEPRINT.md](file:///C:/DoCho/AI-Village/docs/architecture/SYSTEM_BLUEPRINT.md) và hợp đồng dữ liệu trong [DEPARTMENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/DEPARTMENT_SPEC.md):

- Định danh và khử bí mật tại [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py) và [\_\_init\_\_.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py): bổ sung `ConversationId` prefix `conv_`, `IntakeId` prefix `intk_`, `RefinementId` prefix `ref_`, `TurnId` prefix `turn_`, các hàm kiểm tra và `scrub_secret_text` chuẩn hóa chuỗi chứa khóa bí mật đã redact thành `[REDACTED_SECRET]` trước khi lưu văn bản tự do.
- Sự kiện miền tại [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py): thêm bốn loại sự kiện `reception.request_received`, `reception.task_refined`, `reception.clarification_requested` và `reception.master_decision_recorded` vào `EventType`.
- Hợp đồng sạch và cổng giao dịch tại [reception_models.py](file:///C:/DoCho/AI-Village/core/departments/reception_models.py): tách toàn bộ Pydantic v2 model chế độ `extra="forbid"`, các ngoại lệ fail-closed gồm `ReceptionValidationError`, `ReceptionReferenceError`, `ConversationIsolationError`, `IntakeIdempotencyConflictError`, `StaleIntakeVersionError`, `ReceptionPersistenceError`, cùng `ReceptionTransactionContext` và `ReceptionIntakeRepository` giữ thuần AST.
- Dịch vụ tiếp nhận tại [reception.py](file:///C:/DoCho/AI-Village/core/departments/reception.py) và [\_\_init\_\_.py](file:///C:/DoCho/AI-Village/core/departments/__init__.py):
  - Duy trì tương thích ngược cho `triage_incoming_signal` của `P09.06` và nối thêm lưu trữ bền vững khi có `ReceptionIntakeRepository`, đồng thời sửa `_dedup_token_for_signal` dùng `signal_id` đối với `QUEUED` và `APPENDED` không mang `dedup_key`.
  - Cung cấp `open_conversation`, `receive_user_request`, `acknowledge_intake_by_master`, `resolve_intake_clarification`, `get_conversation`, `list_conversation_turns`, `get_intake_request`, `list_intake_requests`, `list_master_pending_intakes`, `list_task_refinements`, `recover_intake_state` và `list_action_inbox`.
  - Tách rạch ròi ba luồng chính là `QUEUED` tạo Task B độc lập không tự chạy, `APPENDED` ghi `TaskRefinementRecord` mà không sửa `Task.status` hay `Task.description`/`baseline_json` của Task A, và `CLARIFICATION_REQUIRED` giữ nguyên trạng thái `NEEDS_CLARIFICATION` cho yêu cầu mơ hồ hoặc trỏ vào Task đã `SUCCEEDED`/`FAILED`/`CANCELLED`/`TIMED_OUT`.
- Kho lưu trữ SQLite tại [reception_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/reception_repo.py), [sqlite_store.py](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py) và [\_\_init\_\_.py](file:///C:/DoCho/AI-Village/storage/repositories/__init__.py): triển khai `SQLiteReceptionRepository` và `_SQLiteReceptionTransactionContext` chạy gói gọn trong một giao dịch `BEGIN IMMEDIATE` nối trực tiếp với `EventJournal`, bảng `tasks`, bảng `notifications` và hỗ trợ khôi phục trạng thái qua `recover_intake_state`.

## 4. Database Schema Migration `0016`

Tệp [0016_reception_conversations_and_intake.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0016_reception_conversations_and_intake.sql) giữ nguyên toàn bộ migration từ `0001` đến `0015` và bổ sung bảy bảng mới:

1. `conversations`: quản lý phiên hội thoại theo `conversation_id`, `owner_actor_id`, `project_id`, `status` và `record_version`.
2. `intake_requests`: lưu dấu vết tiếp nhận bền vững kèm `request_fingerprint` dài 64 ký tự SHA-256, trạng thái xử lý, `master_handoff_status`, `record_version`, `outcome_json` và chỉ mục duy nhất bán phần `idx_intake_idempotency` trên hai cột `idempotency_scope` và `idempotency_key` nhằm chặn trùng khóa ngay tại SQLite.
3. `conversation_task_links`: khóa mỗi `task_id` làm khóa chính tham chiếu duy nhất một `conversation_id` để ngăn một Task bị gắn nhầm sang nhiều hội thoại khác nhau.
4. `conversation_turns`: ghi lịch sử trao đổi theo cặp khóa duy nhất `(conversation_id, turn_index)` và khóa bất biến bằng hai trigger `trg_conversation_turns_no_update`, `trg_conversation_turns_no_delete`.
5. `task_refinements`: ghi nối đuôi bổ sung phạm vi cho Task theo `(task_id, sequence_in_task)` và khóa bất biến bằng hai trigger `trg_task_refinements_no_update`, `trg_task_refinements_no_delete`.
6. `action_inbox_items`: lưu bền vững danh sách việc cần xử lý tại Mission Control Action Inbox theo độ ưu tiên và thời gian.
7. `reception_dedup_state`: lưu bền vững bộ đếm khử trùng lặp cảnh báo sự cố của Reception thay cho dictionary trong bộ nhớ.

## 5. Security, Concurrency & Transaction Guarantees

Các lớp bảo vệ đã kiểm chứng cho `P11.01`:

- Phân quyền qua `PermissionManager` và `PolicyEvaluator`: mọi lời gọi đều đối soát `AuthContext`, chặn `ACTOR_IDENTITY_MISMATCH` nếu `claimed_actor_id` khác với danh tính thật, chặn `worker_agent` mở hội thoại, gửi yêu cầu người dùng hay ký xác nhận thay Master, và chặn đọc hội thoại hoặc refinement của người dùng khác hay Task không được phân công.
- Cô lập hội thoại: cố tình gửi `REFINEMENT` từ Conversation B trỏ vào Task A thuộc Conversation A bị từ chối với `CROSS_CONVERSATION_TASK_CONFLICT`.
- An toàn dữ liệu bí mật: metadata có khóa nhạy cảm bị chặn ngay ở Pydantic `ValidationError` bởi `validate_safe_metadata`; chuỗi bí mật trong `summary` hoặc mô tả được redact qua `scrub_secret_text` trước khi ghi vào Task, Intake, Turn, Refinement, Action Inbox và `EventJournal`.
- Tính nguyên tử và đồng thời: mọi bước tạo Task B, ghi `TaskRefinementRecord`, chèn `ConversationTurnRecord`, `ActionInboxItem`, `NotificationRef` và `Event` nằm chung trong một transaction `BEGIN IMMEDIATE`. Khi xảy ra lỗi giữa chừng trước khi commit, toàn bộ thay đổi bị SQLite rollback hoàn toàn không để lại bản ghi mồ côi.
- Idempotency và cạnh tranh: gửi trùng `idempotency_key` cùng `request_fingerprint` trả về kết quả cũ với `idempotent_replay=True` không tạo Task thứ hai; cùng khóa nhưng khác nội dung bật `IntakeIdempotencyConflictError`; sai `expected_record_version` bật `StaleIntakeVersionError`; hủy đồng thời Task A không làm hỏng lịch sử event replay `recover_task` của Task A lẫn Task B mới tạo.

## 6. Test Matrix & Verification Results

Bộ kiểm thử mới [test_p11_01_concurrent_intake.py](file:///C:/DoCho/AI-Village/tests/departments/test_p11_01_concurrent_intake.py) gồm bảy hàm test kiểm chứng đầy đủ các nhóm yêu cầu `#0` đến `#18` và kịch bản offline cho Gate G:

| Hàm kiểm thử | Phạm vi kiểm chứng | Kết quả |
| --- | --- | --- |
| `test_p11_01_00_ast_purity_and_migration_0016_immutability` | Ranh giới AST của `reception*.py`, kiểm chứng unit repo thuần trong bộ nhớ và trigger cấm sửa xóa trên `conversation_turns` và `task_refinements` | PASSED |
| `test_p11_01_01_to_06_concurrent_independent_refinement_and_ambiguous_hold` | Kịch bản A tạo Task B `QUEUED` không chạm Task A, kịch bản B ghi `TaskRefinementRecord` nối đuôi, kịch bản C giữ `CLARIFICATION_REQUIRED` và giải quyết rõ ràng sau đó | PASSED |
| `test_p11_01_07_08_09_idempotency_identical_text_and_concurrent_requests` | Kịch bản D phát lại idempotent, từ chối xung đột fingerprint, tách biệt hai yêu cầu giống chữ không khóa, sửa băm `triage_incoming_signal` và storm 8 luồng đồng thời | PASSED |
| `test_p11_01_10_11_terminal_task_and_concurrent_cancellation` | Từ chối gắn bổ sung vào Task đã `CANCELLED`, cách ly Task B và bảo toàn replay sạch `VERIFIED_CLEAN` khi hủy Task A song song qua `WorkflowLifecycleController.request_cancel` | PASSED |
| `test_p11_01_12_13_14_15_failure_rollback_events_and_restart_recovery` | Chặn fail-closed khi thiếu repo bền vững, rollback SQLite toàn phần khi lỗi giữa transaction, nhất quán chuỗi `EventJournal` và khôi phục sạch sau restart không tự chạy Task `QUEUED` | PASSED |
| `test_p11_01_16_17_18_security_invalid_refs_and_secret_redaction` | Chặn Worker, spoofing, xâm nhập chéo hội thoại, sai `conversation_id`/`task_id`/`run_id`, chặn secret trên metadata và redact secret trên toàn bộ văn bản lưu trữ | PASSED |
| `test_p11_01_gate_g_concurrent_task_handoff_to_master_without_overwriting_active_task` | Kịch bản tích hợp offline cho Gate G từ lúc Task A đang chạy cho đến khi Reception phân loại Task B, Master xác nhận tiếp nhận và Task A hoàn tất độc lập | PASSED |

Tại commit triển khai `22accc0`, lệnh kiểm thử mục tiêu gồm `tests/departments/test_p11_01_concurrent_intake.py` cùng hồi quy `test_p09_05_07_departments.py`, `test_gate_f_end_to_end.py`, `test_p10_01_process_retrospectives.py`, `test_p10_02_pattern_detection.py`, `tests/skills`, `tests/storage`, `test_p04_01_core_contracts_unit.py`, `test_p04_03_event_journal_unit.py` và `test_p04_06_workflow_lifecycle_unit.py` đạt `95 passed in 32.06s` với 0 ca lỗi.

Sau khi cập nhật xong `docs/PROGRESS.md`, `tests/standards/test_standards_consistency.py`, `tests/README.md`, `.agents/rules/project-memory.md` và báo cáo kiểm toán này, toàn bộ bộ kiểm thử `python -m pytest tests -v -rs` cùng `git diff --check` được chạy kiểm chứng trước khi tạo commit tài liệu và đẩy lên `origin/main`.

## 7. Known Limitations & Next Step

- Mức nghiệm thu của `P11.01` là `OFFLINE_INTEGRATION_VERIFIED`; các bài kiểm thử live CLI `agy.exe`, provider contract live, benchmark CrewAI riêng và Docker sandbox live vẫn đang bỏ qua do môi trường chạy offline.
- Một số dịch vụ phòng ban khác như `ResourcesDepartmentService` và `ServiceUxDepartmentService`, cũng như các phần nâng cao của `PHASE 11` về notification severity policy trong `P11.02`, scheduler hàng đợi thực thi trong `P11.03`, resource ledger trong `P11.04` và dọn dẹp tài nguyên sau Task trong `P11.05`, chưa triển khai ở đợt này.
- Bước kế tiếp là `P11.02` xử lý phân loại mức khẩn, tác động, phát sinh notification chống lặp vô hạn theo chính sách và lựa chọn Agent cần hỏi hoặc báo Master khi khẩn cấp.
