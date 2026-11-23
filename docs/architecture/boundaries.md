# AI Village: Ranh giới Hệ thống (System Boundaries)

Tài liệu này xác lập các ranh giới tin cậy, ranh giới dữ liệu, phân quyền thực thi, danh mục thao tác được bảo vệ và quy tắc xử lý lỗi của AI Village. Toàn bộ quy định dưới đây được tổng hợp thống nhất với [SYSTEM_BLUEPRINT.md](file:///C:/DoCho/AI-Village/docs/architecture/SYSTEM_BLUEPRINT.md), [RULE_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/RULE_SPEC.md), [TOOL_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/TOOL_SPEC.md) và [AGENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/AGENT_SPEC.md), không cấp thêm quyền hạn trái với đặc tả quản trị.

> **Lưu ý trạng thái triển khai:** Các ranh giới dưới đây là thiết kế kiến trúc bắt buộc cho toàn bộ hệ thống. Các thành phần thực thi tại Runtime như Permission Manager, Tool Runtime, Engine Adapter, Storage và API chưa được lập trình trong Chặng A.

## A. Ranh giới Tin cậy (Trust Boundaries)

Hệ thống phân tách 7 ranh giới tin cậy nhằm ngăn ngừa việc thành phần ở tầng ngoài hoặc nội dung không tin cậy tác động trực tiếp vào lõi điều phối và máy chủ:

| Ranh giới | Chiều dữ liệu | Kiểm soát bắt buộc tại điểm giao tiếp |
| :--- | :--- | :--- |
| **User -> API** | Yêu cầu tác vụ, tệp đính kèm, quyết định duyệt, cấu hình Connection | Xác thực định danh phiên cục bộ, kiểm tra kích thước gói tin, lọc ký tự điều khiển đường dẫn, không cho phép client tự gửi tham số cấp quyền hệ thống hoặc bỏ qua Approval Gate; sau khi lưu API key, API chỉ trả metadata và nhận diện đã che, không trả secret thô về trình duyệt. |
| **API -> Core Runtime** | DTO nhiệm vụ, tín hiệu hủy, phản hồi phê duyệt | `api/` chỉ định tuyến và xác thực schema đầu vào; mọi thay đổi trạng thái nhiệm vụ phải đi qua guard của Core State Machine, không cho phép API ghi thẳng vào cơ sở dữ liệu lõi. |
| **Core -> Engine Adapter** | Prompt, ngữ cảnh phiên, `connection_id`, `model_id`, danh mục công cụ cho phép | Đầu ra từ mô hình ngôn ngữ qua Engine Adapter luôn bị xem là dữ liệu chưa kiểm chứng; lời mô hình tự tuyên bố hoàn thành không được chuyển trạng thái Task sang `COMPLETED`. Việc giải mã `credential_ref` chỉ thực hiện tại ranh giới khởi tạo adapter ở backend. |
| **Core -> Tool Runtime** | Lời gọi công cụ (`tool_id`, tham số, ngữ cảnh run) | Mọi lời gọi công cụ bắt buộc phải đi qua Permission Manager để nhận `PolicyDecision` hợp lệ trước khi Tool Runtime khởi chạy. |
| **Tool Runtime -> Sandbox** | Lệnh thực thi code, script, tra cứu mạng | Thực thi trong không gian cách ly theo `task_id`, chuẩn hóa đường dẫn tuyệt đối (canonical path), chặn thoát thư mục (`..`), áp dụng giới hạn timeout, CPU/RAM và chính sách mạng. |
| **Runtime -> Storage** | Ghi trạng thái, artifact, event journal, bộ nhớ | Thực thi thông qua tầng `storage/` có transaction và kiểm tra ràng buộc khóa ngoại; toàn bộ stdout/stderr phải qua bộ lọc che giấu bí mật (`redaction`) trước khi lưu. |
| **Runtime -> UI Event Stream** | Sự kiện tiến độ, yêu cầu duyệt, kết quả artifact | Truyền phát chỉ đọc các sự kiện đã ghi trong Event Journal; tuyệt đối không lộ khóa API, biến môi trường bí mật, đường dẫn hệ thống nhạy cảm hay quyền thực thi nội bộ lên trình duyệt. |

## B. Ranh giới Dữ liệu (Data Boundaries)

Dữ liệu trong AI Village được phân vùng theo mục đích lưu trữ, tính bất biến và chính sách kiểm soát Git:

| Loại dữ liệu | Vị trí lưu trữ | Quyền đọc / ghi | Chính sách Git và bảo vệ |
| :--- | :--- | :--- | :--- |
| **Input của nhiệm vụ** | `workspace/{task_id}/inputs/` | Agent trong nhóm công tác đọc theo `fs_read`; người dùng hoặc hệ thống nạp đầu vào. | Bị `.gitignore` loại trừ khỏi Git; dữ liệu đính kèm bên ngoài được xử lý như nội dung không tin cậy. |
| **Workspace tạm thời** | `workspace/{task_id}/` | Agent được phân công đọc và ghi trong phạm vi tác vụ; cấm đọc ghi chéo giữa các `task_id`. | Bị `.gitignore` loại trừ hoàn toàn; có thể dọn dẹp sau khi bàn giao hoặc hủy nhiệm vụ. |
| **Output của nhiệm vụ & Artifacts hoàn chỉnh** | `output/{task_id}/` và `storage/artifacts/` | Agent ghi sản phẩm nháp trong workspace; chỉ Vault và luồng bàn giao hợp lệ đưa vào kho lưu trữ kèm `artifact_id`, phiên bản SemVer và SHA-256. | `output/*` bị `.gitignore` loại trừ để không đẩy sản phẩm riêng tư lên GitHub; chỉ giữ thư mục rỗng qua `.gitkeep`. |
| **Runtime State** | `.runtime/` (SQLite DB, session state, cache) | Chỉ tầng `storage/` và Core Runtime truy xuất có kiểm soát; UI không kết nối trực tiếp vào DB. | Thư mục `.runtime/` bị `.gitignore` loại trừ tuyệt đối khỏi Git. |
| **Audit Events & Logs** | `.runtime/` (Event Journal, execution logs) | Ghi nối tiếp (append-only) bởi Core Runtime và Governance; buộc lọc sạch token/mật khẩu trước khi ghi. | Bị `.gitignore` loại trừ khỏi Git; làm căn cứ phục hồi phiên và kiểm toán sau sự cố. |
| **Memory** | Quản lý qua `storage/` tại `.runtime/` | Phân tách 3 tầng: `private` (chỉ phiên Agent sở hữu), `project` (giới hạn trong `assigned_team_id`), `global` (chỉ đọc với Agent thường). | Không trộn lẫn vào cấu hình tĩnh tại `agents/`; bị loại trừ khỏi Git. |
| **Knowledge** | `storage/knowledge/` | Agent thường chỉ có quyền đọc tri thức chung; ghi hoặc cập nhật tri thức chuẩn hóa phải qua kiểm duyệt của Process, QA và Vault. | Tách biệt dữ liệu tri thức chung với dữ liệu tạm của từng nhiệm vụ. |
| **Secrets & Credentials** | Biến môi trường hệ thống, `.env` cục bộ hoặc kho lưu bí mật backend trong tương lai | Chỉ thành phần backend secret resolver được kiểm soát lúc khởi tạo adapter mới được giải mã `credential_ref`; cấm Agent, Master, Skill, Tool và trình duyệt (`localStorage`/URL/UI) đọc hoặc giữ API key thô. | `.env` và mọi tệp khóa bị `.gitignore` chặn tuyệt đối; chỉ `.env.example` chứa placeholder không nhạy cảm được theo dõi trong Git. |
| **Connection Metadata** | `storage/` quản lý metadata vào `.runtime/` | Chỉ người vận hành có quyền thêm, xóa, bật/tắt `connection_id`; UI chỉ đọc tên, `provider_id`, trạng thái và chuỗi nhận diện đã che. | Tách biệt `connection_id` khỏi `credential_ref`; không lưu API key thô trong `agents/` hoặc Git. |

## C. Ranh giới Phân quyền (Permission Boundaries)

Mô hình kiểm soát quyền tuân thủ cấu trúc phân tầng trong [RULE_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/RULE_SPEC.md), áp dụng nhất quán nguyên tắc `Deny-Overrides`, `Approval-Before-Allow` và `Default-Deny` (Fail-Closed):

1. **Quyền của Worker Agent:**
   - Chỉ thực thi các công cụ có mặt trong `tools.allowed` của Agent Manifest và đã ở trạng thái `AVAILABLE` trong Tool Registry.
   - Phạm vi tệp bị khóa vào `permissions.fs_read` và `permissions.fs_write` gắn với `workspace/{task_id}/` hoặc nhánh phát triển được cấp phép trong sandbox.
   - Không được tự sửa manifest của chính mình để mở rộng quyền, không được đọc tệp bí mật, không được ghi vào `core/`, `engines/` hay nhánh chính production.
2. **Quyền của Master Agent (`core.master`):**
   - Được lập kế hoạch, lựa chọn nhóm công tác theo năng lực, mở hoặc đóng phiên họp (tối đa theo giới hạn lượt quy định), phân xử bất đồng và gửi yêu cầu xin phê duyệt tới người vận hành.
   - Bị cấm vượt qua Permission Manager, bị cấm tự gán trạng thái `COMPLETED` thay cho bước xác minh độc lập của QA, bị cấm sửa đổi Architectural Invariants và bị cấm tự triển khai vào production.
3. **Permission Manager (`core/governance/`):**
   - Là thành phần kiểm soát quyền bằng mã nguồn xác định (deterministic code), không dựa vào suy luận tự do của LLM.
   - Đánh giá mọi hành động qua 7 tầng ưu tiên từ Architectural Invariants xuống Skill Execution Policies và trả về đối tượng `PolicyDecision` có cấu trúc (`ALLOW`, `DENY`, `APPROVAL_REQUIRED`, `ERROR`).
4. **Tool Allowlist:**
   - Danh mục `default_tools` ở cấp phòng ban chỉ mang tính gợi ý chuyên môn, không tự cấp quyền cho Agent.
   - Mọi công cụ gọi thực tế phải vừa nằm trong Tool Registry hợp lệ, vừa nằm trong `tools.allowed` của Agent Manifest và vượt qua kiểm tra Action ID tương ứng.
5. **Skill Allowlist:**
   - Chỉ các kỹ năng nằm tại `skills/approved/`, thỏa mãn đủ 6 điều kiện kiểm định QA và có toàn bộ `requirements.tools` nằm gọn trong `tools.allowed` của Agent mới được nạp.
   - Kỹ năng không bao giờ được cấp thêm quyền hay đường dẫn mới cho Agent; tài liệu trong `references/` bị đối xử như nội dung không tin cậy.
6. **Human Approval Gate:**
   - Quy trình 8 bước bắt buộc cho mọi hành động chạm ngưỡng `REQUIRE_APPROVAL`.
   - Agent tạm dừng nhánh thực thi tại `WAITING_APPROVAL`; chỉ người vận hành được cấp quyền duyệt (`APPROVED`) hoặc từ chối (`REJECTED`) kèm kiểm tra `approval_id` còn hạn, chưa tái sử dụng và khớp nguyên vẹn tham số.
   - Phê duyệt của con người không được phép vô hiệu hóa các Architectural Invariants (ví dụ: không thể duyệt để xuất `.env` hoặc bỏ qua kiểm chứng QA).

## D. Danh mục Thao tác Được bảo vệ (Protected Operations)

Các hành động sau đây bị mặc định chặn hoặc bắt buộc phải đi qua Human Approval Gate với đầy đủ bản ghi tác động trước khi thực thi:

1. **Xóa dữ liệu quan trọng:** Xóa hàng loạt tệp, xóa dữ liệu ngoài thư mục tạm của tác vụ hiện hành hoặc thanh lọc vĩnh viễn artifact trong Vault.
2. **Cài đặt package ở phạm vi hệ thống:** Cài đặt thư viện Python/Node/hệ điều hành lên máy chủ host ngoài sandbox cô lập (mặc định `DENY` trên host; trong sandbox phải có kiểm soát và xin duyệt nếu nạp gói mới từ bên ngoài).
3. **Thay đổi cấu hình bảo mật:** Chỉnh sửa rule trong `core/governance/`, thay đổi chính sách mạng, sửa quyền Agent Manifest hoặc nới lỏng giới hạn sandbox.
4. **Ghi vào vùng ngoài workspace được cấp phép:** Mọi thao tác ghi hoặc sửa mã nguồn lõi (`core/`, `engines/`, `tools/`, `storage/`, `AGENTS.md`), ghi chéo sang workspace tác vụ khác hoặc ghi đè nhánh chính.
5. **Gọi API hoặc chuyển đổi Provider phát sinh chi phí / đổi vùng dữ liệu:** Sử dụng AI Provider trả phí vượt ngưỡng cảnh báo ngân sách, chạy tác vụ hàng loạt quy mô lớn, hoặc fallback sang kết nối ngoài allowlist, đắt hơn ngân sách hay đưa dữ liệu từ `local_only` ra API đám mây bên ngoài.
6. **Triển khai lên production:** Đưa mã nguồn, kỹ năng mới (`skills/approved/`) hoặc cấu hình thay đổi ra môi trường vận hành chính thức.
7. **Truy cập hoặc thay đổi tài nguyên nhạy cảm:** Đọc biến môi trường chứa khóa, tệp `.env`, chứng chỉ số, khóa cá nhân, cơ sở dữ liệu runtime thô hoặc dữ liệu cá nhân ngoài phạm vi bài toán.

## E. Ranh giới Xử lý Lỗi (Failure Boundaries)

Khi gặp sự cố tại bất kỳ ranh giới nào, hệ thống áp dụng nguyên tắc **Fail-Closed** và báo cáo trung thực thay vì ngụy tạo thành công hoặc thử lại mù quáng:

| Tình huống lỗi | Hành vi bắt buộc của Runtime |
| :--- | :--- |
| **Provider mất kết nối** | Thử lại có giới hạn theo cấu hình `max_retries` với backoff hợp lệ nếu thao tác an toàn để thử lại; vượt ngưỡng thì chuyển nhánh tác vụ sang `BLOCKED`, lưu dấu vết lỗi và báo Reception/Master. |
| **Permission Engine gặp lỗi** | Áp dụng Fail-Closed tuyệt đối: trả `PolicyDecision(ERROR)`, chặn toàn bộ thao tác ghi, xóa, terminal, mạng và cài gói; phát sự kiện `CRITICAL_GOVERNANCE_ERROR` tới Reception và Master. |
| **Tool trả kết quả không hợp lệ** | Nếu dữ liệu không khớp `output_schema`, gán trạng thái `EXECUTION_FAILED` (`OUTPUT_INVALID`), lưu nhật ký thô đã lọc bí mật để kiểm tra và chặn truyền dữ liệu hỏng sang bước kế tiếp. |
| **Runtime không biết trạng thái thực thi** | Cấm suy đoán thành công hoặc giữ nguyên trạng thái đang chạy giả tạo; với thao tác không idempotent (`safe_retry: false`) bị đứt kết nối giữa chừng, chặn tự động retry (`PARTIAL_EXECUTION`), giữ nguyên hiện trường và yêu cầu Master hoặc con người kiểm tra. |
| **Tác vụ vượt timeout** | Cưỡng chế chấm dứt toàn bộ cây tiến trình con đúng ngưỡng `timeout_seconds`, trả `ToolResult.status = TIMED_OUT`, dọn tài nguyên và không retry nếu công cụ không hỗ trợ chạy lại an toàn. |
| **Quota không còn** | Ngừng gọi Provider ngay lập tức (`QUOTA_EXCEEDED`), không thử lại vô tận, ghi nhận số liệu thực tế (hoặc `null` kèm `UNAVAILABLE_FROM_PROVIDER` nếu thiếu số đo) và báo Resources cùng Master tạm dừng hoặc phân bổ lại; không xoay vòng API key để né giới hạn. |
| **Provider không hỗ trợ công cụ bắt buộc** | Chỉ chuyển sang kết nối/model dự phòng khi nằm trong chính sách và ngân sách cho phép; nếu không có phương án hợp lệ thì trả `EXECUTION_FAILED` (`TOOL_UNSUPPORTED_BY_PROVIDER`), chuyển nhánh tác vụ sang `BLOCKED` và leo thang Master/người dùng, không giả lập thực thi. |
| **Người dùng từ chối phê duyệt** | Hủy bỏ hành động nhạy cảm ngay lập tức (`APPROVAL_REJECTED`), không thực thi công cụ, ghi quyết định vào Audit Trail và trả kết quả từ chối có cấu trúc về Agent để tìm phương án thay thế hoặc dừng nhánh việc. |
