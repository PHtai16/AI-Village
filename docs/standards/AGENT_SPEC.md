# Tiêu chuẩn Định nghĩa AI Agent (Agent Specification)

Tài liệu này xác lập cấu trúc chuẩn mực cho việc khai báo, quản lý vòng đời và vận hành các AI Agent trong hệ thống Multi-Agent AI Village. Tiêu chuẩn này đóng vai trò giao ước dữ liệu cho Agent Registry tại `agents/` và Agent Runtime tại `core/`.

## 1. Nguyên lý kiến trúc

Agent trong AI Village được định nghĩa là một thực thể lao động số có ranh giới trách nhiệm, vai trò cụ thể và quyền hạn rõ ràng:

1. **Thực thể logic độc lập:** Agent không tương đương với một tài khoản AI hay một khóa API riêng biệt. Nhiều Agent với các vai trò khác nhau có thể sử dụng chung một Engine hoặc một mô hình ngôn ngữ nền tảng.
2. **Tách biệt tĩnh và động:** Cấu hình tĩnh (Static Manifest) quy định danh tính, năng lực, quyền hạn và ranh giới an toàn. Trạng thái động (Runtime State) phản ánh ngữ cảnh phiên làm việc, tiến độ tác vụ, tài nguyên thực tế và lịch sử tương tác.
3. **Tổ chức phòng ban linh hoạt:** Hệ thống cung cấp sẵn 9 phòng ban chức năng mặc định (`hr`, `resources`, `service`, `marketing`, `it`, `reception`, `process`, `qa`, `vault`). Danh mục này có thể mở rộng thông qua Department Registry tại `agents/departments/`. Master có thể triệu tập nhân viên vào bất kỳ nhóm công tác liên phòng ban nào tùy theo đòi hỏi thực tế của nhiệm vụ. Riêng Master sử dụng định danh phạm vi điều hành `department: "orchestration"` gắn với `system_role: "orchestrator"`.
4. **Không phụ thuộc framework điều phối:** Cấu trúc định nghĩa Agent tương thích với các mô hình điều phối phổ biến như CrewAI hoặc orchestrator nội bộ, không gắn chặt mã nguồn vào API riêng của bất kỳ thư viện bên thứ ba nào.
5. **Chứng thực độc lập bằng Runtime:** Mọi trạng thái và kết quả công việc phải do Agent Runtime và hệ thống kiểm thử xác nhận từ dữ liệu thực tế, tuyệt đối không dựa vào lời tuyên bố trong nội dung phản hồi của mô hình ngôn ngữ.

## 2. Các thành phần đặc tả chi tiết

### 2.1. Identity (Danh tính)
Xác định sự tồn tại duy nhất của Agent trong hệ thống:
- `spec_version`: Phiên bản của chuẩn đặc tả dữ liệu (ví dụ: `"1.0.0"`).
- `id`: Định danh duy nhất theo định dạng `dept.role_name`, viết thường, ngăn cách bằng dấu chấm (ví dụ: `it.code_reviewer`, hoặc `core.master` đối với Master Agent).
- `name`: Tên gọi hiển thị của nhân viên (ví dụ: "Quốc Bảo").
- `role`: Chức danh chuyên môn (ví dụ: "Senior Code Quality Inspector").
- `system_role`: Phân loại vai trò hệ thống, nhận giá trị `"worker"` hoặc `"orchestrator"`.
  - **Quy tắc ràng buộc có điều kiện:** Worker Agent bắt buộc dùng `system_role: "worker"` và tham chiếu một `department` hợp lệ trong Department Registry; không được tự khai báo `"orchestrator"` để tăng quyền. Chỉ Master Agent (`id: "core.master"`) mới được dùng `system_role: "orchestrator"` và phạm vi `department: "orchestration"` (không xem là phòng ban chuyên môn thứ 10). Thuộc tính `system_role` không tự động cấp quyền thực thi; Permission Manager luôn kiểm tra quyền độc lập cho mọi Agent.
- `department`: Phòng ban chủ quản. Chấp nhận một trong 9 phòng ban mặc định hoặc phòng ban tùy biến đã đăng ký hợp lệ trong `agents/departments/` (ngoại lệ `"orchestration"` chỉ dành cho `system_role: "orchestrator"`).
- `description`: Mô tả súc tích về phạm vi trách nhiệm của vai trò.

### 2.2. Mission (Mục đích và trách nhiệm)
Xác định lý do tồn tại và phạm vi công việc:
- `mission.purpose`: Tuyên bố mục đích giải thích rõ Agent giải quyết bài toán nào.
- `mission.responsibilities`: Danh sách các trách nhiệm cụ thể Agent được phép và bắt buộc thực hiện.
- `mission.non_goals`: Danh sách các việc không thuộc phạm vi, ngăn ngừa việc chồng chéo quyền hạn giữa các nhân viên.

### 2.3. Capabilities (Năng lực thực tế)
Liệt kê các năng lực có thể kiểm chứng độc lập:
- `capabilities.skills`: Danh mục nhãn năng lực thực tế dùng để Master tìm kiếm và ghép đội (ví dụ: `git_diff_analysis`, `python_ast_audit`).
- `capabilities.inputs`: Định dạng dữ liệu đầu vào có thể tiếp nhận (`git_diff`, `source_code`, `test_report`).
- `capabilities.outputs`: Định dạng dữ liệu đầu ra có thể tạo lập (`markdown_review_report`, `structured_findings_json`).

### 2.4. AI Engine (Động cơ trí tuệ)
Cấu hình kết nối runtime:
- `engine.default_type`: Engine mặc định là `antigravity`. Có thể cấu hình sang `openai_compatible`, `anthropic_compatible`, hoặc `local_llm`.
- `engine.model_tier`: Nhóm mô hình ưu tiên phù hợp với độ phức tạp của vai trò (`pro`, `flash`, `code`).
- `engine.parameters`: Cặp khóa và giá trị tham số vận hành như `temperature`, `timeout_seconds`.

### 2.5. Memory (Phạm vi bộ nhớ)
Phân định phạm vi lưu trữ thông tin:
- `memory.scopes`: Danh sách các phạm vi bộ nhớ được cấp phép truy xuất, bao gồm `private` cho ghi chú nội bộ của phiên, `project` cho bối cảnh nhiệm vụ chung, và `global` cho tri thức chung toàn hệ thống tại `storage/knowledge/`.

### 2.6. Skills (Kỹ năng nghiệp vụ)
Liên kết các quy trình chuẩn hóa:
- `skills.allowed`: Danh sách mã định danh kỹ năng (`skill_id` dạng `snake_case`, ví dụ: `git_diff_code_review`) từ thư mục `skills/approved/` mà Agent được cấp phép sử dụng. Phiên bản thực thi cụ thể được xác định và cố định khi Task bắt đầu.

### 2.7. Tools (Công cụ thực thi)
Danh mục các công cụ hệ thống được cấp quyền gọi:
- `tools.allowed`: Danh sách định danh công cụ trong `tools/` (ví dụ: `tools.files.read`, `tools.code.run_linter`).

### 2.8. Permissions (Chính sách phân quyền)
Ranh giới an toàn cho các thao tác tài nguyên:
- `permissions.fs_read`: Danh sách phạm vi đường dẫn được đọc.
- `permissions.fs_write`: Danh sách phạm vi đường dẫn được ghi. Đối với các IT Agent thực hiện nhiệm vụ lập trình, quyền ghi được cấp trong không gian làm việc của nhiệm vụ (`workspace/{task_id}/`) hoặc trên nhánh phát triển được chỉ định trong sandbox. Tuyệt đối cấm quyền ghi đè mã nguồn production hoặc sửa trực tiếp nhánh chính khi chưa có phê duyệt.
- `permissions.network`: Chính sách kết nối mạng (`none`, `intranet`, `full`).
- `permissions.requires_approval`: Danh sách Action ID (định dạng `<domain>.<action>`, ví dụ: `code.exec_custom`) bắt buộc phải dừng lại chờ người vận hành phê duyệt trước khi thực thi.

### 2.9. Limits (Giới hạn thực thi)
Các tham số kiểm soát an toàn tài nguyên:
- `limits.timeout_seconds`: Thời gian chạy tối đa cho mỗi lượt thao tác.
- `limits.max_retries`: Số lần thử lại tối đa khi gặp sự cố tạm thời trước khi chuyển trạng thái lỗi.

### 2.10. Task Lifecycle (Vòng đời nhiệm vụ)
Trạng thái thực thi của Agent trong một nhiệm vụ được quản lý qua máy trạng thái:
- `QUEUED`: Đã tiếp nhận yêu cầu từ Master, đang chờ phân bổ tài nguyên.
- `ASSIGNED`: Đã vào phiên làm việc, nhận đầy đủ dữ liệu đầu vào.
- `RUNNING`: Đang phân tích, gọi công cụ hoặc xử lý dữ liệu.
- `WAITING_INPUT`: Tạm dừng để chờ phản hồi từ Agent khác hoặc làm rõ từ Master.
- `WAITING_APPROVAL`: Tạm dừng có chủ đích để chờ con người phê duyệt hành động nhạy cảm.
- `BLOCKED`: Bị nghẽn do yếu tố khách quan bên ngoài như dịch vụ ngoài mất kết nối, thiếu file đầu vào hoặc công cụ phụ thuộc gặp sự cố. Trạng thái này phân biệt rõ với `FAILED` và `WAITING_APPROVAL`.
- `COMPLETED`: Hoàn thành toàn bộ công việc và xuất artifact kèm bằng chứng hợp lệ.
- `FAILED`: Thất bại do lỗi thực thi nội bộ hoặc không thể vượt qua bài kiểm tra sau khi hết số lần thử lại.
- `CANCELLED`: Tác vụ bị hủy chủ động bởi Master hoặc người dùng.

Toàn bộ các trạng thái trên do Agent Runtime tại `core/` quan sát và xác nhận từ sự kiện hệ thống thực tế (mã thoát lệnh, tiến trình hệ điều hành, event journal). Lời tự xưng hoàn thành trong văn bản phản hồi của mô hình không có giá trị quyết định trạng thái.

### 2.11. Communication & Collaboration (Giao thức trao đổi)
Cơ chế phối hợp nội bộ:
- Trao đổi trực tiếp có cấu trúc giữa hai Agent cùng nhóm công tác để xử lý công việc kỹ thuật.
- Tham gia họp bàn dưới sự chủ tọa của Master với vai trò cụ thể.
- Tranh luận phản biện có cấu trúc bắt buộc phải đưa ra luận điểm, phản biện và bằng chứng dữ liệu, không tranh luận suông.

### 2.12. Handoff & Quality (Bàn giao và chất lượng)
Quy chuẩn chuyển giao sản phẩm:
- Bàn giao kèm định danh sản phẩm trong `workspace/` hoặc `output/`.
- Đi kèm biên bản bằng chứng thực tế gồm mã băm SHA-256, log kiểm thử, kết quả chạy linter.
- Thẩm định độc lập: Agent không được tự chấm PASS cho sản phẩm của mình; việc nghiệm thu do bộ phận QA hoặc test runner độc lập thực hiện.

### 2.13. Security Boundaries (Ranh giới bảo mật)
Các nguyên tắc bất biến:
- Tuyệt đối không đọc, ghi hoặc truyền phát các tệp chứa khóa bí mật, tệp `.env`, khóa SSH.
- Quyền can thiệp mã nguồn của IT Agent bị giới hạn trong workspace hoặc nhánh thử nghiệm, nghiêm cấm sửa đổi trực tiếp môi trường production.
- Mọi hành vi vượt quyền đều bị runtime chặn và ghi nhật ký cảnh báo.

## 3. Bảng phân định Schema: Tĩnh và Động

### 3.1. Static Manifest Schema (Khai báo tĩnh)

Cấu hình tĩnh lưu trữ trong tệp `agent.yaml` (hoặc `manifest.yaml`, `manifest.json`) tại thư mục của vai trò trong `agents/`.

| Tên trường | Kiểu dữ liệu | Bắt buộc | Ràng buộc giá trị và mô tả |
| :--- | :--- | :--- | :--- |
| `spec_version` | String | Có | Phiên bản chuẩn đặc tả, ví dụ: `"1.0.0"`. |
| `id` | String | Có | Khóa chính duy nhất, định dạng `^[a-z0-9_]+\.[a-z0-9_]+$`. |
| `name` | String | Có | Tên hiển thị, độ dài từ 3 đến 64 ký tự. |
| `role` | String | Có | Chức danh chuyên môn, độ dài từ 3 đến 64 ký tự. |
| `system_role` | String | Có | Giá trị hợp lệ: `"worker"` hoặc `"orchestrator"`. Không tự cấp quyền thực thi. |
| `department` | String | Có | Tên phòng ban hợp lệ trong `agents/departments/`; ngoại lệ `"orchestration"` chỉ dùng cho `id: "core.master"` với `system_role: "orchestrator"`. |
| `description` | String | Có | Mô tả ngắn gọn phạm vi trách nhiệm. |
| `mission.purpose` | String | Có | Mục đích hoạt động cốt lõi. |
| `mission.responsibilities` | List[String] | Có | Danh sách ít nhất 1 trách nhiệm cụ thể. |
| `mission.non_goals` | List[String] | Không | Danh sách các việc không thuộc phạm vi xử lý. |
| `capabilities.skills` | List[String] | Có | Danh mục nhãn năng lực thực tế có thể kiểm chứng. |
| `capabilities.inputs` | List[String] | Có | Định dạng dữ liệu chấp nhận đầu vào. |
| `capabilities.outputs` | List[String] | Có | Định dạng dữ liệu sinh ra ở đầu ra. |
| `engine.default_type` | String | Có | Loại engine điều khiển, mặc định là `"antigravity"`. |
| `engine.model_tier` | String | Có | Nhóm mô hình ưu tiên: `"pro"`, `"flash"`, `"code"`. |
| `engine.parameters` | Object | Không | Tham số vận hành bổ sung như `temperature`, `timeout_seconds`. |
| `memory.scopes` | List[String] | Có | Danh sách phạm vi bộ nhớ, chứa ít nhất `"private"`. |
| `skills.allowed` | List[String] | Không | Danh sách `skill_id` (định dạng `^[a-z0-9_]+$`) từ `skills/approved/`. |
| `tools.allowed` | List[String] | Có | Danh mục Tool ID (`tools.<category>.<name>`) được phép triệu gọi. |
| `permissions.fs_read` | List[String] | Có | Danh sách phạm vi đường dẫn được đọc. |
| `permissions.fs_write` | List[String] | Có | Danh sách phạm vi đường dẫn được ghi (chỉ trong workspace/nhánh dev). |
| `permissions.network` | String | Có | Mức độ kết nối mạng: `"none"`, `"intranet"`, `"full"`. |
| `permissions.requires_approval` | List[String] | Không | Danh sách Action ID (`<domain>.<action>`) bắt buộc con người phê duyệt. |
| `limits.timeout_seconds` | Integer | Có | Giới hạn thời gian tối đa cho mỗi lượt thao tác. |
| `limits.max_retries` | Integer | Có | Số lần thử lại tối đa khi gặp lỗi tạm thời. |

### 3.2. Dynamic Runtime State Schema (Trạng thái động)

Dữ liệu trạng thái được Agent Runtime cập nhật liên tục vào cơ sở dữ liệu tại `.runtime/` và không đưa vào Git.

| Tên trường | Kiểu dữ liệu | Bắt buộc | Ràng buộc giá trị và mô tả |
| :--- | :--- | :--- | :--- |
| `session_id` | String | Có | Mã định danh phiên làm việc, UUID v4. |
| `agent_id` | String | Có | Khóa ngoại tham chiếu đến trường `id` của Static Manifest. |
| `current_task_id` | Optional[String] | Không | Mã nhiệm vụ đang xử lý nếu có. |
| `status` | String | Có | Trạng thái vòng đời: `QUEUED`, `ASSIGNED`, `RUNNING`, `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED`. |
| `current_step` | Optional[String] | Không | Mô tả hành động đang diễn ra trong runtime. |
| `assigned_team_id` | Optional[String] | Không | Mã nhóm công tác do Master thiết lập. |
| `context_tokens_used` | Optional[Integer] | Không | Số token thực tế tiêu thụ trong ngữ cảnh. Nhận giá trị `null` nếu provider không hỗ trợ cung cấp số liệu. |
| `telemetry_status` | String | Có | Tình trạng số liệu tài nguyên: `VERIFIED_MEASURED`, `UNAVAILABLE_FROM_PROVIDER`, `ESTIMATED_PENDING_AUDIT`. Nghiêm cấm bịa số liệu. |
| `total_tool_calls` | Integer | Có | Số lượt công cụ đã được thực thi thật trong phiên. |
| `last_active_at` | Timestamp | Có | Thời điểm hoạt động gần nhất theo chuẩn ISO 8601. |
| `error_count` | Integer | Có | Số lỗi liên tiếp ghi nhận trong phiên hiện tại. |
| `active_artifacts` | List[String] | Không | Danh sách đường dẫn tệp tin đang mở hoặc vừa xuất bản. |

## 4. Hồ sơ minh họa mẫu: IT Code Reviewer

Hồ sơ dưới đây là ví dụ chuẩn mực khớp 100% với Static Manifest Schema, sử dụng cho vai trò thẩm định chất lượng mã nguồn thuộc bộ phận IT.

```yaml
spec_version: "1.0.0"
id: "it.code_reviewer"
name: "Quốc Bảo"
role: "Senior Code Quality Inspector"
system_role: "worker"
department: "it"
description: "Chuyên gia đánh giá chất lượng mã nguồn, kiểm tra tuân thủ kiến trúc và phát hiện lỗ hổng bảo mật."

mission:
  purpose: "Thẩm định độc lập các thay đổi trong mã nguồn trước khi tích hợp vào nhánh chính."
  responsibilities:
    - "Phân tích git diff và kiểm tra tính tuân thủ quy chuẩn PEP 8 và Clean Code."
    - "Phát hiện các đoạn mã có nguy cơ rò rỉ secret hoặc vi phạm an toàn bộ nhớ."
    - "Đối chiếu cấu trúc file thay đổi với ranh giới trách nhiệm của các module."
  non_goals:
    - "Không trực tiếp viết mã tính năng thay cho lập trình viên."
    - "Không tự ý phê duyệt merge request vào kho lưu trữ."

capabilities:
  skills:
    - "git_diff_analysis"
    - "python_ast_audit"
    - "security_secret_scanning"
  inputs:
    - "git_diff"
    - "source_code"
    - "test_report"
  outputs:
    - "markdown_review_report"
    - "structured_findings_json"

engine:
  default_type: "antigravity"
  model_tier: "code"
  parameters:
    temperature: 0.1
    timeout_seconds: 180

memory:
  scopes:
    - "private"
    - "project"
    - "global"

skills:
  allowed:
    - "git_diff_code_review"
    - "secret_security_audit"

tools:
  allowed:
    - "tools.files.read"
    - "tools.code.run_linter"
    - "tools.code.run_ast_parser"

permissions:
  fs_read:
    - "workspace/*"
    - "docs/*"
  fs_write:
    - "workspace/{task_id}/review_reports/*"
  network: "none"
  requires_approval:
    - "code.exec_custom"

limits:
  timeout_seconds: 180
  max_retries: 2
```

## 5. Kết luận vận hành

Tiêu chuẩn đặc tả Agent này đảm bảo tính nhất quán tuyệt đối giữa việc khai báo cấu hình nhân sự và cơ chế giám sát thực thi của hệ thống. Bất kỳ Agent mới nào được tạo lập đều phải vượt qua bài kiểm tra hợp lệ của schema này trước khi được nạp vào Agent Registry.
