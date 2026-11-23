# Tiêu chuẩn Kỹ thuật Hệ thống Quy tắc và Quản trị (Rule Specification)

Tài liệu này xác lập kiến trúc chuẩn mực, cấu trúc dữ liệu, cơ chế giải quyết xung đột và mô hình thực thi cho hệ thống quy tắc quản trị (Rule System) của AI Village. Mục tiêu của tiêu chuẩn là bảo đảm mọi chủ thể gồm Master Agent, Worker Agent, Skill và Tool luôn hoạt động trong ranh giới an toàn và thẩm quyền được cấp phép.

Đây là tài liệu đặc tả thiết kế cho thành phần Governance tại `core/governance/` trong các giai đoạn triển khai tiếp theo. Toàn bộ cơ chế thực thi runtime nêu trong tài liệu đang ở mức thiết kế, chưa được lập trình hay kích hoạt.

## 1. Kiến trúc hệ thống quy tắc (Rule System Architecture)

Rule System không phải một phòng ban chức năng và không phải một mô hình ngôn ngữ độc lập. Hệ thống này là tập hợp các hợp đồng chính sách có cấu trúc được đánh giá, cưỡng chế thực thi và ghi nhật ký bởi thành phần Governance trong Core Runtime.

### 1.1. Phân biệt tám lớp quy tắc

Hệ thống phân tách quy tắc quản trị thành 8 lớp có mức độ chi phối khác nhau:

1. **Architectural Invariants (Nguyên tắc bất biến):** Các ràng buộc kiến trúc nền tảng quy định tại `SYSTEM_BLUEPRINT.md`. Lớp này mang tính tuyệt đối, không thể bị vô hiệu hóa bởi bất kỳ Rule cấp dưới, cấu hình Agent hay phê duyệt tạm thời nào.
2. **System Policies (Chính sách toàn hệ thống):** Các quy định an ninh và vận hành chung cho toàn bộ AI Village như cấm truy cập tệp bí mật, cách ly sandbox, chặn vòng lặp vô hạn.
3. **Permission Rules (Quy tắc quyền truy cập tài nguyên):** Kiểm soát chi tiết thao tác đọc, ghi, xóa tệp tin, thực thi terminal, truy cập mạng, gọi AI Provider và truy xuất bộ nhớ.
4. **Agent Rules (Giới hạn hành vi từng Agent):** Quy định phạm vi hành vi cụ thể gắn với vai trò hoặc từng Agent, bao gồm giới hạn thẩm quyền đối với Master Agent và Worker Agent.
5. **Task Rules (Quy tắc theo nhiệm vụ):** Ràng buộc động phát sinh theo ngữ cảnh từng tác vụ như phạm vi đường dẫn `workspace/{task_id}/`, hạn mức ngân sách và thời gian tối đa.
6. **Skill Execution Rules (Điều kiện sử dụng Skills):** Quy định điều kiện xác thực trạng thái `APPROVED`, đối chiếu quyền công cụ trước khi nạp Skill và cô lập nội dung tham chiếu.
7. **Approval Rules (Quy tắc phê duyệt):** Xác định danh mục các hành động nhạy cảm bắt buộc phải tạm dừng thực thi để chờ quyết định phê duyệt trực tiếp từ người vận hành.
8. **Verification Rules (Quy tắc nghiệm thu và chứng thực):** Điều kiện bắt buộc để chuyển trạng thái hoàn thành, yêu cầu chứng thực độc lập từ QA kèm bằng chứng thực tế.

### 1.2. Phân định bốn khái niệm quản trị

- **Rule:** Một đơn vị quy tắc nguyên tử gồm điều kiện kích hoạt, phạm vi áp dụng và hiệu lực cấu hình (`ALLOW`, `DENY`, `REQUIRE_APPROVAL`, `AUDIT_ONLY`).
- **Permission:** Quyền truy cập cụ thể vào một tài nguyên hoặc công cụ xác định, được cấp phát cho một chủ thể trong một phạm vi giới hạn.
- **Policy:** Tập hợp nhiều Rule được nhóm theo miền quản trị (ví dụ: Security Policy, File Access Policy) để phục vụ đánh giá nhất quán.
- **Approval:** Quyết định cho phép có thời hạn và phạm vi đơn lẻ do người vận hành cấp qua Approval Gate cho một hành động nhạy cảm cụ thể.

## 2. Định danh và Cấu trúc dữ liệu Rule (Rule Identity & Schema)

Mỗi Rule được khai báo dưới dạng tệp cấu hình có cấu trúc (YAML hoặc JSON) với bảng đặc tả các trường như sau:

| Tên trường | Kiểu dữ liệu | Bắt buộc | Ràng buộc giá trị và mô tả |
| :--- | :--- | :--- | :--- |
| `spec_version` | String | Có | Phiên bản của chuẩn đặc tả Rule, ví dụ `"1.0.0"`. |
| `rule_id` | String | Có | Định danh duy nhất, định dạng `^[a-z0-9_]+(\.[a-z0-9_]+)+$` (ví dụ: `sec.deny_secrets_access`). |
| `version` | String | Có | Phiên bản Rule theo chuẩn SemVer (`MAJOR.MINOR.PATCH`). |
| `name` | String | Có | Tên hiển thị ngắn gọn, độ dài từ 3 đến 100 ký tự. |
| `description` | String | Có | Giải thích rõ mục đích quản trị và tác động của Rule. |
| `category` | String | Có | Một trong 8 giá trị: `invariant`, `system_policy`, `permission`, `agent_behavior`, `task_constraint`, `skill_execution`, `approval`, `verification`. |
| `scope` | Object | Có | Đối tượng xác định phạm vi gồm `targets` (như `global`, `master`, `worker`, `agent_id`, `task_id`) và `resources` (mẫu đường dẫn hoặc tài nguyên). |
| `priority` | Integer | Có | Độ ưu tiên từ `1` đến `1000`. Số lớn hơn có ưu tiên cao hơn khi xét trong cùng tầng hiệu lực. |
| `effect` | String | Có | Hiệu lực đánh giá: `ALLOW`, `DENY`, `REQUIRE_APPROVAL`, `AUDIT_ONLY`. |
| `conditions` | Object | Có | Biểu thức điều kiện có cấu trúc dựa trên thuộc tính của Agent, Task, Workspace, Tool hoặc Environment. |
| `actions` | List[String] | Có | Danh sách Action ID (`<domain>.<action>`) bị chi phối (ít nhất 1 phần tử, ví dụ: `files.read`, `files.write`, `terminal.exec`, `code.exec_custom`, `task.complete`). |
| `approval_required` | Boolean | Có | Cờ đánh dấu cần cổng phê duyệt. Nếu `effect` là `REQUIRE_APPROVAL`, trường này bắt buộc phải là `true`. |
| `status` | String | Có | Trạng thái vòng đời Rule: `DRAFT`, `UNDER_REVIEW`, `ACTIVE`, `DEPRECATED`, `DISABLED`. |
| `evidence_requirements` | List[String] | Tùy chọn | Danh sách bằng chứng bắt buộc phải ghi nhận khi Rule khớp. Bắt buộc có ít nhất 1 mục đối với nhóm `verification` và `approval`. |

## 3. Phân tầng thứ bậc và Giải quyết xung đột

### 3.1. Thứ tự ưu tiên phân tầng

Khi Policy Engine đánh giá một yêu cầu thực thi hành động, các lớp quy tắc được áp dụng theo thứ tự quyền lực từ cao xuống thấp:

1. **Tầng 1: Architectural Invariants** (Tuyệt đối bất khả xâm phạm).
2. **Tầng 2: System Security Policies** (Bảo vệ bí mật, cách ly ranh giới hệ thống).
3. **Tầng 3: Explicit DENY Rules** (Mọi quy tắc từ chối tường minh ở tầng chính sách hoặc nhiệm vụ).
4. **Tầng 4: Human Approval Rules** (Yêu cầu phê duyệt của con người đối với hành động nhạy cảm).
5. **Tầng 5: Task & Workspace Scope Rules** (Giới hạn theo không gian nhiệm vụ cụ thể).
6. **Tầng 6: Agent Manifest Permissions** (Quyền hạn khai báo trong hồ sơ Agent; luôn bị giới hạn bởi các tầng trên).
7. **Tầng 7: Skill Execution Policies** (Hướng dẫn kỹ năng; không thể mở rộng hay ghi đè Agent Manifest).

### 3.2. Nguyên tắc xử lý xung đột

- **Ưu tiên từ chối (`DENY` thắng `ALLOW`):** Nếu một hành động khớp đồng thời một Rule `ALLOW` và một Rule `DENY` trên cùng tài nguyên, quyết định cuối cùng luôn là `DENY`.
- **Phê duyệt trước khi cho phép (`REQUIRE_APPROVAL` thắng `ALLOW`):** Khi hành động được phép về mặt công cụ trong Agent Manifest nhưng chạm Rule `REQUIRE_APPROVAL`, hệ thống phải dừng lại ở trạng thái chờ phê duyệt.
- **Mặc định từ chối (Default-Deny / Fail-Closed):** Đối với các hành động có rủi ro thay đổi hệ thống hoặc truy xuất dữ liệu (ghi/xóa file, chạy lệnh terminal, truy cập mạng, gọi công cụ ngoài allowlist, cài đặt thư viện), nếu không có Rule `ALLOW` hợp lệ khớp hoàn toàn, hệ thống mặc định từ chối thực thi.
- **Giới hạn của phê duyệt:** Quyết định chấp thuận của người dùng tại Approval Gate chỉ giải phóng đúng hành động nhạy cảm được yêu cầu trong phạm vi cho phép, tuyệt đối không được dùng để vô hiệu hóa các Architectural Invariants (ví dụ: không thể duyệt cho Agent tự chấm PASS thay QA hay xuất khóa `.env` ra ngoài).

## 4. Quy tắc phân quyền cho Agent (Agent Permission Rules)

Hệ thống kiểm soát quyền hạn của Agent theo ngữ cảnh kết hợp giữa `agent_id`, `task_id` và `workspace_path` trên 9 nhóm hành vi trọng yếu:

1. **Thao tác tệp tin (Đọc, Ghi, Xóa):** Mọi đường dẫn đầu vào phải được chuẩn hóa tuyệt đối (canonical path resolution) để chặn tấn công thoát thư mục (`..`). Quyền đọc bám sát `permissions.fs_read`; quyền ghi và xóa chỉ hợp lệ trong `workspace/{task_id}/`, `output/{task_id}/` hoặc nhánh phát triển được cấp phép trong sandbox. Cấm ghi đè mã nguồn production hoặc thư mục hệ thống.
2. **Thực thi Terminal:** Chỉ cho phép chạy các lệnh nằm trong danh mục trắng (allowlist) bên trong sandbox cách ly. Chặn mọi lệnh phá hủy hệ thống, đọc biến môi trường chứa khóa bí mật hoặc quét mạng trái phép.
3. **Truy cập Internet:** Kiểm soát theo cấu hình `permissions.network` (`none`, `intranet`, `full`). Khi được phép truy cập, chỉ kết nối đến các tên miền hợp lệ theo chính sách nhiệm vụ và ghi lại URL cùng dấu thời gian.
4. **Sử dụng AI Provider, Connection và Credential:** Chỉ người vận hành có quyền thêm, xóa, bật/tắt Provider Connection hoặc quản trị thông tin xác thực; Agent và Master không được tự tạo hoặc sửa Connection. Agent Manifest và Policy chỉ được tham chiếu `connection_id` hoặc `credential_ref` phi bí mật; chỉ ranh giới backend secret resolver khi khởi tạo adapter mới được giải mã secret trong bộ nhớ, tuyệt đối không trả secret về Agent, Tool, log hay trình duyệt. Mọi định tuyến hoặc fallback sang Provider/Model khác phải nằm trong allowlist và ngân sách được phép; các thay đổi làm tăng ngưỡng chi phí đáng kể hoặc thay đổi vùng bảo mật dữ liệu (như chuyển từ `local_only` ra API đám mây ngoài) bắt buộc kích hoạt `REQUIRE_APPROVAL`. Khi thiếu số liệu quota từ Provider, hệ thống ghi `null` kèm `UNAVAILABLE_FROM_PROVIDER` và nghiêm cấm xoay vòng API key để né giới hạn sử dụng.
5. **Cài đặt thư viện (Dependencies):** Mặc định `DENY` trên môi trường máy chủ. Chỉ được phép cài đặt bên trong sandbox cô lập và kích hoạt `REQUIRE_APPROVAL` nếu cài gói mới từ nguồn bên ngoài.
6. **Thay đổi cấu hình hệ thống:** Agent thông thường bị `DENY` chỉnh sửa tệp cấu hình lõi, `AGENTS.md`, `core/`, `engines/` hoặc các tệp tiêu chuẩn. Mọi thay đổi Agent Registry hoặc Department Registry phải thông qua quy trình kiểm tra hợp lệ của HR và Master.
7. **Tiêu thụ tài nguyên hệ thống:** Giới hạn thời gian xử lý mỗi lượt (`timeout_seconds`), số lần thử lại tối đa (`max_retries`) và mức sử dụng CPU/RAM của sandbox theo ngưỡng tác vụ.
8. **Truy cập bộ nhớ Agent khác:** Agent chỉ được đọc và ghi `private` memory thuộc phiên làm việc của chính mình. Quyền truy xuất `project` memory bị giới hạn trong nhóm công tác (`assigned_team_id`) của nhiệm vụ hiện tại. Với `global` memory tại Vault, Agent chỉ có quyền đọc và không tự ý ghi đè khi chưa qua quy trình duyệt của Process và QA.
9. **Đăng ký hoặc thay đổi hồ sơ Agent:** Agent không thể tự sửa manifest của bản thân để mở rộng quyền hạn. Việc tạo mới, vô hiệu hóa hoặc điều chuyển Agent phải tuân thủ quy trình quản trị tại Agent Registry.

## 5. Quy tắc quản trị đối với Master Agent

Master nắm quyền điều hành nghiệp vụ nhưng vẫn chịu sự ràng buộc chặt chẽ của Governance:

- **Các hành động Master được phép thực hiện:** Lập kế hoạch tác vụ; lựa chọn và phân công Worker Agent dựa trên năng lực; thành lập và giải thể nhóm công tác động; triệu tập, chủ tọa và đóng các phiên họp (tuân thủ giới hạn số lượt tranh luận); đề xuất lựa chọn hoặc chuyển đổi Provider, Connection, Model trong phạm vi allowlist, chính sách bảo mật dữ liệu và trần ngân sách đã cấp phép; điều phối tiến độ và phân xử bất đồng kỹ thuật; đề xuất thay đổi quy trình hoặc xin người dùng phê duyệt các hành động nhạy cảm.
- **Các hành động bị nghiêm cấm đối với Master:** Vượt qua hoặc vô hiệu hóa Permission Manager; tự đánh dấu `PASS` hoặc chuyển nhiệm vụ sang `COMPLETED` thay cho QA; tự ý thêm/xóa/bật/tắt Provider Connection hoặc truy xuất API key thô; tự ý điều hướng sang kết nối ngoài allowlist hay vượt trần ngân sách mà chưa có `APPROVED` từ người vận hành; tự ý cấp quyền vượt quá giới hạn của System Policies; sửa đổi các Architectural Invariants; tự triển khai thay đổi trực tiếp vào môi trường production khi chưa có lệnh phê duyệt từ người dùng.

## 6. Chính sách đối với Tool và Skill

Để ngăn ngừa lỗ hổng leo thang quyền qua công cụ hoặc tài liệu hướng dẫn, hệ thống áp dụng các chính sách bắt buộc:

- **Kiểm tra trước mỗi lượt gọi công cụ:** Mọi Tool Call từ Agent phải đi qua bộ đánh giá quyền của Permission Manager trước khi Tool Runtime tiếp nhận thực thi.
- **Giới hạn theo Tool Registry:** Chỉ cho phép gọi các công cụ đã đăng ký hợp lệ tại `tools/` và được liệt kê rõ trong `tools.allowed` của Agent Manifest.
- **Điều kiện nạp Skill:** Chỉ cho phép nạp kỹ năng nằm trong `skills/approved/` có trạng thái `APPROVED` (đã qua kiểm chứng độc lập của QA) và toàn bộ `requirements.tools` của Skill phải nằm gọn trong `tools.allowed` của Agent.
- **Không tự nâng quyền:** Khai báo trong `SKILL.md` tuyệt đối không thể cấp thêm quyền công cụ hoặc mở rộng phạm vi đường dẫn cho Agent.
- **Kiểm soát script đính kèm:** Không tự động chạy bất kỳ script nào trong `scripts/` của một Skill chỉ vì tệp đó tồn tại. Script chỉ được thực thi bên trong sandbox sau khi đã qua rà soát an toàn của IT và QA.
- **Cô lập dữ liệu tham chiếu:** Toàn bộ tài liệu trong `references/` hoặc dữ liệu thu thập từ nguồn ngoài được phân loại là Untrusted Content, chỉ dùng để đọc tham khảo và không có thẩm quyền kích hoạt mệnh lệnh hệ thống.
- **Bảo vệ chính sách quản trị:** Nghiêm cấm Skill chứa chỉ thị sửa đổi Rule System hoặc hướng dẫn bỏ qua Approval Gate.

## 7. Cổng phê duyệt của con người (Human Approval Gate)

Mọi hành động nhạy cảm chạm ngưỡng kiểm soát đều phải đi qua quy trình 8 bước có thể kiểm chứng:

1. **Phát hiện ngưỡng nhạy cảm:** Policy Engine đánh giá hành động và trả về quyết định `APPROVAL_REQUIRED`.
2. **Khởi tạo yêu cầu phê duyệt:** Core Runtime tạo bản ghi `ApprovalRequest` mang mã định danh `approval_id` duy nhất, đính kèm thông tin tác vụ và thời hạn hiệu lực.
3. **Tạm dừng chính xác hành động:** Hệ thống chuyển nhánh thực thi của Agent yêu cầu sang trạng thái `WAITING_APPROVAL`, giữ nguyên trạng thái các nhánh công việc độc lập khác không liên quan.
4. **Trình bày tác động minh bạch:** Đưa yêu cầu lên Approval Center của giao diện 2D, hiển thị rõ chủ thể yêu cầu, loại hành động, tệp tin hoặc lệnh dự kiến, mức độ rủi ro và tác động hệ thống.
5. **Quyết định từ người dùng:** Người vận hành lựa chọn chấp thuận (`APPROVED`) hoặc từ chối (`REJECTED`) kèm ghi chú lý do.
6. **Xác minh hiệu lực quyết định:** Trước khi thực thi, Core Runtime kiểm tra chữ ký phiên người dùng hợp lệ, xác nhận `approval_id` chưa hết hạn (`expires_at`), chưa bị dùng trước đó và các tham số hành động khớp nguyên vẹn với bản ghi xin phép ban đầu.
7. **Thực thi hoặc hủy bỏ:** Nếu hợp lệ và được duyệt, Tool Runtime thực thi đúng một lần cho hành động đã duyệt. Nếu bị từ chối hoặc hết hạn, hành động bị hủy bỏ và trả kết quả từ chối có cấu trúc về Agent.
8. **Lưu vết kiểm toán:** Ghi nhận toàn bộ chuỗi sự kiện yêu cầu, quyết định và kết quả thực thi vào Audit Trail và Event Journal.

Master chỉ có thể đề xuất hoặc tóm tắt lý do xin phép; chỉ người dùng mới có quyền phê duyệt. Tuyệt đối cấm mọi cơ chế cho phép Agent tự sinh hoặc giả mạo phản hồi phê duyệt.

## 8. Mô hình thực thi tại Runtime (Runtime Enforcement Design)

Trong kiến trúc mục tiêu của `core/governance/`, Rule System được cưỡng chế thực thi bằng mã nguồn xác định (deterministic code), không dựa vào lời nhắc hay suy luận tự do của LLM:

### 8.1. Luồng kiểm soát thực thi

```text
Agent Intent -> Action/Tool Request -> Policy Evaluator -> Structured PolicyDecision
   ├── ALLOW -------------> Tool Runtime (Sandbox) -> Audit Event
   ├── APPROVAL_REQUIRED -> Approval Gate -> Wait User -> Verify -> Tool Runtime / Cancel
   └── DENY / ERROR ------> Block Action -> Security Audit Event -> Structured Error to Agent
```

### 8.2. Kết quả đánh giá có cấu trúc (`PolicyDecision`) và Ánh xạ 3 tầng

Mọi lượt kiểm tra quyền phải trả về một đối tượng `PolicyDecision` có cấu trúc chuẩn xác để Core Runtime xử lý:

- `decision`: `ALLOW`, `DENY`, `APPROVAL_REQUIRED`, hoặc `ERROR`.
- `matched_rule_ids`: Danh sách các `rule_id` đã khớp điều kiện.
- `rule_versions`: Phiên bản chính xác của các Rule được áp dụng tại thời điểm đánh giá.
- `reason_code`: Mã lý do chuẩn hóa (ví dụ: `PATH_OUTSIDE_WORKSPACE`, `MISSING_TOOL_PERMISSION`, `APPROVAL_EXPIRED`).
- `message`: Thông báo kỹ thuật rõ ràng phục vụ ghi nhật ký và phản hồi Agent.
- `audit_event_id`: Mã liên kết đến bản ghi Audit Event trong Event Journal.

Bảng ánh xạ nhất quán giữa `Rule.effect`, `PolicyDecision.decision` và `ToolResult.status`:

| Tầng 1: `Rule.effect` | Tầng 2: `PolicyDecision.decision` | Tầng 3: `ToolResult.status` (tham chiếu `TOOL_SPEC.md`) | Ý nghĩa và quy tắc chuyển tiếp |
| :--- | :--- | :--- | :--- |
| `ALLOW` | `ALLOW` | Cho phép chạy (`EXECUTION_SUCCEEDED` hoặc `EXECUTION_FAILED` tùy thực thi) | Chỉ cấp khi có `ALLOW` hợp lệ và không vướng `DENY` hoặc `REQUIRE_APPROVAL`. |
| `DENY` | `DENY` | `PERMISSION_DENIED` | Chặn tuyệt đối trước khi chạm Tool Runtime; `DENY` luôn thắng `ALLOW`. |
| `REQUIRE_APPROVAL` | `APPROVAL_REQUIRED` | `APPROVAL_REQUIRED` | Tạm dừng nhánh thực thi chờ Approval Gate (`WAITING_APPROVAL`). |
| `AUDIT_ONLY` | Không tự tạo `ALLOW` | Phụ thuộc Rule khác; thiếu `ALLOW` vẫn bị `DENY` | Chỉ phát Audit Event ghi nhận dấu vết; tuyệt đối không tự biến thành quyền `ALLOW`. |
| Lỗi đánh giá / thiếu ngữ cảnh | `ERROR` | `PERMISSION_DENIED` hoặc `EXECUTION_FAILED` (Fail-Closed) | Hệ thống từ chối thực thi hành động có rủi ro khi Policy Engine lỗi. |

Tuyệt đối không đánh đồng `PolicyDecision` với `ToolResult.status` hoặc `Task Status` của Agent. Một `PolicyDecision(ALLOW)` chỉ cho phép gọi công cụ; một `ToolResult(EXECUTION_SUCCEEDED)` không tự động làm Task chuyển sang `COMPLETED`.

### 8.3. Ghi nhận giới hạn hiện tại

Các cơ chế Policy Evaluator, Permission Manager và Audit Logger mô tả trong mục này là đặc tả kiến trúc dành cho các phase xây dựng Core Runtime sau này. Tài liệu này không tuyên bố các thành phần đó đã được lập trình hay đang vận hành trong mã nguồn hiện tại.

## 9. Vòng đời và Phiên bản hóa Rule (Rule Lifecycle & Versioning)

### 9.1. Năm trạng thái vòng đời

1. **`DRAFT`:** Bản thảo quy tắc đang được thiết kế, chưa áp dụng vào đánh giá thực tế.
2. **`UNDER_REVIEW`:** Rule đang được kiểm tra xung đột và chạy thử nghiệm trên các kịch bản kiểm thử trước khi phát hành.
3. **`ACTIVE`:** Rule đã được phê duyệt, đang có hiệu lực cưỡng chế trong Policy Engine.
4. **`DEPRECATED`:** Rule cũ đã có phiên bản thay thế tốt hơn, ngừng áp dụng cho các nhiệm vụ mới sau thời điểm chuyển đổi.
5. **`DISABLED`:** Rule bị tắt hoặc thu hồi khẩn cấp khi phát hiện sai sót logic gây ảnh hưởng vận hành.

### 9.2. Quy tắc quản lý thay đổi và khôi phục

- **Phiên bản bất biến:** Rule tuân thủ SemVer (`MAJOR.MINOR.PATCH`). Một phiên bản đã chuyển sang `ACTIVE` không được sửa đổi tại chỗ; mọi chỉnh sửa phải tạo phiên bản mới.
- **Bảo vệ tác vụ đang chạy:** Khi một tác vụ khởi tạo, Policy Engine gắn kết một ảnh chụp bộ phiên bản Rule (Rule Snapshot) cho phiên chạy đó để tránh tình trạng thay đổi quy tắc nghiệp vụ giữa chừng làm hỏng logic đang xử lý. Ngoại lệ duy nhất là các bản vá bảo mật khẩn cấp chuyển trạng thái `DISABLED` hoặc bổ sung `DENY` nghiêm trọng có kiểm soát.
- **Kiểm soát kích hoạt:** Các Rule thuộc nhóm bảo mật, phân quyền hoặc phê duyệt nhạy cảm không bao giờ được tự động kích hoạt sang `ACTIVE` chỉ vì Master hoặc một Worker Agent đề xuất; bắt buộc phải qua bước kiểm chứng độc lập và người dùng phê duyệt.
- **Lịch sử và Rollback:** Mọi thao tác kích hoạt, sửa đổi hoặc vô hiệu hóa Rule phải lưu lại nhật ký kiểm toán kèm thông tin người thực hiện, cho phép khôi phục nhanh về phiên bản `ACTIVE` trước đó khi cần.

## 10. Xử lý sự cố và Tình huống bảo mật (Security & Failure Handling)

Hệ thống áp dụng nguyên tắc an toàn mặc định (Fail-Closed) trước các tình huống bất thường:

| Tình huống sự cố | Hành vi mặc định của Runtime | Hành động ghi nhận và leo thang |
| :--- | :--- | :--- |
| Agent yêu cầu quyền vượt phạm vi manifest | Trả về `DENY`, chặn thao tác ngay tại ranh giới. | Ghi Audit Event `PERMISSION_ESCALATION_ATTEMPT`. |
| Tool không nằm trong Tool Registry hoặc allowlist | Trả về `DENY`, không khởi tạo Tool Runtime. | Ghi Audit Event `TOOL_NOT_ALLOWED`. |
| Skill chứa dấu hiệu prompt injection | Cô lập khối văn bản tham chiếu, chặn thực thi chỉ dẫn. | Đánh dấu cảnh báo Skill, báo QA và Master xem xét chuyển `DISABLED`. |
| Thiếu dữ liệu xác minh quyền (thiếu `task_id` hoặc `workspace`) | Áp dụng Fail-Closed, trả về `DENY`. | Chuyển nhánh tác vụ sang `BLOCKED` hoặc `FAILED`, ghi log lỗi ngữ cảnh. |
| Approval đã hết hiệu lực hoặc tham số thay đổi | Từ chối thực thi (`DENY`), hủy token phê duyệt cũ. | Yêu cầu tạo lại `ApprovalRequest` mới với tham số cập nhật. |
| Policy Engine gặp lỗi nạp Rule hoặc lỗi nội bộ | Áp dụng Fail-Closed, chặn mọi lệnh ghi/terminal/mạng/API. | Phát sự kiện `CRITICAL_GOVERNANCE_ERROR` tới Reception và Master. |
| Phát hiện Rule xung đột chưa tường minh | Áp dụng `DENY` thắng `ALLOW`, `REQUIRE_APPROVAL` thắng `ALLOW`. | Ghi log cảnh báo xung đột kèm các `rule_id` liên quan cho quản trị viên. |
| Agent cố ghi tệp ra ngoài workspace cho phép | Chặn ở tầng chuẩn hóa đường dẫn (`DENY`). | Ghi Audit Event `PATH_TRAVERSAL_OR_OUT_OF_SCOPE`. |
| Agent cố đọc tệp chứa secrets (`.env`, khóa SSH, token) | Chặn tuyệt đối ở System Security Policy (`DENY`). | Phát cảnh báo bảo mật mức cao ngay lập tức. |
| Cuộc họp hoặc retry lặp lại vượt ngưỡng cho phép | Cưỡng chế ngắt vòng lặp khi chạm trần `limits`. | Chuyển trạng thái sang `BLOCKED` hoặc giao Master phân xử kết thúc. |

## 11. Các kịch bản kiểm chứng độc lập (Verification Scenarios)

Phần này đặc tả 7 kịch bản kiểm thử dùng để nghiệm thu thành phần Policy Engine và Permission Manager khi triển khai code ở các phase sau (chưa viết mã test ở giai đoạn này):

1. **Kịch bản 1: Agent đọc tệp hợp lệ trong workspace**
   - *Điều kiện:* Agent có quyền `workspace/{task_id}/*` trong `fs_read`, đọc tệp `workspace/{task_id}/inputs/data.csv`.
   - *Expected Result:* `PolicyDecision.decision == ALLOW`.
   - *Evidence cần kiểm chứng:* Bản ghi Audit Event chứa `rule_id` khớp, đường dẫn đã chuẩn hóa hợp lệ, không phát sinh lỗi bảo mật.

2. **Kịch bản 2: Agent cố ghi tệp ra ngoài workspace**
   - *Điều kiện:* Agent gọi `tools.files.write` tới đường dẫn `core/orchestration/engine.py` hoặc `../.env`.
   - *Expected Result:* `PolicyDecision.decision == DENY` với `reason_code: PATH_OUTSIDE_WORKSPACE`.
   - *Evidence cần kiểm chứng:* Mã băm SHA-256 của tệp đích trước và sau khi gọi không thay đổi; Audit Event ghi nhận từ chối.

3. **Kịch bản 3: Agent gọi Tool không có trong danh mục cho phép**
   - *Điều kiện:* Agent chỉ có `tools.files.read` nhưng cố gọi `tools.code.run_script`.
   - *Expected Result:* `PolicyDecision.decision == DENY` với `reason_code: TOOL_NOT_ALLOWED`.
   - *Evidence cần kiểm chứng:* Tool Runtime không được gọi; bộ đếm thực thi của công cụ đó giữ nguyên ở mức 0; có Audit Event.

4. **Kịch bản 4: Agent yêu cầu hành động cần người dùng phê duyệt**
   - *Điều kiện:* Agent gọi công cụ `tools.code.run_custom_script` đòi hỏi Action ID `code.exec_custom` nằm trong `permissions.requires_approval`.
   - *Expected Result:* `PolicyDecision.decision == APPROVAL_REQUIRED`; trạng thái Agent chuyển sang `WAITING_APPROVAL`.
   - *Evidence cần kiểm chứng:* Đối tượng `ApprovalRequest` được tạo với `approval_id` hợp lệ; hành động chưa thực thi trước khi nhận phản hồi `APPROVED`.

5. **Kịch bản 5: Skill cố ghi đè chính sách hệ thống hoặc nâng quyền**
   - *Điều kiện:* Tệp `SKILL.md` giả lập chứa chỉ dẫn yêu cầu truy cập mạng khi Agent đang đặt `permissions.network: "none"`.
   - *Expected Result:* `PolicyDecision.decision == DENY` đối với hành động gọi mạng.
   - *Evidence cần kiểm chứng:* Permission Manager giữ nguyên giới hạn từ Agent Manifest và System Policy; ghi nhận cảnh báo vi phạm cấu trúc Skill.

6. **Kịch bản 6: Master cố tự nghiệm thu kết quả thay cho QA**
   - *Điều kiện:* Master phát lệnh chuyển trạng thái nhiệm vụ sang `COMPLETED` khi chưa có bản ghi xác nhận từ QA.
   - *Expected Result:* Verification Rule trả về `DENY` với `reason_code: MISSING_QA_VERIFICATION`.
   - *Evidence cần kiểm chứng:* Task State Machine giữ nguyên trạng thái trước đó, từ chối hoàn tất nhiệm vụ khi thiếu bằng chứng kiểm thử độc lập.

7. **Kịch bản 7: Hai Rule có xung đột quyền trên cùng một hành động**
   - *Điều kiện:* Một Rule cho phép `ALLOW` ghi trong `workspace/*`, nhưng một Rule bảo mật từ chối `DENY` ghi đối với tệp có đuôi `.key` hoặc `.env`.
   - *Expected Result:* `PolicyDecision.decision == DENY` theo nguyên tắc Deny-Overrides.
   - *Evidence cần kiểm chứng:* Đối tượng `PolicyDecision` liệt kê cả hai Rule khớp trong `matched_rule_ids` và giải thích rõ Rule `DENY` được ưu tiên áp dụng.

## 12. Hồ sơ minh họa mẫu (Example Rules)

Ba ví dụ dưới đây tuân thủ đúng bảng Rule Identity & Schema tại Mục 2 và đặt `status: "DRAFT"` để phản ánh đây chỉ là ví dụ thiết kế minh họa cấu trúc, chưa phải chính sách đã được triển khai hay kích hoạt trong Runtime.

### 12.1. Ví dụ 1: Cho phép đọc tệp hợp lệ trong workspace

```yaml
spec_version: "1.0.0"
rule_id: "fs.allow_workspace_read"
version: "1.0.0"
name: "Allow Agent Read Inside Task Workspace"
description: "Cho phép Agent đọc các tệp nằm trong phạm vi thư mục workspace của nhiệm vụ đang được phân công."
category: "permission"
scope:
  targets:
    - "worker"
    - "master"
  resources:
    - "workspace/{task_id}/**"
priority: 100
effect: "ALLOW"
conditions:
  all:
    - field: "task.status"
      operator: "equals"
      value: "RUNNING"
    - field: "resource.canonical_path_in_workspace"
      operator: "equals"
      value: true
actions:
  - "files.read"
approval_required: false
status: "DRAFT"
evidence_requirements:
  - "audit_event"
```

### 12.2. Ví dụ 2: Từ chối tuyệt đối truy cập bí mật hệ thống

```yaml
spec_version: "1.0.0"
rule_id: "sec.deny_secrets_access"
version: "1.0.0"
name: "Deny Access to Secrets and Credentials"
description: "Chặn tuyệt đối mọi hành vi đọc, ghi hoặc xóa các tệp cấu hình bí mật, khóa cá nhân và thông tin xác thực."
category: "system_policy"
scope:
  targets:
    - "global"
  resources:
    - "**/.env"
    - "**/.env.*"
    - "**/*.pem"
    - "**/*.key"
    - "**/credentials.json"
    - ".runtime/**"
priority: 990
effect: "DENY"
conditions:
  any:
    - field: "resource.matches_sensitive_pattern"
      operator: "equals"
      value: true
actions:
  - "files.read"
  - "files.write"
  - "files.delete"
  - "terminal.exec"
approval_required: false
status: "DRAFT"
evidence_requirements:
  - "audit_event"
  - "security_alert"
```

### 12.3. Ví dụ 3: Yêu cầu phê duyệt khi chạy script tùy biến

```yaml
spec_version: "1.0.0"
rule_id: "approval.require_custom_script"
version: "1.0.0"
name: "Require Human Approval for Custom Script Execution"
description: "Tạm dừng để người vận hành phê duyệt trước khi Agent thực thi một đoạn script tùy biến chưa qua đóng gói."
category: "approval"
scope:
  targets:
    - "worker"
  resources:
    - "tools.code.run_custom_script"
priority: 500
effect: "REQUIRE_APPROVAL"
conditions:
  all:
    - field: "tool.id"
      operator: "equals"
      value: "tools.code.run_custom_script"
    - field: "environment.mode"
      operator: "equals"
      value: "sandbox"
actions:
  - "code.exec_custom"
approval_required: true
status: "DRAFT"
evidence_requirements:
  - "approval_id"
  - "audit_event"
  - "execution_log"
```

## 13. Trạng thái đồng bộ liên tài liệu

Bốn điểm cần đồng bộ phát hiện trong quá trình xây dựng `RULE_SPEC.md` đã được kiểm toán và xử lý thống nhất tại bước nghiệm thu P01B.07 (xem chi tiết tại `docs/reviews/PHASE_01B_AUDIT.md`):

1. `skills.allowed` trong `AGENT_SPEC.md` đã được thống nhất dùng `skill_id` dạng `snake_case` (`^[a-z0-9_]+$`).
2. Các ví dụ thiết kế trong `SKILL_SPEC.md` đã chuyển về trạng thái `DRAFT`, tách biệt hoàn toàn với 6 điều kiện công nhận Skill `APPROVED` thực tế.
3. Trường `system_role` (`worker` / `orchestrator`) và phạm vi `department: "orchestration"` cho Master Agent (`id: "core.master"`) đã được đưa vào Static Manifest Schema của `AGENT_SPEC.md`.
4. Đường dẫn Department Registry trong `AGENT_SPEC.md` đã được thống nhất về `agents/departments/`.
