# Báo cáo kiểm toán hoàn thành PHASE 11 / P11.03

Tài liệu này ghi nhận kết quả kiểm tra, thiết kế kiến trúc, thay đổi cơ sở dữ liệu, cơ chế cô lập quota và bằng chứng kiểm thử cho hạng mục `P11.03` về Execution Scheduler, hàng đợi ưu tiên, cấp lease thực thi và lịch chạy tác vụ.

## 1. Executive Summary

Hạng mục `P11.03` đạt mức `OFFLINE_INTEGRATION_VERIFIED` tại commit `9065d87d35946affea564400da0e4f5b7e423c60` với nhãn rút gọn `9065d87`. Hệ thống bổ sung dịch vụ `ExecutionSchedulerService` tại gói thuần AST mới `core/optimization/`, lưu trạng thái hàng đợi, bucket quota, lease thực thi có TTL và siêu dữ liệu lịch làm việc xuống SQLite qua migration `0018` cùng `SQLiteSchedulerRepository`.

## 2. Root Causes

1. Các pha trước mới có `evaluate_fallback` và `EngineConnectionRef` nhưng chưa có bộ lập lịch bền vững để giới hạn concurrency, rate limit theo cửa sổ thời gian và hạn mức quota thực thi trước khi Worker chạy tác vụ.
2. Hai kết nối cùng `project_ref`, cùng `account_ref` hoặc thiếu bằng chứng độc lập `independent_quota_proof` có thể bị hiểu nhầm thành hai nguồn quota tách biệt nếu kiểm đếm theo từng `connection_id` riêng lẻ.
3. Thiếu hàng đợi hiển thị rõ trạng thái chờ và lease nguyên tử có thời hạn TTL để ngăn trùng lặp thực thi hoặc treo slot mãi mãi khi quy trình sập giữa chừng.
4. Thiếu cấu trúc `scheduled_tasks` chuẩn hóa múi giờ IANA, chu kỳ lặp và rào phê duyệt để phân tách tác vụ chạy ngay với tác vụ chờ lịch thật.

## 3. Architecture Decisions

- Mở gói `core/optimization/` gồm `scheduler_models.py`, `scheduler.py` và `__init__.py` giữ thuần AST, đồng thời cập nhật `allowed_core_subdirs` tại [test_stage_a_baseline.py](file:///C:/DoCho/AI-Village/tests/stage_a/test_stage_a_baseline.py).
- Bổ sung định danh `QueueEntryId` tiền tố `qent_`, `LeaseId` tiền tố `lease_`, `ScheduleId` tiền tố `sched_` tại [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py) và xuất ký hiệu tại [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py).
- Bổ sung tám sự kiện dòng `scheduler.*` tại [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py).
- Hàm `compute_quota_bucket_key` trong [scheduler.py](file:///C:/DoCho/AI-Village/core/optimization/scheduler.py) gộp chung bucket theo `project_ref` hoặc `account_ref` khi `quota_scope_kind` là `PROJECT_SHARED`, `ACCOUNT_SHARED`, `ORG_SHARED` hay `UNKNOWN_SHARED`, chỉ tách bucket riêng ở mức `CONNECTION_VERIFIED` khi chính sách có bằng chứng độc lập hợp lệ.
- Chặn mọi thao tác chuyển kết nối nhằm né giới hạn bằng lỗi `KeyRotationForbiddenError` từ hàm `evaluate_connection_switch`.
- Tách rạch ròi trạng thái `RESERVED` khi vừa cấp lease với `ACTIVE` chỉ được ghi nhận qua `confirm_execution_started`, áp dụng fairness aging để nâng hạng mục `LOW` chờ lâu và hỗ trợ `recover_expired_leases` thu hồi lease quá hạn về `READY`.
- Hàm `evaluate_due_schedules` đối soát trạng thái phê duyệt thực tế trên bảng `approval_requests` trước khi đưa lịch đến hạn vào hàng đợi.

## 4. Files Changed

- [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py)
- [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py)
- [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py)
- [scheduler_models.py](file:///C:/DoCho/AI-Village/core/optimization/scheduler_models.py)
- [scheduler.py](file:///C:/DoCho/AI-Village/core/optimization/scheduler.py)
- [__init__.py](file:///C:/DoCho/AI-Village/core/optimization/__init__.py)
- [0018_scheduler_queue_leases_and_schedules.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0018_scheduler_queue_leases_and_schedules.sql)
- [scheduler_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/scheduler_repo.py)
- [sqlite_store.py](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py)
- [__init__.py](file:///C:/DoCho/AI-Village/storage/repositories/__init__.py)
- [test_stage_a_baseline.py](file:///C:/DoCho/AI-Village/tests/stage_a/test_stage_a_baseline.py)
- [conftest.py](file:///C:/DoCho/AI-Village/tests/optimization/conftest.py)
- [test_p11_03_scheduler.py](file:///C:/DoCho/AI-Village/tests/optimization/test_p11_03_scheduler.py)

## 5. Database Migrations

Tệp [0018_scheduler_queue_leases_and_schedules.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0018_scheduler_queue_leases_and_schedules.sql) giữ nguyên toàn bộ migration từ `0001` đến `0017` và tạo sáu bảng mới:

1. `scheduler_quota_buckets`: lưu bộ đếm và giới hạn concurrency, rate limit, quota theo `quota_bucket_key`.
2. `scheduler_quota_policies`: ánh xạ cặp `provider_id` và `connection_id` vào bucket và cấu hình backoff.
3. `scheduler_queue_entries`: hàng đợi công việc kèm chỉ mục duy nhất bán phần khóa một tác vụ chỉ có tối đa một mục đang hoạt động và chống trùng lặp `idempotency_key`.
4. `scheduler_execution_leases`: cấp phát slot thực thi có `owner_token`, `expires_at` và chỉ mục duy nhất bán phần cho mỗi mục hàng đợi.
5. `scheduler_rate_limit_events`: nhật ký gọi nối đuôi theo cửa sổ thời gian với hai trigger `trg_sched_rate_events_no_update` và `trg_sched_rate_events_no_delete`.
6. `scheduled_tasks`: siêu dữ liệu lịch làm việc thật theo múi giờ IANA, chu kỳ lặp và liên kết `approval_id`.

## 6. Security & Governance

- Kiểm soát phân quyền qua `PermissionManager` và `authorize_and_audit`; Worker không được tự ý cấp phát slot, thay đổi hàng đợi hay đăng ký lịch.
- Khóa chặt xoay key né giới hạn Provider, từ chối chuỗi nhạy cảm trong metadata và yêu cầu phê duyệt hợp lệ tại `approval_requests` đối với lịch nhạy cảm.
- Chỉ phát sự kiện `scheduler.*` thành công khi toàn bộ transaction SQLite cam kết an toàn.

## 7. Concurrency Behavior

Giao dịch `BEGIN IMMEDIATE` trong `SQLiteSchedulerRepository.execute_transaction` bảo đảm bước đọc bộ đếm bucket, chèn lease và chuyển trạng thái hàng đợi diễn ra nguyên tử. Bài kiểm thử đa luồng xác nhận hai luồng cùng xin slot cho bucket giới hạn 1 chỉ có đúng một luồng nhận lease, không phát sinh vượt ngưỡng concurrency.

## 8. Test Matrix

| Hàm kiểm thử | Nội dung kiểm chứng | Kết quả |
| --- | --- | --- |
| `test_p11_03_ast_purity_and_migration_0018_triggers` | Độ thuần AST của `core/optimization` và trigger bất biến `0018` | PASSED |
| `test_p11_03_concurrency_shared_and_unproven_quota_scopes_and_key_rotation_blocked` | Gộp bucket dùng chung, chưa chứng minh độc lập và chặn xoay key | PASSED |
| `test_p11_03_rate_limit_backoff_retry_budget_fairness_and_active_start_truthfulness` | Giới hạn tốc độ, backoff, cạn retry, fairness aging và tách `RESERVED`/`ACTIVE` | PASSED |
| `test_p11_03_concurrency_threads_lease_expiry_restart_schedules_and_guards` | Đua đa luồng, lease hết hạn, phục hồi sau restart, lịch và kiểm soát quyền | PASSED |

## 9. Exact Commands Executed & 10. Actual Test Results

- Trước commit `9065d87`: `git diff --check; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -m pytest tests/optimization/test_p11_03_scheduler.py tests/departments/test_p11_01_concurrent_intake.py tests/departments/test_p11_02_interrupt_routing.py tests/storage/test_p04_04_sqlite_migrations.py tests/core/test_p04_01_core_contracts_unit.py tests/core/test_p04_06_workflow_lifecycle_unit.py tests/stage_a/test_stage_a_baseline.py -v` đạt `49 passed in 16.20s`, exit code `0`.
- Sau cập nhật tài liệu và allowlist `tests/standards/test_standards_consistency.py`, chạy kiểm chứng nhất quán trước khi commit docs và đồng bộ lên `origin/main`.

## 11. Git Commit SHA

- Commit triển khai mã nguồn, migration `0018` và kiểm thử `P11.03`: `9065d87d35946affea564400da0e4f5b7e423c60` với thông điệp `feat(scheduler): enforce execution concurrency and quota policies`.

## 12. Known Limitations, 13. Verification Level & 14. Remaining Blockers

- Mức xác minh hiện tại: `OFFLINE_INTEGRATION_VERIFIED`.
- Các bài kiểm thử live CLI `agy.exe`, Docker sandbox live và benchmark CrewAI riêng vẫn đang bỏ qua ở môi trường offline; `P11.04` sổ cái tài nguyên và `P11.05` dọn dẹp sau Task cùng handoff Process sẽ được triển khai ở hai bước kế tiếp.
