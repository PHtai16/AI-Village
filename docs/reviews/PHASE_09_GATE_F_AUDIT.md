# Báo cáo kiểm toán hoàn thành Phase 09 và Gate F

Tài liệu này ghi nhận kết quả triển khai, ranh giới kiến trúc và bằng chứng kiểm thử cho các hạng mục `P09.02` đến `P09.09` cùng tiêu chí `Gate F`, tiếp nối hạng mục `P09.01` ở các commit `4d302c7` và `04e3e73`. Toàn bộ mã nguồn chức năng và bộ kiểm thử phòng ban được khóa tại commit `9f31d92`.

## 1. Phạm vi triển khai tại `core/departments/` và `storage/`

Các phòng ban mới đều đặt tại gói `core/departments/` với lớp dùng chung `core/departments/_common.py`, giữ thuần khiết AST tầng `core/` bằng cách giao tiếp qua các giao diện `Protocol` thay vì phụ thuộc trực tiếp vào `sqlite3`, `storage`, `subprocess`, `httpx` hay tầng giao diện. Mọi thao tác công cụ đều đi qua `ToolInvokerPort` gắn với `ToolRuntime` thật và cơ chế ghi nhật ký sự kiện tập trung.

| Hạng mục | Thành phần chính | Giới hạn và cơ chế an toàn | Tệp nguồn |
| --- | --- | --- | --- |
| `P09.02` Resources | `ResourcesDepartmentService`, `DefaultHostMetricsSampler`, `QuotaScopeRecord`, `evaluate_connection_quota_isolation`, `recommend_allocation`, `compile_post_run_inventory` | Không tự suy diễn hai key có hai quota độc lập; gán `null` kèm nhãn `UNAVAILABLE_FROM_PROVIDER` khi provider không trả chi phí hoặc quota; xuất bảng kiểm kê Excel và phát `RESOURCE_WARNING` khi chạm ngưỡng | `core/departments/resources.py` |
| `P09.03` Service/UX | `ServiceUxDepartmentService`, `clarify_brief`, `record_user_feedback`, `compare_deliverable_against_brief` | Ghi nhận phản hồi chỉnh sửa của người dùng và đối chiếu với brief ban đầu nhưng luôn gắn `qa_substitute=False` để không thay thế khâu kiểm định độc lập | `core/departments/service_ux.py` |
| `P09.04` Marketing | `MarketingDepartmentService`, `run_campaign_workflow` | Thực thi `tools.research.web_search` và `web_fetch` gắn nhãn `UNTRUSTED_EXTERNAL_DATA`, sinh kế hoạch theo dữ liệu thu thập và xuất tài liệu `.md` hoặc `.docx` thật trong workspace | `core/departments/marketing.py` |
| `P09.05` IT/R&D | `ItRdDepartmentService`, `write_isolated_files`, `run_sandbox_tests`, `ItProductionMutationError` | Chỉ cho phép tạo và thử nghiệm mã trong vùng cách ly; chặn tuyệt đối mọi thao tác ghi chạm vào đường dẫn production được bảo vệ | `core/departments/it_rd.py` |
| `P09.06` Reception | `ReceptionDepartmentService`, `ActionInboxItem`, `NotificationStorePort` | Phân loại năm mức `QUEUED`, `APPENDED`, `ANOMALOUS`, `EMERGENCY` và `APPROVAL_REQUIRED`; định tuyến đúng mục tiêu, khử trùng lặp cảnh báo và đẩy đầu việc vào kho thông báo | `core/departments/reception.py` |
| `P09.07` Process/Skills | `ProcessDepartmentService`, `record_task_retrospective`, `detect_patterns_and_propose_skills` | Tổng hợp lỗi, công cụ và thời gian sau mỗi task; đề xuất skill khi phát hiện mẫu lặp nhưng khóa cứng `benchmark_status="NOT_BENCHMARKED"` và `approved_for_production=False` | `core/departments/process.py` |
| `P09.08` Operations/QA | `QaDepartmentService`, `inspect_bundle`, `verify_deliverables`, `QaEvidenceRequiredError` | Cưỡng chế độc lập `auth_context.actor_id != producer_agent_id` và vai trò `trusted_verifier` hoặc `human_operator`; đối chiếu SHA-256, tạo `EvidenceRecord` bất biến và phát `qa.verification_recorded` với `source="qa_verifier"` | `core/departments/qa.py` |
| `P09.09` Vault | `VaultDepartmentService`, `submit_document`, `get_version_history_with_preview`, `compare_versions`, `reuse_artifact_in_task` | Nối vào `ArtifactVaultPort` và `KnowledgeVaultPort` để quản lý chuỗi phiên bản, xem trước, so sánh khác biệt, bảo vệ thùng rác và tái sử dụng artifact giữa các task | `core/departments/vault.py` |

Ở tầng lưu trữ, `storage/artifacts/vault.py` được bổ sung phương thức `update_verification_status` trên `ArtifactVaultService`. Phương thức này yêu cầu quyền `trusted_verifier` hoặc `human_operator`, từ chối tự kiểm chứng với lỗi `SELF_VERIFICATION_FORBIDDEN` khi người kiểm tra trùng `owner_agent_id`, bắt buộc `evidence_reference` khác rỗng khi chuyển trạng thái `VERIFIED_PASS`, đồng thời cập nhật đồng bộ cả hai bảng `artifact_registry_entries` và `artifacts` trong một transaction trước khi ghi sự kiện `qa.verification_recorded`.

## 2. Kết quả kiểm thử và đánh giá Gate F

Tại commit `9f31d92`, ranh giới tĩnh `tests/stage_a/test_stage_a_baseline.py` đã bổ sung `core/departments` vào `allowed_core_subdirs`, đi kèm bốn mô-đun kiểm thử tại `tests/departments/`:

1. `tests/departments/conftest.py`: khung dựng kho lưu trữ tạm, đăng ký agent, tạo task và phê duyệt governance.
2. `tests/departments/test_p09_02_04_departments.py`: kiểm tra thuần khiết AST của `core/departments/` cùng các ca kiểm thử `P09.02`, `P09.03` và `P09.04`.
3. `tests/departments/test_p09_05_07_departments.py`: kiểm thử chặn ghi production của `P09.05`, phân loại năm mức và khử trùng lặp của `P09.06`, ghi nhận hồi cứu và đề xuất skill của `P09.07`.
4. `tests/departments/test_p09_08_09_departments.py`: kiểm thử nguyên tắc QA độc lập, chặn `VERIFIED_PASS` thiếu chứng cứ, kiểm tra sai lệch SHA-256, đối chiếu replay sự kiện và toàn bộ vòng đời `Vault`.
5. `tests/departments/test_gate_f_end_to_end.py`: kịch bản `Gate F` chạy ba đề bài khác loại gồm Code, Research và Office qua `MasterPlanningService`, lập ba tổ đội tối thiểu khác nhau qua `DynamicTeamBuilderService`, nghiệm thu QA độc lập và tổng hợp gói bàn giao `VERIFIED` qua `CollaborationDeliveryService.compile_master_delivery`.

Lượt chạy mục tiêu `python -m pytest tests/departments tests/storage tests/stage_a -v` ngay trên mã nguồn commit `9f31d92` đạt `47 passed in 75.42s`. Lượt chạy toàn kho sau khi hoàn tất đồng bộ tài liệu và tiêu chuẩn kiểm tra là điều kiện nghiệm thu cuối cùng trước khi đồng bộ lên `origin/main`.

## 3. Giới hạn và mức nghiệm thu thực tế

Mức nghiệm thu hiện tại của `Gate F` là `OFFLINE_INTEGRATION_VERIFIED`. Các dịch vụ tìm kiếm web, tải trang HTTP và thực thi lệnh sandbox trong bài kiểm thử sử dụng `ToolRuntime` thật gắn kèm test double cho backend mạng và trình chạy lệnh, bởi môi trường kiểm thử mặc định không bật `AI_VILLAGE_RUN_LIVE=1` và dịch vụ Docker Desktop trên máy đang tắt nên `test_docker_sandbox_live_execution` cùng nhóm kiểm thử live CLI vẫn nằm trong tập skipped.

Toàn bộ lịch sử tiến độ tại `docs/PROGRESS.md` và danh sách mã cho phép tại `tests/standards/test_standards_consistency.py` đã được cập nhật khớp với trạng thái `P09.01` đến `P09.09`. Khi có yêu cầu nghiệm thu kết nối mạng thật hoặc chạy sandbox Docker trực tiếp, các bài kiểm thử offline này làm nền tảng trực tiếp để nối backend thật mà không phải sửa hợp đồng tầng `core/departments/`.
