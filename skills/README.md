# Thư mục `skills/`: Kho Kỹ năng Nghiệp vụ và Vòng đời Kiểm chứng

Thư mục `skills/` lưu trữ các quy trình nghiệp vụ tái sử dụng của AI Village, tách biệt rõ giữa khu vực phát triển ứng viên, khu vực đã nghiệm thu và schema kiểm tra hợp lệ.

> **Trạng thái hiện tại:** Kho kỹ năng rỗng trong Chặng A. Không tạo sẵn kỹ năng ở trạng thái `APPROVED` và không sao chép lại các file template từ `docs/templates/`.

## 1. Tài liệu Tham chiếu

- Đặc tả Kỹ năng (Schema `SKILL.md`, 10 mục hợp đồng thực thi, vòng đời 7 trạng thái và Phương án C): [docs/standards/SKILL_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/SKILL_SPEC.md).

## 2. Cấu trúc Thư mục Dự kiến

- `skills/candidates/`: Lưu trữ bản thảo kỹ năng đang trong giai đoạn `CANDIDATE` hoặc `UNDER_REVIEW` chờ IT hoàn thiện và QA kiểm chứng.
- `skills/approved/`: Chỉ lưu trữ các kỹ năng đã vượt qua kiểm định độc lập ở trạng thái `APPROVED`, phân vùng bất biến theo `{skill_id}/{version}/`.
- `skills/schemas/`: Lưu trữ các tệp JSON Schema dùng để thẩm định cấu trúc frontmatter của `SKILL.md`.

## 3. Quy tắc Quản trị Vòng đời Kỹ năng

1. **Vòng đời 7 trạng thái:** `DRAFT` -> `CANDIDATE` -> `UNDER_REVIEW` -> `APPROVED` (hoặc `REJECTED`), cùng các trạng thái quản trị sau phát hành `DEPRECATED` và `DISABLED`.
2. **Điều kiện công nhận `APPROVED`:** Một kỹ năng chỉ hợp lệ khi tồn tại trong `skills/approved/`, khớp schema manifest, có biên bản kiểm thử độc lập của QA (fixture, happy path, failure path, benchmark A/B hoặc rubric), có quyết định kích hoạt hợp lệ, được đối chiếu quyền công cụ và xác minh toàn vẹn tệp. Chuỗi `status: "APPROVED"` tự khai trong YAML không có giá trị kích hoạt.
3. **Kiểm chứng QA độc lập:** Agent hoặc phòng ban đề xuất kỹ năng không được tự kiểm thử và tự cấp chứng nhận đạt.
4. **Ranh giới quyền hạn của Skill:** Skill chỉ hướng dẫn trình tự sử dụng các công cụ đã có trong `agent.tools.allowed`, tuyệt đối không tự nâng quyền, không tự mở rộng đường dẫn file và không tự động thực thi các script trong `scripts/` ngoài sandbox đã duyệt.
5. **Tự cải tiến liên tục (Phương án C):** Chu trình 9 bước bắt đầu từ ghi nhận execution log thực tế tại `.runtime/`, chỉ đề xuất kỹ năng mới tại `skills/candidates/` khi phòng Process phát hiện mẫu lặp đủ căn cứ (từ 3 đến 5 nhiệm vụ tương đồng trở lên), kiểm thử benchmark A/B bởi IT và QA, xin duyệt con người khi có quyền/chi phí mới và hỗ trợ rollback phiên bản tức thì.
