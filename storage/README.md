# Thư mục `storage/`: Tầng Lưu trữ Bền vững (Persistence Layer)

Thư mục `storage/` chứa mã nguồn truy xuất cơ sở dữ liệu, quản lý kho sản phẩm, kho tri thức và quy trình sao lưu của AI Village. Thư mục này **chỉ chứa mã persistence**, không chứa tệp cơ sở dữ liệu thật hay dữ liệu phiên chạy.

> **Trạng thái hiện tại:** Đã hoàn thiện `PHASE 04` (`db/`, `repositories/`) và `PHASE 08` (`artifacts/` cho Artifact Registry/Preview/Diff/Trash/Backup, `knowledge/` cho Evidence Vault và Knowledge Search theo phân quyền, cùng migration `0012`); dữ liệu chạy thật chỉ lưu tại `.runtime/`, không tạo tệp `.db` trong `storage/`.

## 1. Phân tách Mã Persistence và Dữ liệu Runtime

| Khái niệm | Vị trí lưu trữ | Nội dung và trách nhiệm |
| :--- | :--- | :--- |
| **Persistence Implementation** | `storage/` (`db/`, `repositories/`, `artifacts/`, `knowledge/`) | Mã Python định nghĩa migrations, SQLite connection/transaction, repository pattern, bộ đánh chỉ mục tìm kiếm và logic sao lưu/khôi phục (triển khai ở `PHASE 04` và `PHASE 08`). |
| **Runtime Database** | `.runtime/` (gitignored) | Tệp cơ sở dữ liệu SQLite thực tế chứa trạng thái `tasks`, `runs`, `sessions`, `approvals`, `resource_ledger` đang vận hành trên máy cục bộ. |
| **Artifact Storage** | `output/` và đường dẫn quản lý bởi Artifact Registry | Lưu trữ sản phẩm bàn giao đã kiểm chứng kèm `artifact_id`, phiên bản SemVer, liên kết `task_id`/`run_id`, `content_type` và mã băm SHA-256 chống ghi đè hoặc nhân bản tùy tiện. |
| **Knowledge Storage** | `storage/knowledge/` (quản lý truy xuất qua repository) | Tri thức chuyên môn tái sử dụng được kiểm duyệt bởi Process, QA và Vault; ưu tiên tìm kiếm theo metadata trước khi bổ sung semantic search tùy chọn. |
| **Memory** | Quản lý bởi `storage/` tại vùng lưu trữ runtime (`.runtime/`) | Phân tách 3 phạm vi `private` (phiên Agent), `project` (nhóm công tác của nhiệm vụ) và `global` (tri thức chung chỉ đọc cho Worker thường). |
| **Backup & Retention** | Cơ chế backup/trash có kiểm soát | Sao lưu nhất quán cơ sở dữ liệu và tài sản quan trọng; hỗ trợ thùng rác (soft delete), khôi phục (restore) và bảo vệ tệp tham chiếu dùng chung giữa các task khác nhau. |
| **Audit History** | Event Journal & Audit Trail trong `.runtime/` | Bản ghi nối tiếp (append-only) các sự kiện nhiệm vụ, quyết định phân quyền `PolicyDecision`, lượt phê duyệt `ApprovalRequest` và log đã lọc bỏ bí mật (`redacted`). |

## 2. Nguyên tắc Bất biến

- UI (`ui/`) và API (`api/`) tuyệt đối không mở kết nối SQLite trực tiếp để bỏ qua các guard của `core/` và `storage/repositories/`.
- Không đưa dữ liệu trong `.runtime/` hay các tệp `.db`, `.sqlite`, `.sqlite3` vào lịch sử Git.
