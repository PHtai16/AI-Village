# BÁO CÁO KIỂM TOÁN PHASE 09 / P09.01 BỘ PHẬN HR

- **Phạm vi:** Tiểu phase `P09.01` nghiệp vụ phòng ban HR gồm quản lý agent, thay role, điều chuyển phòng ban, đề xuất tuyển người, cấp/thu hồi kỹ năng và cung cấp Agent status cùng capability metadata cho Explorer.
- **Commit mã nguồn nền:** `4d302c7` trên nhánh `main` của `PHtai16/AI-Village`.
- **Mức xác minh `P09.01`:** `PASS` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- **Trạng thái `Gate F`:** `NOT MET` vì `P09.02` đến `P09.09` chưa triển khai.

## 1. Tái sử dụng hạ tầng và phân tách tầng kiến trúc

`HrDepartmentService` trong `core/agents/hr.py` cùng các hợp đồng tại `core/agents/hr_models.py` bám kỷ luật `ponytail` mức `full` bằng cách tái sử dụng trực tiếp `AgentRegistryService` trên nền migration `0004_agent_registry.sql` và `AgentRecruitmentService` trên nền migration `0009_agent_recruitment.sql`, không mở bảng SQLite song song hay sửa các migration `0001` đến `0012`.

Ranh giới AST của `core/agents/` được giữ nguyên hoàn toàn: không import `sqlite3`, `storage`, `sqlalchemy`, `subprocess`, `crewai`, `api`, `ui`, `httpx`, `urllib`, `requests` hay `core.orchestration`. Việc đọc trạng thái chạy sống từ tầng điều phối đi qua protocol cấu trúc `RuntimeStateReader` tương thích trực tiếp với `WorkerRuntimeStateSnapshot` của `WorkerSessionLifecycleService.get_worker_runtime_state`.

## 2. Cơ chế kiểm soát quyền, kỹ năng và tính trung thực Explorer

1. Mọi thao tác `change_role`, `transfer_department` và `grant_skills` đều chặn cứng mục tiêu `core.master` với mã lỗi `MASTER_IDENTITY_IMMUTABLE` và đi qua `AgentRegistryService.update_agent` để giữ lịch sử revision bất biến, kiểm tra khóa lạc quan `expected_record_version` và không hủy các `Run` đang chạy.
2. Cổng Governance trong `core/governance/evaluator.py` được giữ nguyên bất biến: `worker_agent`, `master_agent`, ngữ cảnh `None` hoặc `agent_claimed` bị từ chối thay đổi role, phòng ban hoặc kỹ năng; điều chuyển phòng ban qua `agent.change_department` và mở rộng `skills.allowed` qua `agent.change_permissions` đều yêu cầu `human_operator` cùng `approval_id` dùng một lần qua `ApprovalService`.
3. Cổng `ApprovedSkillCatalogPort` mặc định gắn `FailClosedEmptySkillCatalog` trước khi Phase 10 mở `skills/approved/`: mọi lời gọi `grant_skills` hay `propose_recruitment` mang kỹ năng chưa có trạng thái `APPROVED` thật trong danh mục đều bị chặn bằng `HrSkillApprovalError` với mã `UNAPPROVED_SKILL`.
4. `AgentExplorerSummary` trong `core/agents/registry.py` được bổ sung `declared_inputs`, `declared_outputs`, `skills_allowed` và `tools_allowed`; `HrDepartmentService.export_explorer_catalog` hợp nhất dữ liệu Registry, 9 `DepartmentManifest`, đề xuất tuyển dụng đang mở qua `AgentRecruitmentService.list_proposals()` và khối `HrAgentRuntimeProjection` đặt `runtime_state_source="NOT_OBSERVED"` khi không gắn `RuntimeStateReader`.

## 3. Rà soát 4 điểm chạm kho mã

| Điểm chạm kho mã | Kết quả kiểm tra | Ghi chú áp dụng |
| :--- | :--- | :--- |
| `tests/standards/test_standards_consistency.py` | Đã cập nhật | Bổ sung `09.01` vào allowlist hoàn thành và kiểm chứng khớp `docs/PROGRESS.md` |
| `tests/stage_a/test_stage_a_baseline.py` | Đạt nguyên trạng | `core/agents` đã nằm sẵn trong `allowed_core_subdirs`; giữ nguyên `agents/` YAML |
| `core/governance/evaluator.py` | Giữ nguyên bất biến | Đầy đủ `agent.update`, `agent.change_department`, `agent.change_permissions`, `agent.recruit` |
| `storage/db/migrations/` | Không thêm migration | Tái sử dụng nguyên vẹn `0004_agent_registry.sql` và `0009_agent_recruitment.sql` |

## 4. Tệp thay đổi và kiểm chứng tự động

- Tệp mới ở lõi và kiểm thử trong commit `4d302c7`: `core/agents/hr_models.py`, `core/agents/hr.py`, `tests/agents/conftest.py`, `tests/agents/test_p09_01_hr_department.py`.
- Tệp cập nhật ở lõi trong commit `4d302c7`: `core/agents/__init__.py`, `core/agents/registry.py`, `core/agents/recruitment.py`.
- Lượt kiểm thử `tests/agents` đạt `38 passed in 13.76s` với 12 ca mới của `P09.01` bao phủ AST boundary, SQLite end-to-end, bảo vệ `core.master`, ma trận actor Governance, `UNAPPROVED_SKILL`, thiếu prerequisite capability, stale lock, bảo toàn `Run` đang chạy, rollback khi `EventJournal` lỗi, `assess_capability_gap -> propose_recruitment -> evaluate -> activate` và đối chiếu `RuntimeStateReader`.
- Lượt chạy toàn bộ bộ kiểm thử cho commit `4d302c7` đạt `281 passed, 25 skipped in 81.66s` và `git diff --check` thoát mã `0`. Hai nhóm bị bỏ qua gồm 23 ca `@pytest.mark.live` khi chưa bật `AI_VILLAGE_RUN_LIVE=1` cùng Docker Desktop daemon, và 2 ca CrewAI do chưa kích hoạt `.runtime/crewai-bench-venv`.
