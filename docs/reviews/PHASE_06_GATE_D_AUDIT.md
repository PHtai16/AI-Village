# BÁO CÁO KIỂM TOÁN PHASE 06 VÀ NGHIỆM THU GATE D

- **Phạm vi:** Tiểu phase `P06.01` đến `P06.06` và điều kiện mở khóa `Gate D` thuộc Chặng D trên nhánh `main` của `PHtai16/AI-Village`.
- **Trạng thái `Gate D`:** `PASS` ở mức `Offline End-to-End Verified`.
- **Mức xác minh live CLI:** Chưa chạy `AI_VILLAGE_RUN_LIVE=1` hay `crewai`; không công bố `LIVE_VERIFIED`.

## 1. Kiến trúc Phase 06 và ranh giới tầng

Mã nguồn mới được tách gọn thành ba module thuần thuộc `core/orchestration/` cùng một repository bền vững:

- `core/orchestration/meeting_models.py`: hợp đồng Pydantic v2 cho `MeetingRecord`, `MeetingTurnRecord`, `MasterDecisionRecord`, `WorkerConflictRecord`, `MasterDeliverySummary` và cổng giao thức `CollaborationRepository`.
- `core/orchestration/meetings.py`: dịch vụ `MeetingCoordinatorService` phụ trách `P06.01` đến `P06.04`, dùng chung helper danh tính từ `meeting_models.py`.
- `core/orchestration/delivery.py`: dịch vụ `CollaborationDeliveryService` phụ trách `P06.05` và `P06.06`, tái sử dụng `compute_sha256_file` từ `engines/antigravity/verification.py` và `validate_workspace_path` từ `core/governance/workspace.py`.
- `storage/db/migrations/0010_meetings_collaboration_delivery.sql` và `storage/repositories/collaboration_repo.py`: gắn `SQLiteCollaborationRepository` vào `SQLiteRepositoryStore.collaboration`.

Kiểm thử AST xác nhận ba tệp trong `core/orchestration/` không nhập khẩu `sqlite3`, `storage`, `sqlalchemy`, `subprocess`, `crewai`, `api`, `ui`, `httpx`, `urllib` hay `requests`.

## 2. Cơ chế họp, phản biện và chống đồng thuận giả

1. `P06.01`: `evaluate_meeting_necessity()` cho phép nhiệm vụ đơn giản bỏ qua phiên họp với sự kiện `meeting.skipped` và không sinh bản ghi họp hay hội thoại giả. Khi phức tạp hoặc có lỗi QA cần phân xử, `open_meeting()` chỉ mời `core.master` làm chủ tọa cùng đúng các Worker đang `ACTIVE` được phân công ở `TeamProposal`.
2. `P06.02`: `submit_turn()` hỗ trợ đủ 5 dạng lượt gồm `PROPOSAL`, `OBJECTION`, `EVIDENCE`, `UNCERTAINTY` và `ACTION_ITEM`. Mọi `OBJECTION` không có `evidence_references` bị chặn ngay qua `MeetingEvidenceError`.
3. `P06.03`: giới hạn `max_rounds` và `max_turns` tự động chuyển trạng thái họp sang `NEEDS_MASTER_DECISION` và chặn lượt vượt ngưỡng bằng `MeetingTurnLimitExceededError`. Cơ chế chống đồng thuận giả chặn phát biểu trùng lặp verbatim, chặn đồng thuận chỉ có một người nói và chặn khẳng định đồng thuận khi `OBJECTION` trước đó chưa có phản biện kèm chứng cứ.
4. `P06.04`: `record_master_decision()` lưu `MasterDecisionRecord` chứa `user_visible_rationale` và `acknowledged_dissent` vào bảng `meeting_decisions` có hai trigger `BEFORE UPDATE` và `BEFORE DELETE` bảo vệ lịch sử bất biến. Tham số `substitute_qa=True` bị từ chối fail-closed qua `DecisionSubstituteQaForbiddenError`, đồng thời Worker bị Governance chặn tại quyền `meeting.convene` và `meeting.decide`.

## 3. Tranh chấp Worker và Bàn giao Master

1. `P06.05`: `detect_file_or_task_conflict()` phát hiện ghi trùng đường dẫn tương đối hoặc trùng bước chạy khi hai Worker chưa tuần tự hóa bằng Handoff `ACKNOWLEDGED`. `resolve_worker_conflict()` chỉ cho phép Master đóng tranh chấp khi gắn `decision_id` hoặc `handoff_id` đạt `ACKNOWLEDGED`. `build_handoff_collaboration_history()` tổng hợp chuỗi `StoredWorkerHandoff`, `HandoffRevisionRecord` và danh sách tranh chấp theo Task và Run.
2. `P06.06`: `compile_master_delivery()` rà soát toàn bộ `Artifact` trên đĩa qua SHA-256 thực tế, kiểm tra `VERIFIED_PASS`, `evidence_reference`, liên kết `download_ref` và tranh chấp còn mở. Bất kỳ thiếu sót nào đều cưỡng chế `delivery_verification_status = "NOT_VERIFIED"` và `qa_verified = False`, đồng thời `inv.master_delivery_no_qa_bypass` chặn cờ ghi đè `override_qa_unverified`.

## 4. Ma trận nghiệm thu tiêu chí Gate D

| Tiêu chí Gate D | Thành phần kiểm chứng | Bằng chứng thực thi | Trạng thái |
| :--- | :--- | :--- | :--- |
| Nhiệm vụ 2 Agent có cuộc họp thật, không dựng sẵn | `MeetingCoordinatorService` | `test_p06_ast_purity_and_simple_task_skips_meeting`, `test_p06_01_04_structured_debate_turn_limits_and_decision_restart` | **PASS** |
| Chống vòng lặp tranh luận vô tận và đồng thuận giả | `submit_turn` + Governance `meeting.*` | `test_p06_fail_closed_anti_fake_consensus_and_governance` với 8 ca `@pytest.mark.parametrize` | **PASS** |
| Handoff có định danh và xử lý tranh chấp file/task | `WorkerHandoffService` + `CollaborationDeliveryService` | `test_p06_05_worker_conflict_detection_resolution_and_handoff_history` | **PASS** |
| QA phát hiện lỗi, sửa lỗi và xác minh trước bàn giao | `verify_engine_artifact` + `compile_master_delivery` + `WorkflowLifecycleController` | `test_gate_d_end_to_end_two_agents_meeting_handoff_qa_fix_and_delivery` và `test_p06_06_master_delivery_enforces_not_verified_without_qa_and_blocks_bypass` | **PASS** |
| Lưu trữ bền vững SQLite và khôi phục sau restart | Migration `0010` + `recover_task` + `recover_run` | Cả 2 bài restart trong bộ `test_p06_01_04_meetings.py` và `test_p06_05_06_delivery_and_gate_d.py` đạt `VERIFIED_CLEAN` | **PASS** |

## 5. Bằng chứng kiểm thử và giới hạn

- Bộ kiểm thử Phase 06 và E2E Gate D: `python -m pytest tests/orchestration/test_p06_01_04_meetings.py tests/orchestration/test_p06_05_06_delivery_and_gate_d.py -v` đạt `13 passed in 20.81s`.
- Toàn bộ kho kiểm thử sau lần sửa mã: `python -m pytest tests -v -rs` đạt `235 passed, 24 skipped`, trong đó `22` ca live bỏ qua vì chưa bật `AI_VILLAGE_RUN_LIVE=1` và `2` ca `crewai` bỏ qua vì chưa cài đặt thư viện `crewai`; `git diff --check` thoát mã `0`.
- Giới hạn hiện tại: tất cả ca chạy Engine trong Gate D dùng `AcceptanceFakeAdapter` offline; chưa chạy với CLI `agy.exe` thật hoặc thư viện `crewai`.
