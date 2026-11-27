# BÁO CÁO KIỂM TOÁN PHASE 05 / P05.06: EVIDENCE-BASED WORKER HANDOFF

- **Phạm vi:** Tiểu phase `P05.06` và bộ kịch bản tích hợp Section VII cho cặp `P05.05 + P05.06`.
- **Commit nền:** `593005f` (`P05.05`).
- **Mức xác minh:** `OFFLINE_INTEGRATION_VERIFIED`.
- **Trạng thái Gate tổng hợp Phase 05:** Chưa mở khóa Gate D; chưa triển khai `P05.07`.

## 1. Kiến trúc Handoff

Luồng bàn giao giữa hai Worker A và Worker B vận hành thông qua `WorkerHandoffService` tại `core/orchestration/handoff.py` và các mô hình dữ liệu thuần Pydantic v2 tại `core/orchestration/handoff_models.py`. Hai tệp thuộc tầng `core/orchestration/` chỉ nhập khẩu hợp đồng miền và không phụ thuộc trực tiếp vào `sqlite3`, giữ đúng ranh giới kiến trúc của dự án.

Điểm kết nối với tầng thực thi nằm tại `WorkerSessionLifecycleService` trong `core/orchestration/worker_sessions.py`. Khi `WorkerExecutionRequest.require_acknowledged_handoffs` được bật cho một bước có phụ thuộc upstream, dịch vụ session bắt buộc mọi Handoff trỏ vào `target_step_id` phải tồn tại và đạt trạng thái `ACKNOWLEDGED` trước khi cho phép khởi tạo session hoặc kích hoạt Engine của Worker B.

## 2. Hợp đồng dữ liệu Handoff

Hợp đồng trung tâm là `WorkerHandoffRecord` kèm theo các trạng thái vòng đời `PROPOSED`, `VALIDATED`, `READY`, `DELIVERED`, `ACKNOWLEDGED`, `REJECTED` và `BLOCKED`. Định danh hợp lệ tuân theo mẫu `hoff_*` qua `HandoffId` và hàm `new_handoff_id()`.

Mỗi bản ghi lưu chặt `task_id`, `run_id`, `plan_id`, `plan_revision`, `team_proposal_id`, `source_step_id`, `target_step_id`, `from_agent_id`, `to_agent_id`, `from_session_id`, `to_session_id`, danh sách `HandoffArtifactReference`, `evidence_references`, `decision_references`, `input_context_summary`, `expected_next_action`, `acceptance_requirements`, `HandoffContextPackage`, `correlation_id` và `content_sha256`. Hàm `compute_content_sha256()` tạo dấu vân tay SHA-256 cố định trên tập trường nội dung nhằm phát hiện xung đột hoặc sửa đổi ngầm khi chuyển trạng thái.

Các yêu cầu thao tác được chuẩn hóa qua `HandoffCreateRequest`, `HandoffDeliveryRequest`, `HandoffAcknowledgeRequest` và `HandoffRejectRequest`. Tất cả mô hình áp dụng `ConfigDict(extra="forbid")` và bắt buộc mốc thời gian có múi giờ qua `validate_aware_datetime`.

## 3. Kiểm tra phụ thuộc và DAG

Trước khi tạo bản ghi `READY`, phương thức `create_and_validate_handoff()` đối chiếu toàn bộ chuỗi liên kết thực thi:

1. `Task` và `Run` phải tồn tại, thuộc cùng một định danh và chưa rơi vào trạng thái kết thúc.
2. `MasterWorkPlan` phải ở trạng thái `APPLIED` và `TeamProposal` phải ở trạng thái `ASSIGNED` khớp đúng `plan_revision`.
3. `source_step_id` phải nằm trong `target_step.depends_on` để chặn bàn giao ngược chiều DAG hoặc nhảy bước không có quan hệ phụ thuộc.
4. Nếu đã có bản ghi Handoff chưa bị `REJECTED` trên cùng cặp cạnh bước và chưa chỉ định tường minh `handoff_id`, hệ thống nâng ngoại lệ `DuplicateHandoffConflictError` với mã `DUPLICATE_STEP_HANDOFF`.

## 4. Xác thực danh tính gửi và nhận

`WorkerHandoffRecord` loại trừ hoàn toàn `core.master` khỏi cả hai đầu gửi và nhận. Hành vi tự bàn giao bị chặn mặc định bởi `HandoffValidationError` với mã `SELF_HANDOFF_FORBIDDEN` trừ trường hợp `allow_self_handoff=True`.

Phía gửi `from_agent_id` phải khớp phân công của `source_step_id` và sở hữu `from_session_id` ở trạng thái `CLOSED`. Phía nhận `to_agent_id` phải được phân công cho `target_step_id`, tồn tại dưới trạng thái `ACTIVE` trong `AgentRegistryService`, không lệch phiên bản hoặc hash so với bản ghim trong `TeamProposal`, có đủ `required_tools`, thỏa chính sách lưu trú dữ liệu `local_only` và cấu hình `routing_proposal`. Khi `acknowledge_handoff()` chạy dưới vai trò `worker_agent`, tác nhân gọi phải trùng `to_agent_id` và khớp `to_session_id` đã giao.

## 5. Liên kết Artifact và bằng chứng QA

Phương thức `_validate_and_hash_artifacts()` thẩm định chéo từng `ArtifactId` giữa SQLite Metadata, quyền hệ thống tệp và trạng thái vật lý trên đĩa:

1. Kiểm tra `artifact.task_id`, `artifact.run_id` và `artifact.owner_agent_id` khớp `from_agent_id`.
2. Kiểm tra `validate_workspace_path()`, `receiver_manifest.permissions.fs_read` và `target_step.allowed_read_paths`.
3. Xác nhận tệp tồn tại thực tế trên đĩa và tính lại SHA-256 để khớp `artifact.sha256`. Mọi thay đổi nội dung hoặc mất tệp đều bị chặn với mã `ARTIFACT_SHA256_MISMATCH` hoặc `ARTIFACT_FILE_MISSING` tại cả bước tạo lẫn bước `deliver_handoff()`.
4. Khi `require_verified_artifacts=True`, Artifact bắt buộc đạt `verification_status == "VERIFIED_PASS"` và có `evidence_reference` độc lập.

## 6. Đóng gói ngữ cảnh tối giản

`HandoffContextPackage` giới hạn `source_summary` và `input_context_summary` tối đa 2000 ký tự qua `_reject_full_transcript_or_secrets()`. Hàm kiểm tra từ chối chuỗi có dấu hiệu dump hội thoại dài như cặp `"role":`/`"content":`, mẫu `\nuser:`/`\nassistant:` hoặc `--- full transcript ---`.

Mọi gói ngữ cảnh đều gắn cố định hằng số `UNTRUSTED_UPSTREAM_DATA_NOTICE` ở trường `untrusted_data_notice`, buộc Worker B xem tóm tắt và Artifact từ bước trước là dữ liệu đầu vào chưa đáng tin cậy thay vì chỉ thị hệ thống hoặc quyền mở rộng.

## 7. Kiểm soát Governance và Approval

`core/governance/evaluator.py` bổ sung hành động `agent.handoff` vào `PERMISSION_MATRIX_ACTIONS` với quy tắc tường minh cho `human_operator`, `master_agent`, `worker_agent` và `runtime_service`, không cấp wildcard.

Quy tắc bất biến `inv.handoff_no_privilege_escalation` từ chối mọi yêu cầu `agent.handoff` chứa cờ `privilege_expansion`, `escalate_receiver_permissions`, `bypass_qa` hoặc `grant_tools`. Từng bước `create_and_validate_handoff()`, `deliver_handoff()`, `acknowledge_handoff()` và `reject_handoff()` đều đi qua `_authorize_handoff_action()` kết nối `PermissionManager` và `ApprovalService`.

## 8. Lưu trữ SQLite và tính toàn vẹn lịch sử

Migration `storage/db/migrations/0008_worker_handoffs.sql` bổ sung hai bảng gắn khóa ngoại chặt tới `tasks`, `runs(run_id, task_id)`, `master_plans`, `team_proposals`, `sessions` và `events`:

- `worker_handoffs`: lưu ảnh chụp hiện tại cùng khóa lạc quan `record_version`, dấu băm `content_sha256`, `handoff_json` hợp lệ JSON và các cột chỉ mục truy vấn.
- `worker_handoff_revisions`: lưu chuỗi lịch sử chuyển trạng thái bất biến được bảo vệ bởi hai trigger `trg_worker_handoff_revisions_no_update` và `trg_worker_handoff_revisions_no_delete`.

`SQLiteWorkerHandoffRepository` tại `storage/repositories/handoff_repo.py` được gắn vào `SQLiteRepositoryStore.handoffs`, thực thi `create_handoff_atomic()` và `transition_handoff_atomic()` trong cùng `transaction_scope()` với `events.append_on_connection()`. Nếu ghi `Event` thất bại hoặc `record_version` sai lệch, toàn bộ giao dịch SQLite được rollback.

## 9. Nhật ký sự kiện

Mọi chuyển trạng thái hợp lệ đều sinh bản ghi `Event` loại `agent.handoff` đã làm sạch payload qua `sanitize_event_payload()`, mang nhãn `handoff_phase` tương ứng `ready`, `delivered`, `acknowledged` hoặc `rejected`, gắn `task_id`, `run_id`, `session_id`, `artifact_id` và `correlation_id`. Trình tự sự kiện tuân theo mốc thời gian đơn điệu tăng để vượt qua kiểm tra `validate_journal_entries()` khi replay.

## 10. Khôi phục sau Restart

Khi tạo mới `SQLiteRepositoryStore` trên cùng tệp SQLite sau khi đóng kết nối mô phỏng khởi động lại tiến trình, trạng thái `WorkerHandoffRecord`, `record_version`, chuỗi `HandoffRevisionRecord`, `Session` của Worker A và Worker B cùng `Event` lịch sử được khôi phục nguyên vẹn. Phép giao trùng khớp `to_session_id` khi bản ghi đã ở trạng thái `DELIVERED` giữ tính idempotent và trả nguyên trạng thái đã lưu. Các kịch bản tích hợp cũng xác nhận `recover_task()` và `recover_run()` tiếp tục đối khớp dữ liệu SQLite với Event Journal sau khi có Handoff.

## 11. Kiểm thử tiêm lỗi

Bộ `tests/orchestration/test_p05_06_worker_handoff.py` kiểm chứng các đường lỗi cưỡng bức fail-closed gồm danh tính `core.master`, tự bàn giao, `Task`/`Run` không khớp, `source_step_id` không nằm trong `depends_on`, Worker A chưa đóng session, Worker B bị `DISABLED` hoặc trôi `manifest_revision`, Artifact giả mạo trên đĩa lệch SHA-256, Artifact `PENDING` hoặc thiếu `evidence_reference`, Worker B thiếu quyền `fs_read`, lộ khóa bí mật trong summary, dump full chat transcript, chỉ thị leo thang `inv.handoff_no_privilege_escalation`, xung đột lạc quan `StaleHandoffVersionError` và rollback hoàn toàn khi `events.append_on_connection()` từ chối.

## 12. Kịch bản tích hợp Section VII

Tệp `tests/orchestration/test_p05_05_06_integration_acceptance.py` chạy đủ 5 kịch bản tích hợp A, B, C, D và E liên kết xuyên suốt từ `AgentRegistryService`, `MasterPlanningService`, `DynamicTeamBuilderService`, `WorkerSessionLifecycleService`, `WorkerHandoffService`, `EngineRegistry`, `verify_engine_artifact()`, `SQLiteRepositoryStore` tới `WorkflowLifecycleController` và `recover_task()`/`recover_run()`:

- Scenario A: một Worker thực thi trọn bước, lưu SQLite và khôi phục sau restart.
- Scenario B: hai Worker dùng chung một `EngineConnectionRef` vẫn giữ hai UUID v4 `Session` và `provider_conversation_id` tách biệt qua restart.
- Scenario C: Worker A hoàn tất step 1 kèm Artifact `VERIFIED_PASS`, bàn giao cho Worker B qua `READY -> DELIVERED -> ACKNOWLEDGED`, sau đó Worker B mới chạy step 2 thành công.
- Scenario D: vô hiệu hóa Worker B giữa quy trình giữ nguyên bằng chứng Worker A đã chạy và chặn Worker B ở lớp preflight trước khi chạm Engine.
- Scenario E: ma trận tiêm lỗi sập Engine, hủy đang chạy, giả mạo Artifact SHA-256 và mất kết nối SQLite đều fail-closed sạch sẽ.

## 13. Đánh giá bảo mật

1. Mọi chuỗi đầu vào trong Handoff, rationale từ chối, note xác nhận và payload Event đều đi qua `ensure_not_secret()`, `reject_secret_text()` hoặc `redact_secrets()`.
2. Đường dẫn đọc phạm vi làm việc đi qua `validate_safe_relative_path()` và `validate_workspace_path()`, chặn directory traversal ra ngoài workspace gốc.
3. Gói ngữ cảnh đóng dấu `UNTRUSTED_UPSTREAM_DATA_NOTICE` để tách ranh giới dữ liệu và lệnh điều khiển giữa hai Worker.
4. Bảng `worker_handoff_revisions` khóa cứng cập nhật và xóa ngay ở mức trigger SQLite.

## 14. Giới hạn hiện tại và phạm vi dừng

- Mọi ca kiểm thử Engine trong batch này chạy qua adapter/stub offline đã đăng ký trong `EngineRegistry`; chưa bật `AI_VILLAGE_RUN_LIVE=1` để gọi Antigravity CLI thực tế và chưa cài CrewAI nên nhóm test live/CrewAI vẫn ở trạng thái skipped có điều kiện.
- Chưa mở rộng sang `P05.07` về tuyển mới Agent và chưa mở khóa Gate D của Phase 05.

## 15. Kết quả thẩm định

- **Phán quyết:** `PASS` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- **Kết quả kiểm thử có mục tiêu:**
  - `python -m pytest tests/orchestration/test_p05_05_worker_session_lifecycle.py -v`: `7 passed`
  - `python -m pytest tests/orchestration/test_p05_06_worker_handoff.py -v`: `4 passed`
  - `python -m pytest tests/orchestration/test_p05_05_06_integration_acceptance.py -v`: `5 passed`
  - `python -m pytest tests/orchestration -v`: `31 passed`
- **Kết quả kiểm thử từng phân hệ:**
  - `python -m pytest tests/agents -v`: `14 passed`
  - `python -m pytest tests/storage -v`: `20 passed`
  - `python -m pytest tests/core -v`: `79 passed`
  - `python -m pytest tests/stage_a tests/standards -v`: `13 passed`
- **Kết quả toàn bộ kho mã nguồn:** `python -m pytest tests -v -rs` đạt `210 passed, 24 skipped`, trong đó `22` ca live bỏ qua vì chưa bật `AI_VILLAGE_RUN_LIVE=1` và `2` ca bỏ qua vì chưa cài đặt thư viện `crewai`. Lệnh `git diff --check` trả mã thoát `0`.

