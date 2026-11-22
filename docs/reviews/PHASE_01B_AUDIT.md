# Báo cáo Nghiệm thu và Kiểm toán Chéo: PHASE 01B

**Trạng thái PHASE 01B:** `PASS` (đối với phạm vi đặc tả kiến trúc, tiêu chuẩn kỹ thuật và bộ template nền tảng).

> **Ghi chú lộ trình:** PHASE 01B là chặng bổ sung trong quá trình phát triển nhằm chuẩn hóa kiến trúc và hợp đồng dữ liệu trước khi xây dựng mã nguồn. PHASE 01B không nằm trong đánh số gốc PHASE 00–17 của `docs/PROGRESS.md` và không thay thế các điều kiện nghiệm thu của PHASE 00, PHASE 01 hay Gate A.

## A. Executive Summary

- **Mục tiêu PHASE 01B:** Xây dựng hệ thống tài liệu kiến trúc trung tâm (Single Source of Truth), 5 bộ tiêu chuẩn kỹ thuật liên kết chặt chẽ cho Agent, Department, Skill, Rule, Tool cùng bộ template chuẩn để làm nền cho Agent Registry, Skill Registry, Tool Runtime và Permission Manager.
- **Các nhiệm vụ đã thực hiện:** Hoàn tất từ `P01B.01`, `P01B.01-R`, `P01B.02`, `P01B.02A`, `P01B.02A-G`, `P01B.03`, `P01B.04`, `P01B.05`, `P01B.06` (tạo 3 template) đến `P01B.07` (kiểm toán và đồng bộ chéo toàn bộ tài liệu).
- **Tài liệu đầu ra:** 1 tài liệu Blueprint, 5 tài liệu Specification, 3 bộ template tại `docs/templates/`, 1 bộ kiểm thử tự động tại `tests/standards/test_standards_consistency.py` và báo cáo nghiệm thu này.
- **Phạm vi đã kiểm chứng:** Sự tồn tại của tệp tin, cấu trúc Markdown cân bằng, cú pháp YAML hợp lệ, chuẩn đặt tên định danh (`Tool ID`, `Action ID`, `Skill ID`, `Capability ID`), phân tách trạng thái/vòng đời, quy tắc phân quyền Fail-Closed và không rò rỉ thông tin bí mật.
- **Các thành phần chưa triển khai:** Mã nguồn Python/JS của Core Runtime, Permission Manager, Agent/Skill/Tool Registry, Engine Adapter cho Antigravity CLI, giao diện 2D/3D, cơ sở dữ liệu SQLite và các tài liệu riêng của PHASE 00 (`vision.md`, `boundaries.md`, `reuse_inventory.md`, ADR) hoặc README từng thư mục trong PHASE 01 (`P01.02`–`P01.15`).

## B. Specification Inventory

| Thành phần | Đường dẫn | Vai trò | Trạng thái sau P01B.07 |
| :--- | :--- | :--- | :--- |
| System Blueprint | `docs/architecture/SYSTEM_BLUEPRINT.md` | Single Source of Truth cho tầm nhìn và kiến trúc | Đã đồng bộ |
| Agent Spec | `docs/standards/AGENT_SPEC.md` | Chuẩn khai báo nhân sự AI, `system_role` và vòng đời tác vụ | Đã đồng bộ |
| Department Spec | `docs/standards/DEPARTMENT_SPEC.md` | Chuẩn bộ phận chức năng và cơ chế mở rộng `agents/departments/` | Đã đồng bộ |
| Skill Spec | `docs/standards/SKILL_SPEC.md` | Chuẩn quy trình kỹ năng và Phương án C | Đã đồng bộ |
| Rule Spec | `docs/standards/RULE_SPEC.md` | Chuẩn hệ thống quy tắc 8 lớp, Approval Gate và `PolicyDecision` | Đã đồng bộ |
| Tool Spec | `docs/standards/TOOL_SPEC.md` | Chuẩn công cụ, `ToolResult` và ánh xạ Tool ID vs Action ID | Đã đồng bộ |
| Agent Template | `docs/templates/agent/agent.yaml` | Mẫu manifest tĩnh cho Worker Agent (`system_role: worker`) | Đã kiểm chứng |
| Department Template | `docs/templates/department/department.yaml` | Mẫu manifest phòng ban ở trạng thái `INACTIVE` | Đã kiểm chứng |
| Skill Template | `docs/templates/skill/SKILL.md` | Mẫu kỹ năng ở trạng thái `DRAFT` đủ 10 mục hợp đồng | Đã kiểm chứng |
| Standards Test | `tests/standards/test_standards_consistency.py` | Bộ kiểm thử tự động cú pháp YAML, schema và quy tắc ID | 6/6 PASSED |

## C. Cross-Spec Consistency Matrix

| Hạng mục | Agent | Department | Skill | Rule | Tool | Blueprint |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Identity** | PASS (`dept.role_name`, `core.master`, `system_role`) | PASS (`department_id` `snake_case`, không dùng `orchestration`) | PASS (`skill_id` `snake_case`) | PASS (`rule_id` phân cấp dấu chấm) | PASS (`tools.<cat>.<name>`) | PASS (`core.master`, `orchestration`) |
| **Permissions** | PASS (`tools.allowed` dùng Tool ID; `requires_approval` dùng Action ID) | PASS (`default_tools` chỉ khuyến nghị, không tự cấp quyền) | PASS (Không tự nâng quyền; `requirements.tools` dùng Tool ID) | PASS (`actions` dùng Action ID; Deny-Overrides; Fail-Closed) | PASS (`permissions_required` dùng Action ID) | PASS (3 cấp thẩm quyền và Permission Manager) |
| **Runtime Status** | PASS (Task Lifecycle 9 trạng thái có `BLOCKED`, do Runtime xác nhận) | PASS (Phân tách `Task Squad` và `Runtime Task State`) | PASS (Không gộp Skill Lifecycle vào Task Lifecycle) | PASS (`PolicyDecision` 4 trạng thái tách khỏi `Rule.effect`) | PASS (`ToolResult.status` tách khỏi `VERIFIED_PASS/FAIL`) | PASS (Evidence First) |
| **Registry** | PASS (`agents/`, Registry động) | PASS (`agents/departments/`) | PASS (`skills/candidates/`, `approved/`, `schemas/`) | PASS (`core/governance/` quản lý tập trung) | PASS (Tool Registry phân biệt `DRAFT`/`AVAILABLE`) | PASS (Ranh giới 12 thư mục) |
| **Lifecycle** | PASS (9 trạng thái Task Lifecycle) | PASS (`ACTIVE`, `INACTIVE`, `DEPRECATED`) | PASS (7 trạng thái Skill; 6 điều kiện `APPROVED`) | PASS (5 trạng thái Rule; Snapshot theo Run) | PASS (5 trạng thái Tool Availability + 6 Execution Status) | PASS (Vòng đời Phương án C) |
| **Versioning** | PASS (`spec_version`, Registry version) | PASS (`spec_version`) | PASS (SemVer bất biến + Rollback) | PASS (SemVer bất biến + Rollback) | PASS (SemVer + Compatibility + Rollback) | PASS (SemVer cho Vault & Skills) |
| **Evidence** | PASS (`telemetry_status`, cấm bịa token) | PASS (Handoff kèm SHA-256 và test log) | PASS (Test fixture, benchmark A/B, QA report) | PASS (`evidence_requirements`, Audit Trail) | PASS (`ToolResult.evidence`, `artifact_references`, `redacted` logs) | PASS (Traceability Matrix & QA độc lập) |
| **Security** | PASS (Chống đọc secrets; IT chỉ sửa trong workspace/sandbox) | PASS (Cấm tự đổi chính sách hay đẩy production) | PASS (Chống prompt injection; cấm tự chạy script) | PASS (Fail-Closed; Approval không vượt Invariants) | PASS (Canonical path; sandbox; chống retry mù quáng) | PASS (9 Architectural Invariants) |

*Lưu ý:* Không có ô nào ở trạng thái `N/A` do cả 6 tài liệu đều liên kết chặt chẽ trên 8 khía cạnh kiến trúc này.

## D. Findings & Resolutions

| ID | Mức độ | Tài liệu & Vị trí | Mô tả vấn đề | Ảnh hưởng | Cách giải quyết | Tệp đã sửa | Trạng thái |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **AUD-01** | MAJOR | `AGENT_SPEC.md` (Mục 2.1, 3.1, 4); `SYSTEM_BLUEPRINT.md` (Mục 3, 15) | `AGENT_SPEC.md` thiếu trường `system_role` và ràng buộc điều kiện cho Master; Blueprint còn ghi `"orchestration" (hoặc "executive")`. | Dễ gây lỗi khi thiết kế Agent Registry, cho phép Worker tự nhận vai trò Master hoặc sai tên phạm vi. | Bổ sung `system_role` (`worker`/`orchestrator`), chuẩn hóa duy nhất `department: "orchestration"` cho `core.master`, kèm quy tắc validation có điều kiện và cấm tự nâng quyền. | `AGENT_SPEC.md`, `SYSTEM_BLUEPRINT.md` | Đã sửa & kiểm chứng |
| **AUD-02** | MAJOR | `AGENT_SPEC.md` (Mục 1.3, dòng 11) | Ghi đường dẫn Department Registry tại `departments/` thay vì `agents/departments/`. | Gây nguy cơ tạo nhầm thư mục `departments/` ở gốc repository trái kiến trúc. | Cập nhật đúng đường dẫn `agents/departments/`. | `AGENT_SPEC.md` | Đã sửa & kiểm chứng |
| **AUD-03** | MAJOR | `DEPARTMENT_SPEC.md` (Mục 2.1, dòng 26; Mục 3.1) | Mô tả `default_tools` có cụm "hoặc cấp phép mặc định cho nhân viên". | Tạo hiểu nhầm phòng ban tự cấp quyền công cụ cho Agent, vi phạm Invariant số 8. | Sửa câu chữ khẳng định `default_tools` chỉ mang tính khuyến nghị; quyền thực thi do Agent Manifest và Permission Manager quyết định. | `DEPARTMENT_SPEC.md` | Đã sửa & kiểm chứng |
| **AUD-04** | MAJOR | `AGENT_SPEC.md` (Mục 2.6, 3.1, 4) | Ví dụ YAML dùng `skills.allowed` phân cấp dấu chấm (`skills.code_review.standard_audit`) trái chuẩn `skill_id` `snake_case` của `SKILL_SPEC.md`. | Agent Registry không khớp được với Skill Registry khi tra cứu theo `skill_id`. | Chuẩn hóa `skills.allowed` dùng `skill_id` (`^[a-z0-9_]+$`), cập nhật ví dụ sang `git_diff_code_review` và `secret_security_audit`, nêu rõ version được ghim khi Task bắt đầu. | `AGENT_SPEC.md` | Đã sửa & kiểm chứng |
| **AUD-05** | MAJOR | `SKILL_SPEC.md` (Mục 5, Mục 12.1, Mục 12.2) | Hai ví dụ minh họa mẫu đặt `status: "APPROVED"` dù là thiết kế chưa kiểm thử; thiếu danh sách đầy đủ 6 tiêu chí xác nhận `APPROVED` thực tế. | Nguy cơ hệ thống nạp nhầm bản thảo mẫu hoặc cho phép YAML tự khai `APPROVED` vượt qua kiểm định QA. | Chuyển 2 ví dụ về `status: "DRAFT"`; bổ sung đủ 6 điều kiện bắt buộc tại Mục 5 để công nhận Skill `APPROVED`. | `SKILL_SPEC.md` | Đã sửa & kiểm chứng |
| **AUD-06** | MAJOR | `AGENT_SPEC.md` (Mục 2.8, 3.1, 4); `TOOL_SPEC.md` (Mục 12) | `AGENT_SPEC.md` đặt Tool ID (`tools.code.run_custom_script`) vào `permissions.requires_approval` thay vì Action ID (`code.exec_custom`). | Lệch kiểu định danh khi đối chiếu với `Rule.actions` trong `RULE_SPEC.md`. | Chuẩn hóa `permissions.requires_approval` dùng Action ID (`code.exec_custom`) và bổ sung bảng ánh xạ chuẩn Tool ID <-> Action ID <-> Capability ID tại `TOOL_SPEC.md`. | `AGENT_SPEC.md`, `TOOL_SPEC.md` | Đã sửa & kiểm chứng |
| **AUD-07** | MAJOR | `RULE_SPEC.md` (Mục 1.2, dòng 26; Mục 8.2) | Mục 1.2 liệt kê `APPROVAL_REQUIRED` thay cho `REQUIRE_APPROVAL` trong mô tả `Rule.effect`; thiếu bảng ánh xạ 3 tầng với `PolicyDecision` và `ToolResult`. | Dễ gây nhầm lẫn giữa giá trị cấu hình Rule, quyết định của Policy Engine và kết quả của Tool Runtime. | Sửa Mục 1.2 thành `REQUIRE_APPROVAL`, bổ sung bảng ánh xạ 3 tầng tại Mục 8.2, làm rõ `AUDIT_ONLY` không bao giờ tự biến thành `ALLOW`. | `RULE_SPEC.md` | Đã sửa & kiểm chứng |
| **AUD-08** | MINOR | `RULE_SPEC.md` (Mục 12), `TOOL_SPEC.md` (Mục 14), `SYSTEM_BLUEPRINT.md` (Mục 14), `AGENT_SPEC.md` (Mục 3.1) | Ví dụ thiết kế trong `RULE_SPEC.md` và `TOOL_SPEC.md` đặt trạng thái `ACTIVE`/`AVAILABLE`; Traceability Matrix thiếu P01B.03–07; `AGENT_SPEC.md` ghi `manifest.yaml` thay vì `agent.yaml`. | Gây hiểu nhầm ví dụ minh họa là cấu hình đang chạy; khó truy xuất đầy đủ giai đoạn P01B trong Blueprint. | Chuyển các ví dụ thiết kế Rule/Tool về `status: "DRAFT"`; cập nhật Traceability Matrix trong Blueprint (giữ nguyên REQ-05–17 là chưa triển khai); ưu tiên tên tệp `agent.yaml`. | `RULE_SPEC.md`, `TOOL_SPEC.md`, `SYSTEM_BLUEPRINT.md`, `AGENT_SPEC.md` | Đã sửa & kiểm chứng |
| **AUD-09** | INFORMATIONAL | `AGENTS.md` (Mục 3, dòng 35) | `AGENTS.md` còn câu lưu ý cũ rằng trường `system_role` đang chờ đồng bộ vào `AGENT_SPEC.md`. | Không ảnh hưởng hợp đồng kỹ thuật; chỉ là ghi chú cũ sau khi P01B.07 đã đồng bộ xong. | Giữ nguyên `AGENTS.md` trong P01B.07 đúng giới hạn tệp được phép sửa; ghi nhận để cập nhật câu chữ nếu mở đợt rà soát quy trình sau. | Không sửa (ngoài phạm vi P01B.07) | Đã ghi nhận |

## E. Verification Evidence

Các kiểm chứng thực tế đã chạy sau khi hoàn tất toàn bộ chỉnh sửa:

1. **Kiểm tra bộ kiểm thử tự động (`pytest`):**
   - Lệnh: `python -m pytest tests/standards/test_standards_consistency.py -v`
   - Kết quả: `6 passed` (Python 3.13.13, pytest 9.1.1, PyYAML 6.0.3).
   - Phạm vi đã xác minh bằng code: sự tồn tại của 6 tài liệu và 3 template; cân bằng cặp dấu ```` ``` ```` trên mọi file Markdown; không có chuỗi rò rỉ bí mật (`AIza...`, `ghp_...`, `sk-...`, private keys); phân tích YAML (parse thực tế) toàn bộ 3 template và 10 ví dụ nhúng trong 5 tài liệu Specification khớp schema, enum và quy tắc đặt tên ID.
2. **Kiểm tra cấu trúc và ranh giới thư mục:**
   - Lệnh: `Get-ChildItem -Path agents,skills -Recurse -Force` xác nhận thư mục `agents/` và `skills/` chỉ chứa `.gitkeep`, không có Agent hay Skill thực tế nào được kích hoạt.
3. **Kiểm tra quy chuẩn văn phong và Git diff:**
   - Rà soát từ cấm và dấu gạch ngang dài (em dash) bằng PowerShell trên 6 tài liệu: không phát hiện vi phạm.
   - Kiểm tra `git diff --check` và `git status` để bảo đảm không có lỗi khoảng trắng dư thừa và không có tệp nào ngoài phạm vi cho phép.

## F. Open Issues

1. **Ghi chú cũ tại `AGENTS.md` (Mục 3, dòng 35):** Đề cập `system_role` đang chờ đồng bộ; cần cập nhật câu chữ nếu có đợt rà soát `AGENTS.md` trong tương lai (không sửa trong P01B.07 vì ngoài phạm vi tệp cho phép).
2. **Các thành phần của PHASE 00 và PHASE 01 chưa hoàn thiện:** Các tài liệu `docs/architecture/vision.md`, `boundaries.md`, `reuse_inventory.md`, ADR chọn stack (`P00.01`–`P00.04`), `.env.example` (`P01.01`) và `README.md` cho từng thư mục kỹ thuật (`P01.02`–`P01.15`) vẫn ở trạng thái chưa làm; Gate A chưa đóng.
3. **Năng lực Runtime và Engine chưa kiểm chứng:** Toàn bộ cơ chế thực thi Antigravity CLI, streaming events, session isolation, Permission Manager, Tool Runtime và UI 2D/3D mới dừng ở mức đặc tả thiết kế và sẽ được kiểm chứng bắt đầu từ các phase triển khai tiếp theo.

## G. Final Acceptance

- **Kết luận nghiệm thu PHASE 01B:** **`PASS`**
- **Căn cứ đánh giá:** Toàn bộ 6 tài liệu nền tảng kiến trúc/tiêu chuẩn và 3 template chuẩn đều tồn tại đầy đủ; toàn bộ 8 phát hiện bất nhất mức MAJOR và MINOR qua kiểm toán chéo đã được xử lý triệt để và chứng minh bằng kiểm thử cú pháp/hợp đồng thực tế (`6/6 passed`); không còn lỗi CRITICAL hay MAJOR mở; phạm vi thay đổi tuân thủ nghiêm ngặt ranh giới tài liệu, không tạo mã nguồn Runtime giả mạo và không đánh dấu hoàn thành các hạng mục chưa triển khai của PHASE 00–01 hay PHASE 02–17.
