# Báo cáo kiểm toán hoàn thành PHASE 10 / P10.01

Tài liệu này ghi nhận kết quả phân tích kiến trúc, triển khai mã nguồn, cấu trúc cơ sở dữ liệu, kiểm soát bảo mật và bằng chứng kiểm thử cho hạng mục `P10.01` về tổng hợp hồi cứu Task và lịch sử thực thi dựa trên dữ liệu thực tế.

## 1. Executive Summary

Hạng mục `P10.01` hoàn thành ở mức `OFFLINE_INTEGRATION_VERIFIED` tại commit `258ac105ac5ff9e9df80bed71163eac3de2b2366` với mã rút gọn `258ac10`. `ProcessDepartmentService` chuyển từ lưu trữ trong bộ nhớ sang mô hình tổng hợp dữ liệu thực tế qua `ExecutionHistoryReaderPort` và ghi bền vững theo từng phiên bản qua `ProcessRetrospectiveRepository` trên SQLite.

Cùng đợt này, lỗi khuyết tên công cụ trong `ActivityTimelineProjection` tại `core/events/projections.py` đã được khắc phục để đọc cả trường `tool_name` và `tool_id` từ sự kiện `tool.call_completed` do `ToolRuntime._finalize` phát ra. Toàn bộ bộ kiểm thử `tests/departments/test_p10_01_process_retrospectives.py` bao phủ 20 kịch bản nghiệm thu đều đạt kết quả xanh cùng bộ kiểm thử hồi quy toàn kho với 304 ca đạt và 25 ca bỏ qua có điều kiện môi trường rõ ràng.

## 2. Existing Architecture Analysis

Kiểm tra trực tiếp mã nguồn trước khi triển khai xác định bốn điểm xuất phát của hệ thống:

1. `ProcessDepartmentService` trong `core/departments/process.py` trước đây chỉ lưu `ProcessRetrospectiveRecord` vào danh sách trong bộ nhớ và phụ thuộc hoàn toàn vào `TaskRetrospectiveInput` do caller tự truyền, chưa tổng hợp dữ liệu từ `tasks`, `runs`, `sessions`, `checklist_items`, `tool_executions`, `events`, Vault hay Registry.
2. `ToolRuntime._finalize` trong `core/tools/runtime.py` ghi sự kiện `tool.call_completed` với khóa `tool_id` trong khi `ActivityTimelineProjection` tại `core/events/projections.py` chỉ đọc khóa `tool_name`.
3. `ToolRuntime` cũng ghi đường dẫn tệp đầu ra tương đối vào `ToolExecutionRecord.artifact_ids` đối với các công cụ thao tác tệp thay vì chỉ lưu mã `art_*`. Do đó bộ tổng hợp cần tách rạch ròi mã artifact `art_*` vào `artifact_ids` và đường dẫn workspace vào `output_paths`.
4. Chưa có bảng SQLite nào cho hồi cứu Task và chưa có sự kiện chuẩn cho lịch sử sử dụng Skill tại runtime; `skills/` trên đĩa vẫn trống chờ `P10.03` trở đi và các manifest tại `agents/` khai báo `skills.allowed: []`.

## 3. Implementation Details

Phạm vi sửa đổi và tạo mới gồm năm nhóm chính:

- Sửa lỗi projection tại [projections.py](file:///C:/DoCho/AI-Village/core/events/projections.py#L70-L97): `_format_summary` và `_entry_to_timeline_row` đọc `event.redacted_payload.get("tool_name") or event.redacted_payload.get("tool_id")` để hỗ trợ sự kiện từ `ToolRuntime._finalize` mà vẫn tương thích ngược với sự kiện dùng `tool_name`.
- Mở rộng hợp đồng sự kiện tại [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py#L98-L101): bổ sung `process.retrospective_recorded` và `skill.usage_observed` vào `EventType`.
- Khai báo hợp đồng dữ liệu và cổng sạch tại [process_models.py](file:///C:/DoCho/AI-Village/core/departments/process_models.py): định nghĩa các Pydantic v2 model chế độ `extra="forbid"` gồm `TaskExecutionSummary`, `RetrospectiveStepRecord`, `RetrospectiveToolSummary`, `RetrospectiveErrorRecord`, `RetrospectiveFixRecord`, `RetrospectiveProviderUsage`, `SkillUsageRecord`, `TaskExecutionEvidenceBundle`, `CompileTaskRetrospectiveRequest`, `StoredTaskRetrospective`, `TaskRetrospectiveRevision`, `TaskRetrospectiveOutcome`; hai cổng `ExecutionHistoryReaderPort` và `ProcessRetrospectiveRepository`; các ngoại lệ `ProcessValidationError`, `ProcessTaskNotFoundError`, `ProcessOwnershipError`, `DuplicateRetrospectiveConflictError` và `StaleRetrospectiveVersionError`.
- Triển khai nghiệp vụ tổng hợp tại [process.py](file:///C:/DoCho/AI-Village/core/departments/process.py) và xuất ký hiệu tại [\_\_init\_\_.py](file:///C:/DoCho/AI-Village/core/departments/__init__.py):
  - `compile_task_retrospective` gom chứng cứ thật qua `ExecutionHistoryReaderPort.gather_task_execution_bundle`, tính `source_fingerprint` SHA-256 trên toàn bộ dữ liệu thực thi chuẩn hóa và chỉ tạo mới hoặc tăng revision khi dữ liệu thật thay đổi.
  - Xử lý thời gian và chi phí trung thực qua `DurationSource` gồm `run_telemetry_measured`, `timestamps_measured`, `tool_duration_sum`, `explicit_input` hoặc `UNAVAILABLE`, cùng `CostStatus` gồm `VERIFIED_MEASURED`, `ESTIMATED_FROM_TOOLS_OR_RUN` hoặc `UNAVAILABLE_FROM_PROVIDER`, giữ nguyên `cost_usd=None` khi không có số liệu từ provider hay tool.
  - Phân biệt trạng thái Skill qua `SkillUsageStatus` gồm `OBSERVED`, `DECLARED_OR_ALLOWED_ONLY_NOT_INVOKED` và `UNAVAILABLE_NO_SKILL_DATA`; chỉ đánh dấu `invoked=True` khi có sự kiện `skill.usage_observed` từ nguồn tin cậy thuộc `core_runtime`, `orchestrator`, `agent_runtime` hoặc `tool_runtime`.
  - Tái dựng lỗi và biện pháp khắc phục chỉ dựa trên bằng chứng từ `run_error`, `tool_execution`, `event_journal`, `qa_verification`, `tool_retry_recovery`, `run_retry_recovery`, `qa_reverification` và `conflict_decision`, không tạo dữ liệu giả từ văn bản suy đoán của AI.
  - Bảo toàn tính đọc đối với `tasks`, `runs` và trạng thái kiểm định QA; duy trì tương thích ngược cho phương thức `record_task_retrospective` của `P09.07`.
- Nối tầng lưu trữ SQLite tại [process_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/process_repo.py) và [sqlite_store.py](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py): triển khai `SQLiteProcessRepository` cho cả hai cổng đọc và ghi với giao dịch `BEGIN IMMEDIATE`, khóa lạc quan `record_version`, đối soát `source_fingerprint` và gắn thuộc tính `process` trên `SQLiteRepositoryStore`.

## 4. Database Changes

Tệp migration [0013_process_retrospectives.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0013_process_retrospectives.sql) giữ nguyên toàn bộ các migration `0001` đến `0012` và bổ sung ba bảng mới:

1. `task_retrospectives`: khóa chính `retrospective_id` định dạng `ret_*`, cột `task_id UNIQUE REFERENCES tasks(task_id) ON DELETE RESTRICT` bảo đảm mỗi Task có tối đa một bản ghi chính thống, lưu `active_revision`, `record_version`, `source_fingerprint` dài 64 ký tự SHA-256, `summary_json` hợp lệ chuẩn JSON, `legacy_record_json`, khóa ngoại kép `(primary_run_id, task_id)` tham chiếu `runs(run_id, task_id)` và khóa ngoại `last_audit_event_id` tham chiếu `events(event_id)`.
2. `task_retrospective_revisions`: khóa chính ghép `(retrospective_id, revision_number)` lưu toàn bộ ảnh chụp lịch sử không thể thay đổi, được khóa cứng ở tầng SQLite bằng hai trigger `trg_task_retrospective_revisions_no_update` và `trg_task_retrospective_revisions_no_delete`.
3. `task_retrospective_runs`: bảng nối nhiều Run cho một hồi cứu với khóa chính `(retrospective_id, run_id)` và khóa ngoại kép `(run_id, task_id) REFERENCES runs(run_id, task_id) ON DELETE RESTRICT`, đi kèm chỉ mục `idx_task_retro_runs_run_id` phục vụ truy vấn theo Run ID.

## 5. Event Flow

Luồng ghi sự kiện giữ nguyên lịch sử bất biến của `EventJournal`:

- Khi gọi `compile_task_retrospective`, dịch vụ trước hết kiểm tra quyền qua `authorize_and_audit` phát sự kiện `governance.decision_recorded`.
- Nếu `existing.source_fingerprint` trùng khớp tuyệt đối, hàm kết thúc ở nhánh idempotent với `deduplicated=True` mà không ghi thêm `process.retrospective_recorded` và không ghi đè bản ghi cũ.
- Khi có dữ liệu thực thi mới, `SQLiteProcessRepository.upsert_retrospective_atomic` mở giao dịch `BEGIN IMMEDIATE`, gọi `events.append_on_connection` ghi `process.retrospective_recorded` ngay trong cùng transaction SQLite trước khi cập nhật `task_retrospectives`, chèn dòng mới vào `task_retrospective_revisions` và đồng bộ `task_retrospective_runs`. Nếu bất kỳ thao tác SQL nào thất bại, SQLite rollback toàn bộ cả bảng dữ liệu lẫn dòng sự kiện mới chèn.
- `ActivityTimelineProjection` đọc thống nhất mọi sự kiện có `tool_name` hoặc `tool_id`, hiển thị chính xác công cụ đã gọi theo trình tự `sequence_number`.

## 6. Security Audit

Các kiểm soát an toàn áp dụng và đã kiểm chứng cho `P10.01`:

- Lọc bí mật đầu vào qua `redact_secrets` trong `scrub_text` cho thông điệp lỗi, mô tả khắc phục, đường dẫn và nhãn bước, đồng thời kiểm tra lại toàn bộ `summary` bằng `validate_safe_payload` trước khi ghi xuống SQLite; mọi secret được khử trước khi lưu và không lọt vào payload hay JSON.
- Kiểm tra phạm vi Task và Workspace ở `compile_task_retrospective`: từ chối thực thi nếu `workspace_root` hoặc `target_path` vi phạm chuẩn path an toàn qua `validate_workspace_path`, và chặn tác nhân `worker_agent` khi `task_id` không nằm trong `assigned_task_ids` bằng `ProcessOwnershipError`.
- Kiểm tra quyền đọc ở `get_task_retrospective`, `get_retrospective_by_id`, `get_retrospective_for_run`, `list_retrospective_records` và `list_retrospective_revisions` để Worker không đọc được hồi cứu của Task khác.
- Chỉ chấp nhận sự kiện kỹ năng từ nguồn tin cậy nội bộ, ngăn suy diễn sử dụng Skill từ văn bản AI hoặc từ danh sách `capabilities.skills` của manifest.
- Giữ thuần khiết ranh giới kiến trúc AST tại `core/departments/`: kiểm thử tự động xác minh không import `sqlite3`, `storage`, `api`, `ui` hay thư viện runtime bên ngoài vào `core/departments/`.

## 7. Test Results

Bộ kiểm thử mới [test_p10_01_process_retrospectives.py](file:///C:/DoCho/AI-Village/tests/departments/test_p10_01_process_retrospectives.py) gồm 8 hàm kiểm thử bao phủ đầy đủ 20 kịch bản bắt buộc:

| Hàm kiểm thử | Kịch bản bao phủ | Kết quả |
| --- | --- | --- |
| `test_p10_01_00_ast_boundary_purity_for_process_modules` | Ranh giới AST của `process_models.py` và `process.py` | PASSED |
| `test_p10_01_01_completed_task_full_summary_and_18_timeline_tool_id` | #1 Task thành công đầy đủ trường dữ liệu thật và #18 Timeline nhận `tool_id` | PASSED |
| `test_p10_01_02_03_failed_and_cancelled_tasks_honest_states` | #2 Task thất bại, #3 Task hủy và #8 vắng dữ liệu chi phí giữ `None` | PASSED |
| `test_p10_01_04_06_07_17_multi_run_tool_errors_retries_and_task_run_lookup` | #4 Đa Run, #5 Đa Tool, #6 Lỗi Tool, #7 Retry hồi phục và #17 Truy vấn Task/Run | PASSED |
| `test_p10_01_09_10_skill_usage_truthfulness_declared_vs_actually_invoked` | #9 Không có Skill và #10 Chỉ ghi nhận Skill khi có sự kiện `skill.usage_observed` tin cậy | PASSED |
| `test_p10_01_11_12_optional_fields_secrets_and_cross_task_security` | #11 Thiếu trường tùy chọn, #12 Khử secret và chặn đọc ghi trái quyền giữa các Task | PASSED |
| `test_p10_01_13_14_15_16_dedup_concurrency_rollback_restart_and_triggers` | #13 Chống ghi trùng, #14 Ghi đồng thời đa luồng, #15 Rollback SQLite, #16 Phục hồi sau restart và trigger bất biến | PASSED |
| `test_p10_01_19_20_never_alters_qa_verification_or_task_lifecycle` | #19 Không đổi trạng thái QA và #20 Không sửa vòng đời Task/Run khi AI tự nhận DONE | PASSED |

Nhóm kiểm thử mục tiêu gồm `test_p10_01_process_retrospectives.py`, `test_p09_05_07_departments.py`, `test_gate_f_end_to_end.py`, `test_p04_03_event_journal_unit.py` và `test_p04_04_sqlite_migrations.py` đạt `36 passed in 14.35s`.

## 8. Regression Results

Tại commit mã nguồn `258ac10`, lệnh `git diff --check` không báo lỗi định dạng khoảng trắng và lệnh `python -m pytest tests -v -rs` đạt `304 passed, 25 skipped in 84.38s` với 0 ca thất bại. Toàn bộ 25 ca bỏ qua thuộc ba nhóm điều kiện ngoại vi giữ nguyên từ Chặng F:

- 22 ca kiểm thử live CLI, provider contract, Gate B và agent runtime yêu cầu biến môi trường `AI_VILLAGE_RUN_LIVE=1`.
- 2 ca benchmark CrewAI yêu cầu gói `crewai` không cài trong môi trường Python 3.13 chính của kho.
- 1 ca Docker sandbox live trong `tests/tools/test_code_and_mcp_tools.py` yêu cầu `AI_VILLAGE_RUN_LIVE=1` và Docker Desktop đang chạy.

Sau bước cập nhật `docs/PROGRESS.md`, `tests/standards/test_standards_consistency.py`, `tests/README.md`, `.agents/rules/project-memory.md` và báo cáo này, toàn bộ bộ kiểm thử cùng `git diff --check` được chạy lại một lượt nữa trước khi tạo commit tài liệu và đẩy lên `origin/main`.

## 9. Known Limitations

- Hiện chưa có runner thực thi gói Skill trong `skills/`, nên các Worker chạy tác vụ thường ngày chưa tự phát sự kiện `skill.usage_observed`; trạng thái kỹ năng được ghi nhận trung thực là `DECLARED_OR_ALLOWED_ONLY_NOT_INVOKED` hoặc `UNAVAILABLE_NO_SKILL_DATA`. Khả năng thu thập thực tế đã sẵn sàng cho `skill.usage_observed` của các phase kế tiếp.
- Các bản ghi mẫu lỗi lặp `SkillCandidateProposal` trong `ProcessDepartmentService.detect_patterns_and_propose_skills`, cùng dữ liệu miền của `ResourcesDepartmentService`, `ServiceUxDepartmentService` và hàng đợi của `ReceptionDepartmentService`, vẫn nằm trong bộ nhớ kết hợp `EventJournal` và nằm ngoài phạm vi bảng mới của `P10.01`, sẽ giải quyết tiếp ở `P10.02+` và `PHASE 11`.
- Mức nghiệm thu của `P10.01` là `OFFLINE_INTEGRATION_VERIFIED`; chưa kích hoạt vòng chạy live CLI qua `agy.exe` trong đợt kiểm chứng này.

## 10. Git Commit SHA

- Commit triển khai mã nguồn, migration và kiểm thử `P10.01`: `258ac105ac5ff9e9df80bed71163eac3de2b2366` với nhãn rút gọn `258ac10` và thông điệp `feat(process): implement persistent task retrospectives`.
- Commit đồng bộ `docs/PROGRESS.md`, `docs/reviews/PHASE_10_P10_01_AUDIT.md`, `.agents/rules/project-memory.md`, `tests/README.md` và `tests/standards/test_standards_consistency.py` được tạo kế tiếp trên nhánh `main` sau lượt kiểm chứng cuối cùng.

## 11. Next Step

Chuyển sang `P10.02`: tổng hợp phân tích mẫu lặp từ dữ liệu `task_retrospectives` và `task_retrospective_runs` qua nhiều Task để phát hiện lỗi lặp, thao tác dư thừa và đề xuất cải tiến có chứng cứ định lượng làm đầu vào cho ứng viên kỹ năng ở `P10.03`.
