# Báo cáo kiểm toán hoàn thành PHASE 11 / P11.04

Tài liệu này ghi nhận kết quả kiểm tra, thiết kế kiến trúc, thay đổi cơ sở dữ liệu, kiểm soát trung thực số liệu và bằng chứng kiểm thử cho hạng mục `P11.04` về sổ cái tài nguyên bền vững, cảnh báo ngân sách và ngưỡng quota.

## 1. Executive Summary

Hạng mục `P11.04` đạt mức `OFFLINE_INTEGRATION_VERIFIED` tại commit `aba62d0e4f876884bd6cc7c6d047936e6e820cc3` với nhãn rút gọn `aba62d0`. `ResourcesDepartmentService` được mở rộng tương thích ngược bằng bộ hợp đồng thuần AST `resources_models.py`, logic nghiệp vụ `resources_ledger.py`, migration SQLite `0019` và `SQLiteResourcesRepository` gắn tại `SQLiteRepositoryStore.resources`.

## 2. Root Causes

1. Dịch vụ `ResourcesDepartmentService` từ `P09.02` chỉ lưu snapshot và danh sách kiểm kê trong bộ nhớ RAM nên toàn bộ lịch sử tiêu thụ, chi phí và quota mất sạch khi khởi động lại tiến trình.
2. Hệ thống chưa có sổ cái nối đuôi bền vững gắn đủ `provider_id`, `connection_id` và `model_id` với cơ chế ép tách bạch giữa số liệu đo thật, số liệu chưa có từ Provider và số liệu ước lượng có nhãn.
3. Chưa có bảng chính sách ngân sách và ngưỡng quota theo từng phạm vi để phát cảnh báo `WARNING` và leo thang `CRITICAL` có khử trùng lặp, khôi phục tự động khi nâng hạn mức và đưa việc duyệt vào `action_inbox_items` cho Master và Operator mà không hủy ngang Task.

## 3. Architecture Decisions

- Bổ sung định danh `ResourcePolicyId` tiền tố `rpol_`, `ResourceLedgerId` tiền tố `rled_`, `ResourceAlertId` tiền tố `ralt_` tại [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py), xuất tại [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py) và tám sự kiện `resources.*` tại [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py).
- Tạo hợp đồng thuần AST tại [resources_models.py](file:///C:/DoCho/AI-Village/core/departments/resources_models.py) và hàm xử lý tại [resources_ledger.py](file:///C:/DoCho/AI-Village/core/departments/resources_ledger.py), nối tùy chọn `ledger_repository=None` vào [resources.py](file:///C:/DoCho/AI-Village/core/departments/resources.py) để giữ nguyên hoàn toàn các ca kiểm thử `P09.02`.
- Bảo toàn trung thực dữ liệu đo: mọi giá trị không có từ Provider hoặc host đều giữ nguyên `None` và không bị ép về `0`; tách riêng `actual_cost_usd` với `estimated_cost_usd`; bắt buộc `is_estimated=True`, `pricing_table_ref` và `estimate_label` rõ ràng cho các bản ghi ước lượng.
- Hàm `derive_quota_bucket_key` tổng hợp quota qua `quota_bucket_key` dùng chung mỗi khi các kết nối nằm cùng `project_ref`, cùng `account_ref` hoặc thiếu `independent_quota_proof`, chặn việc chia nhiều key để né cảnh báo quota.
- Cơ chế `_evaluate_policy_thresholds` khử trùng lặp thông báo cùng mức severity qua chỉ mục `dedup_key`, leo thang `WARNING` lên `CRITICAL` kèm `NotificationRef` loại `RESOURCE_WARNING` và `ActionInboxItem` định tuyến tới `core.master` cùng `human_operator`, tự khôi phục cảnh báo về `RESOLVED` nếu hạn mức được nâng lên dưới ngưỡng cảnh báo và tuyệt đối không hủy `Task` hoặc tự nâng cấp lên model đắt hơn.

## 4. Files Changed

- [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py)
- [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py)
- [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py)
- [resources_models.py](file:///C:/DoCho/AI-Village/core/departments/resources_models.py)
- [resources_ledger.py](file:///C:/DoCho/AI-Village/core/departments/resources_ledger.py)
- [resources.py](file:///C:/DoCho/AI-Village/core/departments/resources.py)
- [__init__.py](file:///C:/DoCho/AI-Village/core/departments/__init__.py)
- [0019_resource_ledger_and_alerts.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0019_resource_ledger_and_alerts.sql)
- [resources_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/resources_repo.py)
- [sqlite_store.py](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py)
- [__init__.py](file:///C:/DoCho/AI-Village/storage/repositories/__init__.py)
- [test_p11_04_resource_ledger.py](file:///C:/DoCho/AI-Village/tests/departments/test_p11_04_resource_ledger.py)

## 5. Database Migrations

Tệp [0019_resource_ledger_and_alerts.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0019_resource_ledger_and_alerts.sql) giữ nguyên các migration `0001` đến `0018` và tạo ba bảng mới:

1. `resource_budget_policies`: lưu hạn mức chi phí, thời gian, bộ nhớ, token, quota cùng `warning_ratio`, `critical_ratio` và cờ phê duyệt.
2. `resource_ledger_entries`: sổ cái nối đuôi bất biến có chỉ mục duy nhất bán phần chống trùng lặp `idempotency_key` cùng hai trigger `trg_resource_ledger_no_update` và `trg_resource_ledger_no_delete`.
3. `resource_threshold_alerts`: quản lý vòng đời cảnh báo, bộ đếm `occurrence_count`, khóa lạc quan `record_version`, trạng thái phục hồi và chỉ mục duy nhất bán phần `idx_resource_alerts_active_dedup` cho các cảnh báo đang mở.

## 6. Security & Governance

- Worker bị chặn quản trị chính sách ngân sách, chặn ACK hoặc RESOLVE cảnh báo tài nguyên, chặn ghi sổ hộ tác nhân khác với mã `ACTOR_IDENTITY_MISMATCH` và chỉ được ghi nhận cho `Task` nằm trong `assigned_task_ids`.
- Khóa bí mật trong metadata bị từ chối ở lớp kiểm tra hợp đồng và các chuỗi nhạy cảm trong ghi chú hoặc nhãn bị redact qua `scrub_secret_text` trước khi lưu SQLite.

## 7. Concurrency Behavior

Giao dịch `BEGIN IMMEDIATE` trong `SQLiteResourcesRepository.execute_transaction` bảo toàn tính nguyên tử giữa bước ghi sổ cái, tổng hợp tiêu thụ phạm vi và cập nhật cảnh báo. Kiểm thử 6 luồng chạy đồng thời trên cùng bucket quota đạt đủ 6 bản ghi không lỗi khóa và cộng dồn chính xác `75` đơn vị quota, trong khi lỗi giả lập trước commit hoàn tác sạch không để lại dữ liệu mồ côi.

## 8. Test Matrix

| Hàm kiểm thử | Nội dung kiểm chứng | Kết quả |
| --- | --- | --- |
| `test_p11_04_ast_purity_and_migration_0019_immutability` | Độ thuần AST của khối `resources` và trigger bất biến `0019` | PASSED |
| `test_p11_04_truthful_telemetry_unavailable_vs_estimated_vs_actual` | Giữ nguyên `None`, tách `actual` với `estimated` có nhãn và tổng hợp Task | PASSED |
| `test_p11_04_shared_and_unproven_quota_scopes_trigger_combined_quota_alerts` | Gộp bucket dùng chung cho hai key và phát cảnh báo quota `WARNING`/`CRITICAL` | PASSED |
| `test_p11_04_budget_warning_dedup_escalation_recovery_and_non_destructive_safety` | Khử trùng lặp, leo thang, Action Inbox, giữ Task chạy, ACK và tự khôi phục | PASSED |
| `test_p11_04_idempotency_concurrency_rollback_restart_security_and_secret_scrub` | Idempotency, phân quyền Worker, scrub bí mật, rollback, 6 luồng và restart | PASSED |

## 9. Exact Commands Executed & 10. Actual Test Results

- Trước commit `aba62d0`: `git diff --check; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -m pytest tests/departments/test_p11_04_resource_ledger.py tests/departments/test_p09_02_04_departments.py tests/departments/test_p11_01_concurrent_intake.py tests/departments/test_p11_02_interrupt_routing.py tests/optimization/test_p11_03_scheduler.py tests/storage/test_p04_04_sqlite_migrations.py tests/core/test_p04_01_core_contracts_unit.py tests/stage_a/test_stage_a_baseline.py -v` đạt `54 passed in 21.60s`, exit code `0`.
- Sau cập nhật tài liệu kiểm toán và allowlist, chạy kiểm tra nhất quán trước khi commit docs và đồng bộ lên `origin/main`.

## 11. Git Commit SHA

- Commit triển khai mã nguồn, migration `0019` và kiểm thử `P11.04`: `aba62d0e4f876884bd6cc7c6d047936e6e820cc3` với thông điệp `feat(resources): add persistent resource ledger and budget alerts`.

## 12. Known Limitations, 13. Verification Level & 14. Remaining Blockers

- Mức xác minh hiện tại: `OFFLINE_INTEGRATION_VERIFIED`.
- Chưa chạy các ca live CLI `agy.exe`, Docker sandbox live hay benchmark CrewAI riêng trong môi trường offline; bước kế tiếp là `P11.05` dọn dẹp an toàn sau Task và handoff dữ liệu sang Process cùng nghiệm thu `Gate G`.
