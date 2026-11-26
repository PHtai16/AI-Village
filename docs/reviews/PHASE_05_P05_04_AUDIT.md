# Báo cáo Kiểm toán Phase 05 / P05.04: Capability-Based Dynamic Team Builder

**Ngày kiểm toán:** 2026-11-26  
**Nhánh / Repository:** `main` (`PHtai16/AI-Village`)  
**Commit `P05.03`:** `11ae005c07d8cdc3a62c922e489f9a73d9d20792` (`feat(orchestration): implement structured master planning`)  
**Trạng thái nghiệm thu `P05.04`:** **PASS (Offline End-to-End Verified; không gán `LIVE_VERIFIED`)**

---

## 1. Kiến trúc Ghép Nhóm Động (`core/orchestration/team*`)

Cơ chế tuyển chọn và phân công nhóm làm việc tách riêng khỏi giao diện và tầng cơ sở dữ liệu thông qua Protocol và kiểm soát tĩnh:

- Hợp đồng dữ liệu tại [`core/orchestration/team_models.py`](file:///C:/DoCho/AI-Village/core/orchestration/team_models.py) chuẩn hóa `PinnedAgentAssignment`, `StepAssignmentEntry`, `CandidateEvaluation`, `TeamGovernanceCheck`, `TeamProposal`, `StoredTeamProposal`, `TeamProposalRevisionRecord`, `TeamStalenessReport`, `TeamBuildRequest`, `TeamAssignmentResult` và Protocol `TeamProposalRepository`.
- Bộ máy điều phối nhóm tại [`core/orchestration/team_builder.py`](file:///C:/DoCho/AI-Village/core/orchestration/team_builder.py) triển khai `DynamicTeamBuilderService` nối qua [`AgentRegistryService`](file:///C:/DoCho/AI-Village/core/agents/registry.py), [`PlanRepository`](file:///C:/DoCho/AI-Village/core/orchestration/planning_models.py) và [`PermissionManager`](file:///C:/DoCho/AI-Village/core/governance/manager.py).
- Kiểm thử AST trong [`tests/orchestration/test_p05_04_dynamic_team_builder.py`](file:///C:/DoCho/AI-Village/tests/orchestration/test_p05_04_dynamic_team_builder.py) xác minh hai mô-đun `team_models.py` và `team_builder.py` không import `sqlite3`, `storage`, `sqlalchemy`, `subprocess`, `crewai`, `api`, `ui`, `httpx`, `urllib` hay `requests`.
- Bộ tệp [`agents/`](file:///C:/DoCho/AI-Village/agents/README.md) giữ nguyên đúng hai hồ sơ thật `core.master` và `it.artifact_worker`. Mọi nhân sự nhiều phòng ban dùng để kiểm thử được nạp dưới dạng fixture hợp lệ trong cơ sở dữ liệu SQLite tạm tại `tmp_path`.

---

## 2. Thuật toán Lọc Ứng viên và Tối thiểu Hóa Đội hình

`_compute_proposal` truy vấn cả danh sách đầy đủ để lập nhật ký loại trừ minh bạch lẫn danh sách hợp lệ qua `search_agents(AgentSearchQuery(eligible_only=True))`:

- Loại trừ cứng `core.master` và mọi Agent có `system_role == "orchestrator"` bằng mã `ORCHESTRATOR_NOT_WORKER`. Không cho phép `core.master` đứng tên thành viên hoặc chủ bước thực thi.
- Loại trừ mọi hồ sơ chưa ở trạng thái `ACTIVE` gồm `DRAFT`, `VALIDATED`, `DISABLED` và `REJECTED` qua mã `AGENT_NOT_ACTIVE:<status>`.
- Đối chiếu ba mức xác minh năng lực `declared`, `validated` và `observed_in_execution` dựa trực tiếp vào `capability_verification` của Registry. Khi tác vụ yêu cầu mức `observed_in_execution` nhưng Agent mới có nhãn tĩnh, hệ thống trả về `BLOCKED_MISSING_CAPABILITY` thay vì nâng mức ảo.
- Kiểm tra tương thích `input_formats`, `output_formats`, danh sách tool `tools.allowed` so với `required_tools`, quyền ghi `fs_write` khi bước có `allowed_write_paths`, và chính sách nhà cung cấp/mô hình/`local_only`.
- Hàm `_solve_minimal_team_cover` thực thi thuật toán phủ tập hợp cực tiểu bằng nhánh cận chính xác khi số ứng viên hợp lệ tối đa 16 và quay về greedy xác định khi tập lớn hơn. Một Agent đủ năng lực bao phủ toàn bộ bước sẽ được chọn đơn lẻ mà không kéo phòng ban dư thừa; các tác vụ liên phòng ban chỉ gọi đúng các phòng ban cần thiết.

---

## 3. Ghim Phiên bản Manifest, Phát hiện Sai lệch và Phân công Nguyên tử

- Mỗi thành viên trong `pinned_members` ghi chặt `agent_id`, `manifest_revision`, `manifest_sha256` và `spec_version`. Các trường `quota_remaining` và `estimated_cost_usd` giữ `None` do chưa có dữ liệu đo trực tiếp từ provider trong bước ghép nhóm.
- `check_proposal_staleness` đối soát từng bản ghi ghim với Registry sống, phát hiện `AGENT_MISSING`, `AGENT_NOT_ACTIVE`, `MANIFEST_REVISION_DRIFT` và `MANIFEST_HASH_DRIFT`. Khi phát hiện sai lệch, đề xuất chuyển sang `STALE`, ghi nhận sự kiện `team.stale_detected` và từ chối `apply_team_assignment` bằng `StaleTeamProposalError`.
- `rebuild_team_proposal` cho phép người điều phối tái lập đề xuất gắn phiên bản manifest mới với khóa lạc quan `expected_record_version`, giữ bất biến các phiên bản lịch sử cũ.
- `apply_team_assignment` kiểm tra quyền `agent.assign` và Human Approval Gate qua `dispatch_protected_action`, cập nhật `assigned_agent_id` trên từng `ChecklistItem` trạng thái `PENDING`, chuyển `Task` từ `QUEUED` sang `ASSIGNED` thông qua hàm máy trạng thái chuẩn `transition_task`, đồng thời ghi nhận sự kiện `team.assigned` và `task.status_changed` trong một giao dịch SQLite. Thao tác phân công không tạo `Run`, không chuyển `Task` sang `RUNNING` hay `COMPLETED` và không bịa `completed_at` hoặc `evidence_reference`.

---

## 4. Lưu trữ SQLite, Khóa Lịch sử và Kiểm thử Nghiệm thu Tích hợp

- Migration [`storage/db/migrations/0006_team_proposals.sql`](file:///C:/DoCho/AI-Village/storage/db/migrations/0006_team_proposals.sql) bổ sung hai bảng `team_proposals` và `team_proposal_revisions`, các chỉ mục tìm kiếm theo task/plan/status và cặp trigger `trg_team_proposal_revisions_no_update` cùng `trg_team_proposal_revisions_no_delete` chặn sửa hoặc xóa lịch sử ở mức SQL.
- Lớp [`SQLiteTeamProposalRepository`](file:///C:/DoCho/AI-Village/storage/repositories/team_repo.py) gắn vào thuộc tính `store.teams` của [`SQLiteRepositoryStore`](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py). Tiêm lỗi ghi `Task` giữa giao dịch xác nhận rollback toàn phần không để lại trạng thái nửa vời.
- Bộ kiểm thử chấp nhận tích hợp [`tests/orchestration/test_p05_03_04_integration_acceptance.py`](file:///C:/DoCho/AI-Village/tests/orchestration/test_p05_03_04_integration_acceptance.py) chạy trọn 4 kịch bản thực tế trên SQLite: tác vụ đơn với manifest gốc và khởi động lại Store, tác vụ đa năng lực liên phòng ban trên dữ liệu fixture loại trừ nhân sự dư, tác vụ thiếu năng lực trả trạng thái `PARTIAL` trung thực, và kịch bản sai lệch phiên bản manifest hoặc vô hiệu hóa Agent chặn phân công cũ.

---

## 5. Kết quả Kiểm thử Xác minh Mục IX

Các lệnh xác minh chạy trên Python `3.13.13`, SQLite `3.50.4`, PyYAML `6.0.3`, pytest `9.1.1`:

1. `python -m pytest tests/orchestration/test_p05_03_master_planning.py -v`: **`7 passed in 7.02s`**
2. `python -m pytest tests/orchestration/test_p05_04_dynamic_team_builder.py -v`: **`4 passed in 12.16s`**
3. `python -m pytest tests/orchestration/test_p05_03_04_integration_acceptance.py -v`: **`4 passed in 11.07s`**
4. `python -m pytest tests/agents -v`: **`14 passed in 8.79s`**
5. `python -m pytest tests/storage -v`: **`20 passed in 15.00s`**
6. `python -m pytest tests/core -v`: **`79 passed in 22.98s`**
7. `python -m pytest tests/stage_a tests/standards -v`: **`13 passed in 2.40s`**
8. Bộ kiểm thử toàn kho `python -m pytest tests -v -rs` và `git diff --check` chạy ngay sau lần cập nhật tài liệu cuối cùng trước khi commit bàn giao.

---

## 6. Phán quyết Gate 2 (`P05.04`)

Tiểu phase `P05.04` đạt **PASS (Offline End-to-End Verified)**. Không gắn nhãn `LIVE_VERIFIED` do kiểm thử sử dụng dữ liệu SQLite nội bộ và chưa triển khai vòng đời phiên chạy Worker của `P05.05`. Giữ nguyên trạng thái `PHASE 05: ĐANG TRIỂN KHAI`, chưa đánh dấu Gate D PASS và dừng đúng ranh giới trước `P05.05`.
