import { useEffect, useState, useCallback } from "react";
import TopBar from "@/components/TopBar";
import { api, formatApiErrorDetail } from "@/lib/api";
import {
  Users, Plus, Trash2, X, KeyRound, RotateCcw, Pencil, Crown,
} from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/auth/AuthContext";
import { useNavigate } from "react-router-dom";

function fmt(iso) {
  if (!iso) return "-";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

export default function AdminUsersPage() {
  const { user, refreshMe } = useAuth();
  const nav = useNavigate();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);

  // Add modal
  const [showAdd, setShowAdd] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [newName, setNewName] = useState("");
  const [newPass, setNewPass] = useState("");
  const [newLimit, setNewLimit] = useState(15000);
  const [newRate, setNewRate] = useState(1.0);
  const [newRole, setNewRole] = useState("user");
  const [creating, setCreating] = useState(false);

  // Edit modal
  const [editTarget, setEditTarget] = useState(null);
  const [editPass, setEditPass] = useState("");
  const [editLimit, setEditLimit] = useState(0);
  const [editRate, setEditRate] = useState(1.0);
  const [editName, setEditName] = useState("");
  const [editRole, setEditRole] = useState("user");
  const [saving, setSaving] = useState(false);

  // Guard: only admin
  useEffect(() => {
    if (user && user.role !== "admin") {
      toast.error("Admin access required");
      nav("/dashboard", { replace: true });
    }
  }, [user, nav]);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/users");
      setUsers(data);
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { const id = setInterval(load, 8000); return () => clearInterval(id); }, [load]);

  const create = async (e) => {
    e?.preventDefault?.();
    if (!newEmail.trim() || !newPass) return toast.error("Email and password required");
    if (newPass.length < 6) return toast.error("Password must be at least 6 characters");
    setCreating(true);
    try {
      await api.post("/admin/users", {
        email: newEmail.trim().toLowerCase(),
        password: newPass,
        name: newName.trim() || undefined,
        role: newRole,
        usage_limit: parseInt(newLimit, 10) || 0,
        credit_rate: parseFloat(newRate) || 1.0,
      });
      toast.success(`User '${newEmail}' created`);
      setShowAdd(false);
      setNewEmail(""); setNewName(""); setNewPass("");
      setNewLimit(15000); setNewRate(1.0); setNewRole("user");
      load();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally {
      setCreating(false);
    }
  };

  const openEdit = (u) => {
    setEditTarget(u);
    setEditPass("");
    setEditLimit(u.usage_limit);
    setEditRate(u.credit_rate ?? 1.0);
    setEditName(u.name);
    setEditRole(u.role);
  };

  const saveEdit = async (e) => {
    e?.preventDefault?.();
    if (!editTarget) return;
    setSaving(true);
    try {
      const payload = {
        name: editName,
        usage_limit: parseInt(editLimit, 10) || 0,
        credit_rate: parseFloat(editRate) || 0,
        role: editRole,
      };
      if (editPass) {
        if (editPass.length < 6) {
          toast.error("Password must be at least 6 characters");
          setSaving(false);
          return;
        }
        payload.password = editPass;
      }
      await api.put(`/admin/users/${editTarget.id}`, payload);
      toast.success("Updated");
      setEditTarget(null);
      load();
      if (editTarget.id === user.id) refreshMe();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally {
      setSaving(false);
    }
  };

  const resetUsage = async (u) => {
    if (!window.confirm(`Reset usage to 0 for ${u.email}?`)) return;
    try {
      await api.post(`/admin/users/${u.id}/reset-usage`);
      toast.success("Usage reset");
      load();
      if (u.id === user.id) refreshMe();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    }
  };

  const del = async (u) => {
    if (!window.confirm(`Delete user ${u.email}? Their tasks, workers, and name files will also be deleted.`)) return;
    try {
      await api.delete(`/admin/users/${u.id}`);
      toast.success("Deleted");
      load();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    }
  };

  return (
    <div className="zs-shell">
      <TopBar usage={user?.usage || 0} usageLimit={user?.usage_limit || 15000} switchTo="classic" />

      <div className="px-6 pb-10 max-w-[1400px] mx-auto">
        <div className="flex items-center justify-between mb-5">
          <div className="flex items-center gap-3">
            <div className="section-icon icon-amber"><Users size={18} /></div>
            <h1 className="text-2xl font-bold text-white">Admin · Users & Credits</h1>
            <span className="text-white/40 text-sm">({users.length})</span>
          </div>
          <button onClick={() => setShowAdd(true)} className="zs-btn zs-btn-primary" data-testid="add-user-button">
            <Plus size={14} /> Add User
          </button>
        </div>

        <div className="zs-card-2 p-5">
          {loading ? (
            <div className="text-white/50 text-center py-10">Loading…</div>
          ) : users.length === 0 ? (
            <div className="text-center py-12 text-white/50">No users yet</div>
          ) : (
            <div className="zs-table-wrap">
              <table className="zs-table">
                <thead>
                  <tr>
                    <th>Email</th><th>Name</th><th>Role</th>
                    <th>Usage / Credits</th><th>Rate (₹→cr)</th><th>Created</th><th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => {
                    const pct = u.usage_limit > 0 ? Math.min(100, Math.round((u.usage / u.usage_limit) * 100)) : 0;
                    return (
                      <tr key={u.id} data-testid={`user-row-${u.id}`}>
                        <td className="font-mono text-white text-sm">{u.email}</td>
                        <td className="text-white/80">{u.name}</td>
                        <td>
                          {u.role === "admin" ? (
                            <span className="status-badge status-scheduled" data-testid={`role-${u.id}`}>
                              <Crown size={11} /> admin
                            </span>
                          ) : (
                            <span className="status-badge status-completed">user</span>
                          )}
                        </td>
                        <td>
                          <div className="flex items-center gap-2 min-w-[180px]">
                            <div className="flex-1 h-2 bg-white/10 rounded-full overflow-hidden">
                              <div className={`h-full ${pct > 85 ? "bg-red-500" : pct > 60 ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${pct}%` }} />
                            </div>
                            <span className="text-xs text-white/70 font-mono whitespace-nowrap">{u.usage} / {u.usage_limit}</span>
                          </div>
                        </td>
                        <td className="text-emerald-300 font-mono text-xs" data-testid={`rate-${u.id}`}>
                          {u.credit_rate ?? 1.0}/₹
                        </td>
                        <td className="text-white/60 text-xs">{fmt(u.created_at)}</td>
                        <td>
                          <div className="flex gap-1.5">
                            <button onClick={() => openEdit(u)}
                              className="zs-btn zs-btn-secondary !py-1 !px-2 text-xs"
                              data-testid={`edit-${u.id}`} title="Edit">
                              <Pencil size={12} />
                            </button>
                            <button onClick={() => resetUsage(u)}
                              className="zs-btn zs-btn-warning !py-1 !px-2 text-xs"
                              data-testid={`reset-${u.id}`} title="Reset usage to 0">
                              <RotateCcw size={12} />
                            </button>
                            <button
                              onClick={() => del(u)}
                              disabled={u.id === user.id}
                              className="zs-btn zs-btn-danger !py-1 !px-2 text-xs"
                              data-testid={`delete-${u.id}`}
                              title={u.id === user.id ? "Cannot delete yourself" : "Delete user"}
                            >
                              <Trash2 size={12} />
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

        <div className="zs-card-2 p-5 mt-6 text-white/80 text-sm">
          <h2 className="font-bold text-lg text-white mb-2 flex items-center gap-2"><KeyRound size={16}/> Admin notes</h2>
          <ul className="list-disc list-inside space-y-1">
            <li><b>Credits</b> = <code>usage_limit</code> (max members a user can sum across tasks). Each task they create increments their <code>usage</code> by the member count.</li>
            <li>Use <b>Reset</b> to zero out usage (e.g. monthly renewal).</li>
            <li>Regular users <b>cannot see Workers/Admin</b> sections — they only see the dashboard.</li>
            <li>RDP workers are shared across all users — any user's task is picked up by any online worker automatically.</li>
          </ul>
        </div>
      </div>

      {showAdd && (
        <Modal onClose={() => setShowAdd(false)} title="Add New User" testid="add-user-modal">
          <form onSubmit={create} className="space-y-4">
            <Row label="Email">
              <input className="zs-input" type="email" required placeholder="user@example.com"
                value={newEmail} onChange={(e) => setNewEmail(e.target.value)}
                data-testid="new-email-input" autoFocus />
            </Row>
            <Row label="Name (optional)">
              <input className="zs-input" placeholder="Full name"
                value={newName} onChange={(e) => setNewName(e.target.value)}
                data-testid="new-name-input" />
            </Row>
            <Row label="Password (min 6 chars)">
              <input className="zs-input" type="password" required placeholder="••••••"
                value={newPass} onChange={(e) => setNewPass(e.target.value)}
                data-testid="new-password-input" />
            </Row>
            <Row label="Usage Limit (credits)">
              <input className="zs-input" type="number" min="0" max="10000000"
                value={newLimit} onChange={(e) => setNewLimit(e.target.value)}
                data-testid="new-limit-input" />
            </Row>
            <Row label="Credit Rate (credits per ₹1)">
              <input className="zs-input" type="number" min="0" step="0.01"
                value={newRate} onChange={(e) => setNewRate(e.target.value)}
                data-testid="new-rate-input" />
              <div className="text-xs text-white/40 mt-1">e.g. 0.5 means ₹100 → 50 credits; 1.0 means ₹1 = 1 credit.</div>
            </Row>
            <Row label="Role">
              <select className="zs-input" value={newRole} onChange={(e) => setNewRole(e.target.value)}
                data-testid="new-role-select">
                <option value="user">user (regular)</option>
                <option value="admin">admin (sees Workers + Admin)</option>
              </select>
            </Row>
            <div className="flex gap-3 pt-2">
              <button type="submit" disabled={creating} className="zs-btn zs-btn-primary flex-1" data-testid="create-user-submit">
                {creating ? <span className="zs-spin" /> : "Create User"}
              </button>
              <button type="button" onClick={() => setShowAdd(false)} className="zs-btn zs-btn-ghost">Cancel</button>
            </div>
          </form>
        </Modal>
      )}

      {editTarget && (
        <Modal onClose={() => setEditTarget(null)} title={`Edit · ${editTarget.email}`} testid="edit-user-modal">
          <form onSubmit={saveEdit} className="space-y-4">
            <Row label="Name">
              <input className="zs-input" value={editName} onChange={(e) => setEditName(e.target.value)} data-testid="edit-name-input" />
            </Row>
            <Row label="New password (leave blank to keep)">
              <input className="zs-input" type="password" placeholder="••••••"
                value={editPass} onChange={(e) => setEditPass(e.target.value)}
                data-testid="edit-password-input" />
            </Row>
            <Row label="Usage Limit (credits)">
              <input className="zs-input" type="number" min="0" value={editLimit}
                onChange={(e) => setEditLimit(e.target.value)} data-testid="edit-limit-input" />
            </Row>
            <Row label="Credit Rate (credits per ₹1)">
              <input className="zs-input" type="number" min="0" step="0.01" value={editRate}
                onChange={(e) => setEditRate(e.target.value)} data-testid="edit-rate-input" />
              <div className="text-xs text-white/40 mt-1">e.g. 0.5 means ₹100 → 50 credits; 1.0 means ₹1 = 1 credit.</div>
            </Row>
            <Row label="Role">
              <select
                className="zs-input"
                value={editRole}
                onChange={(e) => setEditRole(e.target.value)}
                disabled={editTarget.id === user.id}
                data-testid="edit-role-select"
              >
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
              {editTarget.id === user.id && <div className="text-xs text-white/40 mt-1">You can't demote yourself.</div>}
            </Row>
            <div className="flex gap-3 pt-2">
              <button type="submit" disabled={saving} className="zs-btn zs-btn-primary flex-1" data-testid="save-edit-submit">
                {saving ? <span className="zs-spin" /> : "Save Changes"}
              </button>
              <button type="button" onClick={() => setEditTarget(null)} className="zs-btn zs-btn-ghost">Cancel</button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}

function Row({ label, children }) {
  return (
    <div>
      <div className="zs-label">{label}</div>
      {children}
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
