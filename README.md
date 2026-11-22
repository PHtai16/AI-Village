# AI Village

Hệ thống Multi-Agent vận hành theo mô hình làm việc trực quan, phân phối nhiệm vụ linh hoạt và kiểm chứng bằng công cụ độc lập.

## Mục tiêu dự án

AI Village được thiết kế để giải quyết các bài toán phức tạp thông qua sự cộng tác giữa nhiều Agent chuyên biệt. Người dùng tương tác trực tiếp với Master để giao nhiệm vụ. Master phân tích yêu cầu, thành lập đội ngũ phù hợp theo năng lực, điều phối quá trình trao đổi và tổng hợp sản phẩm bàn giao cuối cùng.

Hệ thống đặt trọng tâm vào tính xác thực: mọi kết quả, báo cáo và trạng thái đều phải dựa trên dữ liệu thực tế thu thập qua công cụ, không chấp nhận việc giả lập kết quả hoặc tự xác nhận hoàn thành khi chưa kiểm chứng.

## Kiến trúc Multi-Agent

Khác với các hệ thống đường ống cố định, AI Village xây dựng mô hình cộng tác động:

- **Điều phối linh hoạt:** Mỗi nhiệm vụ có một nhóm công tác riêng biệt được tạo lập dựa trên yêu cầu kỹ năng cụ thể. Các Agent không bị trói buộc vào một chuỗi xử lý cứng nhắc.
- **Vai trò của Master:** Master tiếp nhận mục tiêu từ người dùng, lập kế hoạch thực hiện, chỉ định nhân sự, quyết định tổ chức trao đổi nội bộ khi cần và chịu trách nhiệm nghiệm thu bàn giao. Master không thay thế bộ phận kiểm thử để tự đánh giá sản phẩm.
- **Tổ chức phòng ban theo chức năng:** Hệ thống chia thành 9 nhóm chức năng bao gồm Nhân sự & Đào tạo, Tài nguyên & Tối ưu, Dịch vụ & Trải nghiệm, Marketing & Sáng tạo, IT & R&D, Phản ứng nhanh / Lễ tân, Quy trình & Kỹ năng, Vận hành & QA, Kho & Tri thức. Mỗi Agent trực thuộc một phòng ban chủ quản nhưng có thể tham gia nhiều dự án liên phòng ban.
- **Giao tiếp ngang hàng có cấu trúc:** Các Agent trong cùng nhóm công tác được trao đổi trực tiếp để giải quyết vấn đề kỹ thuật, sau đó chuyển giao sản phẩm kèm bằng chứng thực thi.

## Nền tảng thực thi (AI Engine)

Hệ thống thiết kế tầng giao tiếp engine độc lập:

- **Antigravity:** Dự kiến là engine mặc định, chịu trách nhiệm kích hoạt công cụ thực tế, quản lý phiên làm việc và tạo ra artifact cụ thể.
- **Khả năng mở rộng Provider:** Tầng adapter cho phép tích hợp thêm các nhà cung cấp mô hình khác thông qua giao diện thống nhất, đảm bảo logic điều phối của hệ thống không phụ thuộc vào một dịch vụ duy nhất.

## Cấu trúc thư mục

Kiến trúc mã nguồn được phân định rõ ràng giữa định nghĩa, nghiệp vụ điều phối, công cụ thực thi và dữ liệu vận hành:

| Thư mục | Trách nhiệm |
| :--- | :--- |
| `agents/` | Định nghĩa nhân viên AI, vai trò, năng lực, quyền hạn và manifest phòng ban. |
| `core/` | Thuật toán điều phối, máy trạng thái nhiệm vụ, quản lý phiên, nhật ký sự kiện và kiểm soát chính sách. |
| `engines/` | Adapter kết nối Antigravity và các nhà cung cấp AI khác. |
| `tools/` | Công cụ thực thi dùng chung cho thao tác file, thực thi mã nguồn, duyệt web, tra cứu và xử lý tài liệu văn phòng. |
| `skills/` | Quy trình và kỹ năng đã kiểm chứng, có thể tái sử dụng cho nhiều Agent. |
| `storage/` | Mã truy xuất cơ sở dữ liệu, quản lý artifact, bộ nhớ dài hạn và kho tri thức. |
| `api/` | Điểm giao tiếp HTTP và truyền phát sự kiện thời gian thực giữa các thành phần. |
| `ui/` | Giao diện tương tác người dùng gồm Chat, Dashboard, phòng họp và không gian hiển thị 3D. |
| `sandbox/` | Môi trường cách ly để thực thi mã nguồn và kiểm thử an toàn. |
| `tests/` | Bộ kiểm thử tự động gồm unit test, contract test, integration test và kịch bản nghiệm thu. |
| `docs/` | Tài liệu kiến trúc, hồ sơ quyết định thiết kế (ADR) và quy trình vận hành. |
| `workspace/` | Không gian làm việc tạm thời và dữ liệu đầu vào theo từng nhiệm vụ. |
| `output/` | Sản phẩm hoàn chỉnh được bàn giao cho người dùng theo từng nhiệm vụ. |
| `.runtime/` | Dữ liệu trạng thái thực tế khi chạy gồm cơ sở dữ liệu, file ghi nhật ký, bộ nhớ đệm và phiên làm việc. |

## Nguyên tắc phát triển theo từng Phase

Dự án tuân thủ nghiêm ngặt lộ trình từng bước được quy định tại `docs/PROGRESS.md`:

1. Mỗi tiểu phase giải quyết đúng một phạm vi cụ thể với tiêu chí kiểm chứng rõ ràng.
2. Không viết mã phỏng đoán cho tương lai hoặc cài đặt thư viện khi chưa đến giai đoạn yêu cầu.
3. Chỉ xác nhận hoàn thành khi có bằng chứng thực thi, kiểm thử tương ứng vượt qua và commit Git được lưu trữ.
4. Mọi Agent tham gia phát triển phải tuân thủ hướng dẫn tại `AGENTS.md`.
