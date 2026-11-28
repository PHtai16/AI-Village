# Báo cáo kiểm toán hoàn thành PHASE 10 — Continuous Improvement

Tài liệu này tổng hợp kiến trúc, thay đổi cơ sở dữ liệu, kiểm soát phân quyền và kết quả kiểm chứng thực tế cho toàn bộ `PHASE 10` từ `P10.01` đến `P10.05` hiện thực hóa quy trình tự cải tiến liên tục Phương án C.

## 1. Tóm tắt điều hành

Toàn bộ năm hạng mục `P10.01` đến `P10.05` đã hoàn tất ở mức `OFFLINE_INTEGRATION_VERIFIED` qua năm commit nối tiếp trên nhánh `main` gồm `258ac10`, `3d5f5bb`, `f53ca1f`, `dd42ac9` và `df8ea49`. Hệ thống hiện khép kín đầy đủ 9 bước Phương án C theo `SKILL_SPEC.md`, từ thu thập hồi cứu thực thi dữ liệu thật, quét mẫu lặp đa Task, đóng gói Skill Candidate chuẩn 10 mục hợp đồng, kiểm định kỹ thuật IT kết hợp benchmark A/B độc lập của QA, đến bước Master đề xuất kích hoạt kèm Approval Gate, cập nhật danh mục kỹ năng được HR cấp phát, đo lường hậu nghiệm thực tế và rollback tức thì về phiên bản ổn định trước đó.

## 2. Tổng hợp các tiểu phase đã hoàn tất

| Tiểu phase | Phạm vi và năng lực chính | Thành phần mã nguồn | Commit SHA |
| --- | --- | --- | --- |
| `P10.01` | Hồi cứu thực thi dữ liệu thật theo phiên bản bất biến, sửa lỗi `tool_id` trong timeline | [process_models.py](file:///C:/DoCho/AI-Village/core/departments/process_models.py), [process.py](file:///C:/DoCho/AI-Village/core/departments/process.py), [projections.py](file:///C:/DoCho/AI-Village/core/events/projections.py), [process_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/process_repo.py), [0013_process_retrospectives.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0013_process_retrospectives.sql) | `258ac10` |
| `P10.02` | Quét hồi cứu đa Task phát hiện lỗi lặp, thao tác dư thừa và thiếu hụt kỹ năng | [process_models.py](file:///C:/DoCho/AI-Village/core/departments/process_models.py), [process.py](file:///C:/DoCho/AI-Village/core/departments/process.py), [process_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/process_repo.py), [0014_pattern_improvements.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0014_pattern_improvements.sql) | `3d5f5bb` |
| `P10.03` | Đóng gói Skill Candidate 10 mục hợp đồng, điều kiện dùng, I/O, rủi ro, fixtures, SemVer và SHA-256 | [models.py](file:///C:/DoCho/AI-Village/core/skills/models.py), [parser.py](file:///C:/DoCho/AI-Village/core/skills/parser.py), [service.py](file:///C:/DoCho/AI-Village/core/skills/service.py), [skill_manifest.schema.json](file:///C:/DoCho/AI-Village/skills/schemas/skill_manifest.schema.json), [0015_skills_lifecycle_and_benchmarks.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0015_skills_lifecycle_and_benchmarks.sql), [skills_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/skills_repo.py) | `f53ca1f` |
| `P10.04` | IT kiểm tra tương thích sandbox/tool; QA độc lập chạy benchmark A/B trên fixture và lưu kết quả bất biến | [benchmarking.py](file:///C:/DoCho/AI-Village/core/skills/benchmarking.py), [service.py](file:///C:/DoCho/AI-Village/core/skills/service.py), [skills_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/skills_repo.py) | `dd42ac9` |
| `P10.05` | Master đề xuất kích hoạt, Human Approval cho Skill nhạy cảm, nối `ApprovedSkillCatalogPort` sang HR, đo hiệu quả sau triển khai và rollback | [service.py](file:///C:/DoCho/AI-Village/core/skills/service.py), [parser.py](file:///C:/DoCho/AI-Village/core/skills/parser.py), [evaluator.py](file:///C:/DoCho/AI-Village/core/governance/evaluator.py), [skills_repo.py](file:///C:/DoCho/AI-Village/storage/repositories/skills_repo.py) | `df8ea49` |

## 3. Ranh giới kiến trúc và cơ sở dữ liệu

- Tầng lõi mới tại `core/skills/` giữ thuần khiết AST hoàn toàn, không import `sqlite3`, `storage`, `tools`, `subprocess`, `api` hay `ui`, giao tiếp với SQLite thông qua `SkillLifecycleRepository`, `ProcessImprovementRepository`, `ProcessRetrospectiveRepository` và cổng `EvidenceVaultPort` tái sử dụng từ [qa.py](file:///C:/DoCho/AI-Village/core/departments/qa.py).
- Ba migration SQLite nối tiếp giữ nguyên toàn bộ lịch sử schema cũ:
  - [0013_process_retrospectives.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0013_process_retrospectives.sql) tạo `task_retrospectives`, `task_retrospective_revisions` khóa chống sửa xóa bằng trigger và `task_retrospective_runs`.
  - [0014_pattern_improvements.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0014_pattern_improvements.sql) tạo `improvement_suggestions` với khóa duy nhất `(pattern_type, pattern_key)` và `improvement_task_links` tham chiếu khóa ngoại tới `tasks` và `task_retrospectives`.
  - [0015_skills_lifecycle_and_benchmarks.sql](file:///C:/DoCho/AI-Village/storage/db/migrations/0015_skills_lifecycle_and_benchmarks.sql) tạo `skill_packages`, `skill_active_pointers`, `skill_it_reviews`, `skill_benchmarks` có trigger bất biến, `skill_activation_proposals` liên kết `approval_requests` và `skill_improvement_outcomes` có trigger bất biến.
- `SQLiteRepositoryStore` tại [sqlite_store.py](file:///C:/DoCho/AI-Village/storage/repositories/sqlite_store.py) gắn cả `self.process` và `self.skills` với khóa giao dịch `BEGIN IMMEDIATE` và SAVEPOINT đồng bộ cùng `EventJournal`.

## 4. Kiểm soát bảo mật, độc lập QA và quyền hạn

- **Không tự nâng quyền hoặc tự phê duyệt từ YAML:** Hàm `register_external_candidate_dir` và `parse_and_validate_skill_markdown` không bao giờ coi trường `status: "APPROVED"` trong `SKILL.md` là bằng chứng kích hoạt; mọi nâng cấp lên `APPROVED` phải đi qua `SkillLifecycleService.activate_skill_version` sau benchmark QA đạt và quyết định Governance hợp lệ.
- **Tách bạch nhiệm vụ giữa tác giả, IT và QA:** Trong `run_qa_benchmark`, tác giả tạo Skill, tài khoản thuộc `frontmatter.author`, nhân sự Worker và nhân sự IT từng ghi nhận `record_it_technical_review` đều bị chặn bằng `SkillIndependenceError`.
- **Phê duyệt con người cho thay đổi nhạy cảm:** `PolicyEvaluator` tại [evaluator.py](file:///C:/DoCho/AI-Village/core/governance/evaluator.py) bổ sung năm action `skill.propose`, `skill.review_technical`, `skill.benchmark`, `skill.activate` và `skill.rollback`, chặn `worker_agent` kích hoạt, rollback hay benchmark, đồng thời ép `APPROVAL_REQUIRED` với `SENSITIVE_SKILL_APPROVAL_REQUIRED` khi Skill chứa script thực thi, phụ thuộc ngoài, công cụ nhạy cảm, mức rủi ro `medium` trở lên, thay đổi luồng nhạy cảm hoặc bổ sung công cụ mới so với phiên bản trước.
- **Bảo vệ toàn vẹn đĩa và khử bí mật:** Toàn bộ nội dung văn bản, script và kết quả benchmark đều qua lọc secret; mọi truy cập `get_approved_skill`, `get_active_skill_markdown` và `rollback_skill` kiểm tra lại SHA-256 trên đĩa và từ chối ngay khi tệp bị sửa trái phép.

## 5. Kết quả kiểm thử chuyên biệt và toàn kho

Các bộ kiểm thử chuyên biệt được xây dựng và xác minh qua từng tiểu phase:

- [test_p10_01_process_retrospectives.py](file:///C:/DoCho/AI-Village/tests/departments/test_p10_01_process_retrospectives.py): 8 hàm bao phủ 20 kịch bản nghiệm thu `P10.01`.
- [test_p10_02_pattern_detection.py](file:///C:/DoCho/AI-Village/tests/departments/test_p10_02_pattern_detection.py): 3 hàm kiểm thử `min_occurrences=3`, 3 nhóm pattern, khử trùng lặp, nâng version, cách ly Worker và rollback giao dịch SQLite.
- [test_p10_03_skill_candidates.py](file:///C:/DoCho/AI-Village/tests/skills/test_p10_03_skill_candidates.py): 3 hàm kiểm thử thuần AST `core/skills/`, schema JSON, gắn `source_improvement_id`, chống giả mạo YAML `APPROVED` trước `HrDepartmentService`, chặn script đọc `.env` và chặn path traversal.
- [test_p10_04_it_qa_benchmarks.py](file:///C:/DoCho/AI-Village/tests/skills/test_p10_04_it_qa_benchmarks.py): 2 hàm kiểm thử quy trình IT + QA qua `ToolRuntime` thật, chặn tự chứng thực, trạng thái `PASSED`, `FAILED` và `INCONCLUSIVE`, trigger bất biến `skill_benchmarks` và phát hiện sửa đổi tệp trên đĩa.
- [test_p10_05_activation_outcomes_and_rollback.py](file:///C:/DoCho/AI-Village/tests/skills/test_p10_05_activation_outcomes_and_rollback.py): 2 hàm kiểm thử tách biệt Skill đọc thuần với Skill nhạy cảm cần duyệt con người, nâng cấp `1.0.0` lên `1.1.0`, rollback tức thì về `1.0.0`, khóa khẩn cấp `disable_skill`, từ chối kỹ năng chưa duyệt ở HR và đo lường `skill_improvement_outcomes` bất biến.
- [test_p10_phase_10_e2e.py](file:///C:/DoCho/AI-Village/tests/skills/test_p10_phase_10_e2e.py): kiểm thử tích hợp trọn vẹn 9 bước Phương án C kết hợp `ProcessDepartmentService`, `SkillLifecycleService`, `ApprovalService`, `HrDepartmentService` và phục hồi sau khởi động lại SQLite.

Tập hợp toàn bộ 19 ca kiểm thử trên đạt `19 passed in 19.58s` trên commit `df8ea49`. Lượt kiểm thử toàn kho và `git diff --check` cuối cùng được chạy ngay sau khi hoàn tất đồng bộ tài liệu trước khi đẩy lên `origin/main`.

## 6. Giới hạn đã ghi nhận và bước kế tiếp

- Bộ kiểm thử vẫn chạy ở mức `OFFLINE_INTEGRATION_VERIFIED`: nhóm ca live cần `AI_VILLAGE_RUN_LIVE=1`, hai ca `crewai` và ca Docker sandbox live tiếp tục giữ trạng thái skipped ngoài phạm vi môi trường offline hiện tại.
- Thư mục `skills/candidates/` và `skills/approved/` trong kho Git tiếp tục giữ rỗng theo ràng buộc baseline của `tests/stage_a/test_stage_a_baseline.py` vì mọi kiểm thử dùng `tmp_path` độc lập.
- Bước kế tiếp chuyển sang `PHASE 11 — Interrupts & Resources` bắt đầu từ `P11.01` và tiến tới nghiệm thu `Gate G`.
