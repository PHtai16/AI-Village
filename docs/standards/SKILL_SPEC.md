# Tiêu chuẩn Kỹ thuật Hệ thống Kỹ năng (Skill Specification)

Tài liệu này xác lập cấu trúc chuẩn mực cho việc định nghĩa, kiểm chứng, quản lý vòng đời và vận hành các Kỹ năng (Skills) trong hệ thống Multi-Agent AI Village. Tiêu chuẩn này làm nền tảng cho kho kỹ năng tại `skills/` và phục vụ cơ chế tự cải tiến liên tục theo Phương án C.

## 1. Bản chất và Phân định khái niệm

Trong AI Village, bốn khái niệm liên quan đến năng lực được phân biệt rõ ràng:

- **Agent:** Thực thể lao động số chịu trách nhiệm thực thi nhiệm vụ, sở hữu danh tính, vai trò, phiên làm việc, bộ nhớ và quyền hạn xác định.
- **Tool:** Công cụ thực thi hành động cơ bản do nền tảng cung cấp tại `tools/` (đọc file, chạy code trong sandbox, tìm kiếm web, truy vấn cơ sở dữ liệu). Tool thuần túy là công cụ cơ học, không chứa kịch bản giải quyết nghiệp vụ hay logic nhân vật.
- **Skill:** Bản hướng dẫn quy trình nghiệp vụ có thể tái sử dụng, mô tả cách thức kết hợp các năng lực và công cụ được cấp quyền để giải quyết một loại bài toán chuyên môn cụ thể. Skill không tự cấp quyền và không trực tiếp thực thi thao tác trên hệ điều hành; Agent Runtime và Tool Runtime mới là nơi thực thi hành động thật.
- **Capability:** Nhãn năng lực chuẩn hóa dùng trong manifest của Agent và Department để Master tìm kiếm và ghép đội ngũ công tác.

Một Skill có thể được nhiều Agent thuộc các phòng ban khác nhau sử dụng nếu Agent đó sở hữu quyền hạn công cụ tương ứng. Hệ thống không gắn chặt Skill vào một nhân viên cụ thể.

## 2. Định danh và Siêu dữ liệu Skill (Skill Identity & Metadata)

Mỗi Skill được định danh duy nhất và theo dõi phiên bản độc lập:

- `spec_version`: Phiên bản của chuẩn đặc tả kỹ năng (ví dụ: `"1.0.0"`).
- `skill_id`: Định danh duy nhất và bất biến, viết thường, ngăn cách bằng dấu gạch dưới (ví dụ: `excel_data_analysis`, `git_diff_code_review`). Mã này giữ nguyên trong suốt vòng đời của kỹ năng.
- `name`: Tên gọi hiển thị chính thức của kỹ năng (ví dụ: "Phân tích Dữ liệu Bảng tính Excel").
- `description`: Tóm tắt chính xác bài toán kỹ năng giải quyết và phạm vi áp dụng.
- `version`: Phiên bản phát hành theo chuẩn SemVer (ví dụ: `"1.0.0"`, `"1.1.0"`).
- `category`: Nhóm chuyên môn (ví dụ: `office_automation`, `code_engineering`, `research_synthesis`, `data_analysis`).
- `author`: Nguồn gốc khởi tạo (`"human_expert"`, `"process_discovery"`, `"it_development"`).
- `maintainer`: Bộ phận chịu trách nhiệm bảo trì (`"process"`, `"it"`, `"qa"`).
- `status`: Trạng thái trong vòng đời kỹ năng.

## 3. Cấu trúc lưu trữ và Schema Manifest

### 3.1. Phân định thư mục trong kho lưu trữ `skills/`
Kho kỹ năng của hệ thống được tổ chức thành 3 khu vực:
- `skills/candidates/`: Nơi lưu trữ các kỹ năng mới được đề xuất hoặc đang trong giai đoạn phát triển và kiểm thử độc lập.
- `skills/approved/`: Nơi lưu trữ các kỹ năng đã vượt qua bài kiểm thử độc lập của QA, được Master đề xuất và người vận hành phê duyệt.
- `skills/schemas/`: Nơi lưu trữ các tệp JSON Schema dùng để kiểm tra tính hợp lệ của manifest.

### 3.2. Cấu trúc gói kỹ năng
Một gói kỹ năng độc lập được đóng gói trong một thư mục với cấu trúc chuẩn:

```text
{skill_id}/
├── SKILL.md          # Bắt buộc: Manifest YAML frontmatter kết hợp nội dung hướng dẫn Markdown
├── scripts/          # Tùy chọn: Mã Python/Shell hỗ trợ tự động hóa trong sandbox
├── references/       # Tùy chọn: Tài liệu tham chiếu, biểu mẫu hoặc tài liệu tra cứu
└── tests/            # Bắt buộc: Dữ liệu mẫu, kịch bản kiểm thử độc lập và benchmark
```

### 3.3. Cấu trúc tệp `SKILL.md` (Manifest Schema)

Tệp `SKILL.md` sử dụng cấu trúc YAML Frontmatter ở đầu tệp và nội dung hướng dẫn chi tiết bằng Markdown ở phần thân:

| Tên trường (Frontmatter) | Kiểu dữ liệu | Bắt buộc | Ràng buộc giá trị và mô tả |
| :--- | :--- | :--- | :--- |
| `spec_version` | String | Có | Phiên bản của chuẩn đặc tả, ví dụ: `"1.0.0"`. |
| `id` | String | Có | Trùng khớp với `skill_id`, định dạng `^[a-z0-9_]+$`. |
| `name` | String | Có | Tên hiển thị, độ dài từ 3 đến 64 ký tự. |
| `version` | String | Có | Phiên bản SemVer hợp lệ, định dạng `MAJOR.MINOR.PATCH`. |
| `description` | String | Có | Mô tả súc tích phạm vi giải quyết của kỹ năng. |
| `category` | String | Có | Nhóm chuyên môn chuẩn hóa của hệ thống. |
| `author` | String | Có | Định danh tác giả khởi tạo. |
| `maintainer` | String | Có | Phòng ban chịu trách nhiệm bảo trì (`it`, `process`, `qa`). |
| `status` | String | Có | Một trong các trạng thái vòng đời hợp lệ. |
| `requirements.tools` | List[String] | Có | Danh sách định danh công cụ bắt buộc phải có từ `tools/`. |
| `requirements.dependencies` | List[String] | Không | Các thư viện mã nguồn cần thiết trong môi trường sandbox. |
| `requirements.min_engine_tier`| String | Không | Nhóm mô hình tối thiểu khuyến nghị (`flash`, `pro`, `code`). |

## 4. Hợp đồng thực thi (Execution Contract)

Phần thân Markdown của tệp `SKILL.md` bắt buộc phải chứa các mục hợp đồng sau:

1. **Mục đích và Phạm vi:** Giải thích rõ kỹ năng giải quyết bài toán gì và trường hợp áp dụng.
2. **Điều kiện kích hoạt (Triggers):** Các tiêu chí cụ thể để Agent quyết định sử dụng kỹ năng này dựa trên đề bài của nhiệm vụ.
3. **Điều kiện cấm kích hoạt (Anti-triggers):** Các tình huống không được sử dụng kỹ năng (ví dụ: dữ liệu quá nhỏ có thể đọc trực tiếp, bài toán yêu cầu giải pháp khác) nhằm tiết kiệm token và thời gian chạy.
4. **Đầu vào cam kết (Inputs):** Định dạng, cấu trúc và đường dẫn của dữ liệu đầu vào.
5. **Đầu ra cam kết (Outputs):** Định dạng, quy cách tệp tin và các trường dữ liệu kết quả phải tạo ra.
6. **Quy trình thực thi từng bước (Execution Steps):** Các bước hướng dẫn tuần tự và rõ ràng: từ bước thu thập dữ liệu, phân tích, gọi công cụ trong sandbox, đến bước kiểm tra và đóng gói.
7. **Ranh giới an toàn và Quyền hạn:** Giới hạn đường dẫn tệp tin và lệnh hệ thống được phép dùng.
8. **Tiêu chí hoàn thành (Definition of Done):** Điều kiện định lượng để xác nhận kỹ năng đã hoàn thành nhiệm vụ.
9. **Yêu cầu bằng chứng kiểm chứng (Evidence):** Danh mục bằng chứng bắt buộc phải sinh ra (mã băm SHA-256 của file đầu ra, log kiểm thử, kết quả đo lường).
10. **Xử lý sự cố và Khôi phục (Error Handling):** Hướng dẫn xử lý khi gặp lỗi định dạng, lỗi công cụ hoặc kết quả không thỏa mãn tiêu chuẩn.

## 5. Cơ chế tìm kiếm và Lựa chọn Kỹ năng (Skill Selection & Discovery)

Hệ thống phân phối kỹ năng theo cơ chế kiểm soát chặt chẽ:

- **Tra cứu theo nhãn năng lực:** Master hoặc Worker Agent tra cứu kỹ năng dựa trên `category` và `capabilities.skills` khớp với yêu cầu của nhiệm vụ.
- **Kiểm tra trạng thái kích hoạt:** Chỉ các kỹ năng có trạng thái `APPROVED` trong thư mục `skills/approved/` mới được phép đưa vào phiên làm việc chính thức.
- **Đối chiếu quyền hạn thực thi:** Agent chỉ được nạp kỹ năng nếu toàn bộ công cụ yêu cầu trong `requirements.tools` nằm trong danh mục `tools.allowed` của Agent Manifest.
- **Tinh gọn ngữ cảnh:** Hệ thống không nạp toàn bộ danh mục kỹ năng vào context window của Agent. Chỉ tệp `SKILL.md` của kỹ năng được lựa chọn mới được truyền vào phiên làm việc khi nhiệm vụ bắt đầu.

## 6. Ranh giới quyền hạn và An toàn nội dung (Permissions & Security)

Quy định an toàn tuyệt đối cho hệ thống kỹ năng:

- **Không tự nâng quyền:** Kỹ năng chỉ là tài liệu hướng dẫn quy trình, tuyệt đối không có quyền tự mở rộng thẩm quyền hoặc cấp thêm quyền truy cập tài nguyên cho Agent. Mọi hành động gọi công cụ đều bị Permission Manager kiểm soát dựa trên manifest của Agent và ranh giới workspace.
- **Cách ly không gian làm việc:** Mọi thao tác đọc và ghi tệp của kỹ năng phải giới hạn trong `workspace/{task_id}/` hoặc `output/{task_id}/`.
- **Phòng chống tiêm mã và hướng dẫn độc hại:** Nội dung trong thư mục `references/` phải được đối xử như dữ liệu người dùng không tin cậy. Agent không được chuyển hóa các đoạn văn bản tham khảo thành mệnh lệnh thực thi hệ thống.
- **Kiểm soát script tự động:** Không được tự ý chạy các script trong `scripts/` trên máy chủ chủ quản. Toàn bộ script phải chạy trong môi trường sandbox cách ly và phải có chữ ký kiểm duyệt của bộ phận IT và QA.
- **Bảo vệ bí mật:** Nghiêm cấm mọi kỹ năng yêu cầu truy xuất hoặc ghi lại các khóa API, tệp `.env` hoặc thông tin xác thực.

## 7. Vòng đời Kỹ năng (Skill Lifecycle)

Một kỹ năng trải qua các trạng thái quản trị sau:

1. **`DRAFT`:** Kỹ năng đang trong giai đoạn soạn thảo ban đầu bởi chuyên gia hoặc bộ phận Process.
2. **`CANDIDATE`:** Bản thảo đã hoàn tất cấu trúc, được đưa vào `skills/candidates/` để chuyển giao cho IT và QA chuẩn bị kiểm thử.
3. **`UNDER_REVIEW`:** Kỹ năng đang chạy các bài kiểm tra độc lập và đo lường benchmark trong môi trường thử nghiệm.
4. **`APPROVED`:** Kỹ năng đã vượt qua toàn bộ tiêu chuẩn kiểm chứng của QA, được Master phê duyệt và chuyển vào `skills/approved/` để sử dụng live.
5. **`REJECTED`:** Kỹ năng bị từ chối do không vượt qua bài kiểm thử, vi phạm an toàn hoặc không chứng minh được hiệu quả thực tế.
6. **`DEPRECATED`:** Kỹ năng đã có phiên bản thay thế mới hơn, tiếp tục hỗ trợ các tác vụ cũ nhưng không khuyến nghị cho các tác vụ mới.
7. **`DISABLED`:** Kỹ năng bị vô hiệu hóa khẩn cấp do phát hiện lỗi nghiêm trọng, nguy cơ rủi ro hoặc thay đổi chính sách hệ thống.

Trạng thái vòng đời của kỹ năng là đối tượng quản lý của kho lưu trữ tri thức, hoàn toàn độc lập với trạng thái thực thi của từng nhiệm vụ trong Task Lifecycle.

## 8. Quy trình tự cải tiến liên tục: Phương án C (Continuous Improvement)

Hệ thống hiện thực hóa khả năng tự học hỏi thông qua chu trình 9 bước có kiểm soát chặt chẽ:

1. **Ghi nhận thực nghiệm:** Sau mỗi nhiệm vụ, Agent Runtime ghi nhận đầy đủ execution log gồm các bước thực hiện, công cụ đã gọi, thời gian xử lý, lỗi gặp phải và giải pháp khắc phục vào `.runtime/`.
2. **Phân tích mẫu lặp:** Bộ phận Process định kỳ quét nhật ký tác vụ để phát hiện các quy trình thao tác lặp lại nhiều lần hoặc các lỗi phổ biến có tính quy luật (chỉ phân tích khi có bằng chứng từ ít nhất 3 đến 5 nhiệm vụ tương đồng).
3. **Đề xuất kỹ năng ứng viên:** Process soạn thảo tài liệu `SKILL.md` cho kỹ năng mới và lưu vào `skills/candidates/{skill_id}/` với trạng thái `CANDIDATE`.
4. **Hỗ trợ kỹ thuật:** Bộ phận IT rà soát quy trình, xây dựng các script hỗ trợ nếu cần và kiểm tra tính tương thích công cụ trong sandbox.
5. **Thẩm định độc lập:** Bộ phận QA thiết lập test fixture, chạy các ca kiểm thử tự động độc lập và đo lường benchmark A/B so sánh giữa việc dùng kỹ năng mới với cách làm thủ công trước đó.
6. **Đánh giá giá trị:** Master xem xét báo cáo nghiệm thu của QA, đánh giá tính hữu ích thực tế và lập đề xuất kích hoạt.
7. **Phê duyệt thẩm quyền:** Người vận hành hệ thống trực tiếp phê duyệt nếu kỹ năng mới có yêu cầu công cụ mới, phát sinh chi phí hoặc thay đổi hành vi nhạy cảm.
8. **Kích hoạt phát hành:** Kỹ năng được chuyển sang thư mục `skills/approved/{skill_id}/` với số hiệu phiên bản SemVer cố định và cập nhật vào Skill Registry.
9. **Cơ chế khôi phục (Rollback):** Nếu phiên bản kỹ năng mới gây ra lỗi hoặc suy giảm hiệu năng trong quá trình chạy thực tế, Master có quyền hạ cấp và chuyển active version về phiên bản ổn định trước đó ngay lập tức.

Hệ thống không yêu cầu phải tạo kỹ năng sau mọi nhiệm vụ. Nghiêm cấm Agent tự ý sửa đổi và kích hoạt trực tiếp các kỹ năng đang vận hành trong môi trường production.

## 9. Thẩm định độc lập và Bằng chứng kiểm chứng (Testing & Evidence)

Mọi kỹ năng trước khi được duyệt đều phải có bộ kiểm thử độc lập tại `tests/`:

- **Test Fixture:** Bộ dữ liệu đầu vào mẫu đại diện cho các tình huống thực tế của bài toán.
- **Happy Path Cases:** Các kịch bản kiểm tra trường hợp đầu vào chuẩn xác, xác minh sản phẩm đầu ra khớp hoàn toàn với cam kết.
- **Failure Cases:** Các kịch bản kiểm tra trường hợp đầu vào bị lỗi, thiếu file hoặc dữ liệu bất thường để xác nhận cơ chế xử lý lỗi hoạt động ổn định.
- **Đo lường Benchmark:** Đối với kỹ năng cải tiến quy trình, phải có số liệu đo lường định lượng cụ thể: thời gian xử lý giảm bao nhiêu phần trăm, số bước gọi công cụ giảm như thế nào.
- **Tiêu chí định tính:** Đối với các kỹ năng sáng tạo nội dung hoặc tổng hợp tài liệu khó đo bằng công thức, QA và Service phải thiết lập rubric đánh giá định tính cụ thể dựa trên tiêu chí người dùng.
- **Biên bản bằng chứng:** Kết quả kiểm thử phải được lập thành báo cáo QA đính kèm mã băm SHA-256 của các tệp kết quả kiểm tra. Tuyệt đối không chấp nhận lời tự tuyên bố đạt của Agent lập trình.

## 10. Phiên bản hóa và Khôi phục (Versioning & Rollback)

Quy tắc quản lý phiên bản đảm bảo hệ thống không bị gián đoạn:

- **Tuân thủ SemVer:** Tăng `MAJOR` khi thay đổi hợp đồng đầu vào/đầu ra làm mất tương thích; tăng `MINOR` khi bổ sung tính năng hoặc bước xử lý mới giữ tương thích; tăng `PATCH` khi sửa lỗi diễn đạt hoặc tối ưu hóa nhỏ.
- **Tính bất biến của bản phát hành:** Các tệp tin trong `skills/approved/{skill_id}/{version}/` là bất biến. Mọi sự thay đổi bắt buộc phải được tạo thành một phiên bản mới.
- **Không làm gián đoạn tác vụ đang chạy:** Các nhiệm vụ đang thực thi được gắn chặt với phiên bản kỹ năng tại thời điểm khởi tạo, việc cập nhật phiên bản mới không làm ảnh hưởng đến tiến trình của các tác vụ hiện hành.
- **Quy trình Rollback:** Khi phát sinh lỗi hồi quy, hệ thống chỉ cần cập nhật con trỏ phiên bản khuyến nghị trong Agent Manifest hoặc Department Manifest quay về phiên bản trước mà không cần xóa mã nguồn.

## 11. Phân định trách nhiệm giữa các phòng ban

Cơ chế kiểm soát đa bên bảo đảm tính minh bạch trong quản trị kỹ năng:

- **Master:** Đánh giá giá trị thực tế của kỹ năng đối với mục tiêu chung, ra quyết định kích hoạt và chỉ đạo nhóm tác vụ sử dụng.
- **HR:** Cập nhật danh mục kỹ năng được phép vào hồ sơ của các vai trò trong Agent Registry.
- **Process:** Khảo sát nhật ký vận hành, phát hiện nhu cầu cải tiến, chủ trì soạn thảo bản thảo kỹ năng ứng viên.
- **IT:** Lập trình các script tự động hóa hỗ trợ, kiểm tra hạ tầng sandbox và đảm bảo tính tương thích kỹ thuật.
- **QA:** Thiết kế bài kiểm tra, thực hiện kiểm thử độc lập, đo lường benchmark và cấp chứng thực nghiệm thu khách quan.
- **Vault:** Quản lý kho lưu trữ lâu dài, duy trì chỉ mục tra cứu và bảo vệ tính toàn vẹn của các gói kỹ năng.

Mô hình này ngăn chặn triệt để tình trạng một bộ phận duy nhất tự đề xuất, tự viết mã, tự chấm đạt và tự kích hoạt kỹ năng.

## 12. Hồ sơ minh họa mẫu

Dưới đây là hai ví dụ thiết kế mẫu tuân thủ đúng chuẩn đặc tả. Các ví dụ này phục vụ việc chuẩn hóa cấu trúc, chưa đại diện cho các kỹ năng đã được kích hoạt trong runtime.

### 12.1. Ví dụ 1: Kỹ năng Phân tích Dữ liệu Bảng tính (`excel_data_analysis`)

```markdown
---
spec_version: "1.0.0"
id: "excel_data_analysis"
name: "Phân tích Dữ liệu Bảng tính Excel"
version: "1.0.0"
description: "Trích xuất, làm sạch, tính toán thống kê và lập báo cáo phân tích tự động từ các tệp dữ liệu bảng tính."
category: "office_automation"
author: "human_expert"
maintainer: "it"
status: "APPROVED"
requirements:
  tools:
    - "tools.files.read"
    - "tools.files.write"
    - "tools.code.run_script"
  dependencies:
    - "openpyxl"
    - "pandas"
  min_engine_tier: "code"
---

# Hướng dẫn Kỹ năng: Phân tích Dữ liệu Bảng tính Excel

## 1. Mục đích và Phạm vi
Kỹ năng này hướng dẫn Agent cách xử lý các tệp dữ liệu bảng tính lớn (XLSX, CSV) một cách an toàn bằng code trong sandbox, tránh việc nạp toàn bộ dữ liệu thô vào context window.

## 2. Điều kiện kích hoạt và Cấm kích hoạt
- **Kích hoạt khi:** Nhiệm vụ yêu cầu tính toán tổng hợp, trích xuất chỉ số thống kê hoặc tạo biểu đồ từ tệp dữ liệu dạng bảng có từ 50 dòng trở lên.
- **Cấm kích hoạt khi:** Dữ liệu đầu vào chỉ có dưới 20 dòng có thể đọc trực tiếp; hoặc nhiệm vụ yêu cầu chỉnh sửa định dạng văn bản thuần túy không có tính toán.

## 3. Hợp đồng Đầu vào và Đầu ra
- **Đầu vào:** Đường dẫn tệp `.xlsx` hoặc `.csv` hợp lệ nằm trong `workspace/{task_id}/inputs/`.
- **Đầu ra:** Tệp báo cáo tổng hợp `analysis_report.md` và tệp dữ liệu đã làm sạch `processed_data.xlsx` trong `workspace/{task_id}/outputs/`.

## 4. Quy trình thực hiện
1. **Khảo sát cấu trúc:** Sử dụng công cụ `tools.code.run_script` để chạy đoạn mã Python nhỏ in ra tên cột, số dòng và kiểu dữ liệu của tệp bảng tính.
2. **Làm sạch dữ liệu:** Xử lý các giá trị khuyết thiếu và chuẩn hóa định dạng ngày tháng, số liệu.
3. **Tính toán số liệu:** Thực hiện tính toán các chỉ số thống kê mô tả (tổng, trung bình, phân vị) bằng thư viện `pandas` trong sandbox.
4. **Lập báo cáo:** Viết kết quả phân tích vào tệp `analysis_report.md` kèm bảng tóm tắt chỉ số và kết luận thực tế.
5. **Xác minh kết quả:** Đối chiếu lại tổng giá trị số học độc lập để đảm bảo không có sai lệch công thức.

## 5. Ranh giới an toàn và Bằng chứng
- Toàn bộ script phải chạy trong môi trường sandbox cách ly, không được truy cập mạng.
- Bằng chứng nghiệm thu gồm: mã băm SHA-256 của tệp báo cáo đầu ra và log thực thi lệnh Python không có lỗi.

## 6. Xử lý sự cố
- Khi tệp bị khóa hoặc định dạng sai: Báo cáo lỗi cấu trúc bảng tính và dừng lại để xin tệp chuẩn, không tự ý suy đoán dữ liệu thiếu.
```

### 12.2. Ví dụ 2: Kỹ năng Thẩm định Chất lượng Mã nguồn (`git_diff_code_review`)

```markdown
---
spec_version: "1.0.0"
id: "git_diff_code_review"
name: "Thẩm định Chất lượng Mã nguồn qua Git Diff"
version: "1.0.0"
description: "Phân tích các thay đổi trong mã nguồn, kiểm tra tuân thủ kiến trúc, phát hiện lỗi bảo mật và vi phạm chuẩn code."
category: "code_engineering"
author: "human_expert"
maintainer: "qa"
status: "APPROVED"
requirements:
  tools:
    - "tools.files.read"
    - "tools.code.run_linter"
    - "tools.code.run_ast_parser"
  dependencies:
    - "ruff"
    - "pytest"
  min_engine_tier: "code"
---

# Hướng dẫn Kỹ năng: Thẩm định Chất lượng Mã nguồn qua Git Diff

## 1. Mục đích và Phạm vi
Hướng dẫn Agent rà soát độc lập các tệp thay đổi trong mã nguồn trước khi tích hợp vào nhánh chính, đảm bảo mã sạch và không rò rỉ thông tin nhạy cảm.

## 2. Điều kiện kích hoạt và Cấm kích hoạt
- **Kích hoạt khi:** Có yêu cầu kiểm tra mã nguồn, rà soát git diff, chuẩn bị commit hoặc nghiệm thu tính năng mới.
- **Cấm kích hoạt khi:** Nhiệm vụ yêu cầu trực tiếp lập trình tính năng mới hoặc sửa lỗi nghiệp vụ.

## 3. Hợp đồng Đầu vào và Đầu ra
- **Đầu vào:** Nội dung `git diff` hoặc danh sách tệp mã nguồn thay đổi trong `workspace/{task_id}/`.
- **Đầu ra:** Biên bản đánh giá chất lượng `code_review_report.md` và mã lỗi phát hiện (nếu có) dạng JSON.

## 4. Quy trình thực hiện
1. **Kiểm tra an toàn bí mật:** Quét các tệp thay đổi để phát hiện các chuỗi khóa bí mật, mật khẩu hoặc khóa cá nhân.
2. **Kiểm tra cú pháp và định dạng:** Chạy công cụ `tools.code.run_linter` trên các tệp thay đổi để tìm lỗi PEP 8 hoặc lỗi logic tiềm ẩn.
3. **Đối chiếu ranh giới kiến trúc:** So sánh các tệp chỉnh sửa với quy định phân tầng trong tài liệu `SYSTEM_BLUEPRINT.md`.
4. **Kiểm tra độ bao phủ kiểm thử:** Xác nhận mỗi thay đổi mã nguồn logic đều có ca kiểm thử đơn vị tương ứng đi kèm.
5. **Tổng hợp biên bản:** Ghi nhận rõ các điểm đạt, điểm vi phạm và phân loại mức độ rủi ro (CRITICAL, WARNING, INFO).

## 5. Ranh giới an toàn và Bằng chứng
- Agent thực hiện thẩm định không được phép tự sửa mã nguồn của lập trình viên.
- Bằng chứng nghiệm thu gồm: log chạy công cụ linter và biên bản review có chữ ký nghiệm thu của QA.

## 6. Xử lý sự cố
- Khi phát hiện rò rỉ secret: Ngay lập tức gắn nhãn `BLOCKING_SECURITY_ISSUE`, phát thông báo khẩn cấp và từ chối nghiệm thu tác vụ.
```

## 13. Kết luận vận hành

Tiêu chuẩn đặc tả kỹ năng này thiết lập khung làm việc chuẩn xác cho việc tích lũy tri thức và tự cải tiến liên tục trong AI Village. Việc phân định ranh giới giữa quy trình hướng dẫn của Skill và quyền thực thi công cụ của Agent bảo đảm hệ thống luôn vận hành trong ranh giới an toàn, có thể kiểm chứng và sẵn sàng cho các giai đoạn phát triển tiếp theo.
