# Tiêu chuẩn Định nghĩa AI Agent (Agent Specification)

Tài liệu này xác lập cấu trúc chuẩn mực cho việc khai báo, quản lý vòng đời và vận hành các AI Agent trong hệ thống Multi-Agent AI Village. Tiêu chuẩn này đóng vai trò giao ước dữ liệu cho Agent Registry tại `agents/` và Agent Runtime tại `core/`.

## 1. Nguyên lý kiến trúc

Agent trong AI Village được định nghĩa là một thực thể lao động số có ranh giới trách nhiệm, vai trò cụ thể và quyền hạn rõ ràng:

1. **Thực thể logic độc lập:** Agent không tương đương với một tài khoản AI hay một khóa API riêng biệt. Nhiều Agent với các vai trò khác nhau có thể sử dụng chung một Engine hoặc một mô hình ngôn ngữ nền tảng.
2. **Tách biệt tĩnh và động:** Cấu hình tĩnh (Static Manifest) quy định danh tính, năng lực, quyền hạn và phạm vi an toàn. Trạng thái động (Runtime State) phản ánh ngữ cảnh phiên làm việc, tiến độ tác vụ, tài nguyên đã tiêu thụ và lịch sử tương tác.
3. **Tổ chức phòng ban linh hoạt:** Mỗi nhân viên thuộc một phòng ban chức năng chủ quản để phân loại chuyên môn, nhưng Master có thể triệu tập nhân viên vào bất kỳ nhóm dự án liên phòng ban nào tùy theo đòi hỏi thực tế của nhiệm vụ.
4. **Không phụ thuộc framework điều phối:** Cấu trúc định nghĩa Agent tương thích với các mô hình điều phối phổ biến như CrewAI hoặc hệ thống điều phối tùy biến, không gắn chặt mã nguồn vào API nội bộ của bất kỳ thư viện bên thứ ba nào.
5. **Chứng thực bằng hành vi thực tế:** Kết quả do Agent tuyên bố không được xem là hoàn thành nếu thiếu dữ liệu kiểm chứng độc lập từ công cụ hoặc báo cáo QA.

## 2. Các thành phần đặc tả chi tiết

### 2.1. Identity (Danh tính)
Xác định sự tồn tại duy nhất của Agent trong hệ thống:
- `agent_id`: Định danh duy nhất ở dạng định dạng `dept.role_name` hoặc UUID, viết thường, ngăn cách bằng dấu gạch dưới (ví dụ: `it.code_reviewer`).
- `name`: Tên hiển thị của nhân viên (ví dụ: "Minh Trí - Code Reviewer").
- `role`: Chức danh chuyên môn (ví dụ: "Senior Python Reviewer").
- `department`: Phòng ban chủ quản trong 9 nhóm chức năng gồm `hr`, `resources`, `service`, `marketing`, `it`, `reception`, `process`, `qa`, `vault`.
- `avatar`: Đường dẫn định danh hình ảnh đại diện hoặc mã hiệu hiển thị trên giao diện 2D và không gian 3D.

### 2.2. Mission (Mục đích và trách nhiệm)
Xác định lý do tồn tại và phạm vi công việc:
- `purpose`: Tuyên bố mục đích ngắn gọn giải thích Agent giải quyết vấn đề gì.
- `responsibilities`: Danh sách các trách nhiệm cụ thể Agent được phép và phải thực hiện.
- `non_goals`: Các giới hạn công việc mà Agent tuyệt đối không can thiệp, tránh việc chồng chéo vai trò giữa các nhân viên.

### 2.3. Capabilities (Năng lực thực tế)
Liệt kê các năng lực mà Agent thực sự sở hữu và có thể kiểm chứng:
- `skills_required`: Nhãn năng lực dùng để Master tìm kiếm và ghép đội (ví dụ: `python_ast_analysis`, `security_audit`, `git_diff_review`).
- `input_modalities`: Định dạng dữ liệu đầu vào có thể xử lý (`text`, `code_diff`, `json`, `tabular`).
- `output_modalities`: Định dạng dữ liệu đầu ra có thể tạo (`markdown_report`, `json_patch`, `structured_metrics`).

### 2.4. AI Engine (Động cơ trí tuệ)
Cấu hình kết nối runtime:
- `engine_type`: Engine mặc định là `antigravity`. Có thể cấu hình sang `openai_compatible`, `anthropic_compatible`, hoặc `local_llm`.
- `model_preference`: Tên hoặc nhóm mô hình ưu tiên phù hợp với độ phức tạp của vai trò (ví dụ: `pro`, `flash`, `code_specialized`).
- `engine_parameters`: Các tham số vận hành như `temperature`, `max_output_tokens`, `timeout_seconds`.

### 2.5. Session (Quản lý ngữ cảnh)
Kiểm soát vòng đời phiên làm việc:
- `session_mode`: Chế độ ngữ cảnh (`ephemeral_per_task`, `persistent_workspace`, `conversation_bound`).
- `context_window_strategy`: Chiến lược nén hoặc cắt tỉa ngữ cảnh khi đạt giới hạn token (`sliding_window`, `summarization`, `truncate_oldest`).
- `isolation_level`: Mức độ cách ly phiên làm việc để đảm bảo không rò rỉ dữ liệu giữa hai nhiệm vụ độc lập.

### 2.6. Memory (Bộ nhớ đa tầng)
Phân định phạm vi lưu trữ thông tin:
- `private_memory`: Bộ nhớ cá nhân lưu trữ ghi chú ngắn hạn, lịch sử hành vi của riêng Agent trong một phiên.
- `project_memory`: Bộ nhớ dự án chứa thông tin bối cảnh của nhiệm vụ hiện tại, được chia sẻ giữa các thành viên cùng nhóm công tác.
- `global_memory`: Tri thức dùng chung của toàn bộ Village lưu tại `storage/knowledge/`, bao gồm các tiêu chuẩn kỹ thuật và quy định chung.

### 2.7. Skills (Kỹ năng nghiệp vụ)
Liên kết các quy trình đã chuẩn hóa:
- `assigned_skills`: Danh sách mã kỹ năng từ thư mục `skills/approved/` mà Agent được đào tạo để sử dụng.
- `skill_selection_policy`: Cơ chế kích hoạt kỹ năng (`automatic_by_intent`, `explicit_call_only`).

### 2.8. Tools (Công cụ thực thi)
Danh sách các công cụ hệ thống được cấp phép:
- `allowed_tools`: Danh sách định danh công cụ trong `tools/` (ví dụ: `tools.files.read`, `tools.code.run_test`, `tools.research.query`).
- `tool_invocation_mode`: Chế độ thực thi (`sandboxed_only`, `direct_local`).

### 2.9. Permissions (Chính sách phân quyền)
Ranh giới an toàn cho các thao tác tài nguyên:
- `file_system`: Quyền đọc, ghi, xóa được giới hạn nghiêm ngặt trong đường dẫn `workspace/{task_id}/` hoặc `output/{task_id}/`. Tuyệt đối cấm ghi vào thư mục mã nguồn lõi hoặc hệ thống.
- `terminal`: Cho phép thực thi lệnh trong danh mục trắng (allowlist) trong môi trường sandbox, cấm các lệnh phá hủy hoặc can thiệp mạng ngoài quyền.
- `network`: Quy định các địa chỉ và giao thức được phép truy cập (`offline`, `intranet_only`, `public_search`).
- `requires_human_approval`: Danh sách các hành vi buộc phải dừng lại chờ người vận hành duyệt (ví dụ: xóa file hàng loạt, cài đặt package, thực thi lệnh hệ thống nhạy cảm).

### 2.10. Task Lifecycle (Vòng đời nhiệm vụ)
Trạng thái thực thi của Agent trong một nhiệm vụ:
- `QUEUED`: Đã nhận nhiệm vụ từ Master, đang chờ tài nguyên hoặc lượt thực thi.
- `ASSIGNED`: Đã vào phiên làm việc, tiếp nhận yêu cầu và tài liệu đầu vào.
- `RUNNING`: Đang phân tích, gọi công cụ hoặc xử lý dữ liệu.
- `WAITING_INPUT`: Chờ phản hồi từ Agent khác hoặc thông tin bổ sung từ Master.
- `WAITING_APPROVAL`: Chờ phê duyệt của con người đối với hành động nhạy cảm.
- `COMPLETED`: Đã hoàn thành công việc và xuất artifact kèm bằng chứng.
- `FAILED`: Thất bại do lỗi kỹ thuật, cạn tài nguyên hoặc không thể vượt qua bài test.
- `CANCELLED`: Tác vụ bị dừng chủ động bởi Master hoặc người dùng.

### 2.11. Communication (Giao thức trao đổi)
Cơ chế phối hợp nội bộ:
- `direct_messaging`: Nhắn tin có cấu trúc giữa hai Agent cùng nhóm công tác để làm rõ dữ liệu kỹ thuật.
- `meeting_participation`: Tham gia phiên họp có chủ tọa với vai trò cụ thể (`presenter`, `reviewer`, `observer`).
- `structured_debate`: Tranh luận có cấu trúc khi có bất đồng kỹ thuật, bắt buộc phải nêu rõ đề xuất, phản biện và dữ liệu chứng minh.

### 2.12. Handoff (Quy chuẩn bàn giao)
Chuyển giao công việc cho Agent tiếp theo hoặc cho Master:
- `artifact_references`: Danh sách đường dẫn tệp tin sản phẩm được tạo trong `workspace/` hoặc `output/`.
- `evidence_records`: Bằng chứng thực tế gồm mã băm SHA-256 của file, log chạy lệnh, kết quả test.
- `decision_log`: Tóm tắt các quyết định kỹ thuật đã đưa ra và lý do lựa chọn.
- `residual_issues`: Các rủi ro còn tồn đọng hoặc phạm vi chưa được kiểm tra.

### 2.13. Quality (Kiểm chứng chất lượng)
Nguyên tắc thẩm định độc lập:
- Agent không có quyền tự đánh giá PASS cho sản phẩm của mình.
- Sản phẩm bàn giao phải trải qua bộ tiêu chí đánh giá định lượng do bộ phận QA hoặc bài kiểm thử độc lập thực thi.
- Mọi trạng thái hoàn thành thiếu bằng chứng đính kèm đều bị coi là không hợp lệ.

### 2.14. Error Handling (Xử lý sự cố và phục hồi)
Cơ chế chống treo hệ thống:
- `timeout_seconds`: Thời gian tối đa cho một lượt phản hồi hoặc một lần gọi công cụ.
- `retry_policy`: Số lần thử lại tối đa và thuật toán giãn cách thời gian khi gặp sự cố tạm thời.
- `rate_limit_policy`: Xử lý khi chạm trần tốc độ gọi mô hình, tự động hoãn hoặc chuyển tiếp tín hiệu cảnh báo.
- `quota_exceeded_action`: Hành vi khi cạn quota (`pause_and_notify`, `fallback_engine`).
- `crash_recovery`: Khôi phục phiên làm việc từ nhật ký sự kiện mà không làm mất trạng thái trước đó.

### 2.15. Dynamic Registration (Quản lý linh hoạt)
Quản trị danh mục nhân viên tại Agent Registry:
- Khả năng tạo mới một Agent thông qua manifest hợp lệ.
- Cập nhật năng lực, kỹ năng bổ sung hoặc cấu hình engine khi có chứng chỉ đào tạo.
- Vô hiệu hóa hoặc đình chỉ nhân viên khi phát hiện lỗi hành vi lặp lại.
- Điều chuyển nhân viên giữa các phòng ban hoặc bổ nhiệm làm việc dài hạn cho các dự án đặc biệt.

### 2.16. Security Boundaries (Ranh giới bảo mật)
Các điều cấm bất biến:
- Tuyệt đối không đọc, ghi hoặc truyền phát các tệp chứa khóa bí mật, tệp `.env`, khóa SSH.
- Không tự ý thực thi các lệnh vượt ngoài quyền hạn được khai báo trong manifest.
- Không tự ý sửa đổi mã nguồn tại `core/`, `engines/` hoặc các tệp cấu hình hệ thống khi chưa qua kiểm thử sandbox.

## 3. Bảng phân định Schema: Tĩnh và Động

### 3.1. Static Manifest Schema (Khai báo tĩnh)

Cấu hình này được lưu trữ cố định dưới dạng tệp `manifest.json` hoặc `manifest.yaml` trong thư mục của từng vai trò tại `agents/`.

| Trường dữ liệu | Kiểu | Bắt buộc | Ràng buộc giá trị và mô tả |
| :--- | :--- | :--- | :--- |
| `spec_version` | String | Có | Phiên bản chuẩn đặc tả, ví dụ: `"1.0.0"`. |
| `id` | String | Có | Khóa chính, định dạng `^[a-z0-9_]+\.[a-z0-9_]+$`. |
| `name` | String | Có | Độ dài từ 3 đến 64 ký tự. |
| `role` | String | Có | Chức danh chuyên môn, độ dài từ 3 đến 64 ký tự. |
| `department` | String | Có | Một trong 9 phòng ban chuẩn của hệ thống. |
| `description` | String | Có | Mô tả ngắn gọn phạm vi trách nhiệm. |
| `mission.purpose` | String | Có | Mục đích hoạt động. |
| `mission.responsibilities` | List[String] | Có | Danh sách ít nhất 1 trách nhiệm cụ thể. |
| `mission.non_goals` | List[String] | Không | Danh sách các việc không thuộc phạm vi. |
| `capabilities.skills` | List[String] | Có | Danh mục nhãn năng lực thực tế. |
| `capabilities.inputs` | List[String] | Có | Định dạng dữ liệu chấp nhận. |
| `capabilities.outputs` | List[String] | Có | Định dạng dữ liệu sinh ra. |
| `engine.default_type` | String | Có | Mặc định là `"antigravity"`. |
| `engine.model_tier` | String | Có | Một trong các nhóm: `"pro"`, `"flash"`, `"code"`. |
| `engine.parameters` | Object | Không | Cặp key-value tham số như `temperature`, `timeout`. |
| `memory.scopes` | List[String] | Có | Chứa ít nhất `"private"`, có thể có `"project"`, `"global"`. |
| `skills.allowed` | List[String] | Không | Danh sách mã kỹ năng từ thư mục `skills/approved/`. |
| `tools.allowed` | List[String] | Có | Danh mục công cụ được phép sử dụng. |
| `permissions.fs_read` | List[String] | Có | Danh sách phạm vi đường dẫn được đọc. |
| `permissions.fs_write` | List[String] | Có | Danh sách phạm vi đường dẫn được ghi. |
| `permissions.network` | String | Có | Một trong: `"none"`, `"intranet"`, `"full"`. |
| `permissions.requires_approval` | List[String] | Không | Các hành động đòi hỏi con người duyệt. |
| `limits.timeout_seconds` | Integer | Có | Giới hạn thời gian mỗi lượt chạy (ví dụ: 120). |
| `limits.max_retries` | Integer | Có | Số lần thử lại tối đa khi có sự cố (ví dụ: 3). |

### 3.2. Dynamic Runtime State Schema (Trạng thái động)

Dữ liệu này được sinh ra và cập nhật liên tục trong SQLite tại `.runtime/` và không đưa vào Git.

| Trường dữ liệu | Kiểu | Bắt buộc | Ràng buộc giá trị và mô tả |
| :--- | :--- | :--- | :--- |
| `session_id` | String | Có | Mã phiên làm việc hiện tại, UUID v4. |
| `agent_id` | String | Có | Tham chiếu đến static manifest `id`. |
| `current_task_id` | String | Không | Mã nhiệm vụ đang xử lý nếu có. |
| `status` | String | Có | Một trong các trạng thái của Task Lifecycle. |
| `current_step` | String | Không | Mô tả hành động đang diễn ra. |
| `assigned_team_id` | String | Không | Nhóm công tác hiện tại do Master thiết lập. |
| `context_tokens_used` | Integer | Có | Số lượng token hiện tại trong ngữ cảnh. |
| `total_tool_calls` | Integer | Có | Số lần công cụ đã được gọi trong phiên. |
| `last_active_at` | Timestamp | Có | Thời điểm hoạt động gần nhất (ISO 8601). |
| `error_count` | Integer | Có | Số lỗi liên tiếp gặp phải trong phiên. |
| `active_artifacts` | List[String] | Không | Danh sách tệp tin đang thao tác hoặc vừa tạo ra. |

## 4. Hồ sơ minh họa: IT Code Reviewer

Dưới đây là một ví dụ hồ sơ Agent mẫu thể hiện đúng chuẩn khai báo tĩnh của hệ thống. Hồ sơ này phục vụ việc làm rõ cấu trúc dữ liệu, không đại diện cho mã nguồn triển khai thực tế.

```yaml
spec_version: "1.0.0"
id: "it.code_reviewer"
name: "Quốc Bảo"
role: "Senior Code Quality Inspector"
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
    - "skills.code_review.standard_audit"
    - "skills.security.secret_check"

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
    - "tools.code.run_custom_script"

limits:
  timeout_seconds: 180
  max_retries: 2
```

## 5. Kết luận vận hành

Tiêu chuẩn đặc tả Agent này đảm bảo tính nhất quán giữa khâu thiết kế vai trò và khâu điều phối động của Master. Bất kỳ Agent mới nào được đăng ký vào hệ thống đều phải vượt qua bộ kiểm tra hợp lệ của schema này trước khi được phân công nhiệm vụ.
