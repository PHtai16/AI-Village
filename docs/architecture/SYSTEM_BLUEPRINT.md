# AI Village: Bản Thiết kế Kiến trúc Toàn diện (System Blueprint)

Tài liệu này xác lập nền tảng kiến trúc chuẩn mực và đóng vai trò Single Source of Truth cho toàn bộ hệ thống Multi-Agent AI Village. Mọi quyết định thiết kế, triển khai mã nguồn và đánh giá nghiệm thu xuyên suốt các phase đều phải căn cứ vào tài liệu này.

## 1. Tầm nhìn sản phẩm (Product Vision)

AI Village là một hệ thống Multi-Agent đa chức năng vận hành trong một môi trường làm việc trực quan:

- **Bản chất hệ thống:** AI Village là một tổ chức lao động số chuyên giải quyết các bài toán phức tạp thông qua sự phối hợp giữa nhiều Agent có vai trò rõ ràng. Hệ thống không phải là một phần mềm tự động hóa marketing đơn lẻ, không phải là một công ty kinh doanh ngoài đời thực và không phải là một giao diện trò chơi mô phỏng các nhân vật giả mạo.
- **Tổ chức theo năng lực:** Các phòng ban trong hệ thống đại diện cho các miền năng lực chuyên môn, không phải là các bước cố định trên một dây chuyền làm việc bắt buộc. Master điều phối nhân sự linh hoạt tùy theo đặc thù của từng bài toán.
- **Chứng thực bằng dữ liệu thực tế:** Hệ thống vận hành theo nguyên tắc Evidence First. Mọi trạng thái tiến độ, kết quả công việc và số liệu tài nguyên phải xuất phát từ các sự kiện hệ thống thực tế thu thập qua công cụ và môi trường runtime, không chấp nhận việc mô hình ngôn ngữ tự phát ngôn khẳng định hoàn thành khi chưa có bằng chứng kiểm chứng.

## 2. Kiến trúc hệ thống và phân định ranh giới

Mã nguồn và dữ liệu của AI Village được phân định thành các tầng trách nhiệm độc lập:

| Thư mục | Trách nhiệm chính | Sở hữu Logic | Sở hữu Trạng thái | Dữ liệu quản lý |
| :--- | :--- | :--- | :--- | :--- |
| `agents/` | Khai báo manifest cho Agent, Department và Master. | Không | Không | Cấu hình tĩnh, vai trò, năng lực và quyền hạn. |
| `core/` | Điều phối nhiệm vụ, máy trạng thái, phiên làm việc, phân xử và chính sách an ninh. | Có (Toàn quyền điều phối) | Có (State Machine & Session) | Task contracts, event schemas, permission policies. |
| `engines/` | Lớp adapter trung gian kết nối Antigravity, các AI Model API và các nền tảng thực thi khác. | Có (Adapter logic) | Không | Provider/Engine Registry metadata, capability metadata, request/response mapping, telemetry parsing. |
| `tools/` | Thư viện công cụ thực thi độc lập (file, code, browser, research, office). | Có (Thực thi công cụ) | Không | Kết quả trả về từ công cụ hệ thống (`ToolResult`). |
| `skills/` | Quy trình nghiệp vụ tái sử dụng đã được kiểm chứng độc lập. | Có (Workflow logic) | Không | Hướng dẫn thực thi, schema đầu vào và đầu ra. |
| `storage/` | Truy xuất cơ sở dữ liệu bền vững, quản lý artifacts, bộ nhớ dài hạn, tri thức và metadata kết nối/tham chiếu credential. | Có (Persistence logic) | Không | Database migrations, schemas, repositories, connection metadata & secret reference handles (không giữ secret thô trong Git). |
| `api/` | Điểm tiếp nhận HTTP và truyền phát sự kiện thời gian thực ra giao diện. | Không (Chỉ định tuyến) | Không | DTOs, endpoint contracts, event streams (chỉ trả metadata kết nối đã che secret). |
| `ui/` | Giao diện người dùng 2D và không gian hiển thị 3D, bao gồm bảng quản trị kết nối trong tương lai. | Không (Chỉ giao diện) | Có (UI state cục bộ) | Cấu hình hiển thị, components, visual assets (tuyệt đối không lưu hoặc giữ API key thô). |
| `sandbox/` | Môi trường cách ly an toàn cho việc chạy mã nguồn và công cụ. | Không | Không | Chính sách cách ly, Docker fixtures. |
| `workspace/` | Không gian làm việc tạm thời và dữ liệu đầu vào theo từng nhiệm vụ. | Không | Có (Tạm thời theo task) | File trung gian, mã nguồn thử nghiệm (gitignore). |
| `output/` | Sản phẩm hoàn chỉnh được bàn giao cho người dùng theo từng nhiệm vụ. | Không | Không | Artifacts hoàn thiện, biên bản nghiệm thu (gitignore). |
| `.runtime/` | Cơ sở dữ liệu SQLite, file nhật ký, cache và phiên làm việc thực tế. | Không | Có (Persistent runtime) | Database files, event journal, log traces (gitignore). |

## 3. Chủ thể điều hành trung tâm: Master Agent

Master là thực thể điều phối cao nhất trong hệ sinh thái AI Village:

- **Trách nhiệm của Master:** Tiếp nhận mục tiêu từ người dùng, phân tích và phản biện yêu cầu để làm rõ phạm vi, lựa chọn các Agent phù hợp theo nhãn năng lực, triệu tập và chủ trì các phiên họp khi cần, điều phối tiến độ, phân xử bất đồng kỹ thuật, tổng hợp sản phẩm cuối cùng và chịu trách nhiệm bàn giao cho người dùng khi đã có đầy đủ bằng chứng kiểm thử độc lập.
- **Vị trí kiến trúc:** Master là một thực thể độc lập, không thuộc về bất kỳ phòng ban chuyên môn nào trong 9 phòng ban chức năng và tuyệt đối không tự chấm đạt thay cho bộ phận QA.
- **Biểu diễn trong Agent Schema:** Trong cấu hình manifest tại `agents/master/agent.yaml`, Master được định danh với `id: "core.master"`, trường `department: "orchestration"` (phạm vi điều hành đặc biệt, không xem là phòng ban chuyên môn thứ 10) và thuộc tính `system_role: "orchestrator"`. Cách định danh này giúp phân biệt rõ Master với các Worker Agent thuộc các phòng ban chuyên môn (`system_role: "worker"`) mà không phá vỡ schema và không tự động cấp quyền bỏ qua Permission Manager.

## 4. Chín phòng ban chức năng mặc định

Chín phòng ban đại diện cho 9 miền năng lực chuyên biệt của hệ thống:

### 4.1. HR: Nhân sự & Đào tạo
- **Mục đích:** Quản lý vòng đời, năng lực và danh mục nhân sự AI trong hệ thống.
- **Nhiệm vụ cụ thể:** Quản lý hồ sơ nhân viên trong Agent Registry, đánh giá mức độ phù hợp của vai trò, tiếp nhận đề xuất tuyển dụng nhân viên mới, theo dõi lịch sử điều chuyển phòng ban.
- **Đầu vào:** Đề xuất vai trò mới từ Master hoặc Process, đánh giá hiệu suất từ QA.
- **Đầu ra:** Manifest nhân viên mới hoặc cập nhật, danh mục Agent hợp lệ.
- **Quyền hạn:** Đọc và ghi trong `agents/roles/`, `agents/registry/`.
- **Phối hợp thường xuyên:** Master, Process, QA.
- **Không thuộc trách nhiệm:** Không tự điều phối dự án, không trực tiếp thực hiện công việc kỹ thuật của nhiệm vụ.
- **Ví dụ thực tế:** Cấu hình và nạp manifest cho vai trò `it.code_reviewer` vào hệ thống khi dự án cần bổ sung chuyên gia kiểm tra mã nguồn.

### 4.2. Resources: Tài nguyên & Tối ưu
- **Mục đích:** Giám sát, kiểm soát và tối ưu hóa chi phí tài nguyên phần cứng, ngân sách và hạn mức quota AI đa nhà cung cấp.
- **Nhiệm vụ cụ thể:** Thu thập số liệu sử dụng CPU, RAM, thời gian chạy từ hệ thống và token/chi phí thực tế từ Engine Adapter; theo dõi ngân sách và trạng thái sử dụng theo từng Provider, Project/Billing Scope, Connection và Model; cảnh báo khi tài nguyên tiệm cận ngưỡng giới hạn; lập báo cáo kiểm kê tài nguyên sau mỗi lần chạy.
- **Đầu vào:** Dữ liệu đo lường runtime từ Engine Adapter và hệ điều hành.
- **Đầu ra:** Báo cáo kiểm kê tài nguyên, cảnh báo giới hạn quota/ngân sách, khuyến nghị phân bổ.
- **Quyền hạn:** Đọc telemetry hệ thống, phát tín hiệu yêu cầu tạm dừng tác vụ khi vượt trần chi phí; không trực tiếp giải mã API key thô.
- **Phối hợp thường xuyên:** Master, Reception, IT.
- **Không thuộc trách nhiệm:** Không bịa đặt số liệu token hoặc tự suy đoán hai API key có hai quota độc lập khi provider không trả về dữ liệu xác minh; không tự ý hủy tác vụ khi chưa báo cáo Master.
- **Ví dụ thực tế:** Ghi nhận phiên làm việc dùng kết nối `gemini_primary` tiêu tốn 12.500 prompt tokens và 45 giây CPU với trạng thái `VERIFIED_MEASURED`, hoặc đặt `null` kèm `UNAVAILABLE_FROM_PROVIDER` nếu provider không cung cấp thông tin quota còn lại.

### 4.3. Service: Dịch vụ & Trải nghiệm
- **Mục đích:** Đảm bảo sản phẩm đầu ra đáp ứng đúng kỳ vọng và trải nghiệm của người dùng.
- **Nhiệm vụ cụ thể:** Phân tích brief yêu cầu từ góc nhìn người dùng cuối, đánh giá định tính sản phẩm hoàn thiện, xử lý ý kiến phản hồi của người dùng sau khi bàn giao.
- **Đầu vào:** Brief của người dùng, sản phẩm hoàn thiện từ Master.
- **Đầu ra:** Bản đánh giá mức độ phù hợp nghiệp vụ, danh sách điểm cần hoàn thiện giao diện hoặc nội dung.
- **Quyền hạn:** Đọc brief và sản phẩm bàn giao, đề xuất chỉnh sửa về trải nghiệm.
- **Phối hợp thường xuyên:** Master, Marketing, QA.
- **Không thuộc trách nhiệm:** Không thay thế kiểm thử kỹ thuật của QA, không tự ý sửa đổi mã nguồn.
- **Ví dụ thực tế:** Đánh giá một báo cáo phân tích tài chính đã đủ rõ ràng cho ban giám đốc đọc hay chưa và yêu cầu bổ sung bảng tóm tắt chỉ số.

### 4.4. Marketing: Marketing & Sáng tạo
- **Mục đích:** Nghiên cứu thị trường, xây dựng chiến lược truyền thông và sáng tạo nội dung khi có yêu cầu.
- **Nhiệm vụ cụ thể:** Thu thập thông tin đối thủ, phân tích xu hướng thị trường, soạn thảo nội dung truyền thông, thiết kế ý tưởng hình ảnh và thông điệp.
- **Đầu vào:** Đề bài nghiên cứu thị trường hoặc yêu cầu nội dung từ người dùng.
- **Đầu ra:** Báo cáo nghiên cứu thị trường, bài viết, kịch bản, bản thảo thông điệp.
- **Quyền hạn:** Sử dụng các công cụ nghiên cứu web và công cụ tạo nội dung trong phạm vi workspace.
- **Phối hợp thường xuyên:** Master, Service, Vault.
- **Không thuộc trách nhiệm:** Không áp đặt quy trình marketing cho các nhiệm vụ thuần túy về kỹ thuật hoặc văn phòng.
- **Ví dụ thực tế:** Soạn thảo bản thông cáo báo chí và kế hoạch truyền thông cho sản phẩm phần mềm vừa hoàn thành.

### 4.5. IT: Công nghệ thông tin & R&D
- **Mục đích:** Phát triển phần mềm, tích hợp kỹ thuật, tự động hóa và phát triển công cụ mới.
- **Nhiệm vụ cụ thể:** Viết mã nguồn, sửa lỗi, chạy kiểm thử trong sandbox cách ly, tích hợp API, xây dựng công cụ nội bộ mới và duy trì hạ tầng kỹ thuật.
- **Đầu vào:** Yêu cầu kỹ thuật, lỗi phát sinh, thiết kế tính năng mới.
- **Đầu ra:** Mã nguồn, bản vá lỗi, công cụ thực thi mới, kịch bản tự động hóa.
- **Quyền hạn:** Đọc và ghi trong `workspace/{task_id}/` hoặc nhánh dev trong sandbox; chạy linter và test runner.
- **Phối hợp thường xuyên:** Master, QA, Process, Resources.
- **Không thuộc trách nhiệm:** Tuyệt đối không tự ý chỉnh sửa mã nguồn production hoặc nhánh chính khi chưa có phê duyệt.
- **Ví dụ thực tế:** Xây dựng một module phân tích cú pháp AST cho ngôn ngữ Python và chạy kiểm thử tự động trong môi trường sandbox.

### 4.6. Reception: Phản ứng nhanh / Lễ tân
- **Mục đích:** Tiếp nhận, sàng lọc và xử lý các sự kiện phát sinh hoặc yêu cầu mới khi hệ thống đang vận hành.
- **Nhiệm vụ cụ thể:** Lắng nghe tín hiệu ngắt từ người dùng hoặc hệ thống, phân loại mức độ khẩn cấp, xác định phạm vi ảnh hưởng, định tuyến yêu cầu tới các bộ phận liên quan và cảnh báo Master khi có tình huống khẩn cấp.
- **Đầu vào:** Tin nhắn mới từ người dùng, tín hiệu ngắt hệ thống, thông báo lỗi nghiêm trọng từ runtime.
- **Đầu ra:** Phiếu yêu cầu đã phân loại, thông báo điều phối khẩn cấp gửi tới Master.
- **Quyền hạn:** Tiếp nhận thông điệp, tạm dừng hàng đợi tác vụ khi có sự cố khẩn cấp.
- **Phối hợp thường xuyên:** Master, Resources, QA.
- **Không thuộc trách nhiệm:** Không tự ý giải quyết các yêu cầu kỹ thuật phức tạp thay cho các phòng ban chuyên môn.
- **Ví dụ thực tế:** Tiếp nhận yêu cầu dừng khẩn cấp một tác vụ đang chạy vì người dùng cung cấp sai tệp dữ liệu đầu vào và báo cáo Master điều phối xử lý.

### 4.7. Process: Quy trình & Kỹ năng
- **Mục đích:** Tự cải tiến hệ thống thông qua việc đúc kết kinh nghiệm sau mỗi nhiệm vụ.
- **Nhiệm vụ cụ thể:** Phân tích nhật ký thực thi sau khi hoàn thành công việc, phát hiện các thao tác lặp lại hoặc lỗi xử lý phổ biến, đề xuất quy trình chuẩn hóa và xây dựng các Skill mới tại `skills/candidates/`.
- **Đầu vào:** Execution log, nhật ký lỗi, báo cáo nghiệm thu từ QA.
- **Đầu ra:** Đề xuất cải tiến quy trình, bản thảo kỹ năng mới.
- **Quyền hạn:** Đọc lịch sử tác vụ và nhật ký thực thi tại `.runtime/` và `storage/`.
- **Phối hợp thường xuyên:** Master, IT, QA, HR.
- **Không thuộc trách nhiệm:** Không tự ý kích hoạt kỹ năng mới vào hệ thống mà không qua kiểm thử độc lập và phê duyệt.
- **Ví dụ thực tế:** Phát hiện thao tác trích xuất dữ liệu từ tệp PDF dạng bảng bị lặp lại nhiều lần với nhiều lỗi định dạng, từ đó đề xuất xây dựng một kỹ năng chuyên biệt về xử lý bảng biểu.

### 4.8. QA: Vận hành & Kiểm chứng
- **Mục đích:** Giám sát chất lượng độc lập và bảo đảm tính xác thực của mọi sản phẩm bàn giao.
- **Nhiệm vụ cụ thể:** Quan sát tiến trình vận hành của các Agent, chạy các bộ kiểm thử tự động độc lập, xác minh mã băm tệp tin và tính đúng đắn của dữ liệu, từ chối cấp chứng nhận hoàn thành nếu thiếu dữ liệu chứng minh.
- **Đầu vào:** Sản phẩm trung gian hoặc cuối cùng từ các Agent, mã nguồn, tài liệu, dữ liệu xuất xưởng.
- **Đầu ra:** Biên bản nghiệm thu kiểm thử độc lập, danh sách lỗi phát hiện, xác nhận PASS hoặc FAIL.
- **Quyền hạn:** Toàn quyền chạy test runner, thẩm định tính hợp lệ của artifacts và từ chối nghiệm thu.
- **Phối hợp thường xuyên:** Master, IT, Service, Process.
- **Không thuộc trách nhiệm:** Không tham gia trực tiếp vào việc sản xuất ra sản phẩm mà chỉ giữ vai trò thẩm định độc lập.
- **Ví dụ thực tế:** Chạy toàn bộ 25 ca kiểm thử đơn vị cho một module mới do IT lập trình và xác nhận tất cả đều vượt qua trước khi Master bàn giao.

### 4.9. Vault: Kho & Tri thức
- **Mục đích:** Lưu trữ lâu dài, quản lý phiên bản và bảo đảm khả năng truy xuất bền vững cho tri thức và tài sản số của hệ thống.
- **Nhiệm vụ cụ thể:** Lưu trữ an toàn các artifacts đã nghiệm thu, quản lý phiên bản theo chuẩn SemVer, đánh chỉ mục tìm kiếm dữ liệu, bảo tồn kho tri thức chung tại `storage/knowledge/` và thực hiện sao lưu dự phòng.
- **Đầu vào:** Artifacts đã qua kiểm chứng từ QA, kỹ năng đã phê duyệt, tài liệu kiến trúc, log kiểm thử.
- **Đầu ra:** Đường dẫn tệp tin lưu trữ có định danh, kết quả truy xuất tri thức và tài liệu theo phiên bản.
- **Quyền hạn:** Quản lý việc đọc và ghi trong `storage/artifacts/`, `storage/knowledge/`.
- **Phối hợp thường xuyên:** Master, Process, IT, QA.
- **Không thuộc trách nhiệm:** Không tự ý xóa bỏ các phiên bản tài liệu hoặc artifacts lịch sử mà chưa có lệnh dọn dẹp được phê duyệt.
- **Ví dụ thực tế:** Lưu trữ bản báo cáo phân tích mã nguồn phiên bản 1.0.0 kèm mã băm SHA-256 và lập chỉ mục cho phép tìm kiếm lại theo từ khóa sau này.

## 5. Cơ chế cộng tác đa Agent (Multi-Agent Collaboration)

Cộng tác trong AI Village được tổ chức khoa học để đảm bảo hiệu suất và ngăn ngừa vòng lặp vô tận:

- **Phiên làm việc logic riêng biệt:** Mỗi Agent khi thực thi công việc được cấp một phiên làm việc độc lập với ngữ cảnh riêng, đảm bảo không rò rỉ dữ liệu giữa các nhiệm vụ khác nhau.
- **Trao đổi trực tiếp có cấu trúc:** Các Agent trong cùng nhóm công tác được phép gửi thông điệp trực tiếp để chia sẻ thông số kỹ thuật hoặc yêu cầu tài liệu trung gian mà không cần qua trung gian Master.
- **Thành lập nhóm công tác động:** Master phân tích yêu cầu nhiệm vụ và chỉ tập hợp những Agent có năng lực thực sự cần thiết, không điều động toàn bộ 9 phòng ban cho một tác vụ đơn giản.
- **Kỷ luật hội họp:** Các phiên họp chỉ được mở khi có bất đồng kỹ thuật phức tạp hoặc cần sự đồng thuận đa chiều. Cuộc họp bắt buộc phải có chủ tọa là Master, có mục tiêu rõ ràng, giới hạn tối đa số lượt trao đổi (ví dụ: không quá 3 vòng phản biện) và bắt buộc phải kết thúc bằng các quyết định và hành động cụ thể.
- **Chống tranh luận vô hạn:** Nghiêm cấm các vòng lặp tranh luận hoặc thử lại vô tận. Khi hết số lượt tranh luận quy định mà các bên chưa đạt đồng thuận, Master sẽ ra phán quyết cuối cùng dựa trên các bằng chứng kỹ thuật hiện có.

## 6. Nền tảng thực thi, Đa nhà cung cấp và Tích hợp Framework (Multi-Provider, Multi-Key & Framework)

Hệ thống thiết lập ranh giới rõ ràng giữa thực tế đã kiểm chứng và các ý tưởng thiết kế, đồng thời tách biệt tầng điều phối với tầng kết nối AI:

### 6.1. Phân định mức độ sẵn sàng kỹ thuật
1. **Khả năng đã kiểm chứng qua Chặng A và Phase 02:** Cấu trúc tệp tin tĩnh, bộ hợp đồng đặc tả và kiểm thử nhất quán trong Chặng A; UI scaffold tối thiểu build thành công; đồng thời `Antigravity CLI` cùng `Universal Engine Contract` tại `engines/` đã được kiểm chứng chạy thật từ handshake, thao tác công cụ trên đĩa kèm SHA-256, streaming events, session isolation đến process cancellation.
2. **Khả năng ở mức thiết kế hoặc mở rộng tùy chọn:** Cơ chế Event Journal bền vững trên SQLite tại `.runtime/`; kiểm thử gọi HTTP live cho các API Provider mở rộng như Google Gemini, OpenAI hoặc Ollama khi được cấp kết nối hợp lệ; giao diện bộ nhớ đa tầng (`private`, `project`, `global`) và Permission Manager đầy đủ ở các phase sau.
3. **Khả năng cần kiểm chứng tiếp ở Phase 03–15:** Đánh giá tích hợp CrewAI với orchestrator mỏng trên nền `EngineAdapter` và `AgentRuntime` (`PHASE 03`), thực thi công cụ nội bộ AI Village trong `tools/`/`sandbox/` (`PHASE 07`), và tích hợp kiểm chứng live nhiều API Provider trong backlog `P15.02` cùng lựa chọn Secret Vault cục bộ phù hợp.

### 6.2. Kiến trúc Đa nhà cung cấp, Đa kết nối và Đa khóa (Multi-Provider / Multi-Key)
Để bảo đảm tính độc lập nhà cung cấp và hỗ trợ BYOK an toàn, kiến trúc phân tách 8 thành phần phối hợp chặt chẽ theo ranh giới tầng:

1. **Provider / Engine Registry (`engines/`):** Khai báo danh mục mở các loại adapter hỗ trợ (Antigravity CLI, Google Gemini API, OpenAI API, Anthropic API, OpenAI-compatible endpoints, Local LLM như Ollama và các nền tảng Agent tương thích). Không hardcode danh sách Provider đóng làm kẹt khả năng mở rộng trong `core/`.
2. **Connection Registry (`storage/` + `engines/`):** Quản lý nhiều bản ghi kết nối (`connection_id`) cho cùng một Provider (ví dụ hai kết nối `gemini_primary` và `gemini_secondary`), lưu tên hiển thị, `provider_id`, `project_ref` / billing scope tùy chọn, trạng thái bật/tắt, mức ưu tiên và tham chiếu bí mật.
3. **Credential References (`storage/` + backend secret boundary):** Tách biệt hoàn toàn `connection_id` với thông tin xác thực thô bằng `credential_ref`. Agent Manifest, UI và Git tuyệt đối không chứa API key thô; việc giải mã `credential_ref` chỉ diễn ra tại ranh giới khởi tạo adapter ở backend.
4. **Model Capability Metadata (`engines/`):** Mô tả khả năng thật của từng nền tảng/model (LLM API thuần text, structured output, function/tool calling, streaming, cancellation, hay Agent Execution Platform có thao tác công cụ nội sinh) để tránh gán nhầm tác vụ cần công cụ cho mô hình không hỗ trợ.
5. **Routing Policy (`core/orchestration/` + `core/governance/`):** Bộ điều phối trong `core/` lựa chọn kết nối và model dựa trên năng lực yêu cầu, chi phí, độ khả dụng, chính sách bảo mật dữ liệu (như `local_only`) và cấu hình người vận hành. Mọi quyết định chọn hay fallback đều chịu sự kiểm tra của Permission Manager; nghiêm cấm LLM tự chọn kết nối ngoài allowlist hoặc né giới hạn quota bằng cách xoay vòng key.
6. **Resources Quota & Budget Tracking (`core/optimization/` & bộ phận `resources`):** Theo dõi chi phí và hạn mức thực tế gắn với `provider_id`, `connection_id`, `project_ref` và `model_id`. Hệ thống phân biệt rõ quota cấp Key với quota cấp Project/Account/Organization; khi nhà cung cấp không trả số liệu phải đặt `null` và `UNAVAILABLE_FROM_PROVIDER`.
7. **Engine Adapter Boundary (`engines/`):** Chuẩn hóa Request/Result/Event/Telemetry/Cancel giữa `core/` và mọi AI Provider hoặc Agent Execution Platform. Thay đổi Provider hoặc Connection không làm thay đổi danh tính, bộ nhớ hay quyền hạn của Agent.
8. **Tool Runtime & Permission Enforcement (`tools/` & `core/governance/`):** Khi sử dụng LLM API chỉ hỗ trợ sinh văn bản hoặc đề xuất gọi hàm, Tool Runtime của AI Village đứng ra thực thi công cụ thực tế trong `sandbox/` dưới sự kiểm soát Fail-Closed của Permission Manager.

### 6.3. Nguyên tắc tích hợp Framework và Cô lập Phiên
- **Antigravity CLI là Engine thực thi chính** được ưu tiên hoàn thiện và kiểm chứng end-to-end đầu tiên; các API Provider như Gemini, OpenAI, Anthropic hay Ollama là khả năng mở rộng tùy chọn nối qua cùng Universal Engine Contract tại `engines/` khi có nhu cầu và kết nối được cấp phép.
- Mỗi Agent luôn giữ **phiên làm việc (`session_id`) và ngữ cảnh riêng biệt** ngay cả khi nhiều Agent dùng chung một Provider, một Connection hoặc một API key.
- CrewAI được xem là một ứng viên tiềm năng cho tầng điều phối, nhưng hệ thống không khóa chặt kiến trúc vào CrewAI. Tầng `core/` chỉ giao tiếp thông qua giao diện trừu tượng `AgentRuntime`, cho phép thay thế hoặc nâng cấp công cụ điều phối mà không làm xáo trộn nghiệp vụ của các Agent.
- Tuyệt đối không công bố các tính năng chưa chạy thực tế là đã hoàn thành.

## 7. Quy trình tự cải tiến liên tục (Phương án C)

Cơ chế tự hoàn thiện của AI Village được xây dựng trên dữ liệu thực nghiệm:

1. **Ghi nhận thực nghiệm:** Sau mỗi nhiệm vụ, hệ thống tự động ghi nhận nhật ký vận hành gồm các bước thực thi, công cụ đã gọi, thời gian, lỗi phát sinh và cách khắc phục.
2. **Phát hiện mẫu lặp:** Phòng ban Process định kỳ phân tích dữ liệu tích lũy để tìm kiếm các mẫu xử lý lặp lại nhiều lần hoặc các thao tác thủ công tốn thời gian.
3. **Đề xuất kỹ năng mới:** Khi có đủ dữ liệu chứng minh nhu cầu lặp lại, Process soạn thảo bản đặc tả kỹ năng và đưa vào thư mục `skills/candidates/`.
4. **Phát triển và kiểm chứng:** Bộ phận IT chịu trách nhiệm hoàn thiện mã nguồn của kỹ năng. Bộ phận QA thực hiện kiểm thử độc lập qua các bộ dữ liệu mẫu (A/B benchmark).
5. **Kích hoạt an toàn:** Master đánh giá kết quả kiểm thử và đề xuất kích hoạt. Người vận hành phê duyệt nếu kỹ năng yêu cầu quyền hạn mới. Kỹ năng được đưa vào `skills/approved/` có gắn số hiệu phiên bản và có khả năng phục hồi (rollback) về phiên bản trước nếu phát sinh sự cố.

## 8. Tiếp nhận và xử lý ngắt (Reception & Interrupt Handling)

Hệ thống có cơ chế tiếp nhận các yêu cầu phát sinh mà không làm gián đoạn các tác vụ đang chạy:

### 8.1. Phân loại 5 cấp độ yêu cầu
1. **Yêu cầu thông thường (Queued):** Nhiệm vụ độc lập mới, được xếp vào hàng đợi chờ tài nguyên phân bổ theo thứ tự.
2. **Yêu cầu bổ sung (Appended / Refined):** Thông tin làm rõ hoặc tham số bổ sung cho một tác vụ đang chạy; được định tuyến thẳng tới phiên làm việc của nhóm phụ trách để cập nhật ngữ cảnh.
3. **Sự kiện bất thường (Anomalous):** Phát hiện sự sai lệch dữ liệu hoặc hành vi bất thường của Agent; chuyển tới QA và Resources để đánh giá mức độ rủi ro.
4. **Sự cố khẩn cấp (Emergency):** Lỗi nghiêm trọng, nguy cơ rò rỉ dữ liệu hoặc cạn kiệt tài nguyên; Reception phát tín hiệu ngắt khẩn cấp lên Master để dừng tác vụ ngay lập tức.
5. **Yêu cầu cần phê duyệt (Approval Required):** Hành động nhạy cảm chạm ngưỡng kiểm soát; hệ thống tạm dừng tác vụ và gửi thông báo chờ người dùng quyết định.

### 8.2. Định tuyến tinh gọn
Reception chỉ liên hệ đúng các Agent và phòng ban có trách nhiệm trực tiếp với sự việc, tuyệt đối không gửi cảnh báo tràn lan gây gián đoạn các bộ phận khác.

## 9. Kho lưu trữ và Tri thức (Artifact & Knowledge Vault)

Tài sản số của AI Village được quản lý chặt chẽ tại tầng `storage/`:

- **Đối tượng quản lý:** Sản phẩm bàn giao cuối cùng, báo cáo phân tích, mã nguồn đã kiểm thử, kỹ năng chuẩn hóa, nhật ký kiểm thử và tri thức chuyên môn tái sử dụng.
- **Tiêu chuẩn dữ liệu:** Mọi tài sản lưu trong Vault bắt buộc phải có định danh duy nhất (`artifact_id`), phiên bản theo chuẩn SemVer, liên kết với `task_id` và `run_id`, có mã băm SHA-256 để đảm bảo tính toàn vẹn và được gắn nhãn metadata phục vụ việc tìm kiếm chính xác.
- **Phân quyền truy cập:** Dữ liệu trong Vault được phân quyền rõ ràng theo nhiệm vụ và cấp độ bảo mật, ngăn ngừa việc truy xuất trái phép giữa các phiên làm việc độc lập.

## 10. Quản lý tài nguyên và Giám sát chi phí

Hệ thống quản lý tài nguyên dựa trên số liệu thực tế:

- **Các chỉ số theo dõi:** CPU, RAM, thời gian thực thi, hạn ngạch token từ API, chi phí tài chính tương ứng và định danh thực tế (`provider_id`, `connection_id`, `project_ref`, `model_id`) đã sử dụng.
- **Nguyên tắc trung thực:** Nghiêm cấm mọi hành vi tạo số liệu token giả mạo chỉ để lấp đầy trường dữ liệu. Khi AI Provider không hỗ trợ trả về số liệu token hoặc quota còn lại, trường dữ liệu phải được đặt giá trị `null` và gắn nhãn `telemetry_status: "UNAVAILABLE_FROM_PROVIDER"`. Không suy đoán rằng hai API key thuộc cùng một Project hay Account chắc chắn có hạn mức độc lập.
- **Kiểm soát chủ động:** Bộ phận Resources thiết lập các ngưỡng cảnh báo sớm (ví dụ: đạt 80% ngân sách/quota hoặc vượt 180 giây chạy liên tục) để thông báo cho Master có biện pháp điều phối kịp thời; mọi lựa chọn hoặc chuyển dự phòng sang model đắt hơn vượt ngân sách phải xin người dùng phê duyệt.

## 11. Chính sách phân quyền và Nghiệm thu độc lập

Hệ thống thiết lập cơ chế kiểm soát 3 cấp độ thẩm quyền:

1. **Cấp độ Agent:** Tự chủ thực thi các hành động nằm trong danh mục công cụ cho phép (`tools.allowed`) và phạm vi đường dẫn được cấp phép (`permissions.fs_read`, `permissions.fs_write`) trong thư mục `workspace/{task_id}/`.
2. **Cấp độ Master:** Quyết định việc phân rã mục tiêu, thành lập nhóm công tác, phân bổ tài nguyên, chủ trì cuộc họp và quyết định kết thúc tác vụ để chuyển sang bước kiểm chứng.
3. **Cấp độ Người dùng:** Bắt buộc phải có sự phê duyệt trực tiếp của con người đối với các hành động có rủi ro cao gồm: xóa dữ liệu hàng loạt, cài đặt package vào môi trường hệ thống, thực thi các lệnh hệ thống nhạy cảm, thay đổi chính sách bảo mật hoặc triển khai mã nguồn vào production.

**Nguyên tắc nghiệm thu độc lập:** Trạng thái `COMPLETED` chỉ được cấp khi sản phẩm vượt qua bài kiểm chứng độc lập của bộ phận QA với đầy đủ bằng chứng cụ thể (mã băm, log kiểm thử, kết quả thẩm định). Agent sản xuất không có quyền tự quyết định trạng thái hoàn thành của chính mình.

## 12. Giao diện người dùng, Mission Control và Thế giới văn phòng 3D

Trải nghiệm người dùng được phát triển theo lộ trình từng bước, lấy **Mission Control** làm trung tâm quan sát và điều hành chung thay vì chỉ là một hộp chat rời rạc (chi tiết đặc tả chức năng tại [MISSION_CONTROL_UX.md](MISSION_CONTROL_UX.md)):

### 12.1. Mission Control và Trải nghiệm Quan sát Điều hành (Giao diện 2D)
Mission Control không phải phòng ban thứ 10, không phải Agent mới, không phải Runtime thứ hai và không tự lưu trạng thái độc lập. Đây là lớp giao diện tổng hợp dữ liệu thật từ Master, Task Runtime, Agent Registry, Engine Adapter, Tool Runtime, Event Journal, Resource Ledger, Notification System, Approval Center, Artifact Registry, QA Evidence và Provider Connection Registry qua tầng `api/`.

Ngay tại màn hình chính (**Mission Control Home**), người vận hành quan sát theo thứ tự ưu tiên: việc cần người dùng xử lý (`Action Inbox` / `Approvals`), nhiệm vụ đang chạy, nhiệm vụ lỗi hoặc bị chặn, sản phẩm vừa hoàn thành, trạng thái tài nguyên/quota/kết nối và hoạt động gần đây, kèm khả năng truy cập trực tiếp (deep-link) tới đúng đối tượng gốc mà không cần tự nhớ ID hay lục log/thư mục.

Tám phân khu vận hành 2D và các công cụ điều hành tích hợp bao gồm:
1. **Chat với Master & Điều khiển Tác vụ:** Giao việc gắn Task/Project, đính kèm tệp, xem kế hoạch checklist, ngắt/hủy an toàn và mở thẳng Artifact/QA liên quan.
2. **Task Dashboard (Checklist Board, Kanban, Timing & Activity Journal):** Hiển thị bảng bước công việc có bằng chứng thật, bảng Kanban đồng bộ State Machine, đồng hồ thời gian tính từ timestamp gốc (không ETA giả) và dòng nhật ký Event Journal gốc.
3. **Agent & Department Explorer + Engine Status:** Khám phá vai trò/năng lực, phân biệt Agent đăng ký tĩnh với phiên thực sự đang chạy, xem trạng thái Engine/Session thật (không báo `Online` khi chưa kiểm tra kết nối).
4. **Meeting Room:** Quan sát agenda, ý kiến, phản biện có bằng chứng, quyết định Master và action items từ sự kiện họp thật.
5. **Skill Library:** Tra cứu kỹ năng chuẩn hóa, vòng đời 7 trạng thái, lịch sử cải tiến Phương án C và trạng thái benchmark A/B.
6. **Resource Monitor (Usage & Cost & Provider/Connection Manager):** Giám sát tiêu thụ CPU/RAM, token, chi phí có nguồn và trạng thái kết nối theo Provider, Project/Billing Scope, `connection_id`, `model_id`; tách biệt `null` (`Không có dữ liệu từ Provider`) với `0`; quản trị kết nối BYOK mà không lộ secret thô; kết hợp bảng cài đặt hợp lệ qua Governance.
7. **Artifact Vault (Artifact Cabinet, QA Inspection Station, Comparison Board, Trash & Restore):** Tìm kiếm toàn hệ thống (`Global Search` theo metadata có phân quyền), xem trước/tải phiên bản, truy vết biên bản kiểm thử QA, so sánh phiên bản xác định và khôi phục/xóa hàng loạt có bảo vệ tệp dùng chung.
8. **Approval Center & Action Inbox / Notification Center:** Tách biệt thông báo sự kiện (có khử trùng lặp và deep-link) với hộp thư đầu việc cần người dùng ra quyết định, mọi hành động nhạy cảm đi qua Approval Gate.

**Nguyên tắc trung thực dữ liệu UI:** Tuyệt đối không hiển thị số liệu giả, không suy đoán `%` tiến độ hoặc quota còn lại khi thiếu dữ liệu đo, không biến `null` thành `0` và luôn hiển thị đầy đủ trạng thái `Loading`, `Empty`, `Error`, `Offline` và `Unavailable`.

### 12.2. Không gian văn phòng 3D và Đồ vật Tương tác
Không gian 3D (`PHASE 14`) chỉ đóng vai trò một lớp kết xuất trực quan phản chiếu trạng thái thực tế từ Agent Runtime và Event Journal trên cùng nguồn dữ liệu với UI 2D. Animation của nhân vật ảo phải gắn chặt với trạng thái làm việc thật (`IDLE`, `RUNNING`, `BLOCKED`, `IN_MEETING`), tuyệt đối không tạo hoạt cảnh giả khi hệ thống không có tác vụ chạy.

Các thành phần chức năng của Mission Control được ánh xạ thành đồ vật tương tác trong văn phòng 3D (như Bảng trắng cho Task Checklist, Bảng ghim cho Kanban, Màn hình trung tâm cho Mission Control, Màn hình thống kê cho Resource Monitor, Bàn họp, Bàn kiểm định QA, Tủ hồ sơ, Bảng đối chiếu, Sổ nhật ký, Tủ máy chủ, Bảng cấu hình kết nối, Thùng rác, Bảng tra cứu và Bàn Master). Khi người dùng click vào vật thể, 3D chỉ xác định `component_id`/`action_id` để mở đúng bảng 2D tương ứng, đọc chung API/Store và đi qua cùng Permission Manager; không có logic nghiệp vụ riêng trong 3D và không bắt buộc người dùng dùng 3D mới hoàn thành được công việc. Giao diện 2D phải hoàn thiện và đạt Gate H trước khi phát triển thế giới 3D.

## 13. Các nguyên tắc bất biến của kiến trúc (Architectural Invariants)

Đây là các nguyên tắc cốt lõi không được phép vi phạm trong bất kỳ hoàn cảnh nào:

1. **Không hardcode quy trình:** Không ép buộc mọi bài toán phải đi qua một chuỗi phòng ban cố định.
2. **Không giả lập hành vi:** Không tự tạo log giả, không bịa đặt số liệu token và không tự tuyên bố hoàn thành khi chưa chạy công cụ thật.
3. **Tách biệt ranh giới UI:** Tầng giao diện người dùng không được chứa logic điều phối và không được truy cập trực tiếp vào cơ sở dữ liệu lõi.
4. **Độc lập kiểm chứng:** Agent tạo sản phẩm không được tự cấp quyền nghiệm thu PASS cho sản phẩm của mình.
5. **Bảo vệ môi trường production:** Không tự ý ghi đè mã nguồn hệ thống, không sửa đổi trực tiếp nhánh chính khi chưa qua quy trình duyệt an toàn.
6. **Tách bạch cấu hình và runtime:** Dữ liệu trạng thái thực thi tại `.runtime/` tuyệt đối không được trộn lẫn vào các tệp cấu hình tĩnh của Agent và Department trong Git.
7. **Phòng ban mở rộng:** Chín phòng ban hiện tại là danh mục mặc định, không phải là giới hạn cứng vĩnh viễn của hệ thống.
8. **Kiểm soát quyền thực thi:** Việc một phòng ban khai báo `default_tools` chỉ mang tính chất đề xuất; quyền thực thi thực tế của Agent phải do Permission Manager phê chuẩn dựa trên manifest của vai trò.
9. **Bảo vệ bí mật:** Tuyệt đối không lưu trữ khóa API, mật khẩu hoặc thông tin nhạy cảm trong mã nguồn, Agent Manifest, trình duyệt và lịch sử Git; tách biệt `connection_id` khỏi `credential_ref` và không xoay vòng key để né chính sách nhà cung cấp.

## 14. Bảng ma trận truy xuất yêu cầu (Traceability Matrix)

Ma trận liên kết giữa yêu cầu kiến trúc, thành phần phụ trách, lộ trình thực hiện và điều kiện nghiệm thu:

| Mã yêu cầu | Yêu cầu hệ thống | Thành phần phụ trách | Phase trong PROGRESS.md | Điều kiện nghiệm thu |
| :--- | :--- | :--- | :--- | :--- |
| **REQ-01** | Khởi tạo cấu trúc 14 thư mục cấp cao | Kho mã nguồn | P01.01 – P01.15 | Đầy đủ 14 thư mục, Git theo dõi sạch sẽ (thư mục cấp cao đã tạo tại P01; README từng folder thuộc P01.02–P01.15 vẫn ở trạng thái kế hoạch). |
| **REQ-02** | Quy chuẩn hóa đặc tả nhân sự AI | `agents/`, `docs/standards/` | P01B.01, P01B.01-R, P01B.07 | `AGENT_SPEC.md` đầy đủ 16 khía cạnh, schema có `system_role` và ràng buộc chuẩn. |
| **REQ-03** | Quy chuẩn hóa đặc tả phòng ban chức năng | `agents/departments/`, `docs/standards/` | P01B.02, P01B.07 | `DEPARTMENT_SPEC.md` định nghĩa 9 bộ phận và cơ chế mở rộng tại `agents/departments/`. |
| **REQ-04** | Thiết lập bản thiết kế kiến trúc, bộ tiêu chuẩn Skill/Rule/Tool, Templates và Audit | `docs/architecture/`, `docs/standards/`, `docs/templates/`, `docs/reviews/` | P01B.02A, P01B.02A-G, P01B.03 – P01B.07 | `SYSTEM_BLUEPRINT.md`, `SKILL_SPEC.md`, `RULE_SPEC.md`, `TOOL_SPEC.md`, 3 templates và `PHASE_01B_AUDIT.md` nhất quán hoàn toàn. |
| **REQ-05** | Kiểm chứng thực thi Antigravity CLI | `engines/antigravity/` | P02.01 – P02.06 | Gọi lệnh `agy` thật, tạo file thật, xác nhận qua SHA. |
| **REQ-06** | Đánh giá và quyết định tích hợp CrewAI | `core/orchestration/` | P03.01 – P03.03 | Spike tích hợp Master và Worker, tài liệu ADR so sánh. |
| **REQ-07** | Máy trạng thái nhiệm vụ và Event Journal | `core/tasks/`, `core/events/` | P04.01 – P04.07 | Task chuyển trạng thái có guard, event replay được. |
| **REQ-08** | Agent Registry động và Master điều phối | `agents/`, `core/` | P05.01 – P05.07 | Master hiểu brief, ghép đội theo năng lực, handoff có bằng chứng. |
| **REQ-09** | Cơ chế họp bàn và phản biện có cấu trúc | `core/orchestration/` | P06.01 – P06.06 | Phiên họp có chủ tọa, phản biện có chứng cứ, Master ra quyết định. |
| **REQ-10** | Bộ công cụ thực thi độc lập | `tools/` | P07.01 – P07.06 | Tool Registry quản lý file, code, research, office trong sandbox. |
| **REQ-11** | Quản lý kho lưu trữ và tri thức bền vững | `storage/` | P08.01 – P08.05 | Artifact Registry lưu file có hash, tra cứu theo metadata. |
| **REQ-12** | Kích hoạt chức năng của 9 phòng ban | `agents/departments/` | P09.01 – P09.09 | Từng phòng ban có chức năng cốt lõi và bài toán mẫu. |
| **REQ-13** | Cơ chế tự học và tối ưu kỹ năng (Phương án C) | `skills/`, `core/` | P10.01 – P10.05 | Đúc kết kinh nghiệm sau task, benchmark kỹ năng A/B trước khi duyệt. |
| **REQ-14** | Tiếp nhận ngắt và kiểm soát tài nguyên | `core/`, `agents/` | P11.01 – P11.05 | Sàng lọc 5 cấp độ yêu cầu, ghi nhận telemetry trung thực. |
| **REQ-15** | Xây dựng API và giao diện 2D Mission Control hoàn chỉnh | `api/`, `ui/` | P12.01 – P13.08 | Vận hành toàn bộ tác vụ live qua Mission Control Home và 8 phân khu 2D, truy vết checklist/usage/QA/artifact không cần lục log. |
| **REQ-16** | Kết xuất thế giới văn phòng 3D và đồ vật tương tác | `ui/src/village3d/` | P14.01 – P14.06 | 3D phản chiếu sự kiện thực tế từ Event Bus, ánh xạ vật thể về đúng bảng 2D qua `component_id`, không có logic riêng. |
| **REQ-17** | Nghiệm thu tổng thể và bàn giao hệ thống | Toàn hệ thống | P15.01 – P17.04 | Vượt qua các kịch bản live đa lĩnh vực, có tài liệu vận hành. |

*Lưu ý: Các yêu cầu từ REQ-05 trở đi đang ở trạng thái kế hoạch và chưa được đánh dấu hoàn thành.*

## 15. Xử lý các điểm cần lưu ý và Đề xuất điều chỉnh

Trong quá trình xây dựng hệ thống, ba vấn đề kiến trúc quan trọng được làm rõ và ghi nhận giải pháp:

1. **Định danh Master:** Master không thuộc 9 phòng ban chuyên môn nhưng cần có định danh tương thích trong Agent Registry. Giải pháp thống nhất là sử dụng mã định danh `id: "core.master"` với trường `department: "orchestration"` (phạm vi điều hành đặc biệt, không phải phòng ban chuyên môn thứ 10), kết hợp thuộc tính `system_role: "orchestrator"`.
2. **Đường dẫn Department Registry:** Đảm bảo toàn bộ cấu hình phòng ban nằm đúng ranh giới tại `agents/departments/{dept_id}/department.yaml`, không tạo thư mục độc lập ở root repository.
3. **Bản chất của default_tools:** Thuộc tính `default_tools` trong cấu hình phòng ban chỉ mang tính chất khuyến nghị chuyên môn. Quyền thực thi công cụ thực tế của Agent bắt buộc phải được quy định cụ thể tại trường `tools.allowed` trong manifest của Agent và chịu sự kiểm soát của Permission Manager tại `core/governance/`.

**Trạng thái đồng bộ cập nhật (P01B.07):**
- Đã cập nhật `AGENT_SPEC.md` để bổ sung trường `system_role` (`orchestrator` hoặc `worker`), quy tắc kiểm tra điều kiện `orchestration`, đường dẫn `agents/departments/`, `skill_id` dạng `snake_case` và Action ID cho `requires_approval`.
- Đã cập nhật `DEPARTMENT_SPEC.md` để khẳng định `default_tools` chỉ mang tính khuyến nghị, không tự động cấp quyền.
- Đã đồng bộ `SKILL_SPEC.md`, `RULE_SPEC.md` và `TOOL_SPEC.md` theo báo cáo kiểm toán tại `docs/reviews/PHASE_01B_AUDIT.md`.
