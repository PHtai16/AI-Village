# Tiêu chuẩn Kỹ thuật Hệ thống Công cụ (Tool Specification)

Tài liệu này quy định kiến trúc, hợp đồng dữ liệu, vòng đời thực thi, ranh giới an toàn và cơ chế tích hợp cho hệ thống Công cụ (Tools) trong AI Village. Đây là hợp đồng thiết kế nền tảng cho Tool Registry và Tool Runtime trong các phase triển khai tiếp theo.

Toàn bộ cơ chế thực thi runtime, tích hợp Engine và giao diện gọi công cụ trong tài liệu này đang ở mức đặc tả thiết kế, chưa được lập trình hay kiểm chứng chạy thực tế.

## 1. Kiến trúc và Phân định trách nhiệm

Hệ thống tách biệt rõ bảy thành phần tham gia vào một lượt sử dụng công cụ:

- **Agent:** Chủ thể quyết định nhu cầu sử dụng công cụ để giải quyết một phần công việc trong nhiệm vụ được giao.
- **Skill:** Tài liệu hướng dẫn quy trình nghiệp vụ tại `skills/`, mô tả trình tự sử dụng công cụ phù hợp nhưng không có thẩm quyền cấp quyền.
- **Tool:** Giao diện thực hiện một hành động kỹ thuật cụ thể tại `tools/` (như đọc tệp, chạy test, tìm kiếm web). Tool không chứa prompt nhân vật hay logic điều phối của Master.
- **Tool Registry:** Danh mục tập trung quản lý định danh, phiên bản, schema đầu vào/đầu ra, mức độ rủi ro và trạng thái khả dụng của các công cụ đã đăng ký.
- **Tool Runtime:** Thành phần chịu trách nhiệm validate dữ liệu, thiết lập môi trường cách ly, điều phối thực thi, giám sát timeout/cancellation và đóng gói kết quả chuẩn hóa.
- **Permission Manager:** Thành phần thuộc `core/governance/` thực hiện đánh giá quyền độc lập dựa trên Rule System trước mọi lệnh gọi công cụ.
- **Engine Adapter:** Lớp trung gian tại `engines/` làm nhiệm vụ chuyển đổi giao thức giữa Core Runtime với Antigravity CLI hoặc các AI Provider khác.

**Nguyên tắc cách ly thực thi:** Mô hình ngôn ngữ (LLM) tuyệt đối không được phép gọi thẳng các lệnh hệ điều hành hoặc thao tác tệp tin mà bỏ qua Tool Runtime và Permission Manager.

**Phân biệt nguồn gốc công cụ:** Hệ thống phân biệt rõ công cụ nội bộ do AI Village tự phát triển tại `tools/` (chạy trực tiếp dưới sự kiểm soát của Tool Runtime) với các khả năng thao tác công cụ do AI Engine bên ngoài cung cấp (phải đi qua lớp kiểm soát phạm vi của Engine Adapter và sandbox).

## 2. Định danh và Tool Manifest Schema

Mỗi công cụ trong Tool Registry được mô tả bằng một tệp cấu hình có cấu trúc theo bảng đặc tả 16 trường chuẩn dưới đây:

| Tên trường | Kiểu dữ liệu | Bắt buộc | Ràng buộc giá trị và mô tả |
| :--- | :--- | :--- | :--- |
| `spec_version` | String | Có | Phiên bản của chuẩn đặc tả Tool, ví dụ `"1.0.0"`. |
| `tool_id` | String | Có | Khóa định danh duy nhất, định dạng `^tools\.[a-z0-9_]+\.[a-z0-9_]+$` (ví dụ: `tools.files.read`). |
| `name` | String | Có | Tên hiển thị ngắn gọn của công cụ, độ dài từ 3 đến 80 ký tự. |
| `description` | String | Có | Mô tả chính xác công dụng, phạm vi tác động và giới hạn của công cụ. |
| `version` | String | Có | Phiên bản phát hành theo chuẩn SemVer (`MAJOR.MINOR.PATCH`). |
| `category` | String | Có | Một trong 5 nhóm thư mục chuẩn: `files`, `code`, `browser`, `research`, `office`. |
| `capabilities` | List[String] | Có | Danh sách ít nhất 1 nhãn năng lực liên kết để tra cứu trong hệ thống. |
| `input_schema` | Object | Có | Đặc tả JSON Schema cho dữ liệu đầu vào, gồm kiểu, ràng buộc bắt buộc và giới hạn kích thước. |
| `output_schema` | Object | Có | Đặc tả JSON Schema cho dữ liệu kết quả trả về trong trường `output`. |
| `permissions_required` | List[String] | Có | Danh sách Action ID bắt buộc phải được Rule System cho phép trước khi chạy (ví dụ: `files.read`). |
| `execution_environment` | String | Có | Môi trường thực thi: `sandbox_only`, `restricted_local`, hoặc `provider_managed`. |
| `timeout_seconds` | Integer | Có | Thời gian chờ tối đa mặc định tính bằng giây, giá trị hợp lệ từ `1` đến `3600`. |
| `risk_level` | String | Có | Mức độ rủi ro thao tác: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`. |
| `approval_policy` | String | Có | Chính sách xin phê duyệt: `NONE`, `CONDITIONAL`, `ALWAYS_REQUIRED`. |
| `idempotency` | Object | Có | Chứa `supported` (Boolean) và `safe_retry` (Boolean) để kiểm soát việc gọi lại khi gặp sự cố. |
| `status` | String | Có | Trạng thái trong Registry: `DRAFT`, `REGISTERED`, `AVAILABLE`, `DEPRECATED`, `DISABLED`. |

## 3. Phân nhóm công cụ (Tool Categories)

Thư mục `tools/` được tổ chức thành 5 nhóm chức năng độc lập với phòng ban và nhân vật:

1. **`tools/files/`:** Nhóm thao tác tệp tin gồm đọc nội dung, ghi tệp trong phạm vi cho phép, liệt kê cây thư mục, kiểm tra thông tin tệp và tính mã băm SHA-256.
2. **`tools/code/`:** Nhóm thao tác mã nguồn trong sandbox gồm thực thi script kiểm soát, chạy bộ kiểm thử (`pytest`), chạy linter, kiểm tra kiểu dữ liệu và phân tích cú pháp tĩnh (AST).
3. **`tools/browser/`:** Nhóm tự động hóa trình duyệt web phục vụ điều hướng trang, tương tác giao diện và chụp ảnh màn hình kiểm chứng khi môi trường runtime hỗ trợ và được cấp quyền.
4. **`tools/research/`:** Nhóm tra cứu thông tin bên ngoài gồm truy vấn tìm kiếm, tải nội dung tài liệu kỹ thuật, trích xuất nguồn kèm URL, thời điểm truy xuất và đánh dấu ranh giới dữ liệu không tin cậy.
5. **`tools/office/`:** Nhóm xử lý tài liệu văn phòng gồm đọc, trích xuất bảng biểu, tính toán số liệu bằng code và xuất tệp Word (`.docx`), Excel (`.xlsx`), PDF (`.pdf`).

Các nhóm trên chỉ là danh mục thiết kế và hợp đồng dữ liệu; tài liệu này không khẳng định các công cụ cụ thể đã được lập trình hoặc đã chạy kiểm chứng thực tế.

## 4. Hợp đồng Đầu vào, Đầu ra và Chuẩn `ToolResult`

Mỗi công cụ phải khai báo đầy đủ cấu trúc dữ liệu đầu vào, cấu trúc đầu ra, quy tắc validation, giới hạn kích thước dữ liệu (`max_input_bytes`, `max_output_bytes`), mã lỗi đặc thù và điều kiện thành công kỹ thuật.

### 4.1. Cấu trúc chuẩn `ToolResult`

Mọi lời gọi công cụ qua Tool Runtime phải trả về đối tượng có cấu trúc thống nhất:

- `tool_call_id` (String, UUID v4): Mã định danh duy nhất cho từng lần gọi công cụ.
- `tool_id` (String): Mã công cụ được triệu gọi.
- `status` (String): Trạng thái kỹ thuật của lời gọi công cụ:
  - `EXECUTION_SUCCEEDED`: Công cụ chạy hoàn tất và dữ liệu đầu ra khớp với `output_schema`.
  - `EXECUTION_FAILED`: Lỗi validation đầu vào, lỗi thực thi nội bộ hoặc tiến trình kết thúc với mã lỗi.
  - `PERMISSION_DENIED`: Yêu cầu bị Permission Manager từ chối trước khi thực thi.
  - `APPROVAL_REQUIRED`: Yêu cầu đang bị tạm giữ chờ người vận hành phê duyệt qua Approval Gate.
  - `CANCELLED`: Lời gọi bị hủy chủ động bởi hệ thống hoặc người dùng.
  - `TIMED_OUT`: Tiến trình vượt quá ngưỡng `timeout_seconds` và bị cưỡng chế dừng.
- `business_verification_status` (String): Trạng thái kiểm chứng giá trị nghiệp vụ: `UNVERIFIED` (mặc định khi công cụ vừa chạy xong), `VERIFIED_PASS`, `VERIFIED_FAIL`.
- `started_at` (Timestamp ISO 8601) và `finished_at` (Timestamp ISO 8601): Mốc thời gian bắt đầu và kết thúc thực tế.
- `output` (Optional[Object]): Dữ liệu kết quả hợp lệ theo `output_schema`; nhận `null` nếu thất bại hoặc bị từ chối.
- `error` (Optional[Object]): Chi tiết lỗi gồm `error_code`, `message`, `retryable` (Boolean) và `details`.
- `artifact_references` (List[Object]): Danh sách tệp tin sinh ra hoặc thay đổi gồm `path`, `sha256`, `size_bytes` và `content_type`.
- `evidence` (Object): Dữ liệu chứng thực gồm `exit_code`, `redacted_stdout_ref`, `redacted_stderr_ref` và `command_summary`.
- `telemetry` (Object): Số liệu vận hành gồm `duration_ms`, `cpu_time_ms` (Optional), `memory_peak_mb` (Optional) và `telemetry_status`.

### 4.2. Phân tách trạng thái kỹ thuật, nghiệp vụ và vòng đời

Tool Runtime chỉ xác nhận việc công cụ có chạy xong đúng hợp đồng hay không (`EXECUTION_SUCCEEDED`). Hệ thống tuyệt đối không đánh đồng `exit_code == 0` với việc nhiệm vụ đạt chuẩn nghiệp vụ (`VERIFIED_PASS`) hay Task chuyển sang `COMPLETED`. Mọi đánh giá đạt hay không đạt về mặt nội dung và chất lượng phải do bước kiểm chứng độc lập của QA thực hiện.

Các miền trạng thái trong hệ thống hoàn toàn tách biệt (không dùng chung tên để suy diễn đồng nghĩa):
- **Agent Task Lifecycle (`AGENT_SPEC.md`):** `QUEUED`, `ASSIGNED`, `RUNNING`, `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED`.
- **Skill Lifecycle (`SKILL_SPEC.md`):** `DRAFT`, `CANDIDATE`, `UNDER_REVIEW`, `APPROVED`, `REJECTED`, `DEPRECATED`, `DISABLED`.
- **Rule Lifecycle (`RULE_SPEC.md`):** `DRAFT`, `UNDER_REVIEW`, `ACTIVE`, `DEPRECATED`, `DISABLED`.
- **Tool Availability Status (`TOOL_SPEC.md`):** `DRAFT`, `REGISTERED`, `AVAILABLE`, `DEPRECATED`, `DISABLED`.
- **Tool Execution Status (`ToolResult.status`):** `EXECUTION_SUCCEEDED`, `EXECUTION_FAILED`, `PERMISSION_DENIED`, `APPROVAL_REQUIRED`, `CANCELLED`, `TIMED_OUT`.
- **QA Verification Status (`business_verification_status`):** `UNVERIFIED`, `VERIFIED_PASS`, `VERIFIED_FAIL`.

## 5. Vòng đời gọi công cụ (Tool Invocation Lifecycle)

Quy trình 10 bước chuẩn hóa cho một lời gọi công cụ trong thiết kế tương lai:

1. Agent gửi yêu cầu triệu gọi công cụ kèm `tool_id`, tham số đầu vào và ngữ cảnh (`task_id`, `run_id`, `agent_id`).
2. Tool Registry kiểm tra `tool_id` có tồn tại và đang ở trạng thái `AVAILABLE`.
3. Tool Runtime xác thực tham số đầu vào đối chiếu với `input_schema` và kiểm tra ngưỡng dung lượng dữ liệu.
4. Permission Manager đánh giá quyền hạn thực thi dựa trên Rule System và Agent Manifest để sinh `PolicyDecision`.
5. Nếu `PolicyDecision` trả về `APPROVAL_REQUIRED`, hệ thống kích hoạt Approval Gate và tạm dừng nhánh công việc ở trạng thái `WAITING_APPROVAL`.
6. Nếu được phép (`ALLOW`), Tool Runtime chuẩn hóa đường dẫn, thiết lập sandbox cô lập và thực thi công cụ dưới sự giám sát của bộ đếm timeout.
7. Thu thập output, mã thoát lệnh, thông báo lỗi, artifact tạo ra, mã băm SHA-256 và số liệu telemetry thực tế.
8. Lọc bỏ thông tin nhạy cảm (secrets redaction) khỏi log và ghi nhận sự kiện vào Event Journal cùng Audit Trail.
9. Trả về đối tượng `ToolResult` hoàn chỉnh cho Agent Runtime với `business_verification_status: UNVERIFIED`.
10. Bộ phận QA hoặc bộ kiểm chứng độc lập thực hiện đánh giá giá trị nghiệp vụ nếu yêu cầu nhiệm vụ đòi hỏi.

## 6. Kiểm soát quyền hạn và Bảo mật (Permission & Security Enforcement)

Hệ thống áp dụng nguyên tắc phòng thủ nhiều lớp tại ranh giới Tool Runtime:

- **Allowlist bắt buộc:** Agent chỉ được phép triệu gọi công cụ có mặt trong danh sách `tools.allowed` của Agent Manifest và đã đăng ký hợp lệ trong Tool Registry.
- **Ranh giới Skill và Department:** Skill chỉ hướng dẫn cách dùng công cụ, không thể cấp thêm quyền. Danh mục `default_tools` của Department chỉ mang tính gợi ý chuyên môn, không tự động trở thành quyền thực thi của nhân viên.
- **Kiểm tra theo ngữ cảnh kép:** Mọi Tool Call được thẩm định dựa trên sự giao thoa giữa quyền của `agent_id` và ranh giới không gian làm việc của `task_id`.
- **Chuẩn hóa đường dẫn và Sandbox:** Đường dẫn tệp tin bắt buộc phải qua bước canonical path validation trước khi đọc/ghi để ngăn thoát khỏi `workspace/{task_id}/`. Các công cụ thuộc nhóm `code` phải tuân thủ Sandbox Policy, không tự cài đặt package lên máy chủ, không tự ý chạy script chưa duyệt và tuyệt đối không truy cập `.env` hay khóa bí mật.
- **Xử lý khi Policy Engine gặp sự cố:** Nếu Policy Engine không phản hồi, lỗi nạp quy tắc hoặc thiếu dữ liệu ngữ cảnh để xác minh quyền, Tool Runtime áp dụng cơ chế **Fail-Closed**, lập tức từ chối mọi hành động có rủi ro (ghi/xóa file, terminal, mạng, cài gói, custom script) và phát cảnh báo sự cố tới Reception và Master.

## 7. Quản trị Tool Registry

Tool Registry chịu trách nhiệm kiểm soát vòng đời khả dụng của công cụ:

- **Đăng ký và phân loại trạng thái:** Phân biệt rạch ròi giữa công cụ mới khai báo trên giấy (`DRAFT` hoặc `REGISTERED`) với công cụ đã có mã nguồn triển khai, vượt qua bài test đơn vị và sẵn sàng phục vụ (`AVAILABLE`).
- **Tra cứu theo năng lực và phiên bản:** Hỗ trợ Master và Core Runtime tìm công cụ theo `category`, `capabilities` và kiểm tra sự hiện diện của các dependencies cần thiết trong môi trường sandbox.
- **Vô hiệu hóa và cập nhật không gián đoạn:** Cho phép chuyển công cụ gặp lỗi sang trạng thái `DISABLED` ngay lập tức để ngăn các phiên mới sử dụng. Các bản cập nhật phiên bản mới tồn tại song song để không làm gãy các tác vụ cũ đang ghim phiên bản trước đó.
- **Cấm tự cấp quyền:** Không cho phép bất kỳ công cụ nào tự đăng ký vào Registry rồi tự động cấp quyền sử dụng cho Agent nếu chưa qua phê duyệt.

## 8. Tích hợp Engine và Tính trung lập với Provider

Tool Runtime được thiết kế độc lập với công nghệ lõi của các AI Provider và không phụ thuộc bắt buộc vào CrewAI:

- **Giao diện trung lập:** Tầng `engines/` giao tiếp với `core/` thông qua một contract trừu tượng, tách biệt logic gọi công cụ khỏi định dạng API riêng của từng nhà cung cấp mô hình.
- **Tích hợp Antigravity CLI và các Provider khác:**
  - Khi sử dụng Antigravity hoặc các CLI Agent có khả năng thao tác công cụ nội sinh, Engine Adapter chịu trách nhiệm truyền cấu hình giới hạn workspace, giám sát tiến trình và kiểm chứng lại kết quả thực tế trên đĩa sau khi thực thi.
  - Tài liệu này không giả định trước việc Antigravity CLI hỗ trợ một giao thức tool-calling hay streaming JSON cụ thể nào cho đến khi được kiểm chứng thực tế tại Phase 02.
- **Công cụ nội bộ AI Village:** Khi làm việc với các Provider chỉ trả về văn bản hoặc JSON yêu cầu gọi hàm, Tool Runtime của AI Village sẽ trực tiếp điều phối thực thi các công cụ trong `tools/` và trả về `ToolResult` chuẩn hóa.

## 9. An toàn thực thi và Cách ly (Execution Safety & Isolation)

Các biện pháp kỹ thuật bảo vệ sự ổn định của hệ thống máy chủ:

- **Cách ly không gian làm việc:** Mỗi tác vụ sở hữu thư mục `workspace/{task_id}/` riêng biệt; công cụ không được phép đọc/ghi chéo sang workspace của tác vụ khác.
- **Kiểm soát môi trường chạy:** Phân định rõ hai chế độ: chạy trong sandbox cách ly (container hoặc môi trường giới hạn tài nguyên không kết nối mạng ngoài cho nhóm `code`) và thao tác cục bộ bị khóa ranh giới đường dẫn nghiêm ngặt (cho nhóm `files` trong workspace).
- **Giới hạn tài nguyên và đồng thời:** Kiểm soát chặt chẽ `timeout_seconds`, giới hạn bộ nhớ, CPU và số lượng lời gọi công cụ chạy song song để tránh nghẽn hệ thống.
- **Hủy tác vụ an toàn (Cancellation):** Khi nhận tín hiệu hủy, Tool Runtime phải chấm dứt toàn bộ cây tiến trình con đang chạy và thu hồi tài nguyên cấp phát.
- **Idempotency và Retry có kiểm soát:** Chỉ cho phép tự động thử lại đối với các lỗi tạm thời (mất kết nối mạng ngắn hạn, lock tạm thời) khi công cụ khai báo `idempotency.supported: true` và `idempotency.safe_retry: true`. Nếu một thao tác có tác động phụ chưa xác định được lần chạy trước đã thành công hay chưa (như bị đứt kết nối sau khi gửi lệnh ghi/xóa), hệ thống tuyệt đối không tự động chạy lại mù quáng.

## 10. Bằng chứng, Kiểm toán và Telemetry

Mọi lời gọi công cụ phải để lại dấu vết kiểm toán đầy đủ:

- **Truy xuất liên kết:** Mỗi `tool_call_id` phải liên kết chặt chẽ với `task_id`, `run_id`, `agent_id` và `session_id` trong Event Journal.
- **Tính toàn vẹn và bảo mật log:** Lưu trữ dấu thời gian thực tế, mã thoát lệnh, mã băm SHA-256 cho mọi artifact tạo thành. Trước khi ghi log vào `.runtime/` hay truyền qua API, toàn bộ chuỗi stdout/stderr phải đi qua bộ lọc loại bỏ mật khẩu, token và chuỗi kết nối.
- **Trung thực số liệu Telemetry:** Tuyệt đối không tự bịa đặt thời gian chạy, CPU, dung lượng RAM hay lượng token tiêu thụ. Nếu một thông số không được hệ thống đo đạc hoặc không được Provider cung cấp, phải đặt giá trị `null` và đánh dấu `telemetry_status: "UNAVAILABLE_FROM_PROVIDER"`.

## 11. Quản lý phiên bản và Xử lý lỗi (Versioning & Failure Handling)

### 11.1. Phiên bản hóa và Tương thích
- Tuân thủ SemVer (`MAJOR.MINOR.PATCH`). Thay đổi breaking change trên `input_schema` hoặc `output_schema` bắt buộc phải tăng phiên bản `MAJOR`.
- Công cụ cũ được đánh dấu `DEPRECATED` kèm thời gian chuyển tiếp trước khi ngừng hỗ trợ; hỗ trợ rollback nhanh về phiên bản ổn định trước đó trong Registry.

### 11.2. Xử lý các tình huống lỗi chuẩn

| Mã lỗi (`error_code`) | Ý nghĩa kỹ thuật | Hành vi xử lý mặc định và Leo thang |
| :--- | :--- | :--- |
| `QUOTA_EXCEEDED` | Vượt hạn mức gọi API hoặc tài nguyên cấp phép. | Không retry vô hạn; báo ngay cho Resources và Master để tạm dừng hoặc điều chỉnh. |
| `TIMED_OUT` | Công cụ chạy quá thời gian `timeout_seconds`. | Cưỡng chế ngắt tiến trình; nếu `safe_retry: false`, không tự chạy lại và báo Master. |
| `PERMISSION_DENIED` | Vi phạm chính sách phân quyền hoặc ngoài phạm vi workspace. | Chặn thực thi, ghi Audit Event cảnh báo an ninh, trả lỗi cấu trúc cho Agent. |
| `MISSING_DEPENDENCIES` | Thiếu công cụ hệ thống hoặc thư viện trong sandbox. | Không tự cài lên host; đánh dấu nhánh việc `BLOCKED` và báo Master/IT xử lý môi trường. |
| `TOOL_UNAVAILABLE` | Công cụ đang ở trạng thái `DISABLED` hoặc chưa cài đặt. | Từ chối thực thi; đề xuất Agent tìm công cụ thay thế hoặc báo Master. |
| `PARTIAL_EXECUTION` | Thao tác thực hiện dở dang rồi đứt quãng trên hành động không idempotent. | Cấm tự động retry; giữ nguyên hiện trường, gắn cờ yêu cầu Master hoặc con người kiểm tra. |
| `PROVIDER_DISCONNECTED` | Mất kết nối tới Antigravity hoặc AI Provider bên ngoài. | Thử lại giới hạn theo backoff nếu an toàn; quá ngưỡng thì chuyển `BLOCKED` và báo Reception/Master. |
| `OUTPUT_INVALID` | Dữ liệu trả về không khớp `output_schema` của công cụ. | Đánh dấu `EXECUTION_FAILED`, lưu log thô đã lọc secret để IT kiểm tra, không đưa dữ liệu hỏng vào bước sau. |

## 12. Chuẩn hóa Tool ID, Action ID, Capability ID và Permission ID

Đối chiếu giữa `AGENT_SPEC.md`, `DEPARTMENT_SPEC.md`, `SKILL_SPEC.md` và `RULE_SPEC.md`, cần phân biệt rạch ròi bốn loại định danh để tránh nhầm lẫn khi xây dựng Registry và Policy Engine:

1. **Tool ID:** Định danh một công cụ cụ thể trong Tool Registry. Quy chuẩn: `tools.<category>.<tool_name>` (ví dụ: `tools.files.read`, `tools.code.run_test`, `tools.code.run_custom_script`). Được dùng trong `agent.tools.allowed`, `department.default_tools` và `skill.requirements.tools`.
2. **Action ID:** Định danh một loại hành động kỹ thuật bị kiểm soát bởi Rule System. Quy chuẩn: `<domain>.<verb>` viết bằng `snake_case` (ví dụ: `files.read`, `files.write`, `terminal.exec`, `code.run_test`, `code.exec_custom`, `network.http`). Được dùng trong `rule.actions`, `agent.permissions.requires_approval` và `tool.permissions_required`.
3. **Capability ID:** Nhãn mô tả năng lực chuyên môn cấp cao để Master ghép đội ngũ. Quy chuẩn: `snake_case` không chứa dấu chấm (ví dụ: `git_diff_analysis`, `python_ast_audit`, `automated_testing`). Được dùng trong `agent.capabilities.skills` và `department.supported_capabilities`.
4. **Permission ID:** Quyền thực thi tổng hợp gắn kết một Action ID với phạm vi tài nguyên xác định trong bộ kiểm tra quyền (ví dụ: quyền `files.read` trên phạm vi `workspace/{task_id}/**`).

### Cơ chế ánh xạ thống nhất

- Một `tool_id` khai báo các `action_id` mà nó cần trong trường `permissions_required`.
- Khi Agent muốn chạy một công cụ, Permission Manager thực hiện kiểm tra hai bước: (1) xác nhận `tool_id` nằm trong `agent.tools.allowed`, và (2) đánh giá từng `action_id` trong `tool.permissions_required` đối chiếu với tài nguyên yêu cầu qua Rule System.

Bảng ánh xạ chuẩn hóa giữa Tool ID, Action ID và Capability ID:

| Tool ID (`tools.allowed` / `requirements.tools` / `default_tools`) | Action ID (`permissions_required` / `rule.actions` / `requires_approval`) | Capability ID liên quan |
| :--- | :--- | :--- |
| `tools.files.read` | `files.read` | `file_reading`, `workspace_inspection` |
| `tools.files.write` | `files.write` | `file_writing` |
| `tools.code.run_test` | `code.run_test`, `files.read` | `automated_testing` |
| `tools.code.run_linter` | `code.run_linter`, `files.read` | `git_diff_analysis` |
| `tools.code.run_ast_parser` | `code.run_ast_parser`, `files.read` | `python_ast_audit` |
| `tools.code.run_custom_script` | `code.exec_custom`, `terminal.exec` | `custom_automation` |
| `tools.research.query` | `network.http` | `web_research` |

*(Các định danh trên đã được đối chiếu và đồng bộ thống nhất giữa `AGENT_SPEC.md`, `DEPARTMENT_SPEC.md`, `SKILL_SPEC.md`, `RULE_SPEC.md` và `TOOL_SPEC.md` trong P01B.07.)*

## 13. Kịch bản kiểm chứng độc lập (Verification Scenarios)

Bảng 12 kịch bản kiểm thử dùng để nghiệm thu Tool Registry và Tool Runtime khi triển khai code ở các phase sau (chưa viết test code trong P01B.05):

| STT | Tình huống kiểm thử | Expected Result | Evidence cần kiểm chứng |
| :--- | :--- | :--- | :--- |
| 1 | Tool đọc tệp hợp lệ trong workspace | `status: EXECUTION_SUCCEEDED`, `business_verification_status: UNVERIFIED`. | `ToolResult` chứa nội dung đúng, SHA-256 tệp khớp, có Audit Event. |
| 2 | Tool bị từ chối khi truy cập ngoài phạm vi | `status: PERMISSION_DENIED` (`PATH_OUTSIDE_WORKSPACE`). | `PolicyDecision(DENY)`, tệp ngoài không bị chạm tới, có Security Audit Event. |
| 3 | Tool nhận tham số đầu vào sai schema | `status: EXECUTION_FAILED` (`INVALID_INPUT_SCHEMA`). | Chi tiết lỗi JSON Schema validation; không có tiến trình nào được khởi chạy. |
| 4 | Tool kích hoạt yêu cầu Approval Gate | `status: APPROVAL_REQUIRED`; Agent chuyển `WAITING_APPROVAL`. | Bản ghi `ApprovalRequest` tồn tại với `approval_id`; hành động chưa thực thi. |
| 5 | Yêu cầu Approval bị người dùng từ chối | `status: EXECUTION_FAILED` hoặc `CANCELLED` (`APPROVAL_REJECTED`). | Nhật ký quyết định `REJECTED` của người dùng; công cụ không chạy. |
| 6 | Tool chạy vượt quá giới hạn timeout | `status: TIMED_OUT`. | Tiến trình con bị ngắt đúng ngưỡng `timeout_seconds`, không còn tiến trình treo. |
| 7 | Tool nhận lệnh hủy giữa chừng (Cancel) | `status: CANCELLED`. | Tín hiệu hủy được thực thi, tài nguyên dọn dẹp sạch, không ghi nhận hoàn thành giả. |
| 8 | Tool trả về mã thoát khác 0 | `status: EXECUTION_FAILED`. | Ghi nhận đúng `exit_code`, log lỗi đã lọc secret (`redacted_stderr_ref`). |
| 9 | Tool tạo tệp artifact mới | `status: EXECUTION_SUCCEEDED` kèm `artifact_references`. | Tệp tồn tại trên đĩa, mã băm SHA-256 và dung lượng trong `ToolResult` khớp thực tế. |
| 10 | Tool chạy xong (`exit_code == 0`) nhưng kết quả nghiệp vụ sai | Tool báo `EXECUTION_SUCCEEDED`, sau kiểm chứng báo `VERIFIED_FAIL`. | Báo cáo từ QA/validator chứng minh hệ thống không đánh đồng exit code 0 với PASS. |
| 11 | AI Provider không hỗ trợ công cụ yêu cầu | `status: EXECUTION_FAILED` (`TOOL_UNSUPPORTED_BY_PROVIDER`). | Thông báo lỗi chuẩn từ Engine Adapter, tuyệt đối không tạo kết quả giả lập. |
| 12 | Tool lỗi giữa chừng ở thao tác không an toàn để thử lại (`safe_retry: false`) | Chặn retry tự động, trả `EXECUTION_FAILED` (`PARTIAL_EXECUTION`). | Audit log xác nhận chỉ chạy 1 lần, không gọi lại mù quáng, đã báo cáo Master. |

## 14. Hồ sơ minh họa mẫu (Example Tool Manifests)

Ba tệp cấu hình dưới đây tuân thủ hoàn toàn bảng Tool Manifest Schema 16 trường tại Mục 2 và đặt `status: "DRAFT"` để phản ánh đây chỉ là ví dụ thiết kế minh họa cấu trúc dữ liệu, chưa phải công cụ đã được lập trình hay đang hoạt động ở trạng thái `AVAILABLE` trong Runtime.

### 14.1. Ví dụ 1: Công cụ chỉ đọc tệp (`tools.files.read`)

```yaml
spec_version: "1.0.0"
tool_id: "tools.files.read"
name: "Read File in Workspace"
description: "Đọc nội dung tệp văn bản trong phạm vi đường dẫn workspace hoặc tài liệu được cấp phép."
version: "1.0.0"
category: "files"
capabilities:
  - "file_reading"
  - "workspace_inspection"
input_schema:
  type: "object"
  required: ["path"]
  properties:
    path:
      type: "string"
      description: "Đường dẫn tương đối tới tệp cần đọc."
    max_bytes:
      type: "integer"
      default: 1048576
      maximum: 5242880
output_schema:
  type: "object"
  required: ["content", "size_bytes", "sha256"]
  properties:
    content:
      type: "string"
    size_bytes:
      type: "integer"
    sha256:
      type: "string"
permissions_required:
  - "files.read"
execution_environment: "restricted_local"
timeout_seconds: 15
risk_level: "LOW"
approval_policy: "NONE"
idempotency:
  supported: true
  safe_retry: true
status: "DRAFT"
```

### 14.2. Ví dụ 2: Công cụ chạy kiểm thử trong sandbox (`tools.code.run_test`)

```yaml
spec_version: "1.0.0"
tool_id: "tools.code.run_test"
name: "Run Automated Tests in Sandbox"
description: "Thực thi bộ kiểm thử tự động trong môi trường sandbox cách ly và trả về nhật ký kết quả."
version: "1.0.0"
category: "code"
capabilities:
  - "automated_testing"
  - "code_verification"
input_schema:
  type: "object"
  required: ["test_path"]
  properties:
    test_path:
      type: "string"
      description: "Đường dẫn thư mục hoặc tệp test trong workspace."
    framework:
      type: "string"
      enum: ["pytest", "unittest"]
      default: "pytest"
output_schema:
  type: "object"
  required: ["passed", "exit_code", "summary"]
  properties:
    passed:
      type: "boolean"
    exit_code:
      type: "integer"
    summary:
      type: "string"
permissions_required:
  - "code.run_test"
  - "files.read"
execution_environment: "sandbox_only"
timeout_seconds: 300
risk_level: "MEDIUM"
approval_policy: "CONDITIONAL"
idempotency:
  supported: true
  safe_retry: true
status: "DRAFT"
```

### 14.3. Ví dụ 3: Công cụ tra cứu thông tin mạng (`tools.research.query`)

```yaml
spec_version: "1.0.0"
tool_id: "tools.research.query"
name: "Query External Research Sources"
description: "Tìm kiếm và trích xuất tài liệu từ các nguồn công khai được phép theo chính sách mạng của nhiệm vụ."
version: "1.0.0"
category: "research"
capabilities:
  - "web_research"
  - "source_verification"
input_schema:
  type: "object"
  required: ["query"]
  properties:
    query:
      type: "string"
      minLength: 3
      maxLength: 500
    max_results:
      type: "integer"
      default: 5
      maximum: 20
output_schema:
  type: "object"
  required: ["results", "retrieved_at"]
  properties:
    results:
      type: "array"
      items:
        type: "object"
        required: ["url", "title", "snippet"]
    retrieved_at:
      type: "string"
      format: "date-time"
permissions_required:
  - "network.http"
execution_environment: "sandbox_only"
timeout_seconds: 60
risk_level: "MEDIUM"
approval_policy: "CONDITIONAL"
idempotency:
  supported: true
  safe_retry: true
status: "DRAFT"
```
