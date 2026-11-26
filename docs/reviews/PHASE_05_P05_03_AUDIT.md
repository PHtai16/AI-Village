# Báo cáo Kiểm toán Phase 05 / P05.03: Master Planning & Revision History

**Ngày kiểm toán:** 2026-11-26  
**Nhánh / Repository:** `main` (`PHtai16/AI-Village`)  
**Commit nền `P05.02`:** `89045808519837f709e6a2a9442551cf7a28a978`  
**Trạng thái nghiệm thu `P05.03`:** **PASS (Offline End-to-End Verified; không gán `LIVE_VERIFIED`)**

---

## 1. Kiến trúc Tầng Lập kế hoạch (`core/orchestration/planning*`)

Hệ thống tách riêng hợp đồng dữ liệu lập kế hoạch đa bước ở Phase 05 khỏi nguyên mẫu một Worker `P03.01`:

- Các schema `MasterPlan`, `SpikeAgentSpec`, `WorkerHandoff` trong [`core/orchestration/spike_models.py`](file:///C:/DoCho/AI-Village/core/orchestration/spike_models.py) và luồng `run_master_worker_spike` giữ nguyên không chỉnh sửa.
- Mô-đun [`core/orchestration/planning_models.py`](file:///C:/DoCho/AI-Village/core/orchestration/planning_models.py) chuẩn hóa `StructuredUserBrief`, `PlanningClarification`, `PlanRoutingProposal`, `PlanStepSpec`, `PlanCapabilityAssessment`, `MasterWorkPlan`, `StoredMasterPlan`, `PlanRevisionRecord`, `PlanningOutcome` và Protocol `PlanRepository`.
- Mô-đun [`core/orchestration/planning.py`](file:///C:/DoCho/AI-Village/core/orchestration/planning.py) triển khai `MasterPlanningService` cùng `build_master_planning_prompt` và `parse_master_plan_json` kế thừa `parse_strict_json_object`.
- Kiểm thử AST trong [`tests/orchestration/test_p05_03_master_planning.py`](file:///C:/DoCho/AI-Village/tests/orchestration/test_p05_03_master_planning.py) xác nhận hai tệp `planning_models.py` và `planning.py` không import `sqlite3`, `storage`, `sqlalchemy`, `subprocess`, `crewai`, `api`, `ui`, `httpx`, `urllib` hay `requests`.
- Điểm nối [`core/orchestration/__init__.py`](file:///C:/DoCho/AI-Village/core/orchestration/__init__.py) áp dụng cơ chế lazy export theo PEP 562 để tránh vòng phụ thuộc import giữa `core.contracts.compat`, `core.events.journal` và `core.governance`.

---

## 2. Làm rõ Yêu cầu và Kiểm soát Đồ thị Bước (`DAG`)

`MasterPlanningService` xử lý chặt chẽ giai đoạn phân tích trước khi mở bất kỳ `Run` nào:

- Hàm `analyze_brief_completeness` phát hiện mục tiêu mơ hồ, `required_outputs` rỗng hoặc thiếu `acceptance_criteria`, đồng thời lưu kế hoạch trạng thái `NEEDS_CLARIFICATION` kèm danh sách `PlanningClarification`. Hệ thống từ chối gán `apply_plan_to_task` lên kế hoạch `NEEDS_CLARIFICATION` với mã lỗi `PLAN_NOT_APPLICABLE`.
- `MasterWorkPlan` thực thi kiểm tra tính duy nhất của `step_id` và `order`, kiểm tra tồn tại của `depends_on`, cấm tự phụ thuộc bản thân, cấm đề xuất `core.master` làm Worker thực thi bước, và chạy sắp xếp tô-pô Kahn trong `_compute_topological_order` để chặn chu trình phụ thuộc ngay khi xác thực mô hình.
- Hàm `to_checklist_items()` chuyển từng `PlanStepSpec` thành [`ChecklistItem`](file:///C:/DoCho/AI-Village/core/contracts/references.py) có `status="PENDING"`, `assigned_agent_id=None`, `evidence_reference=None` và `completed_at=None`.

---

## 3. Đối chiếu Năng lực và Định tuyến Trong Phạm vi Governance

- `_assess_capabilities` truy vấn trực tiếp [`AgentRegistryService`](file:///C:/DoCho/AI-Village/core/agents/registry.py) với cờ `eligible_only=True` để lọc đúng Worker đang `ACTIVE`, phân tách trạng thái `COVERED_DECLARED`, `COVERED_VALIDATED`, `COVERED_OBSERVED` và gắn cờ `MISSING_CAPABILITY` khi kho nhân sự thật thiếu năng lực thay vì bịa Agent mới.
- Mọi thao tác tạo, sửa và gắn kế hoạch đều đi qua [`PermissionManager`](file:///C:/DoCho/AI-Village/core/governance/manager.py) với action `task.update_plan`: chặn `worker_agent` bằng `PlanGovernanceDeniedError`, chặn nhà cung cấp ngoài `allowed_providers` bằng `PROVIDER_NOT_ALLOWED`, chặn mô hình ngoài `allowed_models` bằng `MODEL_NOT_ALLOWED`, chặn lộ trình `local_only` đẩy ra cloud bằng `LOCAL_ONLY_VIOLATION`, kiểm tra ranh giới đường dẫn workspace qua `validate_workspace_path`, và hỗ trợ Human Approval Gate qua `PlanApprovalRequiredError`.
- Đường gọi Engine trong `create_plan_with_engine` thực hiện qua [`EngineRegistry`](file:///C:/DoCho/AI-Village/engines/registry.py), cho phép thử lại có giới hạn khi JSON chưa chuẩn và dừng fail-closed ở trạng thái `FAILED_VALIDATION` hoặc `BLOCKED_GOVERNANCE` nếu đầu ra vi phạm schema hay chứa khóa bí mật. Quá trình lập kế hoạch không khởi chạy Worker và không đổi `Task.status` sang `RUNNING` hay `COMPLETED`.

---

## 4. Lưu trữ SQLite, Lịch sử Phiên bản và Tính Nguyên tử

- Tệp migration [`storage/db/migrations/0005_master_planning.sql`](file:///C:/DoCho/AI-Village/storage/db/migrations/0005_master_planning.sql) tạo hai bảng `master_plans` và `master_plan_revisions` cùng trigger `trg_master_plan_revisions_no_update` và `trg_master_plan_revisions_no_delete` khóa cứng lịch sử chỉnh sửa kế hoạch ở mức SQLite. Các tệp `0001`–`0004` giữ nguyên không sửa đổi.
- Lớp [`SQLitePlanRepository`](file:///C:/DoCho/AI-Village/storage/repositories/planning_repo.py) gắn vào `store.plans` trên [`SQLiteRepositoryStore`](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py), hỗ trợ khóa lạc quan `record_version` cho `revise_plan` và `apply_plan_to_task` (`StalePlanVersionError`), phát sự kiện `plan.created`, `plan.revised`, `plan.applied` trong bảng `events` và rollback toàn phần nếu bước ghi `Task` thất bại giữa giao dịch.
- Sau khi đóng và mở lại `SQLiteRepositoryStore` trên cùng tệp `.sqlite`, toàn bộ kế hoạch, lịch sử phiên bản, danh sách `ChecklistItem` trạng thái `PENDING` và chuỗi sự kiện kiểm toán khôi phục nguyên trạng.

---

## 5. Kết quả Kiểm thử Thực tế Sau Chỉnh sửa Cuối

Toàn bộ lệnh xác minh chạy trên Python `3.13.13`, SQLite `3.50.4`, PyYAML `6.0.3`, pytest `9.1.1`:

1. `python -c "import core.agents; import core.orchestration; print(core.orchestration.MasterPlanningService)"; python -c "import core.orchestration; import core.agents; print(core.orchestration.MasterWorkPlan)"`: exit `0`, không lỗi vòng import ở cả hai chiều nạp.
2. `python -m pytest tests/orchestration/test_p05_03_master_planning.py -v`: **`7 passed in 4.58s`**.
3. `python -m pytest tests/agents tests/storage tests/core tests/stage_a tests/standards -v`: **`126 passed in 27.99s`**.
4. `python -m pytest tests -v -rs`: **`186 passed, 24 skipped in 44.50s`**, 0 lỗi; 24 mục `SKIPPED` gồm 2 bài CrewAI thiếu gói `crewai` và 22 bài live provider CLI chỉ chạy khi bật `AI_VILLAGE_RUN_LIVE=1`.
5. `git diff --check`: exit `0`, không phát sinh lỗi ký tự trắng thừa hoặc dấu vết xung đột.

---

## 6. Phán quyết Gate 1 (`P05.03`)

Tiểu phase `P05.03` đạt **PASS (Offline End-to-End Verified)**. Mọi kịch bản chạy bằng SQLite thật và `EngineAdapter` giả lập trong bộ test tự động, không gắn nhãn `LIVE_VERIFIED`. Chưa đánh dấu `PHASE 05 DONE` hay mở P05.05–P05.07; đủ điều kiện chuyển tiếp sang triển khai `P05.04 — Dynamic Team Builder`.
