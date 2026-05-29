import { useEffect, useState, useCallback } from "react";
import TopBar from "@/components/TopBar";
import CreateTaskPanel from "@/components/CreateTaskPanel";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { useAuth } from "@/auth/AuthContext";

function fmt(iso) { if (!iso) return "-"; try { return new Date(iso).toLocaleString(); } catch { return iso; } }
function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export default function ClassicDashboardPage() {
  const { user, refreshMe } = useAuth();
  const [active, setActive] = useState([]);
  const [scheduled, setScheduled] = useState([]);
  const [previous, setPrevious] = useState([]);
  const [date, setDate] = useState(todayIso());
  const [selected, setSelected] = useState([]);

  const load = useCallback(async () => {
    try {
      const [a, s, p] = await Promise.all([
        api.get("/tasks/active"),
        api.get("/tasks/scheduled"),
        api.get("/tasks/previous", { params: { date } }),
      ]);
      setActive(a.data); setScheduled(s.data); setPrevious(p.data);
      refreshMe();
    } catch {}
  }, [date, refreshMe]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { const id = setInterval(load, 10000); return () => clearInterval(id); }, [load]);

  const cancel = async (id) => { try { await api.post(`/tasks/${id}/cancel`); load(); } catch { toast.error("Failed"); } };
  const del = async (id) => { try { await api.delete(`/tasks/${id}`); load(); } catch { toast.error("Failed"); } };
  const download = async () => {
    try {
      const { data } = await api.get("/tasks/download");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = `zoom-tasks.json`; a.click();
      URL.revokeObjectURL(url);
    } catch { toast.error("Download failed"); }
  };
  const bulkDelete = async () => {
    if (selected.length === 0) return;
    try { await api.post("/tasks/bulk-delete", selected); setSelected([]); load(); } catch { toast.error("Failed"); }
  };

  return (
    <div className="zs-shell">
      <TopBar usage={user?.usage || 0} usageLimit={user?.usage_limit || 15000} switchTo="new" />

      <div className="px-6 pb-10 grid grid-cols-1 xl:grid-cols-[1fr_460px] gap-6">
        <div className="space-y-6">
          {/* All Meetings - Classic */}
          <section className="zs-card-2 p-5">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-2xl font-bold text-white">All Meetings</h2>
              <button onClick={download} className="zs-btn zs-btn-primary uppercase tracking-wider text-sm" data-testid="classic-download">
                Download Detail's
              </button>
            </div>
            <table className="zs-table zs-table-classic w-full">
              <thead>
                <tr>
                  <th>#</th><th>Meet</th><th>Qty</th><th>Start</th><th>Time out</th>
                  <th>Nm</th><th>Type</th><th>React</th><th>Action</th>
                </tr>
              </thead>
              <tbody>
                {active.length === 0 ? (
                  <tr><td colSpan={9} className="text-center text-white/50 py-6">No active meetings</td></tr>
                ) : active.map((t, i) => (
                  <tr key={t.id}>
                    <td>{i + 1}</td>
                    <td className="font-mono text-indigo-300">{t.meeting_id}</td>
                    <td>{t.members}</td>
                    <td>{fmt(t.started_at)}</td>
                    <td>{t.timeout}s</td>
                    <td>{t.name_source}</td>
                    <td>{t.meeting_type}</td>
                    <td>{t.floating_emoji || t.participant_reactions ? "Yes" : "No"}</td>
                    <td><button onClick={() => cancel(t.id)} className="zs-btn zs-btn-danger !py-1 !px-3 text-sm">Stop</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {/* Scheduled - Classic */}
          <section className="zs-card-2 p-5">
            <h2 className="text-2xl font-bold text-white mb-3">Scheduled Tasks</h2>
            <table className="zs-table zs-table-classic w-full">
              <thead>
                <tr>
                  <th>Meet ID</th><th>Members</th><th>Name</th><th>Type</th>
                  <th>React</th><th>Scheduled Time</th><th>Status</th><th>Action</th>
                </tr>
              </thead>
              <tbody>
                {scheduled.length === 0 ? (
                  <tr><td colSpan={8} className="text-center text-white/50 py-6">No scheduled tasks</td></tr>
                ) : scheduled.map((t) => (
                  <tr key={t.id}>
                    <td className="font-mono text-indigo-300">{t.meeting_id}</td>
                    <td>{t.members}</td>
                    <td>{t.name_source}</td>
                    <td>{t.meeting_type}</td>
                    <td>{t.floating_emoji || t.participant_reactions ? "Yes" : "No"}</td>
                    <td>{fmt(t.scheduled_at)}</td>
                    <td>{t.status}</td>
                    <td><button onClick={() => cancel(t.id)} className="zs-btn zs-btn-danger !py-1 !px-3 text-sm">Cancel</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {/* Previous - Classic */}
          <section className="zs-card-2 p-5">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-2xl font-bold text-white">Previous Scheduled Tasks</h2>
              <div className="flex items-center gap-2">
                <span className="text-sm text-white/70">Filter Date</span>
                <input type="date" value={date} onChange={(e) => setDate(e.target.value)} className="zs-input-classic max-w-[160px]" />
                <button onClick={bulkDelete} className="zs-btn zs-btn-danger !py-1 !px-3 text-sm">Delete</button>
              </div>
            </div>
            <table className="zs-table zs-table-classic w-full">
              <thead>
                <tr>
                  <th style={{ width: 36 }}>
                    <input type="checkbox"
                      checked={previous.length > 0 && selected.length === previous.length}
                      onChange={(e) => setSelected(e.target.checked ? previous.map((p) => p.id) : [])}
                    />
                  </th>
                  <th>Meet ID</th><th>Members</th><th>Name</th><th>Type</th>
                  <th>React</th><th>Scheduled Time</th><th>Status</th><th>Action</th>
                </tr>
              </thead>
              <tbody>
                {previous.length === 0 ? (
                  <tr><td colSpan={9} className="text-center text-white/50 py-6">No completed/failed tasks for this date</td></tr>
                ) : previous.map((t) => (
                  <tr key={t.id}>
                    <td><input type="checkbox" checked={selected.includes(t.id)}
                      onChange={(e) => setSelected((s) => e.target.checked ? [...s, t.id] : s.filter((x) => x !== t.id))} /></td>
                    <td className="font-mono text-indigo-300">{t.meeting_id}</td>
                    <td>{t.members}</td>
                    <td>{t.name_source}</td>
                    <td>{t.meeting_type}</td>
                    <td>{t.floating_emoji || t.participant_reactions ? "Yes" : "No"}</td>
                    <td>{fmt(t.scheduled_at || t.started_at)}</td>
                    <td>{t.status}</td>
                    <td><button onClick={() => del(t.id)} className="zs-btn zs-btn-ghost !py-1 !px-3 text-sm">Delete</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </div>

        <div>
          <CreateTaskPanel onCreated={load} variant="classic" />
        </div>
      </div>
    </div>
  );
}
