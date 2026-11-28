# Báo cáo kiểm toán nghiệm thu CHẶNG G / PHASE 11 — Gate G

Tài liệu này tổng hợp kết quả nghiệm thu tích hợp đầu-cuối cho toàn bộ `PHASE 11 — Interrupts & Resources`, bao gồm các tiểu phase từ `P11.01` đến `P11.05` cùng bản sửa lỗi đồng bộ tái sử dụng artifact chéo Task trong bước phục hồi nhật ký sự kiện.

## 1. Executive Summary

Toàn bộ `PHASE 11` và cổng nghiệm thu `Gate G` đạt mức `OFFLINE_E2E_VERIFIED` tại commit `a2bcac6876fb55e554c7f64eedba557e25e58381` với nhãn rút gọn `a2bcac6`. Luồng tích hợp trên cơ sở dữ liệu SQLite thực chứng minh hệ thống tiếp nhận Task B giữa lúc Task A đang chạy mà không ghi đè trạng thái hay trộn hội thoại của Task A, Master điều phối ưu tiên qua Reception và Execution Scheduler tôn trọng bucket quota dùng chung, hệ thống định tuyến ngắt và sổ cái tài nguyên khử trùng lặp cảnh báo lẫn leo thang ngưỡng `CRITICAL` mà không hủy ngang tác vụ, sau cùng dọn dẹp an toàn trong `managed_temp_root`, bàn giao dữ liệu học hỏi sang Process và phục hồi chính xác qua `recover_task` sau khởi động lại.

## 2. Root Causes & Findings Resolved at Gate G

1. Khi Task B tái sử dụng một artifact do Task A tạo thông qua `VaultDepartmentService.reuse_artifact_in_task`, sự kiện `artifact.recorded` được ghi vào luồng của Task B kèm liên kết trong bảng `artifact_task_references`, nhưng hàm `recover_task` tại [recovery.py](file:///C:/DoCho/AI-Village/storage/repositories/recovery.py) trước đây chỉ đọc `snapshot_task.artifact_ids` và bảng `artifacts` theo `task_id`.
2. Hậu quả là khi tái dựng lịch sử của Task B đang ở trạng thái `RUNNING`, `recover_task` báo nhầm `INVALID_HISTORY` dù liên kết tái sử dụng đã được lưu bền vững trên đĩa.
3. Giải pháp tại `_list_cross_task_referenced_artifact_ids` trong [recovery.py](file:///C:/DoCho/AI-Village/storage/repositories/recovery.py) truy vấn thêm các bản ghi từ `artifact_task_references` nối với `artifact_registry_entries` với điều kiện `e.task_id != task_id`, bảo toàn nguyên tắc fail-closed cho artifact do chính Task đó tạo ra đồng thời khôi phục trạng thái trung thực `NEEDS_RECONCILIATION` cho Task đang chạy dở.

## 3. Consolidated Architecture & Shipped Sub-Phases

| Hạng mục | Thành phần chính | Migration SQLite | Commit triển khai | Mức xác minh |
| --- | --- | --- | --- | --- |
| `P11.01` | `ReceptionDepartmentService`, `SQLiteReceptionRepository`, hội thoại cô lập & refinement | `0016_reception_conversations_and_intake.sql` | `22accc0bf27156b0c1da6a7ab731943cd8515de3` | `OFFLINE_INTEGRATION_VERIFIED` |
| `P11.02` | `InterruptRoutingService`, `SQLiteInterruptRepository`, phân loại 5 chiều & dedup | `0017_interrupts_routing_and_dedup.sql` | `33291b7559f7d31996c13ea8134b9f5312dfb2fa` | `OFFLINE_INTEGRATION_VERIFIED` |
| `P11.03` | `ExecutionSchedulerService`, `SQLiteSchedulerRepository`, bucket quota, lease TTL & lịch | `0018_scheduler_queue_leases_and_schedules.sql` | `9065d87d35946affea564400da0e4f5b7e423c60` | `OFFLINE_INTEGRATION_VERIFIED` |
| `P11.04` | `ResourcesDepartmentService` mở rộng `resources_ledger.py`, `SQLiteResourcesRepository` | `0019_resource_ledger_and_alerts.sql` | `aba62d0e4f876884bd6cc7c6d047936e6e820cc3` | `OFFLINE_INTEGRATION_VERIFIED` |
| `P11.05` | `PostTaskCleanupService`, `ManagedTempCleanupExecutor`, `SQLiteCleanupRepository` | `0020_post_task_cleanup_and_handoffs.sql` | `30aec5eec7a7b4cd4dd325f87df2cc3e79ac2761` | `OFFLINE_INTEGRATION_VERIFIED` |
| `Gate G` | [test_phase_11_gate_g_acceptance.py](file:///C:/DoCho/AI-Village/tests/integration/test_phase_11_gate_g_acceptance.py) & [recovery.py](file:///C:/DoCho/AI-Village/storage/repositories/recovery.py) | `0001`–`0020` | `a2bcac6876fb55e554c7f64eedba557e25e58381` | `OFFLINE_E2E_VERIFIED` |

## 4. End-to-End Gate G Verification Coverage

1. **G1 — Giao Task B khi Task A đang chạy:** Task A nhận lease và chạy ổn định trong `conv_a`; người dùng gửi Task B mức ưu tiên `HIGH` vào `conv_b`; Reception tách biệt hội thoại, tạo Task B trạng thái `QUEUED`, Master xác nhận ưu tiên; Scheduler dùng chung bucket `project:google_gemini:proj_gate_g` giữa `conn_a` và `conn_b`, chặn Task B với mã `BLOCKED_BY_CONCURRENCY` khi Task A đang giữ slot `1/1`, đồng thời giữ nguyên toàn vẹn snapshot và `baseline` của Task A.
2. **G2 — Định tuyến ngắt & Sổ cái tài nguyên:** `InterruptRoutingService` gom sự cố lặp trên Task A với `occurrence_count = 2` và `notification_suppressed = True` không broadcast toàn phòng ban; `ResourcesDepartmentService` lưu cả bản ghi thiếu số liệu từ Provider giữ nguyên `None` lẫn bản ghi ước lượng gắn nhãn `ESTIMATED local pricing gemini-2.5-pro`/`ESTIMATED local pricing gemini-2.5-flash` rồi leo thang từ `WARNING` lên `CRITICAL` trên cả `BUDGET_COST` và `QUOTA`, gửi thông báo và mục `action_inbox_items` tới `core.master` cùng `human_operator` mà không hủy Task A đang chạy.
3. **G3 — Hoàn tất Task A, khởi chạy Task B, dọn dẹp an toàn & Process handoff:** Task A xuất deliverable vào Vault, qua kiểm định QA độc lập kèm `evidence_records`, Task B đăng ký tái sử dụng artifact dùng chung rồi Task A hoàn thành `COMPLETED` và trả lease để Task B lấy slot và chạy `RUNNING`. `PostTaskCleanupService` xóa sạch đúng 1 tệp tạm của Task A trong `managed_temp_root`, chặn xóa tệp ngoài rào và tệp dùng chung `shared_between_a_b.tmp`, bảo toàn Vault artifact, evidence và 3 bản ghi sổ cái, giữ trung thực chi phí `actual_cost_usd_total = 0.45`, `estimated_cost_usd_total = 1.70`, cờ thiếu dữ liệu `has_unavailable_cost_entries = True` và bàn giao liên kết trực tiếp với `task_retrospectives` của `ProcessDepartmentService`.
4. **G4 — Phục hồi sau khởi động lại & Event replay:** Đóng và mở lại `SQLiteRepositoryStore` trên cùng tệp DB xác nhận toàn bộ dữ liệu đa pha bảo toàn nguyên vẹn; `recover_task` cho Task A đạt `VERIFIED_CLEAN` với `snapshot_matches_replay = True`, còn `recover_task` cho Task B đang chạy dở trả về `NEEDS_RECONCILIATION` cùng cảnh báo `UNOBSERVABLE_AFTER_RESTART`.

## 5. Exact Commands Executed & Actual Test Results

- Kiểm chứng nhóm Gate G cùng hồi quy Vault, Stage E và khôi phục: `git diff --check; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -m pytest tests/integration/test_phase_11_gate_g_acceptance.py tests/integration/test_stage_e_cross_stage_sync.py tests/storage/test_p04_05_persistence_acceptance.py tests/departments/test_p09_08_09_departments.py -v` đạt `11 passed in 6.02s`, exit code `0`.
- Kiểm chứng toàn kho tại commit `a2bcac6` trước bước cập nhật tài liệu cuối: `git diff --check; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -m pytest tests -v -rs` đạt `341 passed, 25 skipped in 112.65s`, exit code `0`.
- Danh sách 25 test bỏ qua có lý do: 2 bài kiểm tra CrewAI tại môi trường Python chính do không cài gói `crewai` ngoài `.runtime/crewai-bench-venv`, 22 bài live CLI và provider yêu cầu biến môi trường `AI_VILLAGE_RUN_LIVE=1`, cùng 1 bài Docker sandbox live tại [test_code_and_mcp_tools.py](file:///C:/DoCho/AI-Village/tests/tools/test_code_and_mcp_tools.py#L305) yêu cầu `AI_VILLAGE_RUN_LIVE=1` và Docker Desktop đang chạy.
- Sau khi hoàn tất cập nhật tài liệu kiểm toán cuối, chạy lại toàn bộ `git diff --check` và `python -m pytest tests -v -rs` để bảo đảm trạng thái cuối cùng trước khi commit và push lên `origin/main`.

## 6. Verification Level & Readiness for PHASE 12

- Mức xác minh hiện tại: `OFFLINE_E2E_VERIFIED`.
- Ranh giới kiến trúc giữ nguyên hoàn toàn: toàn bộ mã `core/` vượt qua kiểm tra AST không phụ thuộc SQLite, storage hay UI; toàn bộ migration `0001` đến `0020` nhất quán; không có thay đổi vượt phạm vi sang `api/` hoặc `ui/`.
- Hệ thống sẵn sàng chuyển sang **CHẶNG H / PHASE 12 — API & Realtime** khởi đầu từ `P12.01`.
