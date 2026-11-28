# Báo cáo kiểm toán hoàn thành PHASE 11 / P11.05

Tài liệu này ghi nhận kết quả kiểm tra, thiết kế kiến trúc, thay đổi cơ sở dữ liệu, kiểm soát an toàn hệ thống tệp và bằng chứng kiểm thử cho hạng mục `P11.05` về dọn dẹp tài nguyên an toàn sau Task, kiểm kê trung thực và bàn giao dữ liệu học hỏi cho Process.

## 1. Executive Summary

Hạng mục `P11.05` đạt mức `OFFLINE_INTEGRATION_VERIFIED` tại commit `30aec5eec7a7b4cd4dd325f87df2cc3e79ac2761` với nhãn rút gọn `30aec5e`. Dịch vụ thuần AST `PostTaskCleanupService` cùng `ManagedTempCleanupExecutor` và `SQLiteCleanupRepository` thực thi dọn dẹp tệp tạm nghiêm ngặt trong `managed_temp_root`, thu hồi lease còn sót, lập bảng kiểm kê bảo toàn `None` cho số liệu thiếu và chuyển giao bản tóm tắt cấu trúc tới `ProcessDepartmentService`.

## 2. Root Causes

1. Sau khi hoàn thành hoặc dừng Run thất bại, hệ thống chưa có cơ chế tự thu hồi `scheduler_execution_leases` còn treo và dọn tệp nháp trong phạm vi thư mục tạm được kiểm soát.
2. Nếu xóa tệp thiếu kiểm soát ranh giới thư mục, symlink hoặc danh sách tham chiếu chéo `artifact_task_references`, tiến trình dọn dẹp có nguy cơ xóa nhầm tệp sản phẩm dùng chung giữa nhiều Task, Vault blob hoặc dữ liệu cơ sở dữ liệu.
3. Chưa có bảng lưu trữ lịch sử dọn dẹp gắn kết trực tiếp với kiểm kê tài nguyên và `task_retrospectives` của Process kèm khóa chống gửi handoff trùng lặp cho cùng một cặp Task và Run.

## 3. Architecture Decisions

- Bổ sung định danh `CleanupId` tiền tố `clnp_` và `ProcessHandoffId` tiền tố `phnd_` tại [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py), xuất tại [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py) và ba sự kiện `resources.cleanup_recorded`, `resources.cleanup_blocked`, `resources.process_handoff_recorded` tại [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py).
- Tách mô hình và giao diện cổng thuần AST tại [cleanup_models.py](file:///C:/DoCho/AI-Village/core/optimization/cleanup_models.py) cùng dịch vụ `PostTaskCleanupService` tại [cleanup.py](file:///C:/DoCho/AI-Village/core/optimization/cleanup.py), xuất qua [__init__.py](file:///C:/DoCho/AI-Village/core/optimization/__init__.py).
- Từ chối dọn dẹp khi Task chưa đạt trạng thái kết thúc `COMPLETED`/`FAILED`/`CANCELLED` với mã `TASK_NOT_TERMINAL`, trừ trường hợp tường minh `force_partial_on_failed_run=True` đối với Run đã vào trạng thái kết thúc `SUCCEEDED`/`FAILED`/`CANCELLED`/`TIMED_OUT`.
- Đẩy thao tác tệp thực tế xuống `ManagedTempCleanupExecutor` tại [cleanup_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/cleanup_repo.py) cài đặt `ManagedTempCleanupExecutorPort`: chặn NUL byte, chặn mọi thành phần symlink bằng `_contains_symlink`, chặn thoát khỏi `managed_temp_root`, chặn đường dẫn Vault, evidence, SQLite DB hoặc tệp chia sẻ nhiều Task với nhãn `BLOCKED_UNSAFE_PATH` hoặc `BLOCKED_SHARED_REF`.
- Tận dụng SQLite savepoint lồng nhau trong `transaction_scope` để gọi tùy chọn `ProcessDepartmentService.compile_task_retrospective` và lưu bản ghi `process_learning_handoffs` cùng trạng thái `DELIVERED_TO_PROCESS` hoặc `PENDING_PROCESS_LINK` trong một giao dịch nguyên tử duy nhất.

## 4. Files Changed

- [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py)
- [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py)
- [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py)
- [cleanup_models.py](file:///C:/DoCho/AI-Village/core/optimization/cleanup_models.py)
- [cleanup.py](file:///C:/DoCho/AI-Village/core/optimization/cleanup.py)
- [__init__.py](file:///C:/DoCho/AI-Village/core/optimization/__init__.py)
- [0020_post_task_cleanup_and_handoffs.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0020_post_task_cleanup_and_handoffs.sql)
- [cleanup_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/cleanup_repo.py)
- [sqlite_store.py](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py)
- [__init__.py](file:///C:/DoCho/AI-Village/storage/repositories/__init__.py)
- [test_p11_05_cleanup_and_handoff.py](file:///C:/DoCho/AI-Village/tests/optimization/test_p11_05_cleanup_and_handoff.py)

## 5. Database Migrations

Tệp [0020_post_task_cleanup_and_handoffs.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0020_post_task_cleanup_and_handoffs.sql) giữ nguyên toàn bộ migration từ `0001` đến `0019` và tạo ba bảng:

1. `post_task_cleanups`: lưu trạng thái dọn dẹp, bảng kiểm kê tổng hợp, danh sách tài nguyên được bảo vệ và chỉ mục duy nhất bán phần cho `idempotency_key`.
2. `post_task_cleanup_items`: ghi chi tiết nối đuôi từng tệp, thư mục, lease, artifact, evidence và bản ghi sổ cái kèm trigger `trg_cleanup_items_no_update` và `trg_cleanup_items_no_delete`.
3. `process_learning_handoffs`: lưu gói bàn giao học hỏi nối với `task_retrospectives`, chỉ mục duy nhất `idx_process_handoffs_unique_task_run` chặn gửi handoff trùng cùng cặp Task và Run, kèm hai trigger `trg_process_handoffs_no_update` và `trg_process_handoffs_no_delete`.

## 6. Security & Governance

- Worker bị từ chối quyền thực thi dọn dẹp và gửi handoff qua `authorize_and_audit` với lỗi `CleanupGovernanceError`.
- Đường dẫn vượt phạm vi `managed_temp_root`, symlink, Vault blob, evidence và đường dẫn dùng chung không bao giờ bị xóa khỏi đĩa mà chuyển sang `BLOCKED_UNSAFE_PATH`, `BLOCKED_SHARED_REF` hoặc `PRESERVED` kèm sự kiện `resources.cleanup_blocked`.
- Dữ liệu ghi chú chứa chuỗi bí mật được redact trước khi lưu vào SQLite.

## 7. Concurrency Behavior

Giao dịch `BEGIN IMMEDIATE` kết hợp savepoint trong `SQLiteCleanupRepository` bảo đảm bước thu hồi lease, lưu kiểm kê, lưu danh sách chi tiết, biên dịch hồi cứu Process và chèn handoff cùng thành công hoặc cùng hoàn tác sạch khi có lỗi trước commit. Cơ chế `idempotency_key` phát lại kết quả cũ khi trùng fingerprint và báo lỗi `IDEMPOTENCY_PAYLOAD_MISMATCH` hoặc `DUPLICATE_PROCESS_HANDOFF` khi có xung đột.

## 8. Test Matrix

| Hàm kiểm thử | Nội dung kiểm chứng | Kết quả |
| --- | --- | --- |
| `test_p11_05_ast_purity_and_migration_0020_immutability` | Độ thuần AST của khối dọn dẹp và trigger bất biến bảng `0020` | PASSED |
| `test_p11_05_safe_temp_containment_blocks_escape_symlinks_vault_and_shared_refs` | Xóa đúng tệp tạm trong `managed_temp_root`, chặn vượt rào, symlink, DB, Vault và tệp dùng chung | PASSED |
| `test_p11_05_blocks_non_terminal_task_releases_leases_and_links_process_handoff` | Chặn Task đang chạy, giải phóng lease treo, giữ `None` và liên kết `task_retrospectives` | PASSED |
| `test_p11_05_idempotency_duplicate_handoff_rbac_rollback_and_restart` | Idempotency, chặn duplicate handoff, chặn Worker, rollback và phục hồi sau restart | PASSED |

## 9. Exact Commands Executed & 10. Actual Test Results

- Trước commit `30aec5e`: `git diff --check; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -m pytest tests/optimization/test_p11_05_cleanup_and_handoff.py tests/departments/test_p11_04_resource_ledger.py tests/optimization/test_p11_03_scheduler.py tests/departments/test_p10_01_process_retrospectives.py tests/storage/test_p04_04_sqlite_migrations.py tests/stage_a/test_stage_a_baseline.py -v` đạt `33 passed in 17.95s`, exit code `0`.
- Sau cập nhật tài liệu kiểm toán, chạy kiểm tra nhất quán và Stage A trước khi commit docs và đồng bộ lên `origin/main`.

## 11. Git Commit SHA

- Commit triển khai mã nguồn, migration `0020` và kiểm thử `P11.05`: `30aec5eec7a7b4cd4dd325f87df2cc3e79ac2761` với thông điệp `feat(resources): add safe cleanup and process handoff`.

## 12. Known Limitations, 13. Verification Level & 14. Remaining Blockers

- Mức xác minh hiện tại: `OFFLINE_INTEGRATION_VERIFIED`.
- Chưa chạy các ca live CLI `agy.exe`, Docker sandbox live hay benchmark CrewAI riêng trong môi trường kiểm thử offline; bước cuối cùng của `PHASE 11` là bộ kiểm chứng tích hợp đầu-cuối cho `Gate G`.
