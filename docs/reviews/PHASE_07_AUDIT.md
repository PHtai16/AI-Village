# BÁO CÁO KIỂM TOÁN PHASE 07 VÀ MA TRẬN CÔNG CỤ PHÒNG BAN

- **Phạm vi:** Tiểu phase `P07.01` đến `P07.06` thuộc Chặng E trên nhánh `main` của `PHtai16/AI-Village`, kế thừa commit triển khai chính `8c23d7f`.
- **Trạng thái Phase 07:** `DONE` ở mức `Offline + Local Host End-to-End Verified`.
- **Giới hạn xác minh container sống:** Nhánh chạy container Docker thật trên `tools.code.run_sandbox` được đánh dấu `@pytest.mark.live` và chưa thực thi vì Docker Desktop Linux Engine daemon trên host hiện đang tắt. Khi Docker daemon không khả dụng, công cụ khóa fail-closed với mã lỗi `SANDBOX_UNAVAILABLE` và tuyệt đối không chạy mã trên host.

## 1. Kiến trúc Phase 07 và ranh giới tầng

Mã nguồn được phân tách đúng ranh giới quy định giữa điều phối, thực thi độc lập, lưu trữ bền vững và khai báo phòng ban:

- Tầng lõi thuần tại `core/tools/` gồm `models.py`, `registry.py`, `runtime.py` và `__init__.py` đảm nhiệm hợp đồng `ToolManifest`, vòng đời thực thi 10 bước qua `PermissionManager`, cơ chế hủy hợp tác `ToolCancelledError`, lọc bí mật đầu vào/đầu ra và tổng hợp `ToolUsageSummary`. Kiểm thử AST tại `tests/tools/test_tool_registry_and_runtime.py` xác nhận `core/tools/` không nhập khẩu `sqlite3`, `storage`, `subprocess`, `httpx`, `playwright`, `ddgs`, `markitdown`, `api` hay `ui`.
- Tầng lưu trữ tại `storage/db/migrations/0011_tool_registry_and_executions.sql` và `storage/repositories/tool_repo.py` cung cấp bảng `tool_registry` cùng bảng bất biến `tool_executions` gắn hai trigger `BEFORE UPDATE` và `BEFORE DELETE`.
- Tầng công cụ độc lập tại `tools/` gồm 5 nhóm chức năng `files/`, `office/`, `code/`, `research/`, `browser/`, cộng một cầu nối `tools/mcp_bridge/client.py` duy nhất và hàm đăng ký tập trung `register_phase07_tools` tại `tools/registry_bootstrap.py`.
- Cầu nối MCP `GovernedMcpBridge` chỉ chấp nhận danh sách máy chủ đã phê duyệt, tước toàn bộ biến môi trường chứa API key hoặc token trước khi khởi chạy tiến trình `stdio`, ép timeout và lọc bí mật trên chuỗi kết quả trả về.
- Tầng phòng ban tại `agents/departments/` chỉ lưu 9 tệp `department.yaml` tĩnh khai báo `default_tools` mang tính khuyến nghị. Thực nghiệm xác nhận công cụ có mặt trong `department.default_tools` vẫn bị từ chối với mã `TOOL_NOT_ALLOWED` nếu vắng mặt trong `agent.tools.allowed`.

## 2. Danh mục 17 công cụ chuẩn hóa và 4 máy chủ MCP

| Nhóm công cụ | Các Tool ID đã đăng ký | Tầng thực thi / MCP liên kết | Điểm kiểm soát chính |
| :--- | :--- | :--- | :--- |
| `files` | `tools.files.read`, `tools.files.write`, `tools.files.list`, `tools.files.delete` | Python thuần trên `pathlib` và `hashlib` | Khóa phạm vi qua `validate_workspace_path`, chặn ghi đè ngoài ý muốn, xóa tệp buộc qua Approval Gate |
| `office` | `tools.office.read_docx`, `tools.office.write_docx`, `tools.office.read_excel`, `tools.office.write_excel`, `tools.office.read_pdf`, `tools.office.parse_markdown`, `tools.office.compute_table` | `python-docx`, `openpyxl`, `PyMuPDF`/`pypdf`, `MarkItDown` và `markitdown-mcp` | Ghi nguyên tử kèm SHA-256, chống ghi đè, chuyển Markdown chuẩn hóa, tính bảng tất định không dùng `eval` |
| `code` | `tools.code.run_sandbox`, `tools.code.git_inspect` | Docker CLI cô lập + `sandbox/Dockerfile`, Git CLI đọc-chỉ và `mcp-server-git` | Cờ `--network=none --read-only --cap-drop=ALL --pids-limit=64`, cắt gọn + redact log, fail-closed `SANDBOX_UNAVAILABLE`, chặn injection trên Git |
| `research` | `tools.research.web_fetch`, `tools.research.web_search` | `httpx`, Jina Reader, `mcp-server-fetch` và `ddgs` | Chặn SSRF lẫn DNS-rebinding mọi địa chỉ nội bộ, gắn `UNTRUSTED_EXTERNAL_DATA`, `evidence_refs` và `estimated_cost_usd=None` |
| `browser` | `tools.browser.session`, `tools.browser.interact` | Playwright Chromium headless và `@playwright/mcp` | `browser.interact` bắt buộc Approval Gate dùng một lần, ghi `action_log`, lưu ảnh PNG kèm SHA-256 và `evd_*` |

## 3. Ma trận kiểm chứng 12 kịch bản chuẩn TOOL_SPEC

| Kịch bản chuẩn | Tình huống kiểm thử | Kết quả xác minh trong bộ kiểm thử |
| :--- | :--- | :--- |
| Kịch bản 1 | Đọc tệp hợp lệ trong workspace, trả nội dung, SHA-256, `UNVERIFIED` | Đạt qua `test_file_write_read_list_overwrite_protection_and_sha256` |
| Kịch bản 2 | Truy cập ngoài workspace hoặc `.env` bị từ chối fail-closed | Đạt qua `test_file_tools_fail_closed_boundaries_and_delete_approval` |
| Kịch bản 3 | Tham số sai JSON Schema bị chặn trước khi chạy handler | Đạt qua `test_runtime_lifecycle_permissions_schemas_disabled_and_untrusted_actor` |
| Kịch bản 4 | Công cụ nhạy cảm tạm dừng tại `APPROVAL_REQUIRED` | Đạt qua `test_approval_gate_single_use_and_replay_protection` |
| Kịch bản 5 | Yêu cầu Approval bị từ chối, hết hạn hoặc sai dấu vân tay | Đạt qua `test_rejected_expired_tampered_approval_cancellation_and_no_blind_retry` |
| Kịch bản 6 | Công cụ vượt ngưỡng timeout trả `TIMEOUT` | Đạt qua `test_secret_redaction_immutability_timeout_output_invalid_and_usage_summary` và `test_docker_sandbox_command_hardening_and_execution_states` |
| Kịch bản 7 | Hủy hợp tác trước hoặc giữa lúc thực thi trả `CANCELLED` | Đạt qua `test_rejected_expired_tampered_approval_cancellation_and_no_blind_retry` |
| Kịch bản 8 | Lệnh sandbox trả exit code khác 0, thu stderr đã redact | Đạt qua `test_docker_sandbox_command_hardening_and_execution_states` |
| Kịch bản 9 | Công cụ tạo artifact kèm SHA-256 trên đĩa khớp thực tế | Đạt qua `test_file_write_read_list_overwrite_protection_and_sha256`, `test_docx_excel_pdf_and_markitdown_roundtrips` và `test_real_chromium_browser_session_screenshot_and_approved_interact` |
| Kịch bản 10 | Công cụ chạy thành công luôn giữ `business_verification_status == "UNVERIFIED"` | Đạt qua `test_runtime_lifecycle_permissions_schemas_disabled_and_untrusted_actor` |
| Kịch bản 11 | Tài nguyên không khả dụng hoặc chưa đăng ký từ chối fail-closed | Đạt qua `test_runtime_lifecycle_permissions_schemas_disabled_and_untrusted_actor` và `test_docker_sandbox_command_hardening_and_execution_states` |
| Kịch bản 12 | Thao tác `safe_retry=False` lỗi giữa chừng chỉ chạy một lần | Đạt qua `test_rejected_expired_tampered_approval_cancellation_and_no_blind_retry` |

## 4. Bằng chứng kiểm thử thực tế

- Kiểm thử trực tiếp trên máy cho 4 máy chủ MCP `stdio` thật qua `GovernedMcpBridge` đều thành công: `mcp-server-git` trả `MCP_GIT_OK`, `markitdown-mcp` chuyển đổi HTML sang Markdown, `@playwright/mcp` mở `about:blank` trên Chromium với thư mục tạm biệt lập và `mcp-server-fetch` khởi tạo phiên rồi từ chối địa chỉ nội bộ `127.0.0.1` theo đúng quy tắc an toàn.
- Chạy Playwright Chromium headless thật trên HTTP server cục bộ qua `test_real_chromium_browser_session_screenshot_and_approved_interact` xác minh trích xuất DOM, chụp ảnh PNG khớp SHA-256 và tương tác form sau khi có phê duyệt hợp lệ.
- Chạy bộ kiểm thử riêng Phase 07 tại `tests/tools/` và toàn bộ kho mã nguồn đạt 0 lỗi, chi tiết số lượng test ghi nhận tại [PROGRESS.md](../PROGRESS.md).
