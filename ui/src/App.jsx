export default function App() {
  return (
    <main className="shell">
      <header className="header">
        <span className="badge">Stage A Scaffold</span>
        <h1>AI Village</h1>
        <p className="subtitle">
          Hệ thống Multi-Agent đa chức năng điều phối động qua Master và kiểm chứng bằng bằng chứng thực tế.
        </p>
      </header>

      <section className="card" aria-label="Trạng thái kết nối">
        <h2>Trạng thái hệ thống</h2>
        <p className="status-line">
          <strong>Runtime chưa được kết nối.</strong> Giao diện hiện tại chỉ là khung khởi đầu React + Vite ở Chặng A, chưa có dữ liệu nhiệm vụ hay phiên làm việc thật từ backend.
        </p>
      </section>

      <section className="card" aria-label="Kiến trúc phân tầng">
        <h2>Tóm tắt kiến trúc</h2>
        <ul>
          <li><code>agents/</code>: Khai báo tĩnh danh tính, năng lực và quyền hạn.</li>
          <li><code>core/</code>: Lõi điều phối, máy trạng thái nhiệm vụ và kiểm soát phân quyền.</li>
          <li><code>engines/</code>: Lớp adapter trung lập kết nối Antigravity và các AI Provider.</li>
          <li><code>tools/</code> &amp; <code>sandbox/</code>: Công cụ thực thi độc lập trong vùng cách ly.</li>
          <li><code>storage/</code> &amp; <code>api/</code>: Lưu trữ bền vững và giao tiếp HTTP/SSE.</li>
        </ul>
      </section>

      <section className="card" aria-label="Thông tin dành cho developer">
        <h2>Dành cho Developer</h2>
        <p>
          Tham chiếu tài liệu kiến trúc tại <code>docs/architecture/SYSTEM_BLUEPRINT.md</code>, đặc tả chuẩn tại <code>docs/standards/</code> và lộ trình thực thi tại <code>docs/PROGRESS.md</code>.
        </p>
        <p className="cmd-hint">
          Kiểm tra build cục bộ: <code>cd ui &amp;&amp; npm run build</code>
        </p>
      </section>
    </main>
  );
}
