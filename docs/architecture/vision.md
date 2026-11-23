# AI Village: Tầm nhìn Sản phẩm và Phạm vi MVP (Product Vision & MVP Definition)

Tài liệu này hệ thống hóa tầm nhìn sản phẩm, định hướng kiến trúc đa nhà cung cấp và đa khóa API, phạm vi MVP, ranh giới các hạng mục chưa triển khai trong MVP, định nghĩa hoàn thành và tiêu chí nghiệm thu dựa trên các quyết định nền tảng tại [SYSTEM_BLUEPRINT.md](file:///C:/DoCho/AI-Village/docs/architecture/SYSTEM_BLUEPRINT.md), [README.md](file:///C:/DoCho/AI-Village/README.md) và [PROGRESS.md](file:///C:/DoCho/AI-Village/docs/PROGRESS.md).

## 1. Tầm nhìn Sản phẩm (Product Vision)

AI Village là hệ thống Multi-Agent đa chức năng dưới dạng môi trường làm việc trực quan, tổ chức các nhân viên AI theo miền năng lực chuyên môn và điều phối động thông qua Master Agent, vận hành trên kiến trúc Multi-Provider, Multi-Model, Multi-API-Key và Multi-Platform.

1. **Tổ chức theo năng lực, không ép quy trình cứng:** Chín phòng ban mặc định (`hr`, `resources`, `service`, `marketing`, `it`, `reception`, `process`, `qa`, `vault`) đóng vai trò là các miền chuyên môn và không gian quản lý năng lực. Phòng ban không phải dây chuyền xử lý cố định bắt mọi nhiệm vụ đều phải đi qua đủ 9 bộ phận.
2. **Master điều phối động:** Người dùng giao nhiệm vụ trực tiếp cho Master (`core.master`). Master phân tích đề bài, lựa chọn nhân sự phù hợp theo nhãn năng lực (`capabilities`), thành lập nhóm công tác liên phòng ban khi cần thiết, tổ chức trao đổi có cấu trúc và bàn giao sản phẩm cuối cùng.
3. **Độc lập nhà cung cấp và nền tảng thực thi:** AI Village không bị khóa độc quyền vào Antigravity, Google Gemini, OpenAI, Anthropic hay bất kỳ framework điều phối nào. Người vận hành có thể cấu hình nhiều kết nối AI và phân phối linh hoạt cho các Agent theo nhiệm vụ, năng lực, chi phí, quyền hạn và tình trạng tài nguyên.
4. **Thực thi thật và chứng thực bằng bằng chứng (Evidence First):** Hệ thống phải giải quyết nhiệm vụ thật thông qua Engine Adapter và công cụ thực tế tại `tools/`, tạo ra artifact thật trên đĩa và xác minh bằng dữ liệu đo đạc độc lập (mã băm SHA-256, mã thoát lệnh, nhật ký kiểm thử). Hệ thống nghiêm cấm hành vi mô phỏng tiến độ giả, bịa đặt số liệu token hoặc để Agent sản xuất tự chứng nhận hoàn thành thay cho QA.

## 2. Multi-Provider, Multi-Model & Multi-Agent Execution Vision

### 2.1. Provider Independence

AI Village không bị ràng buộc vào bất kỳ một AI Provider duy nhất nào. Ở tầm nhìn mục tiêu, hệ thống hỗ trợ kết nối nhiều nguồn AI thông qua lớp adapter thống nhất:

- Google Gemini API.
- OpenAI API.
- Anthropic API.
- OpenAI-compatible endpoints.
- Local LLM như Ollama hoặc các endpoint tương thích chạy cục bộ.
- Antigravity CLI.
- Các nền tảng Agent hoặc CLI khác khi có adapter và giao diện thực thi được kiểm chứng.

Danh sách trên xác định định hướng mở rộng kiến trúc, không phải tuyên bố toàn bộ các nhà cung cấp này đã được tích hợp hay kiểm chứng chạy thật trong Chặng A.

### 2.2. Multi-Agent, Multi-Engine

Một nhiệm vụ trong AI Village có thể được xử lý đồng thời hoặc tuần tự bởi nhiều Agent sử dụng các Engine, Provider và Model khác nhau. Ví dụ: Master sử dụng Model A để phân tích và điều phối, IT Agent sử dụng Model B chuyên lập trình, Research Agent sử dụng Model C cho tổng hợp tài liệu và QA Agent sử dụng Model D để phản biện độc lập.

Các Agent trong cùng hệ thống có thể sử dụng:

- Cùng một Provider và cùng một Connection.
- Cùng một Provider nhưng khác Connection.
- Các Provider khác nhau.
- Mô hình chạy cục bộ (Local Model).
- Agent Execution Engine có khả năng thao tác công cụ thật trên workspace.

Việc thay đổi Provider, Connection hoặc Model thực thi tuyệt đối không làm thay đổi danh tính (`id`), quyền hạn (`permissions`), hay phạm vi bộ nhớ (`memory.scopes`) của Agent. Dù dùng chung một Provider hay Connection, mỗi Agent vẫn duy trì phiên làm việc (`session_id`) và ngữ cảnh tách biệt.

### 2.3. Bring Your Own API Key (BYOK)

Người vận hành có toàn quyền tự cung cấp API key hoặc thông tin kết nối hợp lệ của riêng mình. Hệ thống được thiết kế để hỗ trợ đầy đủ vòng đời quản trị kết nối:

- Thêm Provider mới theo hợp đồng adapter chuẩn.
- Đăng ký nhiều Connection độc lập cho cùng một Provider.
- Thêm nhiều API key thuộc cùng một Provider thông qua các tham chiếu bí mật riêng.
- Đặt tên nhận diện cho từng Connection.
- Bật hoặc tắt riêng từng Connection mà không làm mất cấu hình Agent.
- Kiểm tra tính khả dụng của kết nối khi người dùng cho phép.
- Chỉ định Connection ưu tiên theo hồ sơ định tuyến.
- Thu hồi hoặc thay thế credential an toàn.
- Theo dõi trạng thái kết nối và hạn mức khi có dữ liệu thực tế từ nhà cung cấp.

Kiến trúc không giới hạn cứng một Provider chỉ có một API key và không giới hạn hệ thống chỉ có hai key.

### 2.4. Use Case Thiết kế Chính thức: Hai API key Google Gemini

Tình huống một người vận hành sở hữu và khai báo hai API key Google Gemini được đưa vào làm use case thiết kế chính thức của AI Village.

Ví dụ minh họa cấu trúc logic ở mức thiết kế:

```yaml
provider: google_gemini

connections:
  - connection_id: gemini_primary
    credential_ref: secret_ref_primary
    project_ref: google_project_a
    enabled: true

  - connection_id: gemini_secondary
    credential_ref: secret_ref_secondary
    project_ref: google_project_a
    enabled: true
```

Đây chỉ là ví dụ cấu hình minh họa ở mức thiết kế kiến trúc, không phải schema Runtime đã triển khai hay đang hoạt động.

Các ràng buộc bắt buộc đối với use case hai kết nối Gemini và tổng quát cho mọi Provider:

- Hai credential độc lập về định danh tham chiếu (`credential_ref`) và gắn với các `connection_id` tách biệt.
- Có thể gán cho các Agent khác nhau hoặc đặt mức ưu tiên khác nhau.
- Có thể bật hoặc tắt riêng từng kết nối độc lập.
- Không lưu API key thô trong tệp cấu hình Agent, Connection Registry hay Git.
- Không mặc định mỗi API key sở hữu một hạn mức quota độc lập.

Hệ thống bắt buộc phải phân biệt 6 thực thể khác nhau:

1. **Provider:** Nhà cung cấp dịch vụ hoặc nền tảng AI (ví dụ: `google_gemini`, `openai`, `anthropic`, `antigravity`).
2. **Account / Organization:** Tài khoản hoặc tổ chức sở hữu thuê bao tại nhà cung cấp.
3. **Project / Billing Scope:** Dự án đám mây hoặc phạm vi thanh toán nơi API key được phát hành (ví dụ: `google_project_a`).
4. **API Key (Credential):** Chuỗi khóa bí mật xác thực cụ thể, chỉ được tham chiếu qua `credential_ref`.
5. **Connection:** Bản ghi kết nối do người vận hành đặt tên (`connection_id`), liên kết Provider, `credential_ref`, phạm vi dự án, trạng thái bật/tắt và chính sách sử dụng.
6. **Model:** Định danh mô hình cụ thể được gọi qua kết nối đó (`model_id`), khác với phân tầng năng lực chung (`model_tier`).

Nhiều nhà cung cấp áp dụng hạn mức sử dụng ở cấp Project, Account, Organization hoặc từng Model chứ không tính riêng theo từng API key. Hệ thống nghiêm cấm suy đoán rằng hai API key chắc chắn có hai hạn mức quota độc lập, và nghiêm cấm sử dụng cơ chế xoay vòng key để né giới hạn tốc độ hay chính sách sử dụng của nhà cung cấp.

### 2.5. Dynamic Provider & Model Routing

Master hoặc bộ điều phối trong `core/` có thể đề xuất lựa chọn Provider, Connection hoặc Model phù hợp cho từng lượt thực thi dựa trên các tiêu chí:

- Năng lực chuyên môn cần thiết của Agent.
- Loại nhiệm vụ và độ phức tạp yêu cầu.
- Model capabilities hỗ trợ (text, structured output, tool calling, context window).
- Tốc độ phản hồi và độ khả dụng hiện tại của kết nối.
- Hạn mức còn lại nếu được xác minh từ dữ liệu thật của nhà cung cấp.
- Chi phí vận hành và ngưỡng ngân sách nhiệm vụ.
- Quyền hạn thực thi và chính sách bảo mật, quyền riêng tư dữ liệu (ví dụ: nhiệm vụ yêu cầu xử lý cục bộ không được đẩy lên API đám mây bên ngoài).
- Cấu hình ưu tiên và sở thích của người vận hành.

Mọi đề xuất định tuyến đều phải đi qua kiểm tra của Permission Manager (`core/governance/`), tuân thủ chính sách ngân sách do Resources giám sát và các giới hạn thực tế của hệ thống. Không cho phép LLM tự ý chọn hoặc chuyển sang kết nối ngoài phạm vi được cấp phép.

### 2.6. Fallback & Recovery

Trong các tình huống sự cố như model không khả dụng, lỗi API, mất kết nối mạng, hết hạn mức, credential bị vô hiệu hóa, model không hỗ trợ khả năng được yêu cầu hoặc nhà cung cấp gặp gián đoạn, thiết kế tương lai phải tuân thủ các nguyên tắc:

- Chỉ chuyển dự phòng (fallback) sang kết nối hoặc model khác khi nằm trong chính sách được phép.
- Không tự động chuyển sang model có chi phí cao hơn nếu vượt ngân sách hoặc thuộc ngưỡng phải xin phê duyệt của người vận hành.
- Không thử lại vô hạn khi gặp lỗi xác thực, hết quota hoặc lỗi hợp đồng dữ liệu.
- Không tự động chạy lại các tác vụ có tác động phụ khi trạng thái thực thi của lần gọi trước chưa rõ ràng.
- Không tự động né hoặc vượt qua hạn mức sử dụng hay chính sách của nhà cung cấp.
- Ghi nhận chính xác `provider_id`, `connection_id` và `model_id` thực tế đã sử dụng vào Event Journal và Telemetry.

### 2.7. Provider Capability Awareness

Hệ thống không coi mọi Provider đều có năng lực thực thi đồng nhất và bắt buộc phân biệt rõ hai nhóm nền tảng:

- **LLM API:** Tập trung vào sinh văn bản, phân tích dữ liệu, xuất dữ liệu có cấu trúc nếu hỗ trợ và trả về đề xuất gọi hàm/công cụ nếu API hỗ trợ. LLM API không tự có quyền thao tác trực tiếp hệ thống tệp hay tiến trình máy chủ; mọi lời gọi công cụ phải đi qua Tool Runtime và Permission Manager của AI Village.
- **Agent Execution Platform:** Các nền tảng hoặc CLI tác tử (như Antigravity CLI) có khả năng tự quản lý phiên làm việc, tương tác công cụ, thao tác tệp hoặc terminal trong phạm vi workspace được cấp quyền và hỗ trợ streaming hoặc cancellation nếu bản cài đặt đáp ứng.

Không được giả định một LLM API thuần túy có thể thay thế trực tiếp Antigravity CLI mà không cần tầng Tool Runtime của AI Village đứng sau thực thi và kiểm chứng.

### 2.8. Provider & Credential Security

Bảo vệ thông tin xác thực là yêu cầu nền tảng xuyên suốt toàn bộ vòng đời sản phẩm:

- Credential chỉ được giải mã và sử dụng bên trong thành phần backend hoặc secret resolver được kiểm soát chặt chẽ.
- Tuyệt đối không lưu API key thô trong Agent Manifest, Department Manifest, Skill hay Rule YAML.
- Tuyệt đối không đưa API key thật vào kho lưu trữ Git.
- Không ghi credential vào nhật ký thực thi, Event Journal hay dữ liệu telemetry.
- Không gửi secret trở lại trình duyệt sau khi người vận hành đã lưu.
- Không truyền API key qua URL truy cập hoặc tham số truy vấn.
- Không dùng `localStorage` hoặc bộ nhớ trình duyệt làm kho lưu trữ API key.
- Giao diện người dùng chỉ hiển thị tên kết nối, trạng thái hoạt động, nhà cung cấp và chuỗi nhận diện đã che (masked).
- Hỗ trợ thu hồi và thay thế (xoay vòng quản trị) credential an toàn.
- Chính sách phân quyền tại `core/governance/` áp dụng cho cả quyền sử dụng kết nối khi chạy tác vụ lẫn quyền quản trị thêm, xóa, bật, tắt credential.

Thiết kế hướng tới mô hình lưu trữ bí mật ưu tiên cục bộ (local-first secret storage) và giữ giao diện trừu tượng để tích hợp secret manager phù hợp ở các phase sau. Chặng A chưa lựa chọn hay cài đặt một công nghệ lưu khóa cụ thể khi chưa đủ bằng chứng kiểm thử.

## 3. Phạm vi MVP và Sau MVP

### 3.1. MVP: Năng lực Tối thiểu Cần Chứng minh

Phiên bản MVP (tương ứng chặng chứng minh lõi từ Gate B đến Gate D cùng lớp kiểm chứng cơ bản, trước khi mở rộng toàn bộ 9 phòng ban đầy đủ hay giao diện 3D) phân biệt rõ giữa hạng mục thiết kế cấu hình và hạng mục bắt buộc kiểm chứng chạy thật:

1. **Thực thi thật qua Engine kiểm chứng:** Antigravity hoặc một Agent Engine được kiểm chứng có thể thực thi công cụ thật trên workspace, tạo artifact thật kèm mã băm SHA-256 xác minh trên đĩa.
2. **Hợp đồng Engine Adapter độc lập Provider:** Tồn tại hợp đồng Request/Result/Event/Telemetry/Cancel thống nhất tại `engines/`, tách biệt khỏi logic điều phối của `core/`.
3. **Tích hợp tối thiểu một API Provider khác Antigravity:** Chứng minh khả năng tích hợp và gọi thật ít nhất một API Provider song song thông qua adapter chung.
4. **Cấu hình linh hoạt theo Agent:** Cho phép cấu hình lựa chọn Provider, Connection hoặc Model cho Agent qua manifest hoặc cấu hình hợp lệ mà không phải sửa mã nguồn Core Runtime.
5. **Thiết kế Multi-Connection an toàn cho cùng Provider:** Thiết kế quản lý credential hỗ trợ nhiều kết nối thuộc cùng một Provider, tối thiểu biểu diễn và phân biệt được hai kết nối Google Gemini ở mức cấu hình và kiểm tra phân quyền hợp lệ.
6. **Không rò rỉ API key:** Chứng minh qua kiểm thử rằng API key không xuất hiện trong mã nguồn, log, Event Journal, phản hồi API hay trình duyệt.
7. **Lựa chọn kết nối theo quyền và phạm vi:** Chứng minh cơ chế chọn kết nối hợp lệ tuân thủ Permission Manager và phạm vi nhiệm vụ (có kiểm tra Fail-Closed).
8. **Thực thi công cụ qua Tool Runtime cho API Provider:** Agent dùng LLM API có thể thực thi công cụ nội bộ của AI Village thông qua Tool Runtime và Sandbox khi Provider và adapter hỗ trợ gọi công cụ.
9. **Nghiệm thu có bằng chứng và vòng đời Task cơ bản:** Người dùng gửi nhiệm vụ cho Master; Master tuyển chọn Agent theo năng lực; Runtime quản lý trạng thái Task, lỗi, timeout, hủy tác vụ cơ bản; QA kiểm chứng độc lập trước khi Master bàn giao sản phẩm có bằng chứng.

MVP **không** yêu cầu phải lưu hai API key thật trong repository hay bắt buộc tiêu thụ thực tế đồng thời cả hai API key Gemini để nghiệm thu MVP. Không yêu cầu triển khai đầy đủ đồng thời toàn bộ 9 phòng ban ở quy mô hoàn chỉnh trong MVP.

### 3.2. Sau MVP (Long-term Roadmap)

Các năng lực sau thuộc tầm nhìn dài hạn của AI Village và chỉ triển khai ở các phase sau khi các Gate tiền điều kiện đã đạt:

- Dashboard quản trị đa Provider và nhiều API key đầy đủ trên giao diện 2D (`PHASE 12`–`13`).
- Credential Pool nâng cao với chính sách phân bổ theo tải và ngân sách chi tiết.
- Tối ưu hóa động chi phí và hiệu năng giữa các model theo lịch sử thực nghiệm.
- Phân phối tải qua nhiều kết nối hợp lệ theo đúng điều khoản nhà cung cấp.
- Chính sách fallback nhiều tầng nâng cao kèm kiểm soát chi phí tự động xin duyệt.
- Vận hành đồng thời nhiều Agent quy mô lớn trên nhiều tiến trình cô lập.
- Tích hợp mở rộng nhiều Agent Framework và CLI Execution Platform khác ngoài Antigravity.
- Quản lý hạn mức chi tiết theo từng phạm vi Project/Account/Model khi nhà cung cấp trả về số liệu thực tế.
- Benchmark A/B và lựa chọn Model tự động dựa trên bằng chứng kiểm thử thực tế.
- Không gian văn phòng 3D hoàn chỉnh (`PHASE 14`) gắn chặt với sự kiện thật từ Event Journal.
- Cơ chế tự cải tiến quy trình Phương án C hoàn chỉnh với benchmark và phê duyệt của người vận hành.

## 4. Definition of Done

### 4.1. Phân biệt trạng thái Task và trạng thái hoàn thành Phase

AI Village tách biệt hoàn toàn hai hệ thống trạng thái:

- **Trạng thái Task (`Task Lifecycle`):** Phản ánh trạng thái vận hành của một nhiệm vụ cụ thể trong Runtime, được quản lý bởi máy trạng thái tại `core/` và xác nhận bằng sự kiện hệ thống thực tế. Trong [AGENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/AGENT_SPEC.md), trạng thái hoàn thành hợp lệ của Task là `COMPLETED` (tương ứng khái niệm `DONE` ở mức mô tả nghiệp vụ).
- **Trạng thái hoàn thành Phase (`Phase / Roadmap Status`):** Phản ánh mức độ hoàn tất của một tiểu phase phát triển phần mềm trong [PROGRESS.md](file:///C:/DoCho/AI-Village/docs/PROGRESS.md) (`TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`), chỉ được công nhận `DONE` khi có mã nguồn hoặc tài liệu thực tế, test vượt qua và commit Git đã kiểm chứng.

Không được dùng trạng thái `DONE` của một tiểu phase tài liệu để suy diễn rằng chức năng Runtime của Task đã chạy được.

### 4.2. Định nghĩa các trạng thái nhiệm vụ chính

| Trạng thái | Định nghĩa chuẩn trong hệ thống | Điều kiện chuyển trạng thái hợp lệ |
| :--- | :--- | :--- |
| **`DONE` (`COMPLETED`)** | Nhiệm vụ đã hoàn tất toàn bộ đầu ra cam kết và vượt qua bước kiểm chứng độc lập. | Có artifact thực tế, có bằng chứng xác minh (`VERIFIED_PASS` từ QA hoặc test runner độc lập) và được Master xác nhận bàn giao. Agent sản xuất không được tự gán trạng thái này. |
| **`FAILED`** | Nhiệm vụ thất bại do lỗi thực thi nội bộ, sai hợp đồng dữ liệu hoặc không vượt qua kiểm chứng sau khi hết giới hạn thử lại. | Ghi nhận mã lỗi có cấu trúc, nhật ký đã lọc bí mật (`redacted`), dừng mọi retry và báo cáo rõ nguyên nhân thất bại. |
| **`BLOCKED`** | Nhiệm vụ bị đình trệ do yếu tố khách quan bên ngoài hoặc điều kiện tiên quyết chưa đáp ứng (mất kết nối provider, thiếu tệp đầu vào, công cụ chưa khả dụng, thiếu dependency hoặc provider không hỗ trợ công cụ bắt buộc). | Phân biệt rõ với `FAILED` và `WAITING_APPROVAL`; hệ thống giữ nguyên hiện trường an toàn và nêu rõ tác nhân gây nghẽn để xử lý. |
| **`WAITING_APPROVAL`** | Nhiệm vụ hoặc nhánh thực thi tạm dừng có chủ đích để chờ người vận hành phê duyệt hành động nhạy cảm qua Approval Gate. | Phát sinh từ quyết định `APPROVAL_REQUIRED` của Permission Manager, có bản ghi `ApprovalRequest` gắn `approval_id` và thời hạn hiệu lực. |
| **`CANCELLED`** | Nhiệm vụ bị hủy bỏ chủ động theo lệnh của người dùng hoặc Master trước khi hoàn tất. | Tín hiệu hủy chấm dứt toàn bộ tiến trình con trong sandbox, dọn dẹp tài nguyên cấp phát và ghi nhận sự kiện hủy vào Event Journal. |

## 5. Tiêu chí Nghiệm thu MVP (Success Criteria)

MVP chỉ được công nhận đạt khi toàn bộ các tiêu chí kiểm chứng sau có bằng chứng thực thi thật (chưa có tiêu chí nào trong số này được tuyên bố đã triển khai tại Chặng A):

1. **Thực thi thật qua Engine Adapter mặc định và Provider thay thế:** Từ yêu cầu gửi vào ứng dụng, hệ thống gọi được Antigravity CLI (hoặc engine chuẩn hóa theo contract) thao tác công cụ thật trên workspace tạo tệp có SHA-256 khớp thực tế, đồng thời chứng minh thay thế được tối thiểu một API Provider khác qua cùng hợp đồng Engine Adapter mà không đổi mã Core.
2. **Cách ly phiên và xử lý sự cố:** Hai phiên nhiệm vụ chạy tách biệt không trộn ngữ cảnh hay tệp tin dù dùng chung Provider/Connection; các tình huống timeout, cancel, lỗi xác thực và hết quota trả về trạng thái chuẩn xác mà không lặp vô hạn.
3. **Vòng đời Task bền vững:** Nhiệm vụ chuyển trạng thái đúng guard (`QUEUED` -> `RUNNING` -> `WAITING_APPROVAL`/`BLOCKED`/`COMPLETED`/`FAILED`/`CANCELLED`), lưu vào SQLite và không mất hoặc giả mạo trạng thái sau khi khởi động lại hệ thống.
4. **Phối hợp Master và Worker có kiểm chứng độc lập:** Master chọn đúng nhân sự theo năng lực, ghi nhận handoff kèm định danh và QA độc lập phát hiện hoặc xác minh lỗi trước khi Master bàn giao sản phẩm cuối cùng.
5. **An toàn Credential và ranh giới phân quyền:** Thiết kế hỗ trợ nhiều kết nối cho cùng Provider (minh họa qua hai kết nối Gemini); mọi truy cập tệp, công cụ và kết nối AI đều đi qua Permission Manager theo cơ chế Fail-Closed; tuyệt đối không rò rỉ khóa bí mật vào mã nguồn, nhật ký, phản hồi API hay trình duyệt.

## 6. Trạng thái Phê duyệt (Approval)

### 6.1. Đối chiếu các quyết định đã được người vận hành xác nhận

- Định hướng AI Village là hệ thống Multi-Agent đa chức năng (không phải phần mềm marketing chuyên biệt), tổ chức theo 9 phòng ban mặc định mang tính miền năng lực và điều phối qua Master Agent tại [README.md](file:///C:/DoCho/AI-Village/README.md) và [SYSTEM_BLUEPRINT.md](file:///C:/DoCho/AI-Village/docs/architecture/SYSTEM_BLUEPRINT.md).
- Nguyên tắc bất biến: Evidence First, tách biệt QA độc lập, không khóa cứng CrewAI trước khi kiểm chứng Phase 03, ưu tiên 2D trước 3D, bảo mật Fail-Closed và cấu trúc 14 thư mục gốc tại [AGENTS.md](file:///C:/DoCho/AI-Village/AGENTS.md) và bộ tiêu chuẩn `docs/standards/`.
- Yêu cầu chuẩn hóa tầm nhìn Multi-Provider, Multi-Model, Multi-API-Key (BYOK) và Multi-Platform trước khi phê duyệt `P00.01`.

### 6.2. Nội dung chờ người vận hành phê duyệt

- **Trạng thái phê duyệt tài liệu `vision.md`:** `PENDING_USER_APPROVAL`
- **Các mục cụ thể cần người vận hành xác nhận để đóng P00.01 và Mục 5 của `PROGRESS.md`:**
  1. Phê duyệt chính thức bản tầm nhìn sản phẩm đã cập nhật (bao gồm Multi-Provider, Multi-Model, Multi-API-Key BYOK, ranh giới MVP so với Sau MVP và Tiêu chí nghiệm thu MVP) trong tài liệu này.
  2. Chốt các quyết định còn mở tại Mục 5 của [PROGRESS.md](file:///C:/DoCho/AI-Village/docs/PROGRESS.md): tên dự án chính thức (`AI Village`), đường dẫn repository (`C:\DoCho\AI-Village`), phạm vi quyền tự động mặc định của Master trong workspace so với các thao tác cần người duyệt, phương án hiển thị 3D và lựa chọn công nghệ 3D sau khi đạt M4, cũng như việc chốt CrewAI chỉ thực hiện sau P03.01–P03.02.
