# BÁO CÁO KIỂM TOÁN PHASE 05 / P05.07 VÀ ĐÁNH GIÁ ĐIỀU KIỆN GATE D

- **Phạm vi:** Tiểu phase `P05.07` Agent tuyển mới và rà soát điều kiện mở khóa `Gate D`.
- **Commit nền:** `6c4437a` trên nhánh `main` của `PHtai16/AI-Village`.
- **Mức xác minh `P05.07`:** `PASS` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- **Trạng thái `Gate D`:** `NOT MET` vì toàn bộ `PHASE 06` từ `P06.01` đến `P06.06` chưa triển khai.

## 1. Thiết kế luồng tuyển mới theo kỷ luật Ponytail

`core/agents/recruitment_models.py` và `core/agents/recruitment.py` triển khai vòng đời tuyển mới `PROPOSED`, `EVALUATED_PASS`, `EVALUATED_FAIL`, `ACTIVATED` và `REJECTED` tách biệt khỏi trạng thái chạy sống. Thay vì nhân bản logic chèn bảng `agent_registry` và `agent_manifest_versions`, `AgentRecruitmentService.activate_candidate()` ủy quyền khâu đăng ký chính thức cho `AgentRegistryService.register_agent()` và tái sử dụng trực tiếp `_manifest_has_sensitive_initial_grants()` cùng `PermissionManager` và `ApprovalService`.

## 2. Kiểm soát đề xuất, đánh giá độc lập và kích hoạt fail-closed

1. `propose_candidate()` xác thực `AgentManifest` qua `AgentRegistryService.validate_manifest()`, loại trừ tuyệt đối `core.master` và `system_role != "worker"`, chặn trùng định danh đã đăng ký hoặc đề xuất đang mở.
2. `evaluate_candidate()` chặn người đề xuất tự đánh giá bằng mã `SELF_EVALUATION_FORBIDDEN`, kiểm tra tính toàn vẹn SHA-256 của Manifest, độ phủ `needed_capabilities`, `EngineRegistry`, `ConnectionRegistry` và `validate_workspace_path()`. Bất kỳ phát hiện lỗi nào đưa đề xuất sang `EVALUATED_FAIL`.
3. `activate_candidate()` chỉ chạy khi trạng thái đạt `EVALUATED_PASS`, hash Manifest không bị trôi sau đánh giá, và hành động `agent.recruit` cùng `agent.create` hoặc `agent.change_permissions` được Governance cho phép. Các quyền nhạy cảm như `tools.allowed`, mạng `intranet`/`full` hoặc phạm vi bộ nhớ `global` bắt buộc có `approval_id` đã duyệt qua `ApprovalService`.

## 3. Lưu trữ SQLite và nhật ký sự kiện

Migration `storage/db/migrations/0009_agent_recruitment.sql` bổ sung bảng `agent_recruitment_proposals` cùng bảng lịch sử bất biến `agent_recruitment_revisions` được khóa sửa và xóa qua hai trigger `trg_agent_recruitment_revisions_no_update` và `trg_agent_recruitment_revisions_no_delete`. `SQLiteRecruitmentRepository` tại `storage/repositories/recruitment_repo.py` gắn vào `SQLiteRepositoryStore.recruitment`, cam kết giao dịch nguyên tử cùng `events.append_on_connection()` cho 4 `EventType` mới là `agent.recruitment_proposed`, `agent.recruitment_evaluated`, `agent.recruitment_activated` và `agent.recruitment_rejected`.

## 4. Bằng chứng kiểm thử P05.07

- `python -m pytest tests/agents/test_p05_07_agent_recruitment.py -v`: `12 passed in 2.55s`, bao phủ kiểm tra AST thuần tại `core/agents/recruitment*.py`, trigger khóa SQLite, vòng đời `propose -> evaluate -> activate` qua restart, cổng duyệt `ApprovalService`, ma trận 8 ca fail-closed qua `@pytest.mark.parametrize`, khóa lạc quan `StaleRecruitmentVersionError` và rollback toàn phần khi `events.append_on_connection()` từ chối.
- `python -m pytest tests/agents tests/storage -v`: `46 passed in 8.22s`.

## 5. Kết quả kiểm tra điều kiện Gate D

Theo `docs/PROGRESS.md`, `Gate D` đòi hỏi nhiệm vụ cần 2 Agent có một cuộc họp thật không dựng sẵn, handoff có định danh, và lỗi QA tìm được phải được sửa trước khi Master bàn giao.

| Tiêu chí Gate D | Trạng thái hiện tại | Bằng chứng và khoảng trống |
| :--- | :--- | :--- |
| Nền tảng nhân sự, kế hoạch, team, session, tuyển mới `PHASE 05` | Đạt đủ 7/7 mục | `P05.01` đến `P05.07` đã hoàn tất ở mức `OFFLINE_INTEGRATION_VERIFIED` |
| Handoff có định danh giữa Worker | Đạt | `P05.06` đã có `hoff_*`, DAG, SHA-256, QA proof và `ACKNOWLEDGED` |
| Cuộc họp thật giữa 2 Agent và giới hạn lượt phản biện | Chưa đạt | `P06.01`, `P06.02`, `P06.03` đang `[ ]` |
| Decision Records, xử lý tranh chấp và Master bàn giao | Chưa đạt | `P06.04`, `P06.05`, `P06.06` đang `[ ]` |

Kết quả rà soát xác nhận **chưa đủ điều kiện mở khóa `Gate D`**. Bước tiếp theo theo đúng lộ trình là triển khai `PHASE 06` trước khi tiến hành nghiệm thu `Gate D`.
