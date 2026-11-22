# Tiêu chuẩn Định nghĩa Phòng ban Chức năng (Department Specification)

Tài liệu này xác lập tiêu chuẩn định nghĩa, ranh giới trách nhiệm và cơ chế vận hành cho các phòng ban chức năng trong hệ thống Multi-Agent AI Village. Tiêu chuẩn này làm nền tảng cho việc khởi tạo Department Registry tại `agents/departments/` và phục vụ thuật toán lập nhóm công tác động của Master tại `core/`.

## 1. Nguyên lý kiến trúc

Phòng ban trong AI Village được tổ chức theo các nguyên lý vận hành sau:

1. **Phòng ban là không gian quản lý năng lực:** Mỗi phòng ban đại diện cho một miền chuyên môn và chịu trách nhiệm định nghĩa, nâng cấp năng lực cho các Agent trực thuộc. Phòng ban không phải là một đường ống dây chuyền làm việc cố định mà mọi tác vụ đều bắt buộc phải đi qua.
2. **Không đồng nhất phòng ban với tiến trình hay mô hình:** Phòng ban không đại diện cho một tiến trình hệ điều hành, một tài khoản AI hay một mô hình ngôn ngữ riêng biệt. Nhiều Agent trong cùng một phòng ban hoặc giữa các phòng ban khác nhau có thể cùng chia sẻ Antigravity Engine hoặc các AI Provider khác.
3. **Thành viên chủ quản và nhóm dự án linh hoạt:** Mỗi Agent chỉ có một phòng ban chủ quản duy nhất để quản lý hồ sơ chuyên môn, nhưng có thể tham gia đồng thời hoặc tuần tự vào nhiều nhóm công tác liên phòng ban do Master thành lập.
4. **Không phụ thuộc vào framework điều phối:** Cấu trúc phòng ban tương thích với cả kiến trúc điều phối tùy biến lẫn các framework như CrewAI, không tạo ràng buộc cứng vào API nội bộ của bất kỳ thư viện nào.
5. **Tách biệt siêu dữ liệu giao diện:** Các thông tin hình ảnh hiển thị trên giao diện 2D hoặc không gian 3D chỉ đóng vai trò mô tả cách kết xuất đồ họa, hoàn toàn không chứa mã điều khiển hay logic nghiệp vụ AI.

## 2. Các thành phần đặc tả chi tiết

### 2.1. Department Identity (Danh tính phòng ban)
Thông tin nhận diện cơ bản của phòng ban trong hệ thống:
- `spec_version`: Phiên bản của chuẩn đặc tả dữ liệu (ví dụ: `"1.0.0"`).
- `department_id`: Định danh duy nhất, viết thường, ngăn cách bằng dấu gạch dưới (ví dụ: `it`, `hr`, `qa`).
- `name`: Tên gọi hiển thị chính thức của phòng ban (ví dụ: "IT & Nghiên cứu Phát triển").
- `description`: Mô tả súc tích về chức năng và phạm vi nghiệp vụ.
- `purpose`: Tuyên bố mục tiêu tồn tại của bộ phận trong hệ thống Village.
- `responsibilities`: Danh sách các trách nhiệm chuyên môn cụ thể mà phòng ban đảm nhiệm.
- `supported_capabilities`: Danh mục các nhãn năng lực chuẩn hóa mà phòng ban bảo trợ và đào tạo cho các Agent trực thuộc.
- `default_tools`: Danh sách các công cụ trong `tools/` được phòng ban khuyến nghị hoặc cấp phép mặc định cho nhân viên.
- `status`: Trạng thái hoạt động (`ACTIVE`, `INACTIVE`, `DEPRECATED`).
- `visual_metadata`: Cấu hình đồ họa phục vụ giao diện 2D và không gian 3D gồm mã màu nhận diện, biểu tượng và mã vùng văn phòng (`zone_id`).

### 2.2. Chín bộ phận chức năng mặc định
Hệ thống thiết lập sẵn 9 phòng ban chuyên môn nền tảng với ranh giới phân định rõ ràng:

1. **`hr` (Nhân sự & Đào tạo):** Quản lý hồ sơ nhân sự AI trong Agent Registry, theo dõi danh mục vai trò, đánh giá năng lực làm việc, đề xuất cấu hình nhân viên mới và hỗ trợ điều chuyển nhân sự giữa các bộ phận.
2. **`resources` (Tài nguyên & Tối ưu):** Giám sát mức sử dụng CPU, RAM, thời gian chạy và đo lường hạn mức token tiêu thụ từ nhà cung cấp; lập kế hoạch phân bổ và kiểm kê tài nguyên sau mỗi nhiệm vụ.
3. **`service` (Dịch vụ & Trải nghiệm):** Tiếp nhận và phân tích yêu cầu từ góc nhìn người dùng cuối, đánh giá mức độ đáp ứng của sản phẩm so với mục tiêu ban đầu và xử lý các ý kiến phản hồi.
4. **`marketing` (Marketing & Sáng tạo):** Nghiên cứu thị trường, xây dựng chiến lược truyền thông, sáng tạo nội dung văn bản và thiết kế thông điệp khi được cấp quyền sử dụng các công cụ phù hợp.
5. **`it` (IT & R&D):** Phát triển phần mềm, tự động hóa tác vụ kỹ thuật, tích hợp hệ thống, sửa lỗi mã nguồn trong môi trường sandbox cách ly và nghiên cứu phát triển các công cụ mới.
6. **`reception` (Phản ứng nhanh / Lễ tân):** Điểm tiếp nhận các yêu cầu phát sinh hoặc tín hiệu khẩn cấp trong quá trình vận hành, phân loại mức độ tác động, định tuyến tới các bên liên quan và báo cáo Master khi cần xử lý tức thì.
7. **`process` (Quy trình & Kỹ năng):** Thu thập kinh nghiệm và dữ liệu thực thi sau mỗi tác vụ, phát hiện các mẫu xử lý lặp lại, đề xuất chuẩn hóa quy trình và xây dựng các Skill mới vào thư mục `skills/`.
8. **`qa` (Vận hành & QA):** Quan sát tiến trình vận hành, thực hiện kiểm thử độc lập, quản lý nhật ký lỗi và thẩm định bằng chứng thực tế trước khi nghiệm thu sản phẩm.
9. **`vault` (Kho & Tri thức):** Lưu trữ lâu dài, lập chỉ mục tra cứu, quản lý phiên bản và bảo vệ các artifacts, kỹ năng đã kiểm chứng, tài liệu kỹ thuật và biên bản nghiệm thu.

Master là chủ thể điều hành trung tâm của toàn bộ hệ thống, chịu trách nhiệm nhận mục tiêu, thành lập nhóm công tác, chủ trì trao đổi và ký duyệt bàn giao sản phẩm cuối cùng. Master không thuộc về bất kỳ phòng ban chức năng nào và không tự chấm đạt thay cho QA.

### 2.3. Department Membership (Cơ chế thành viên)
Quy tắc quản lý nhân sự thuộc phòng ban:
- **Chủ quản duy nhất:** Mỗi Agent chỉ có một phòng ban chủ quản xác định để đăng ký hồ sơ tại Agent Registry và chịu sự quản lý chuyên môn của bộ phận đó.
- **Tham gia đa nhóm:** Một Agent có thể tham gia vào nhiều nhóm dự án liên phòng ban do Master chỉ định mà không làm thay đổi phòng ban chủ quản.
- **Không nhân bản lãng phí:** Khi hai phòng ban cùng cần một năng lực xử lý (ví dụ: phân tích dữ liệu dạng bảng), hệ thống ưu tiên sử dụng chung Agent sở hữu năng lực đó thay vì tạo ra hai nhân viên giống hệt nhau ở hai bộ phận.
- **Quy trình điều chuyển:** Việc chuyển Agent sang phòng ban chủ quản mới phải được cập nhật vào manifest của Agent, ghi lại lịch sử điều chuyển tại `agents/registry/` và có sự giám sát của HR.

### 2.4. Collaboration & Communication (Giao thức phối hợp)
Phương thức tương tác giữa các bộ phận:
- **Phân công nhiệm vụ:** Master phân rã mục tiêu lớn thành các nhiệm vụ con và giao việc cho các Agent thuộc các phòng ban liên quan. Các phòng ban không tự ý áp đặt chỉ tiêu ngoài thỏa thuận cho bộ phận khác.
- **Nhắn tin trực tiếp có cấu trúc:** Các Agent trong cùng nhóm tác vụ được trao đổi thông tin trực tiếp để làm rõ chi tiết kỹ thuật mà không cần chuyển tiếp qua trưởng bộ phận.
- **Hội họp khi cần thiết:** Các cuộc họp chỉ được triệu tập khi xuất hiện bài toán phức tạp đòi hỏi sự thống nhất đa chiều. Master đóng vai trò chủ tọa và chỉ mời các nhân viên có trách nhiệm trực tiếp.
- **Phản biện bằng chứng:** Mọi tranh luận kỹ thuật phải căn cứ trên số liệu thực tế, kết quả kiểm thử hoặc dữ liệu trích xuất từ công cụ, không chấp nhận việc tranh cãi suy diễn.
- **Chuyển giao sản phẩm kèm chứng cứ:** Quá trình bàn giao giữa các bộ phận phải đính kèm đường dẫn artifact, mã băm SHA-256 và nhật ký kiểm thử.
- **Leo thang xử lý:** Khi phát sinh bất đồng không thể tự hòa giải hoặc gặp rào cản kỹ thuật vượt thẩm quyền, các bên báo cáo lên Master để ra quyết định xử lý.

### 2.5. Department Autonomy (Quyền tự chủ và giới hạn)
Phân định ranh giới hành động của phòng ban:
- **Tự quyết nội bộ:** Bộ phận được toàn quyền phân tích nghiệp vụ, lựa chọn các công cụ trong danh mục cho phép và kiểm thử mã nguồn hoặc tài liệu trong phạm vi workspace được giao.
- **Phối hợp liên phòng ban:** Trao đổi dữ liệu kỹ thuật, chuyển giao sản phẩm trung gian và tham gia các phiên phản biện chung.
- **Báo cáo Master:** Bắt buộc báo cáo khi nhiệm vụ hoàn thành, khi xuất hiện lỗi nghiêm trọng không thể khắc phục, khi cạn tài nguyên hoặc khi có yêu cầu thay đổi hướng đi của dự án.
- **Yêu cầu người dùng duyệt:** Mọi hành vi nhạy cảm như xóa dữ liệu diện rộng, cài đặt gói thư viện hệ thống mới, gọi API tốn phí cao hoặc sửa đổi cấu hình an ninh đều phải tạm dừng chờ người vận hành cấp phép.
- **Các điều cấm tuyệt đối:** Phòng ban không có quyền tự thay đổi chính sách bảo mật của hệ thống, không tự ý cấp thêm quyền hạn cho Agent và không tự ý đưa mã nguồn trực tiếp vào môi trường production.

### 2.6. Dynamic Department Registry (Quản trị phòng ban động)
Cơ chế quản lý danh mục phòng ban:
- **Vị trí lưu trữ:** Toàn bộ cấu hình phòng ban được lưu trữ tại `agents/departments/{dept_id}/department.yaml`. Không tạo thư mục `departments/` độc lập ở cấp gốc repository.
- **Khả năng mở rộng:** Danh sách 9 bộ phận mặc định không phải là giới hạn cứng vĩnh viễn. Người dùng hoặc Master có thể tạo lập thêm các phòng ban mới khi phát sinh nhu cầu chuyên môn đặc thù.
- **Đăng ký và cập nhật:** Phòng ban mới được kích hoạt khi có tệp cấu hình hợp lệ theo đúng schema. Việc cập nhật trách nhiệm hay năng lực được quản lý theo phiên bản tệp.
- **Đình chỉ và hợp nhất:** Khi một phòng ban chuyển sang trạng thái `INACTIVE` hoặc `DEPRECATED`, toàn bộ Agent trực thuộc phải được điều chuyển sang phòng ban khác trước khi đóng mã định danh.

### 2.7. Coordination Boundaries (Ranh giới điều phối)
Phân định trách nhiệm cốt lõi giữa các chủ thể để loại bỏ chồng chéo:
- Master chịu trách nhiệm toàn diện về kết quả cuối cùng bàn giao cho người dùng.
- HR chịu trách nhiệm về tính hợp lệ của hồ sơ nhân viên trong Agent Registry.
- Reception chịu trách nhiệm tiếp nhận và định tuyến các tín hiệu phát sinh từ bên ngoài.
- Resources chịu trách nhiệm đo lường và cảnh báo mức độ tiêu hao tài nguyên thực tế.
- QA chịu trách nhiệm thẩm định chất lượng và cấp chứng thực nghiệm thu độc lập.
- Process chịu trách nhiệm tổng kết kinh nghiệm và đề xuất đóng gói kỹ năng tái sử dụng.
- IT chịu trách nhiệm về mặt kỹ thuật, phát triển mã nguồn và duy trì công cụ trong sandbox.
- Vault chịu trách nhiệm về tính toàn vẹn và khả năng truy xuất dữ liệu lâu dài.

## 3. Department Lifecycle & Schema

### 3.1. Bảng dữ liệu cấu hình phòng ban (Static Department Schema)

Tệp cấu hình được đặt tại `agents/departments/{department_id}/department.yaml`.

| Tên trường | Kiểu dữ liệu | Bắt buộc | Ràng buộc giá trị và mô tả |
| :--- | :--- | :--- | :--- |
| `spec_version` | String | Có | Phiên bản chuẩn đặc tả, ví dụ: `"1.0.0"`. |
| `department_id` | String | Có | Khóa chính duy nhất, định dạng `^[a-z0-9_]+$`. |
| `name` | String | Có | Tên hiển thị của bộ phận, độ dài từ 3 đến 64 ký tự. |
| `description` | String | Có | Mô tả tóm tắt phạm vi chuyên môn của bộ phận. |
| `purpose` | String | Có | Mục tiêu tồn tại chính của bộ phận trong hệ thống. |
| `responsibilities` | List[String] | Có | Danh sách các trách nhiệm cụ thể của bộ phận. |
| `supported_capabilities` | List[String] | Có | Danh mục các nhãn năng lực mà bộ phận bảo trợ. |
| `default_tools` | List[String] | Không | Danh sách công cụ được đề xuất mặc định cho nhân viên. |
| `status` | String | Có | Trạng thái hoạt động: `ACTIVE`, `INACTIVE`, `DEPRECATED`. |
| `visual_metadata.color_code` | String | Có | Mã màu HEX hiển thị (ví dụ: `"#2563EB"`). |
| `visual_metadata.icon` | String | Có | Tên định danh biểu tượng giao diện (ví dụ: `"cpu"`). |
| `visual_metadata.zone_id` | String | Có | Mã định danh khu vực trong không gian 2D/3D (ví dụ: `"zone_it_lab"`). |

### 3.2. Phân định các tầng dữ liệu

Hệ thống phân tách rõ 4 khái niệm dữ liệu liên quan đến phòng ban:
1. **Cấu hình phòng ban (Department Manifest):** Tệp cấu hình tĩnh quy định mục tiêu, trách nhiệm và danh mục năng lực bảo trợ, lưu tại `agents/departments/{department_id}/department.yaml`.
2. **Danh sách nhân sự trực thuộc (Department Roster):** Danh sách các Agent lấy `department_id` này làm phòng ban chủ quản, được truy vấn động từ Agent Registry.
3. **Nhóm công tác dự án (Task Squad):** Nhóm làm việc tạm thời do Master triệu tập gồm nhiều Agent từ các phòng ban khác nhau để giải quyết một nhiệm vụ cụ thể.
4. **Trạng thái vận hành thực tế (Runtime Task State):** Số liệu tài nguyên, tiến trình và các sự kiện diễn ra trong quá trình thực thi, lưu tại `.runtime/` và không đưa vào Git.

## 4. Hồ sơ minh họa mẫu: Phòng ban IT

Dưới đây là tệp cấu hình mẫu cho phòng ban IT (`it`), khớp hoàn toàn với schema quy định và có mối liên kết trực tiếp với vai trò `it.code_reviewer` đã định nghĩa tại `AGENT_SPEC.md`.

```yaml
spec_version: "1.0.0"
department_id: "it"
name: "IT & Nghiên cứu Phát triển"
description: "Bộ phận chịu trách nhiệm phát triển phần mềm, tích hợp kỹ thuật, quản lý công cụ và duy trì an toàn hệ thống."
purpose: "Đảm bảo hạ tầng kỹ thuật vận hành ổn định, xây dựng mã nguồn chất lượng cao và tự động hóa các quy trình công nghệ."

responsibilities:
  - "Phát triển và bảo trì mã nguồn trong môi trường sandbox cách ly."
  - "Thẩm định chất lượng mã nguồn, kiểm tra kiến trúc và rà soát an toàn thông tin."
  - "Phát triển, kiểm thử và tích hợp các công cụ thực thi mới cho hệ thống."
  - "Hỗ trợ khắc phục các sự cố kỹ thuật phát sinh trong quá trình vận hành."

supported_capabilities:
  - "software_development"
  - "git_diff_analysis"
  - "python_ast_audit"
  - "security_secret_scanning"
  - "automated_testing"
  - "tool_integration"

default_tools:
  - "tools.files.read"
  - "tools.files.write"
  - "tools.code.run_linter"
  - "tools.code.run_test"
  - "tools.code.run_ast_parser"

status: "ACTIVE"

visual_metadata:
  color_code: "#2563EB"
  icon: "code-bracket"
  zone_id: "zone_it_lab"
```

## 5. Kết luận vận hành

Tiêu chuẩn đặc tả phòng ban này hoàn thiện bức tranh tổ chức năng lực của AI Village, xác lập ranh giới rõ ràng giữa vai trò điều hành của Master và các khối chuyên môn. Cấu trúc này mở đường cho việc xây dựng Department Registry linh hoạt và tạo điều kiện cho các nhóm công tác liên phòng ban hoạt động hiệu quả.
