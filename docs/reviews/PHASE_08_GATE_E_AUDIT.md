# BÁO CÁO KIỂM TOÁN PHASE 08 VÀ NGHIỆM THU GATE E

- **Phạm vi:** Tiểu phase `P08.01` đến `P08.05` và điều kiện mở khóa `Gate E` thuộc Chặng E trên nhánh `main` của `PHtai16/AI-Village`.
- **Trạng thái `Gate E`:** `PASS` ở mức `Offline + Local FastAPI TestClient End-to-End Verified`.
- **Phạm vi giới hạn của API:** Tầng `api/` mới chỉ mở hai endpoint đọc-chỉ cho `Gate E` gồm tra cứu metadata và tải tệp Artifact có kiểm tra quyền cùng SHA-256; không mở trước endpoint Task, Chat hay SSE của Phase 12.

## 1. Kiến trúc Phase 08 và ranh giới tầng

Mã nguồn được phân tách đúng ranh giới quy định giữa bảng lưu trữ SQLite, lớp dịch vụ Vault/Knowledge và lớp biên HTTP mỏng:

- Migration `storage/db/migrations/0012_vault_artifacts_evidence_knowledge_trash.sql` khởi tạo các bảng `vault_blobs`, `artifact_registry_entries`, `artifact_task_references`, `evidence_records` bất biến qua trigger `BEFORE UPDATE` và `BEFORE DELETE`, `knowledge_entries` và `vault_backups`.
- Gói `storage/artifacts/` gồm `models.py` và `vault.py` cung cấp `ArtifactVaultService` quản lý định danh phiên bản SemVer nối chuỗi `previous_artifact_id`, khử trùng lặp tệp theo SHA-256 tại kho `blobs/`, xem trước theo định dạng bằng `python-docx`, `openpyxl`, `PyMuPDF`, `Pillow`, so sánh cấu trúc xác định với cờ `llm_generated=False`, thùng rác bảo vệ tham chiếu chéo giữa các Task và sao lưu khôi phục SQLite kèm kiểm tra băm trước khi ghi.
- Gói `storage/knowledge/` gồm `models.py` và `vault.py` cung cấp `EvidenceAndKnowledgeService` lưu bằng chứng bất biến, tổng hợp `QaInspectionBundle` cho kiểm chứng QA và thực thi `search_knowledge` theo nguyên tắc lọc phân quyền trước khi chấm điểm từ khóa trên metadata lẫn Markdown với cờ `semantic_search_enabled=False`.
- Lớp biên `api/artifacts.py` chỉ gọi `ArtifactVaultService.get_artifact` và `read_verified_bytes`, trả mã `401`, `403`, `404`, `410` hoặc `500` fail-closed khi thiếu xác thực, sai phạm vi hoặc sai lệch mã băm SHA-256.
- Tầng `core/contracts/event.py` bổ sung 8 sự kiện Vault/Evidence/Knowledge/Backup và `core/governance/evaluator.py` bổ sung 7 Action ID mới gắn khóa `inv.worker_least_privilege` cùng Approval Gate dùng một lần cho `trash.purge` và `vault.restore`.

## 2. Ma trận nghiệm thu tiêu chí Phase 08 và Gate E

| Tiêu chí | Thành phần kiểm chứng | Bằng chứng thực thi | Trạng thái |
| :--- | :--- | :--- | :--- |
| `P08.01` & `P08.02`: Lịch sử phiên bản, khử trùng lặp blob, preview và diff xác định | `ArtifactVaultService` | `test_version_history_blob_deduplication_and_tamper_detection`, `test_format_previews_and_deterministic_comparisons_no_llm` | **PASS** |
| `P08.03` & `P08.04`: Kho bằng chứng bất biến và tìm kiếm tri thức lọc quyền trước | `EvidenceAndKnowledgeService` | `test_evidence_immutability_redaction_and_qa_inspection_bundle`, `test_knowledge_search_filters_permissions_before_ranking` | **PASS** |
| `P08.05`: Backup/restore kiểm SHA-256, retention, trash/restore và bulk trash bảo vệ file dùng chung | `create_backup`, `restore_backup`, `safe_bulk_trash`, `purge_artifact` | `test_trash_restore_shared_task_protection_and_approved_purge`, `test_backup_tamper_detection_approved_restore_and_retention` | **PASS** |
| `Gate E`: Task mới tái sử dụng artifact/tri thức cũ đúng phiên bản, tạo DOCX/XLSX thật qua tools, tải qua API, xóa an toàn không mất file Task khác | `tools.office.*` + `ArtifactVaultService` + `EvidenceAndKnowledgeService` + `create_gate_e_artifact_app` | `test_gate_e_end_to_end_reuse_real_office_download_and_trash_protection` | **PASS** |

## 3. Bằng chứng tổng hợp

Toàn bộ kết quả chạy bộ kiểm thử `tests/storage/` và toàn kho `tests/` sau lần chỉnh sửa cuối cùng cùng thông tin commit được ghi nhận trực tiếp tại [PROGRESS.md](../PROGRESS.md).
