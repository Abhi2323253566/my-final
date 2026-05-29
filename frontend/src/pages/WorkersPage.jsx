import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import TopBar from "@/components/TopBar";
import { api, formatApiErrorDetail } from "@/lib/api";
import { Server, Plus, Trash2, Copy, Cpu, MemoryStick, Activity, X, KeyRound, Download, Pencil, HeartPulse, Zap, AlertTriangle, ShieldCheck, RefreshCw, Layers } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/auth/AuthContext";

function fmt(iso) { if (!iso) return "never"; try { return new Date(iso).toLocaleString(); } catch { return iso; } }

// "2 min ago" / "1 hr ago" style relative time. Used by the Stability column
// so the admin can immediately see how recently a flaky RDP last restarted.
function relTime(iso) {
  if (!iso) return null;
  try {
    const d = new Date(iso);
    const sec = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (sec < 60) return `${Math.floor(sec)}s ago`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return `${Math.floor(sec / 86400)}d ago`;
  } catch { return null; }
}

export default function WorkersPage() {
  const { user } = useAuth();
  const [workers, setWorkers] = useState([]);
  const [fleet, setFleet] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState("");
  const [newCap, setNewCap] = useState(80);
  const [creating, setCreating] = useState(false);
  const [createdToken, setCreatedToken] = useState(null); // { token, name }
  const [editTarget, setEditTarget] = useState(null);
  const [editName, setEditName] = useState("");
  const [editCap, setEditCap] = useState(80);
  const [savingEdit, setSavingEdit] = useState(false);
  // Bulk Set Capacity
  const [showBulk, setShowBulk] = useState(false);
  const [bulkCap, setBulkCap] = useState(80);
  const [bulkSelected, setBulkSelected] = useState(() => new Set());
  const [bulkSaving, setBulkSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [{ data }, healthRes] = await Promise.all([
        api.get("/workers"),
        api.get("/admin/fleet-health").catch(() => ({ data: null })),
      ]);
      setWorkers(data);
      if (healthRes.data) setFleet(healthRes.data);
    } catch (e) { /* ignore */ }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { const id = setInterval(load, 8000); return () => clearInterval(id); }, [load]);

  const create = async (e) => {
    e?.preventDefault?.();
    if (!newName.trim()) return toast.error("Worker name required");
    setCreating(true);
    try {
      const { data } = await api.post("/workers", { name: newName.trim(), capacity_max: parseInt(newCap, 10) || 80 });
      setCreatedToken({ token: data.token, name: data.name });
      setShowAdd(false);
      setNewName(""); setNewCap(80);
      load();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally { setCreating(false); }
  };

  const remove = async (id, name) => {
    if (!window.confirm(`Delete worker "${name}"? Tasks assigned to it will be unassigned.`)) return;
    try { await api.delete(`/workers/${id}`); toast.success("Worker deleted"); load(); }
    catch { toast.error("Delete failed"); }
  };

  const openEdit = (w) => {
    setEditTarget(w);
    setEditName(w.name);
    setEditCap(w.capacity_max);
  };

  const saveEdit = async (e) => {
    e?.preventDefault?.();
    if (!editTarget) return;
    if (!editName.trim()) return toast.error("Name required");
    const cap = parseInt(editCap, 10);
    if (!cap || cap < 1) return toast.error("Capacity must be >= 1");
    setSavingEdit(true);
    try {
      await api.patch(`/workers/${editTarget.id}`, { name: editName.trim(), capacity_max: cap });
      toast.success("Worker updated");
      setEditTarget(null);
      load();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally {
      setSavingEdit(false);
    }
  };

  const openBulk = () => {
    // Pre-select ALL workers by default (most common use case).
    setBulkSelected(new Set(workers.map((w) => w.id)));
    setBulkCap(80);
    setShowBulk(true);
  };

  const toggleBulkOne = (id) => {
    setBulkSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleBulkAll = () => {
    setBulkSelected((prev) =>
      prev.size === workers.length ? new Set() : new Set(workers.map((w) => w.id))
    );
  };

  const saveBulk = async (e) => {
    e?.preventDefault?.();
    const cap = parseInt(bulkCap, 10);
    if (!cap || cap < 1) return toast.error("Capacity must be >= 1");
    if (bulkSelected.size === 0) return toast.error("Select at least one worker");
    setBulkSaving(true);
    try {
      const targets = workers.filter((w) => bulkSelected.has(w.id));
      const results = await Promise.allSettled(
        targets.map((w) =>
          api.patch(`/workers/${w.id}`, { name: w.name, capacity_max: cap })
        )
      );
      const ok = results.filter((r) => r.status === "fulfilled").length;
      const fail = results.length - ok;
      if (fail === 0) {
        toast.success(`Capacity set to ${cap} on ${ok} worker${ok === 1 ? "" : "s"}`);
      } else {
        toast.warning(`Updated ${ok}/${results.length} workers (${fail} failed)`);
      }
      setShowBulk(false);
      load();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Bulk update failed");
    } finally {
      setBulkSaving(false);
    }
  };

  const copy = (txt) => {
    navigator.clipboard?.writeText(txt).then(() => toast.success("Copied to clipboard"))
      .catch(() => toast.error("Copy failed"));
  };

  const downloadEnv = (token, name) => {
    const backend = process.env.REACT_APP_BACKEND_URL;
    const content = `# Save this as .env in the same folder as zoom_worker.py
# Worker: ${name}
DASHBOARD_URL=${backend}
WORKER_TOKEN=${token}
POLL_INTERVAL=10
ZOOM_EXE=C:\\Users\\${"${USERNAME}"}\\AppData\\Roaming\\Zoom\\bin\\Zoom.exe
SPAWN_DELAY_MS=400
`;
    const blob = new Blob([content], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `worker-${name.replace(/[^a-z0-9_-]/gi, "_")}.env`;
    a.click(); URL.revokeObjectURL(url);
  };

  return (
    <div className="zs-shell">
      <TopBar usage={user?.usage || 0} usageLimit={user?.usage_limit || 15000} switchTo="classic" />

      <div className="px-6 pb-10 max-w-[1400px] mx-auto">
        <div className="flex items-center justify-between mb-5">
          <div className="flex items-center gap-3">
            <div className="section-icon icon-blue"><Server size={18} /></div>
            <h1 className="text-2xl font-bold text-white">RDP Workers</h1>
            <span className="text-white/40 text-sm">({workers.length})</span>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <a
              href={`${process.env.REACT_APP_BACKEND_URL}/api/worker/zoom_worker_pool.py`}
              download="zoom_worker_pool.py"
              className="zs-btn zs-btn-primary text-sm"
              data-testid="download-worker-pool-py"
              title="v8 Playwright pool worker — recommended"
            >
              <Zap size={14} /> Worker v8 (pool)
            </a>
            <a
              href={`${process.env.REACT_APP_BACKEND_URL}/api/worker/zoom_worker.py`}
              download="zoom_worker.py"
              className="zs-btn zs-btn-ghost text-sm"
              data-testid="download-worker-py"
              title="v7-lean Selenium worker (legacy)"
            >
              <Download size={14} /> v7 (legacy)
            </a>
            <a
              href={`${process.env.REACT_APP_BACKEND_URL}/api/worker/install_linux.sh`}
              download="install_linux.sh"
              className="zs-btn zs-btn-ghost text-sm"
              data-testid="download-install-linux"
            >
              <Download size={14} /> install_linux.sh
            </a>
            <a
              href={`${process.env.REACT_APP_BACKEND_URL}/api/worker/requirements.txt`}
              download="requirements.txt"
              className="zs-btn zs-btn-ghost text-sm"
              data-testid="download-requirements"
            >
              <Download size={14} /> requirements.txt
            </a>
            <a
              href={`${process.env.REACT_APP_BACKEND_URL}/api/worker/setup-guide-linux`}
              target="_blank"
              rel="noreferrer"
              className="zs-btn zs-btn-ghost text-sm"
              data-testid="rdp-setup-linux-link"
            >
              <KeyRound size={14} /> Linux Setup
            </a>
            <a
              href={`${process.env.REACT_APP_BACKEND_URL}/api/worker/setup-guide`}
              target="_blank"
              rel="noreferrer"
              className="zs-btn zs-btn-ghost text-sm"
              data-testid="rdp-setup-link"
            >
              <KeyRound size={14} /> Win Setup
            </a>
            <button
              onClick={openBulk}
              className="zs-btn zs-btn-secondary"
              data-testid="bulk-set-capacity-button"
              title="Set the same capacity on multiple RDPs in one click"
              disabled={workers.length === 0}
            >
              <Layers size={14} /> Bulk Set Capacity
            </button>
            <button
              onClick={() => setShowAdd(true)}
              className="zs-btn zs-btn-primary"
              data-testid="add-worker-button"
            >
              <Plus size={14} /> Add Worker
            </button>
          </div>
        </div>

        {/* ===== Fleet Health Monitor ===== */}
        {fleet && (
          <div className="zs-card-2 p-4 mb-4" data-testid="fleet-health-card">
            <div className="flex items-center gap-3 mb-3">
              <div className="section-icon icon-blue"><HeartPulse size={16} /></div>
              <h2 className="text-lg font-semibold text-white">Fleet Health Monitor</h2>
              <span className="text-white/40 text-xs">auto-updates every 8s</span>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-7 gap-3">
              <FleetStat label="Total" value={fleet.summary.total} accent="text-white" testid="fleet-total" />
              <FleetStat
                label="Healthy"
                value={fleet.summary.healthy}
                accent="text-emerald-400"
                icon={<ShieldCheck size={14} />}
                testid="fleet-healthy"
              />
              <FleetStat
                label="Warning"
                value={fleet.summary.warning}
                accent="text-amber-400"
                icon={<AlertTriangle size={14} />}
                testid="fleet-warning"
              />
              <FleetStat
                label="Critical"
                value={fleet.summary.critical}
                accent="text-red-400"
                icon={<AlertTriangle size={14} />}
                testid="fleet-critical"
              />
              <FleetStat
                label="Offline"
                value={fleet.summary.offline}
                accent="text-white/60"
                testid="fleet-offline"
              />
              <FleetStat
                label="Unstable"
                value={fleet.summary.unstable ?? 0}
                accent={(fleet.summary.unstable ?? 0) > 0 ? "text-orange-400" : "text-white/40"}
                icon={<RefreshCw size={14} />}
                testid="fleet-unstable"
                hint={(fleet.summary.unstable ?? 0) > 0 ? "RDPs that have auto-restarted" : "all RDPs stable"}
              />
              <FleetStat
                label="Utilization"
                value={`${fleet.summary.utilization_pct}%`}
                accent="text-indigo-300"
                testid="fleet-utilization"
                hint={`${fleet.summary.total_load}/${fleet.summary.total_capacity}`}
              />
            </div>
            {/* ===== PREWARM telemetry (browser pool prewarming across fleet) ===== */}
            {fleet.summary.prewarm && (
              <div className="mt-3 pt-3 border-t border-white/5">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-xs uppercase tracking-wider text-white/40">Prewarm pool</span>
                  <span className="text-[10px] text-emerald-300/80">hot browsers + ready contexts give 1-3s joins</span>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <FleetStat
                    label="Hot Browsers"
                    value={fleet.summary.prewarm.hot_browsers}
                    accent="text-orange-300"
                    testid="prewarm-hot-browsers"
                  />
                  <FleetStat
                    label="Ready Contexts"
                    value={fleet.summary.prewarm.ready_contexts}
                    accent="text-cyan-300"
                    testid="prewarm-ready-contexts"
                  />
                  <FleetStat
                    label="Active Bots"
                    value={fleet.summary.prewarm.active_bots}
                    accent="text-violet-300"
                    testid="prewarm-active-bots"
                  />
                  <FleetStat
                    label="Prewarmed RDPs"
                    value={`${fleet.summary.prewarm.prewarmed_workers}/${fleet.summary.total - fleet.summary.offline}`}
                    accent="text-emerald-300"
                    testid="prewarm-rdps-ready"
                  />
                </div>
              </div>
            )}
          </div>
        )}

        <div className="zs-card-2 p-5">
          {workers.length === 0 ? (
            <div className="text-center py-16">
              <Server size={42} className="mx-auto text-white/20 mb-3" />
              <div className="text-white/70 mb-1">No RDP workers yet</div>
              <div className="text-white/40 text-sm mb-5">Add a worker, copy the token, and paste it into <code className="text-indigo-300">zoom_worker.py</code> on your RDP.</div>
              <button onClick={() => setShowAdd(true)} className="zs-btn zs-btn-primary" data-testid="empty-add-worker">
                <Plus size={14} /> Add your first worker
              </button>
            </div>
          ) : (
            <div className="zs-table-wrap">
              <table className="zs-table">
                <thead>
                  <tr>
                    <th>Name</th><th>Status</th><th>Load / Capacity</th>
                    <th>Pool</th>
                    <th>CPU</th><th>RAM</th><th>Hostname</th><th>OS</th>
                    <th>Last Heartbeat</th>
                    <th title="Crash count from keep-alive supervisor + last restart time">Stability</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {workers.map((w) => {
                    // v8.3.6 STRICT: admin's capacity_max is the ONLY scheduling limit.
                    // reported_capacity (auto-detected) is shown as info only.
                    const adminCap = w.capacity_max || 1;
                    const autoCap = w.reported_capacity;
                    const pct = adminCap > 0 ? Math.round((w.current_load / adminCap) * 100) : 0;
                    const loadLabel = `${w.current_load}/${adminCap}`;
                    const ps = w.pool_stats || null;
                    return (
                      <tr key={w.id} data-testid={`worker-row-${w.id}`}>
                        <td className="font-semibold text-white">{w.name}</td>
                        <td>
                          <span className={`status-badge ${w.status === "online" ? "status-active" : "status-cancelled"}`} data-testid={`worker-status-${w.id}`}>
                            <span className={`inline-block w-2 h-2 rounded-full ${w.status === "online" ? "bg-emerald-400" : "bg-amber-400"} animate-pulse`} />
                            {w.status}
                          </span>
                        </td>
                        <td>
                          <div className="flex items-center gap-2 min-w-[200px]">
                            <div className="flex-1 h-2 bg-white/10 rounded-full overflow-hidden">
                              <div className={`h-full ${pct > 85 ? "bg-red-500" : pct > 60 ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${pct}%` }} />
                            </div>
                            <span className="text-xs text-white/70 font-mono whitespace-nowrap" data-testid={`worker-cap-${w.id}`}>{loadLabel}</span>
                            {autoCap != null && autoCap < adminCap && (
                              <span
                                className="text-[10px] text-white/40 font-mono whitespace-nowrap"
                                title={`Auto-detected hardware safe-cap: ${autoCap}. Scheduler IGNORES this — admin limit is strictly enforced.`}
                              >
                                hw~{autoCap}
                              </span>
                            )}
                          </div>
                        </td>
                        <td data-testid={`worker-pool-${w.id}`}>
                          {ps ? (
                            <div className="flex flex-col text-[11px] leading-tight">
                              <span className="text-orange-300 font-mono">🔥 {ps.browsers ?? 0} hot</span>
                              <span className="text-cyan-300 font-mono">
                                ⚡ {ps.ready_contexts ?? 0}
                                {ps.target_ready != null && ps.target_ready !== ps.ready_contexts && (
                                  <span className="text-cyan-300/60">/{ps.target_ready}</span>
                                )} ready
                              </span>
                              {ps.version && ps.version.startsWith("v8.3") ? (
                                <span className="text-emerald-400 text-[10px]" title={ps.version}>
                                  {ps.version}
                                </span>
                              ) : ps.prewarmed ? (
                                <span className="text-emerald-400 text-[10px]">prewarmed</span>
                              ) : (
                                <span className="text-white/40 text-[10px]">cold</span>
                              )}
                              {ps.match_admin_cap && (
                                <span className="text-amber-300/80 text-[10px]" title="prewarm pool auto-scales to admin capacity_max">
                                  match-cap
                                </span>
                              )}
                              {ps.storage_state_age_hours != null && (
                                <span className="text-white/40 text-[10px]" title="storage_state cookies+localStorage age">
                                  state {ps.storage_state_age_hours}h
                                </span>
                              )}
                            </div>
                          ) : (
                            <span className="text-white/30 text-xs">—</span>
                          )}
                        </td>
                        <td><span className="inline-flex items-center gap-1 text-white/80"><Cpu size={12} /> {w.cpu_pct?.toFixed?.(1) ?? 0}%{w.cpu_count ? ` · ${w.cpu_count}c` : ""}</span></td>
                        <td><span className="inline-flex items-center gap-1 text-white/80"><MemoryStick size={12} /> {w.ram_pct?.toFixed?.(1) ?? 0}%{w.ram_free_gb != null ? ` · ${w.ram_free_gb.toFixed(1)}G free` : ""}</span></td>
                        <td className="text-white/70 font-mono text-xs">{w.hostname || "-"}</td>
                        <td className="text-white/70 text-xs">{w.os_info || "-"}</td>
                        <td className="text-white/60 text-xs">{fmt(w.last_heartbeat)}</td>
                        <td data-testid={`worker-stability-${w.id}`}>
                          <StabilityBadge
                            crashes={w.crash_count}
                            lastRestart={w.last_restart_at}
                            startedAt={w.worker_started_at}
                          />
                        </td>
                        <td>
                          <div className="flex gap-1.5">
                            <button onClick={() => openEdit(w)}
                              className="zs-btn zs-btn-secondary !py-1 !px-2 text-sm"
                              data-testid={`edit-worker-${w.id}`}
                              title="Edit capacity/name">
                              <Pencil size={14} />
                            </button>
                            <button onClick={() => remove(w.id, w.name)}
                              className="zs-btn zs-btn-danger !py-1 !px-2 text-sm"
                              data-testid={`delete-worker-${w.id}`}>
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="zs-card-2 p-5 mt-6">
          <h2 className="text-white font-bold text-lg mb-3 flex items-center gap-2"><Activity size={18} /> One-Click RDP Setup</h2>
          <div className="text-white/80 text-sm space-y-3">
            <p>On your Windows RDP, open <b>PowerShell as Administrator</b> and paste this single command:</p>
            <div className="zs-card p-3 font-mono text-xs text-emerald-300 flex items-center justify-between gap-3">
              <code className="break-all" data-testid="install-command">
                {`$env:DASHBOARD_URL="${process.env.REACT_APP_BACKEND_URL}"; iwr "${process.env.REACT_APP_BACKEND_URL}/api/worker/install.ps1" | iex`}
              </code>
              <button
                onClick={() => {
                  const cmd = `$env:DASHBOARD_URL="${process.env.REACT_APP_BACKEND_URL}"; iwr "${process.env.REACT_APP_BACKEND_URL}/api/worker/install.ps1" | iex`;
                  navigator.clipboard?.writeText(cmd).then(() => toast.success("Command copied!"));
                }}
                className="zs-btn zs-btn-secondary !py-1 !px-3 text-xs flex-shrink-0"
                data-testid="copy-install-command"
              >
                <Copy size={12} /> Copy
              </button>
            </div>
            <p className="text-white/60 text-xs">
              The script will: install Python (if missing), install Chrome (if missing), download <code>zoom_worker.py</code>,
              install Python libraries, ask you for the worker token, and offer to install as a Windows service.
            </p>
          </div>
        </div>

        {/* ============ Migration band — for users with an OLD worker already deployed ============ */}
        <div className="zs-card-2 p-5 mt-6 border-l-4 border-amber-500/60">
          <h2 className="text-white font-bold text-lg mb-3 flex items-center gap-2">
            <Activity size={18} className="text-amber-400" />
            Already running an older worker on Linux VPS? Migrate to v8.3.6
          </h2>
          <div className="text-white/80 text-sm space-y-3">
            <p>
              SSH into your existing VPS as <code className="text-amber-300">root</code> and paste this <b>one command</b>.
              It will <span className="text-emerald-300">stop the old worker</span>, <span className="text-emerald-300">preserve your WORKER_TOKEN</span>,
              install <span className="text-emerald-300">v8.3.6</span> (strict admin-cap + green-screen fix + tap-and-join), and register a <span className="text-emerald-300">systemd service</span> for auto-restart.
            </p>
            <div className="zs-card p-3 font-mono text-xs text-emerald-300 flex items-center justify-between gap-3">
              <code className="break-all" data-testid="migrate-command">
                {`curl -fsSL ${process.env.REACT_APP_BACKEND_URL}/api/worker/migrate.sh | sudo DASHBOARD_URL="${process.env.REACT_APP_BACKEND_URL}" bash`}
              </code>
              <button
                onClick={() => {
                  const cmd = `curl -fsSL ${process.env.REACT_APP_BACKEND_URL}/api/worker/migrate.sh | sudo DASHBOARD_URL="${process.env.REACT_APP_BACKEND_URL}" bash`;
                  navigator.clipboard?.writeText(cmd).then(() => toast.success("Migration command copied!"));
                }}
                className="zs-btn zs-btn-secondary !py-1 !px-3 text-xs flex-shrink-0"
                data-testid="copy-migrate-command"
              >
                <Copy size={12} /> Copy
              </button>
            </div>
            <ul className="text-white/60 text-xs space-y-1 list-disc list-inside ml-1">
              <li>Auto-stops: systemd <code>zoom-worker</code>, pm2 <code>zoom-worker</code>/<code>worker</code>, and any orphan <code>chromium</code> processes</li>
              <li>Backs up old <code>.env</code> and <code>zoom_worker_pool.py</code> with timestamps before overwriting</li>
              <li>If <code>WORKER_TOKEN</code> is missing it will prompt — paste from the dashboard</li>
              <li>After: <code className="text-cyan-300">systemctl status zoom-worker</code> and <code className="text-cyan-300">tail -f /var/log/zoom-worker.log</code></li>
              <li>Dashboard should show <span className="text-emerald-300">v8.3.6-strict-cap</span> in the OS column within ~30s</li>
            </ul>
          </div>
        </div>

        <div className="zs-card-2 p-5 mt-6">
          <h2 className="text-white font-bold text-lg mb-3 flex items-center gap-2"><Activity size={18} /> How it works</h2>
          <ol className="text-white/80 text-sm space-y-2 list-decimal list-inside">
            <li><b>Add Worker</b> → copy the token (shown <b>once</b>) → keep it ready for the installer.</li>
            <li>Run the one-click installer (above) on your RDP — paste the token when prompted.</li>
            <li>Worker uses <b>headless Chrome + Zoom Web Client</b> (no Zoom desktop, no Sandboxie needed).</li>
            <li>Sends heartbeat every 5s with CPU/RAM/load → appears <span className="text-emerald-400">online</span> here.</li>
            <li>Polls <code>/api/workers/me/claim</code> → spawns N Chrome bots → reports <code>joined_count</code>.</li>
            <li>On timeout/success, calls <code>/api/tasks/{"{id}"}/complete</code> → row moves to <b>Previous Tasks</b>.</li>
          </ol>
        </div>
      </div>

      {/* Add worker modal */}
      {showAdd && (
        <Modal onClose={() => setShowAdd(false)} title="Add RDP Worker" testid="add-worker-modal">
          <form onSubmit={create} className="space-y-4">
            <div>
              <div className="zs-label">Worker Name</div>
              <input className="zs-input" placeholder="RDP-Box-1" value={newName}
                onChange={(e) => setNewName(e.target.value)} autoFocus data-testid="worker-name-input" />
            </div>
            <div>
              <div className="zs-label">Capacity (max simultaneous bots)</div>
              <input className="zs-input" type="number" min="1" max="500" value={newCap}
                onChange={(e) => setNewCap(e.target.value)} data-testid="worker-capacity-input" />
              <div className="text-xs text-white/40 mt-1">Recommended: 80–100 for a 32GB / 8 CPU box.</div>
            </div>
            <div className="flex gap-3 pt-2">
              <button type="submit" disabled={creating} className="zs-btn zs-btn-primary flex-1" data-testid="create-worker-submit">
                {creating ? <span className="zs-spin" /> : "Create & Get Token"}
              </button>
              <button type="button" onClick={() => setShowAdd(false)} className="zs-btn zs-btn-ghost">Cancel</button>
            </div>
          </form>
        </Modal>
      )}

      {/* Token reveal modal */}
      {createdToken && (
        <Modal onClose={() => setCreatedToken(null)} title="Worker Token (shown once)" testid="token-modal">
          <div className="space-y-4">
            <div className="text-sm text-white/80">
              Save this token now — it will <b className="text-amber-300">never be shown again</b>. If lost, delete the worker and create a new one.
            </div>
            <div className="zs-card p-3 font-mono text-sm break-all text-emerald-300" data-testid="generated-token">
              {createdToken.token}
            </div>
            <div className="flex gap-2 flex-wrap">
              <button onClick={() => copy(createdToken.token)} className="zs-btn zs-btn-secondary text-sm" data-testid="copy-token-btn">
                <Copy size={14} /> Copy Token
              </button>
              <button onClick={() => downloadEnv(createdToken.token, createdToken.name)} className="zs-btn zs-btn-success text-sm" data-testid="download-env-btn">
                <Download size={14} /> Download .env file
              </button>
              <Link to="/file-editor" className="zs-btn zs-btn-ghost text-sm">Manage Names</Link>
            </div>
            <div className="text-xs text-white/50 pt-2">
              Next: Place the .env file next to <code>zoom_worker.py</code> on your RDP and run the worker.
            </div>
          </div>
        </Modal>
      )}
      {/* Edit worker modal */}
      {editTarget && (
        <Modal onClose={() => setEditTarget(null)} title={`Edit Worker · ${editTarget.name}`} testid="edit-worker-modal">
          <form onSubmit={saveEdit} className="space-y-4">
            <div>
              <div className="zs-label">Worker Name</div>
              <input className="zs-input" value={editName}
                onChange={(e) => setEditName(e.target.value)} autoFocus data-testid="edit-worker-name-input" />
            </div>
            <div>
              <div className="zs-label flex items-center justify-between">
                <span>Max Capacity <span className="text-amber-300">(STRICT LIMIT)</span></span>
                {editTarget.reported_capacity != null && (
                  <span className="text-[11px] font-normal text-white/50">
                    HW auto-detected: <b className="text-cyan-300">{editTarget.reported_capacity}</b> <span className="text-white/30">(info only)</span>
                  </span>
                )}
              </div>
              <input className="zs-input" type="number" min="1" max="5000" value={editCap}
                onChange={(e) => setEditCap(e.target.value)} data-testid="edit-worker-capacity-input" />
              <div className="text-xs text-white/50 mt-1.5 space-y-1">
                <div>
                  <span className="text-emerald-300">●</span> Scheduler will assign <b className="text-white">EXACTLY up to this many bots</b> to this RDP — no auto-shrinking, no auto-override.
                </div>
                <div>
                  <span className="text-white/40">●</span> Set <code>1</code> → exactly 1 bot. Set <code>100</code> → up to 100 bots. Set <code>5000</code> → effectively unlimited.
                </div>
                <div className="text-white/40">
                  <span>●</span> Hardware auto-detected value is shown for reference only and is NOT used for distribution.
                </div>
              </div>
            </div>
            <div className="flex gap-3 pt-2">
              <button type="submit" disabled={savingEdit} className="zs-btn zs-btn-primary flex-1" data-testid="save-worker-edit-btn">
                {savingEdit ? <span className="zs-spin" /> : "Save Changes"}
              </button>
              <button type="button" onClick={() => setEditTarget(null)} className="zs-btn zs-btn-ghost">Cancel</button>
            </div>
          </form>
        </Modal>
      )}
      {/* Bulk Set Capacity modal */}
      {showBulk && (
        <Modal onClose={() => setShowBulk(false)} title="Bulk Set Capacity" testid="bulk-capacity-modal">
          <form onSubmit={saveBulk} className="space-y-4">
            <div>
              <div className="zs-label">New Capacity (applies to all selected RDPs)</div>
              <input
                className="zs-input"
                type="number"
                min="1"
                max="5000"
                value={bulkCap}
                onChange={(e) => setBulkCap(e.target.value)}
                autoFocus
                data-testid="bulk-capacity-input"
              />
              <div className="text-xs text-white/50 mt-1.5">
                <span className="text-emerald-300">●</span> STRICT scheduler limit per RDP. Useful when scaling 30–40 RDPs together.
              </div>
            </div>

            <div>
              <div className="flex items-center justify-between mb-2">
                <div className="zs-label !mb-0">
                  Workers <span className="text-white/40">({bulkSelected.size}/{workers.length} selected)</span>
                </div>
                <button
                  type="button"
                  onClick={toggleBulkAll}
                  className="text-xs text-indigo-300 hover:text-indigo-200"
                  data-testid="bulk-select-all-toggle"
                >
                  {bulkSelected.size === workers.length ? "Deselect all" : "Select all"}
                </button>
              </div>
              <div className="max-h-64 overflow-y-auto rounded-lg border border-white/10 divide-y divide-white/5">
                {workers.map((w) => {
                  const checked = bulkSelected.has(w.id);
                  return (
                    <label
                      key={w.id}
                      className={`flex items-center gap-3 px-3 py-2 cursor-pointer text-sm ${
                        checked ? "bg-indigo-500/10" : "hover:bg-white/5"
                      }`}
                      data-testid={`bulk-row-${w.id}`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleBulkOne(w.id)}
                        className="accent-indigo-500"
                        data-testid={`bulk-checkbox-${w.id}`}
                      />
                      <span className="text-white font-medium flex-1">{w.name}</span>
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded ${
                          w.status === "online"
                            ? "bg-emerald-500/15 text-emerald-300"
                            : "bg-amber-500/15 text-amber-300"
                        }`}
                      >
                        {w.status}
                      </span>
                      <span className="text-xs text-white/50 font-mono whitespace-nowrap">
                        cap {w.capacity_max}
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="flex gap-3 pt-2">
              <button
                type="submit"
                disabled={bulkSaving || bulkSelected.size === 0}
                className="zs-btn zs-btn-primary flex-1"
                data-testid="bulk-save-btn"
              >
                {bulkSaving
                  ? <span className="zs-spin" />
                  : `Apply to ${bulkSelected.size} RDP${bulkSelected.size === 1 ? "" : "s"}`}
              </button>
              <button
                type="button"
                onClick={() => setShowBulk(false)}
                className="zs-btn zs-btn-ghost"
                data-testid="bulk-cancel-btn"
              >
                Cancel
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}

function Modal({ children, onClose, title, testid }) {
  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4" onClick={onClose} data-testid={testid}>
      <div className="zs-card-2 p-6 max-w-md w-full" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-white font-bold text-lg">{title}</h3>
          <button onClick={onClose} className="text-white/60 hover:text-white" aria-label="Close"><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}


function FleetStat({ label, value, accent = "text-white", icon, hint, testid }) {
  return (
    <div className="rounded-xl bg-white/5 border border-white/10 p-3" data-testid={testid}>
      <div className="text-[11px] uppercase tracking-wider text-white/50 flex items-center gap-1">
        {icon}{label}
      </div>
      <div className={`text-2xl font-bold mt-1 ${accent}`}>{value}</div>
      {hint && <div className="text-[10px] text-white/40 mt-0.5 font-mono">{hint}</div>}
    </div>
  );
}

// Stability heatmap pill for the workers table — shows the keep-alive
// supervisor's crash count plus when it last had to restart main_loop and
// how long the worker process has been up. Zero crashes = green, 1-2 = amber,
// 3+ = red. Gives the operator a one-glance heatmap across 40 RDPs.
function StabilityBadge({ crashes, lastRestart, startedAt }) {
  const c = Number.isFinite(crashes) ? crashes : 0;
  let tone, label, Icon;
  if (c === 0) {
    tone = "bg-emerald-500/10 text-emerald-300 border-emerald-500/30";
    label = "stable";
    Icon = ShieldCheck;
  } else if (c <= 2) {
    tone = "bg-amber-500/10 text-amber-300 border-amber-500/30";
    label = `${c} crash${c > 1 ? "es" : ""}`;
    Icon = RefreshCw;
  } else {
    tone = "bg-red-500/15 text-red-300 border-red-500/40";
    label = `${c} crashes`;
    Icon = AlertTriangle;
  }
  const restartRel = relTime(lastRestart);
  const upRel = relTime(startedAt);
  return (
    <div className="flex flex-col gap-0.5 min-w-[110px]">
      <span
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[11px] font-medium w-fit ${tone}`}
        title={lastRestart ? `Last restart: ${fmt(lastRestart)}` : "Never restarted"}
      >
        <Icon size={11} />
        {label}
      </span>
      {c > 0 && restartRel && (
        <span className="text-[10px] text-white/50 font-mono">restart {restartRel}</span>
      )}
      {upRel && (
        <span className="text-[10px] text-white/40 font-mono" title={`Worker booted ${fmt(startedAt)}`}>
          up {upRel}
        </span>
      )}
    </div>
  );
}

