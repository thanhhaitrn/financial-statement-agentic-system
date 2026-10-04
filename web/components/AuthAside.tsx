export default function AuthAside() {
  const bars = [38, 52, 46, 64, 58, 78, 92];
  return (
    <aside className="auth-aside" aria-hidden="true">
      <div>
        <p className="aside-quote">
          Mỗi con số đều có <em>nguồn gốc</em> rõ ràng.
        </p>
        <div className="aside-bars">
          {bars.map((h, i) => (
            <span key={i} style={{ height: `${h}%`, animationDelay: `${0.4 + i * 0.09}s` }} />
          ))}
        </div>
      </div>
    </aside>
  );
}
