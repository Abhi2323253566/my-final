import { useEffect, useState, useCallback } from "react";
import TopBar from "@/components/TopBar";
import CreateTaskPanel from "@/components/CreateTaskPanel";
import { api } from "@/lib/api";
import { Video, Clock, History, Download, Trash2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/auth/AuthContext";

function StatusBadge({ status }) {
  return <span className={`status-badge status-${status}`} data-testid="status-badge">{status}</span>;
}

function fmt(iso) {
  if (!iso) return "-";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export default function DashboardPage() {
  const { user, refreshMe } = useAuth();
  const isAdmin = user?.role === "admin";
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
      setActive(a.data);
      setScheduled(s.data);
      setPrevious(p.data);
      refreshMe();
    } catch (e) { /* ignore */ }
  }, [date, refreshMe]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [load]);

  const onCancel = async (id) => {
    try { await api.post(`/tasks/${id}/cancel`); toast.success("Task cancelled"); load(); }
    catch { toast.error("Failed to cancel"); }
  };

  const onDelete = async (id) => {
    try { await api.delete(`/tasks/${id}`); toast.success("Deleted"); load(); }
    catch { toast.error("Delete failed"); }
  };

  const bulkDelete = async () => {
    if (selected.length === 0) return toast.message("Select rows to delete");
    try {
      await api.post("/tasks/bulk-delete", selected);
      setSelected([]);
      toast.success("Deleted selected");
      load();
    } catch { toast.error("Bulk delete failed"); }
  };

  const download = async () => {
    try {
      const { data } = await api.get("/tasks/download");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `zoom-tasks-${new Date().toISOString().split("T")[0]}.json`;
      a.click(); URL.revokeObjectURL(url);
    } catch { toast.error("Download failed"); }
  };

  return (
    <div className="zs-shell">
      <TopBar usage={user?.usage || 0} usageLimit={user?.usage_limit || 15000} switchTo="classic" />

      <div className="px-6 pb-10 grid grid-cols-1 xl:grid-cols-[1fr_420px] gap-6">
        {/* LEFT COLUMN */}
        <div className="space-y-6">
          {/* All Meetings */}
          <div className="zs-card-2 p-5" data-testid="all-meetings-section">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3">
                <div className="section-icon icon-blue"><Video size={18} /></div>
                <h2 className="text-xl font-bold text-white">All Meetings</h2>
              </div>
              <button onClick={download} className="zs-btn zs-btn-primary" data-testid="download-button">
                <Download size={14} /> Download
              </button>
            </div>
            <div className="zs-table-wrap">
              <table className="zs-table">
                <thead>
                  <tr>
                    <th>#</th><th>Meet</th><th>Qty</th><th>Joined</th>
                    {isAdmin && <th>Worker</th>}
                    <th>Start</th><th>Timeout</th>
                    <th>NM</th><th>Type</th><th>React</th><th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {active.length === 0 ? (
                    <tr className="zs-empty-row"><td colSpan={isAdmin ? 11 : 10}>No active meetings</td></tr>
                  ) : active.map((t, i) => (
                    <tr key={t.id} data-testid={`active-row-${t.id}`}>
                      <td>{i + 1}</td>
                      <td className="font-mono text-indigo-300">{t.meeting_id}</td>
                      <td>{t.members}</td>
                      <td>
                        <div className="flex items-center gap-2 min-w-[90px]">
                          <div className="flex-1 h-1.5 bg-white/10 rounded-full overflow-hidden">
                            <div className="h-full bg-emerald-500" style={{ width: `${Math.min(100, (t.joined_count / t.members) * 100)}%` }} />
                          </div>
                          <span className="text-xs font-mono text-white/80">{t.joined_count}/{t.members}</span>
                        </div>
                      </td>
                      {isAdmin && (
                        <td className="text-sky-300 text-xs">{t.worker_name || <span className="text-white/40">(none)</span>}</td>
                      )}
                      <td>{fmt(t.started_at)}</td>
                      <td>{t.timeout}s</td>
                      <td>{t.name_source}</td>
                      <td>{t.meeting_type}</td>
                      <td>{t.floating_emoji || t.participant_reactions ? "Yes" : "No"}</td>
                      <td>
                        <button onClick={() => onCancel(t.id)} className="zs-btn zs-btn-danger !py-1 !px-3 text-sm" data-testid={`cancel-active-${t.id}`}>
                          <XCircle size={14} /> Stop
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Scheduled Tasks */}
          <div className="zs-card-2 p-5" data-testid="scheduled-tasks-section">
            <div className="flex items-center gap-3 mb-4">
              <div className="section-icon icon-amber"><Clock size={18} /></div>
              <h2 className="text-xl font-bold text-white">Scheduled Tasks</h2>
            </div>
            <div className="zs-table-wrap">
              <table className="zs-table">
                <thead>
                  <tr>
                    <th>Meet ID</th><th>Members</th><th>Name</th><th>Type</th>
                    <th>React</th><th>Scheduled Time</th><th>Status</th><th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {scheduled.length === 0 ? (
                    <tr className="zs-empty-row"><td colSpan={8}>No scheduled tasks</td></tr>
                  ) : scheduled.map((t) => (
                    <tr key={t.id} data-testid={`scheduled-row-${t.id}`}>
                      <td className="font-mono text-indigo-300">{t.meeting_id}</td>
                      <td>{t.members}</td>
                      <td>{t.name_source}</td>
                      <td>{t.meeting_type}</td>
                      <td>{t.floating_emoji || t.participant_reactions ? "Yes" : "No"}</td>
                      <td>{fmt(t.scheduled_at)}</td>
                      <td><StatusBadge status={t.status} /></td>
                      <td>
                        <button onClick={() => onCancel(t.id)} className="zs-btn zs-btn-danger !py-1 !px-3 text-sm" data-testid={`cancel-sched-${t.id}`}>
                          Cancel
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Previous Tasks */}
          <div className="zs-card-2 p-5" data-testid="previous-tasks-section">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3">
                <div className="section-icon icon-indigo"><History size={18} /></div>
                <h2 className="text-xl font-bold text-white">Previous Tasks</h2>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-sm text-white/60">Filter:</span>
                <input type="date" value={date} onChange={(e) => setDate(e.target.value)}
                  className="zs-input !py-1.5 !px-2 max-w-[160px]" data-testid="previous-date-filter" />
                <button onClick={bulkDelete} className="zs-btn zs-btn-danger !py-1.5 !px-3 text-sm" data-testid="bulk-delete-button">
                  <Trash2 size={14} /> Delete
                </button>
              </div>
            </div>
            <div className="zs-table-wrap">
              <table className="zs-table">
                <thead>
                  <tr>
                    <th style={{ width: 36 }}>
                      <input type="checkbox" data-testid="select-all-previous"
                        checked={previous.length > 0 && selected.length === previous.length}
                        onChange={(e) => setSelected(e.target.checked ? previous.map((p) => p.id) : [])} />
                    </th>
                    <th>Meet ID</th><th>Members</th><th>Name</th><th>Type</th>
                    <th>React</th><th>Scheduled Time</th><th>Status</th><th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {previous.length === 0 ? (
                    <tr className="zs-empty-row"><td colSpan={9}>No completed/failed tasks for this date</td></tr>
                  ) : previous.map((t) => (
                    <tr key={t.id} data-testid={`previous-row-${t.id}`}>
                      <td>
                        <input type="checkbox" checked={selected.includes(t.id)}
                          onChange={(e) => setSelected((s) => e.target.checked ? [...s, t.id] : s.filter((x) => x !== t.id))}
                          data-testid={`select-${t.id}`} />
                      </td>
                      <td className="font-mono text-indigo-300">{t.meeting_id}</td>
                      <td>{t.members}</td>
                      <td>{t.name_source}</td>
                      <td>{t.meeting_type}</td>
                      <td>{t.floating_emoji || t.participant_reactions ? "Yes" : "No"}</td>
                      <td>{fmt(t.scheduled_at || t.started_at)}</td>
                      <td><StatusBadge status={t.status} /></td>
                      <td>
                        <button onClick={() => onDelete(t.id)} className="zs-btn zs-btn-ghost !py-1 !px-3 text-sm" data-testid={`delete-prev-${t.id}`}>
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* RIGHT COLUMN */}
        <div>
          <CreateTaskPanel onCreated={load} variant="new" />
        </div>
      </div>
    </div>
  );
}
