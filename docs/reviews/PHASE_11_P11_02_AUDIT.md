# Báo cáo kiểm toán hoàn thành PHASE 11 / P11.02

Tài liệu này ghi nhận kết quả kiểm tra, thiết kế kiến trúc, thay đổi cơ sở dữ liệu, kiểm soát bảo mật và bằng chứng kiểm thử cho hạng mục `P11.02` về phân loại mức khẩn, định tuyến thông báo và khử trùng lặp bền vững.

## 1. Executive Summary

Hạng mục `P11.02` đạt mức `OFFLINE_INTEGRATION_VERIFIED` tại commit `33291b7559f7d31996c13ea8134b9f5312dfb2fa` với nhãn rút gọn `33291b7`. Hệ thống bổ sung dịch vụ `InterruptRoutingService` tách biệt năm chiều đánh giá gồm `classification`, `severity`, `priority`, `urgency` và `impact`, định tuyến có kiểm chứng trạng thái online thực tế của Agent, chống thông báo lặp vô hạn qua SQLite và quản lý vòng đời ACK, escalation và resolution có khóa lạc quan.

## 2. Root Causes

1. Cơ chế `triage_incoming_signal` ở Phase 09 chỉ xử lý tín hiệu đơn lẻ chưa có vòng đời incident, chưa tách biệt `severity` với `priority`, `urgency` hay `impact`, và chưa có luồng ACK, escalation hoặc resolution lưu bền vững.
2. Chưa có bảng lưu các event thuộc cùng một incident đang mở hay cơ chế mở lại incident mới khi sự cố cũ đã ở trạng thái `RESOLVED` hoặc `DISMISSED`.
3. Chưa có cơ chế đối soát `agent_registry` và bảng `sessions` khi định tuyến câu hỏi chuyên môn để tránh mặc định Agent đang online khi Runtime chưa có phiên hoạt động.
4. Worker chưa bị chặn fail-closed khi tự nâng `claimed_severity` lên `CRITICAL` hoặc tự phát tín hiệu `emergency_stop`/`security_breach`, và mẫu kiểm tra chuỗi bí mật `_EMBEDDED_SECRET_RE` trong `identifiers.py` trước đây khớp nhầm vào chuỗi kỹ thuật như `disk-io-stall` do thiếu ranh giới chữ cái phía trước `sk-`.

## 3. Architecture Decisions

- Bổ sung định danh `InterruptId` tiền tố `intr_`, `IncidentId` tiền tố `inc_`, `EscalationId` tiền tố `esc_` cùng sửa ranh giới `(?<![A-Za-z0-9])` cho `_EMBEDDED_SECRET_RE` tại [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py) và xuất ký hiệu tại [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py).
- Bổ sung năm loại sự kiện `reception.interrupt_classified`, `reception.interrupt_acknowledged`, `reception.interrupt_escalated`, `reception.interrupt_resolved` và `reception.notification_suppressed` tại [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py).
- Tách hợp đồng thuần AST tại [interrupt_models.py](file:///C:/DoCho/AI-Village/core/departments/interrupt_models.py) và dịch vụ [interrupts.py](file:///C:/DoCho/AI-Village/core/departments/interrupts.py), xuất qua [__init__.py](file:///C:/DoCho/AI-Village/core/departments/__init__.py):
  - Phân tách rõ `INFORMATIONAL`, `ATTENTION_NEEDED`, `ACTION_REQUIRED`, `HUMAN_INTERVENTION_REQUIRED` và `CRITICAL_EMERGENCY`.
  - Mọi trường hợp Worker tự khai báo `CRITICAL`, `emergency_stop`, `security_breach` hoặc ảnh hưởng `WORKSPACE_WIDE`/`SYSTEM_WIDE` bị từ chối với mã `WORKER_CRITICAL_ESCALATION_DENIED`; khai báo `ERROR` thông thường của Worker bị ghi đè theo policy và lưu cờ `severity_override_applied`.
  - Khử trùng lặp đa tầng không dựa trên văn bản thô: trùng `event_dedup_key` trả về replay `EXACT_EVENT_RETRY`, cùng `incident_key` đang mở cộng dồn `occurrence_count` với `ACTIVE_INCIDENT_AGGREGATED` và đặt `notification_suppressed=True`, còn sự cố tái phát sau `RESOLVED`/`DISMISSED` mở incident mới `POST_RESOLUTION_RECURRENCE`. Hai sự cố độc lập cùng văn bản không chung khóa luôn tách biệt.
  - Định tuyến giới hạn không broadcast toàn bộ 9 phòng ban, luôn báo `core.master` khi `CRITICAL_EMERGENCY`, đưa `HUMAN_INTERVENTION_REQUIRED` vào `action_inbox_items`, kiểm tra `sessions.status = 'ACTIVE'` trước khi đánh dấu `verified_online=True` và giữ nguyên trạng thái Task và Run không đổi.

## 4. Files Changed

- [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py)
- [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py)
- [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py)
- [interrupt_models.py](file:///C:/DoCho/AI-Village/core/departments/interrupt_models.py)
- [interrupts.py](file:///C:/DoCho/AI-Village/core/departments/interrupts.py)
- [__init__.py](file:///C:/DoCho/AI-Village/core/departments/__init__.py)
- [0017_interrupts_routing_and_dedup.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0017_interrupts_routing_and_dedup.sql)
- [interrupt_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/interrupt_repo.py)
- [sqlite_store.py](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py)
- [__init__.py](file:///C:/DoCho/AI-Village/storage/repositories/__init__.py)
- [test_p11_02_interrupt_routing.py](file:///C:/DoCho/AI-Village/tests/departments/test_p11_02_interrupt_routing.py)

## 5. Database Migrations

Tệp [0017_interrupts_routing_and_dedup.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0017_interrupts_routing_and_dedup.sql) giữ nguyên các migration `0001` đến `0016` và bổ sung ba bảng:

1. `interrupt_incidents`: lưu trọn vẹn chiều phân loại, `routing_targets_json`, ràng buộc `broadcast_all_departments = 0`, thông tin ACK, resolution, `record_version` và chỉ mục duy nhất bán phần `idx_interrupt_active_incident` trên hai cột `dedup_scope` và `incident_key` cho các dòng chưa đóng.
2. `interrupt_events`: ghi nối đuôi mọi tín hiệu và phát lại với chỉ mục duy nhất bán phần `idx_interrupt_events_dedup` trên `(dedup_scope, event_dedup_key)` cùng hai trigger `trg_interrupt_events_no_update` và `trg_interrupt_events_no_delete`.
3. `interrupt_escalations`: lưu lịch sử escalation bất biến kèm hai trigger `trg_interrupt_escalations_no_update` và `trg_interrupt_escalations_no_delete`.

## 6. Security & Governance

- Đối soát `PermissionManager` qua `authorize_and_audit`, chặn `ACTOR_IDENTITY_MISMATCH` và kiểm soát `assigned_task_ids` đối với Worker.
- Chặn `UNAUTHORIZED_ROUTING_TARGET` khi gửi tới Agent đã bị vô hiệu hóa, Agent không tồn tại, Agent lệch phòng ban hoặc phòng ban không hợp lệ.
- Chặn Worker thực hiện `escalate_interrupt` hoặc `resolve_interrupt`; kiểm soát `expected_record_version` bằng `StaleInterruptVersionError`.
- Từ chối khóa bí mật trong `metadata` và redact khóa thô trong `summary`/`details` trước khi lưu SQLite.

## 7. Concurrency Behavior

Giao dịch `BEGIN IMMEDIATE` tại `SQLiteInterruptRepository` cô lập toàn bộ quá trình kiểm tra trùng lặp, lưu sự kiện, lưu thông báo và cập nhật bộ đếm. Kiểm thử đua 6 luồng đa tuyến trên cùng `incident_key` hợp nhất gọn vào 1 incident duy nhất với `occurrence_count = 6`. Lỗi giả lập trước commit kích hoạt rollback hoàn toàn không để lại incident hay notification mồ côi.

## 8. Test Matrix

| Hàm kiểm thử | Nội dung kiểm chứng | Kết quả |
| --- | --- | --- |
| `test_p11_02_ast_purity_and_migration_0017_triggers` | Độ thuần AST và trigger bất biến bảng `0017` | PASSED |
| `test_p11_02_classifications_separate_severity_priority_and_non_broadcast_routing` | Đủ 5 cấp phân loại, tách chiều đánh giá, kiểm tra online thật từ `sessions`, báo Master và không sửa Task/Run | PASSED |
| `test_p11_02_worker_security_spoofing_and_unauthorized_target_blocked` | Chặn Worker nâng quyền, giả danh, sai tham chiếu, sai target và redact secret | PASSED |
| `test_p11_02_persistent_dedup_exact_retry_incident_aggregation_and_recurrence` | Dedup retry, cộng dồn incident mở, tách incident độc lập cùng văn bản, mở lại sau resolved và restart | PASSED |
| `test_p11_02_ack_escalation_resolution_lifecycle_concurrency_and_rollback` | Vòng đời ACK khác RESOLVED, lịch sử escalation bất biến, khóa lạc quan, rollback và bão 6 luồng | PASSED |

## 9. Exact Commands Executed & 10. Actual Test Results

- Trước khi commit mã nguồn `33291b7`: `git diff --check; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -m pytest tests/departments/test_p11_02_interrupt_routing.py tests/departments/test_p11_01_concurrent_intake.py tests/departments/test_p09_05_07_departments.py tests/departments/test_gate_f_end_to_end.py tests/storage/test_p04_04_sqlite_migrations.py tests/core/test_p04_01_core_contracts_unit.py tests/stage_a/test_stage_a_baseline.py -v` đạt `50 passed in 19.65s`, exit code `0`.
- Sau bước cập nhật tài liệu, chạy lại `git diff --check` và kiểm thử xác minh tính nhất quán trước khi commit tài liệu và đẩy lên `origin/main`.

## 11. Git Commit SHA

- Commit triển khai mã nguồn, migration và kiểm thử `P11.02`: `33291b7559f7d31996c13ea8134b9f5312dfb2fa` với thông điệp `feat(reception): classify and route operational interrupts`.

## 12. Known Limitations, 13. Verification Level & 14. Remaining Blockers

- Mức xác minh hiện tại: `OFFLINE_INTEGRATION_VERIFIED`.
- Chưa chạy các ca live CLI `agy.exe`, Docker sandbox live hay provider API live trong môi trường kiểm thử offline; `P11.03` Scheduler, `P11.04` Resource Ledger và `P11.05` Cleanup sẽ triển khai ở các bước kế tiếp.
