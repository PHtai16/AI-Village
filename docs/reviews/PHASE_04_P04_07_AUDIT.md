# Báo cáo Kiểm toán Phase 04 / P04.07: Governance, Permission Manager & Human Approval Gate

**Ngày thực hiện:** 2026-11-26  
**Phạm vi kiểm toán:** Tiểu phase `P04.07` trên nhánh `main`  
**Commit tham chiếu trước đó:** [`984f1c963b119736a8f5981a1b582d9744ebcd74`](file:///C:/DoCho/AI-Village/docs/reviews/PHASE_04_P04_06_AUDIT.md) (`P04.06`)  
**Trạng thái nghiệm thu P04.07:** **PASS** *(chưa đánh dấu Gate C PASS hay Phase 04 DONE tại bước này)*

---

## 1. Mục tiêu và Phạm vi

Tiểu phase `P04.07` thiết lập lớp kiểm soát quyền thực thi Fail-Closed cho AI Village trước khi có bất kỳ hành động mang tác động hệ thống nào đi qua ranh giới nghiệp vụ. Trọng tâm triển khai gồm bốn trục chính: bộ định giá chính sách `PolicyEvaluator` theo thứ tự ưu tiên quy tắc tại [`RULE_SPEC.md`](file:///C:/DoCho/AI-Village/docs/standards/RULE_SPEC.md), bộ kiểm soát đường dẫn workspace `validate_workspace_path`, cổng phê duyệt một lần từ con người `ApprovalService` gắn với cơ sở dữ liệu SQLite qua migration `0003`, cùng cầu nối kiểm quyền trực tiếp vào vòng đời điều phối `WorkflowLifecycleController`.

---

## 2. Kiến trúc và Ranh giới Tầng

Toàn bộ mô hình dữ liệu và logic kiểm quyền đặt gọn trong gói mới [`core/governance/`](file:///C:/DoCho/AI-Village/core/governance/__init__.py) tách biệt hoàn toàn khỏi SQLite, UI và tiến trình con:

- Tầng Core thuần túy chỉ phụ thuộc Pydantic v2, thư viện chuẩn Python (`os`, `pathlib`, `hashlib`, `json`, `re`, `fnmatch`, `uuid`, `datetime`), các contract tại `core/contracts`, `core/events`, cùng tiện ích lọc bí mật `redact_secrets` tại `engines/antigravity/diagnostics.py`. Kiểm thử AST xác nhận không có bất kỳ lệnh import `sqlite3`, `storage`, `sqlalchemy`, `subprocess`, `crewai`, `api`, `ui`, `httpx`, `urllib` hay `requests` trong `core/governance/`.
- Tầng lưu trữ bền vững mở rộng tại [`0003_governance_approvals.sql`](file:///C:/DoCho/AI-Village/storage/db/migrations/0003_governance_approvals.sql), [`SQLiteApprovalRepository`](file:///C:/DoCho/AI-Village/storage/repositories/approval_repo.py) và [`SQLiteRepositoryStore`](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py) mà không thay đổi chữ ký hay nội dung các migration cũ `0001` và `0002`.

---

## 3. Hợp đồng Dữ liệu Governance

Tệp [`core/governance/models.py`](file:///C:/DoCho/AI-Village/core/governance/models.py) chuẩn hóa các cấu trúc Pydantic v2 với `extra="forbid"`:

- `TrustedAuthContext`: đóng băng (`frozen=True`) danh tính người gọi gồm `actor_id`, `actor_kind`, `auth_source`, trạng thái `authenticated`, cùng `allowed_actions`, `allowed_tools`, `assigned_task_ids`. Validator từ chối ngay lập tức mọi cấu hình tự nhận `actor_kind="human_operator"` từ nguồn `auth_source="agent_claimed"`.
- `PolicyRule`: ánh xạ schema từ `RULE_SPEC.md` với SemVer, `category`, `targets`, `resources`, `priority`, `effect`, `actions`, `approval_required` và `status`. Ràng buộc mọi quy tắc `REQUIRE_APPROVAL` phải bật `approval_required=True`.
- `RoutingContext`: kiểm tra thông số định tuyến `provider_id`, `connection_id`, `model_id`, `data_residency`, `target_residency`, `billing_scope`, `estimated_cost_usd`, `budget_limit_usd`, `allowed_providers`, `allowed_connections`, `quota_remaining`. Không chứa bí mật và bảo toàn giá trị `None` cho `quota_remaining` khi nhà cung cấp chưa cung cấp số liệu.
- `PolicyRequest` và `PolicyDecision`: gắn chặt timestamp có múi giờ, tham số đã lọc bí mật, `request_fingerprint` SHA-256 64 ký tự hex, `rule_versions`, cờ `system_deny` và `audit_event_id`.
- `ApprovalRequest`, `ApprovalPolicyContext` và `ApprovalDecision`: quản lý trạng thái `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `CONSUMED` cùng phiên bản lạc quan `version`.

---

## 4. Schema Migration `0003` và Bền vững SQLite

Tệp [`0003_governance_approvals.sql`](file:///C:/DoCho/AI-Village/storage/db/migrations/0003_governance_approvals.sql) nâng phiên bản schema lên `3`, bổ sung bảng `approval_requests` với các ràng buộc:

- Khóa ngoại liên kết có kiểm soát tới `tasks(task_id)`, `runs(run_id)`, `runs(run_id, task_id)` và `events(event_id)` cho ba sự kiện tạo, quyết định và tiêu thụ phê duyệt.
- Ràng buộc `CHECK` khóa chặt `requester_kind`, `status`, độ dài 64 hex của `request_fingerprint`, tính hợp lệ JSON của `policy_snapshot_json` và bộ đếm `version >= 1`.
- Ba chỉ mục `idx_approvals_status`, `idx_approvals_task_run`, `idx_approvals_fingerprint`.
- [`SQLiteRepositoryStore`](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py) bổ sung `transaction_scope()` dùng kết nối theo luồng `_local.tx_conn` kết hợp `shared_conn_provider` trên [`SQLiteEventJournal`](file:///C:/DoCho/AI-Village/storage/repositories/event_journal.py), cho phép ghi nhận sự kiện kiểm toán và tiêu thụ phê duyệt cùng hành động đích trong đúng một giao dịch `BEGIN IMMEDIATE` không bế tắc khóa SQLite.

---

## 5. Bộ Định giá Chính sách `PolicyEvaluator`

[`core/governance/evaluator.py`](file:///C:/DoCho/AI-Village/core/governance/evaluator.py) thực thi đánh giá tất định theo thứ tự nghiêm ngặt:

1. Kiểm tra `TrustedAuthContext`: thiếu ngữ cảnh hoặc `authenticated=False` bị từ chối với `MISSING_AUTH_CONTEXT`; ngữ cảnh giả mạo `agent_claimed` bị chặn với `UNTRUSTED_OPERATOR_CLAIM`.
2. Bất biến hệ thống `system_deny=True`: cấm xoay vòng chứng thực `connection.rotate_credential`; chặn `worker_agent` và `master_agent` tự gọi `approval.decide`, `settings.update`, `connection.manage`, `tool.configure` hoặc tự chuyển Task sang `COMPLETED` bỏ qua QA; chặn `worker_agent` gán tác nhân hay xóa thùng rác.
3. Ranh giới workspace, danh sách công cụ cho phép `tool.invoke`, chính sách định tuyến và ngưỡng ngân sách.
4. Quy tắc `allowed_actions` của tác nhân và tập `PolicyRule` đang `ACTIVE` theo đúng độ ưu tiên bất kể giá trị `priority` số học: `DENY` > `REQUIRE_APPROVAL` > `ALLOW`. Quy tắc `AUDIT_ONLY` hoặc tập rỗng tuyệt đối không cấp quyền `ALLOW` mà luôn rơi về `DEFAULT_DENY`.

---

## 6. Bảo vệ Ranh giới Workspace

Hàm [`validate_workspace_path`](file:///C:/DoCho/AI-Village/core/governance/workspace.py) áp dụng đồng thời kiểm tra từ vựng và chuẩn hóa liên kết thật qua `os.path.realpath`:

- Từ chối ngay lập tức đường dẫn chứa `..` với `PATH_TRAVERSAL_DENIED`.
- Chặn đường dẫn nằm ngoài `workspace_root` kể cả khi đi qua symlink hoặc NTFS directory junction trên Windows với `PATH_OUTSIDE_WORKSPACE`.
- Ngăn truy cập sang workspace của Task khác thông qua `cross_task_roots` với `CROSS_TASK_ACCESS_DENIED`.
- Chặn đọc ghi tệp nhạy cảm như `.env`, `.env.*`, `credentials.json`, `token.json`, `id_rsa`, `id_ed25519`, `*.pem`, `*.key`, `.ssh/`, `secrets/`, `.runtime/` với `SECRET_FILE_ACCESS_DENIED`.
- Khóa thao tác ghi hoặc xóa lên thư mục hệ thống `core/`, `engines/`, `agents/`, `storage/`, `.git/`, cùng `AGENTS.md` và `pyproject.toml` với `PROTECTED_PATH_DENIED`.

---

## 7. Danh sách Công cụ và Chính sách Định tuyến

Mọi lời gọi `tool.invoke` phải mang mã công cụ chuẩn `tools.<group>.<name>`, nằm trong tập `registered_tools` nếu evaluator được cấu hình, và xuất hiện trong `auth_context.allowed_tools`. Công cụ chưa đăng ký bị chặn với `UNKNOWN_TOOL_ID`; công cụ ngoài quyền của worker bị chặn với `TOOL_NOT_ALLOWED`.

Đối với định tuyến `RoutingContext`:

- Luồng dữ liệu `local_only` cố gắng chuyển ra `target_residency="cloud"` bị chặn fail-closed `LOCAL_ONLY_VIOLATION` kèm `system_deny=True`.
- `provider_id` hoặc `connection_id` ngoài danh sách cho phép lần lượt nhận `PROVIDER_NOT_ALLOWED` và `CONNECTION_NOT_ALLOWED`.
- Chi phí dự kiến vượt `budget_limit_usd` trả về `APPROVAL_REQUIRED` với mã `BUDGET_EXCEEDED`.
- Trường `quota_remaining=None` giữ nguyên giá trị `None` khi không có dữ liệu từ nhà cung cấp, không thay thế bằng số giả định.

---

## 8. Cổng Phê duyệt Con người và Tiêu thụ Một lần

[`ApprovalService`](file:///C:/DoCho/AI-Village/core/governance/approvals.py) và [`SQLiteApprovalRepository`](file:///C:/DoCho/AI-Village/storage/repositories/approval_repo.py) đảm bảo các thuộc tính an toàn sau:

- `create_approval_request`: ghi trước sự kiện `approval.requested` vào nhật ký rồi mới lưu bản ghi `PENDING` kèm `policy_snapshot` phiên bản quy tắc hiện hành.
- `decide_approval`: chỉ chấp nhận `human_operator` đã xác thực không mang nguồn `agent_claimed`, cấm `requester_id == decider.actor_id` (`SELF_APPROVAL_FORBIDDEN`), kiểm tra quá hạn trước khi cấp `APPROVED` hoặc `REJECTED`, và ghi `approval.resolved`.
- `verify_and_consume`: từ chối nếu `system_deny=True` (`SYSTEM_DENY_NOT_OVERRIDABLE`), kiểm tra trạng thái hiện tại, từ chối tái sử dụng bản ghi đã `CONSUMED` (`APPROVAL_ALREADY_CONSUMED`), kiểm tra quá hạn ngay cả trên bản ghi đã `APPROVED` và chuyển trạng thái `APPROVED` sang `EXPIRED` trước khi báo lỗi `APPROVAL_EXPIRED`, đồng thời đối chiếu chéo `action`, `target_resource`, `task_id`, `run_id` và `request_fingerprint`.
- Khi sửa lỗi phát hiện trong lúc chạy kiểm thử tích hợp: phương thức `update_decision` tại `storage/repositories/approval_repo.py` được điều chỉnh để cho phép cập nhật trạng thái từ `('PENDING', 'APPROVED')` sang `EXPIRED` khi chứng thực tiêu thụ phát hiện phê duyệt đã hết hạn, ngăn lỗi `StaleStateError` che lấp mã lỗi nghiệp vụ `APPROVAL_EXPIRED`.
- Trong `consume_and_execute`: cập nhật `status='CONSUMED'`, chèn sự kiện `governance.decision_recorded` (`outcome="APPROVAL_CONSUMED"`) và chạy `action_fn()` trong cùng giao dịch SQLite duy nhất. Mọi ngoại lệ trong `action_fn()` kích hoạt rollback hoàn toàn cả mutation DB lẫn trạng thái `CONSUMED`, giữ bản ghi ở trạng thái `APPROVED` sạch sẽ.

---

## 9. Permission Manager và Liên kết Vòng đời Task/Run

[`PermissionManager`](file:///C:/DoCho/AI-Village/core/governance/manager.py) bắt buộc ghi nhật ký kiểm toán `governance.decision_recorded` qua `evaluate_and_audit()` trước khi hành động chạy; nếu nhật ký từ chối hoặc phát sinh lỗi ghi, `GovernanceAuditError` được ném ra và hàm đích không bao giờ được thực thi. Ma trận quyền `PERMISSION_MATRIX_ACTIONS` bao phủ các thao tác nhạy cảm từ Mission Control gồm `kanban.transition`, `files.delete`, `tool.configure`, `connection.manage`, `connection.change_provider`, `trash.purge`, `settings.update` và các thao tác vòng đời Task/Run.

Thông qua `PermissionManager.build_lifecycle_hook(auth_context)`, [`WorkflowLifecycleController`](file:///C:/DoCho/AI-Village/core/tasks/lifecycle.py) kiểm tra quyền trên các thao tác khởi chạy `agent.execute`, hủy `task.cancel`, và chuyển nhà cung cấp khi fallback `connection.change_provider`. Tác nhân `worker_agent` bị chặn `PERMISSION_DENIED` khi cố hủy Task/Run hoặc thực hiện fallback liên nhà cung cấp; `master_agent` cũng bị chặn fallback tự động sang nhà cung cấp khác nếu chưa qua phê duyệt con người.

---

## 10. Bằng chứng Kiểm thử

Hai lệnh kiểm chứng chạy thực tế sau chỉnh sửa cuối cùng đều đạt 0 lỗi:

1. Kiểm thử mục tiêu Governance và Storage:
   - Lệnh: `python -m pytest tests/core/test_p04_07_governance_unit.py tests/integration/test_p04_07_governance_persistence.py tests/storage -v`
   - Kết quả: **`32 passed in 4.56s`** (0 failed, 0 skipped; bao gồm 8 test đơn vị trong [`test_p04_07_governance_unit.py`](file:///C:/DoCho/AI-Village/tests/core/test_p04_07_governance_unit.py), 4 test tích hợp SQLite/restart/rollback/hook trong [`test_p04_07_governance_persistence.py`](file:///C:/DoCho/AI-Village/tests/integration/test_p04_07_governance_persistence.py), và 20 test hiện có tại `tests/storage/`).
2. Hồi quy toàn bộ kho kiểm thử và kiểm tra khoảng trắng:
   - Lệnh: `python -m pytest tests -v -rs` và `git diff --check`
   - Kết quả: **`161 passed, 24 skipped in 14.95s`**, `git diff --check` thoát với mã `0`.
   - Chi tiết 24 mục `SKIPPED`: 2 bài benchmark/live CrewAI thiếu gói `crewai` ngoài phạm vi ADR-0002; 22 bài live integration CLI của Phase 02–03 chỉ kích hoạt khi đặt biến môi trường `AI_VILLAGE_RUN_LIVE=1`.

---

## 11. Kiểm toán Bảo mật

- Không lưu khóa API thô hay thông tin nhạy cảm trong `core/governance/*.py`, `storage/repositories/approval_repo.py` hay cấu trúc bảng `approval_requests`. Mọi trường đầu vào (`actor_id`, `parameters`, `risk_summary`, `decision_reason`, `routing.*`) đều qua kiểm tra `ensure_not_secret`, `reject_secret_text`, `validate_safe_payload` và `redact_secrets`.
- Ngăn tuyệt đối việc tự phê duyệt, giả mạo danh tính `human_operator` từ nguồn `agent_claimed`, tái sử dụng mã phê duyệt một lần, sửa đổi tham số sau khi duyệt qua SHA-256 fingerprint, và bỏ qua các quy tắc bất biến hệ thống (`system_deny=True`).

---

## 12. Giới hạn đã Biết

- Hành động nhạy cảm của Mission Control hiện được bảo vệ qua API nội bộ `PermissionManager` và `WorkflowLifecycleController` ở tầng Core/Storage; các endpoint HTTP và nút bấm giao diện sẽ gắn kết vào ranh giới ứng dụng tại Phase tiếp theo khi xây dựng UI/Backend API.
- Bộ kiểm thử hiện tại xác minh hoàn chỉnh ở chế độ offline với SQLite thật và tiến trình giả lập/hệ thống cục bộ; bài kiểm thử `AI_VILLAGE_RUN_LIVE=1` chưa chạy trong lượt này nên không gắn nhãn `LIVE_VERIFIED`.

---

## 13. Kết luận Nghiệm thu P04.07

Tiểu phase `P04.07` đạt toàn bộ tiêu chí kỹ thuật, bảo mật và bền vững dữ liệu trên mã nguồn thực tế (`161 passed, 24 skipped`, 0 lỗi hồi quy). Đủ điều kiện đóng gói commit `P04.07` và chuyển sang đánh giá nghiệm thu cuối Phase 04 tại Gate C.
