import { useEffect, useState, useCallback } from "react";
import TopBar from "@/components/TopBar";
import { api, formatApiErrorDetail } from "@/lib/api";
import { useAuth } from "@/auth/AuthContext";
import { toast } from "sonner";
import { Wallet, CheckCircle2, XCircle, Clock, QrCode, Upload, Save, Eye } from "lucide-react";

function fmt(iso) { if (!iso) return "-"; try { return new Date(iso).toLocaleString(); } catch { return iso; } }

function statusBadge(s) {
  if (s === "approved") return <span className="status-badge status-completed">Approved</span>;
  if (s === "rejected") return <span className="status-badge status-cancelled">Rejected</span>;
  return <span className="status-badge status-scheduled">Pending</span>;
}

export default function AdminTopupPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState("pending");
  const [list, setList] = useState([]);
  const [detail, setDetail] = useState(null); // expanded view with screenshot
  const [decisionNote, setDecisionNote] = useState("");
  const [overrideCredits, setOverrideCredits] = useState("");
  const [busy, setBusy] = useState(false);

  // QR settings
  const [settings, setSettings] = useState({ qr_image: null, upi_id: "", instructions: "" });
  const [editQrImage, setEditQrImage] = useState(null);
  const [savingSettings, setSavingSettings] = useState(false);

  const loadList = useCallback(async () => {
    try {
      const { data } = await api.get(`/admin/topup-requests?status=${tab}`);
      setList(data);
    } catch (e) { /* ignore */ }
  }, [tab]);

  const loadSettings = useCallback(async () => {
    try {
      const { data } = await api.get("/settings/payment");
      setSettings({ qr_image: data.qr_image, upi_id: data.upi_id || "", instructions: data.instructions || "" });
    } catch (e) { /* ignore */ }
  }, []);

  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { loadSettings(); }, [loadSettings]);
  useEffect(() => { const id = setInterval(loadList, 8000); return () => clearInterval(id); }, [loadList]);

  const openDetail = async (t) => {
    try {
      const { data } = await api.get(`/topup/${t.id}`);
      setDetail(data);
      setDecisionNote("");
      setOverrideCredits("");
    } catch (e) {
      toast.error("Failed to load detail");
    }
  };

  const decide = async (action) => {
    if (!detail) return;
    setBusy(true);
    try {
      const payload = { action, admin_note: decisionNote };
      if (overrideCredits !== "") {
        payload.override_credits = parseInt(overrideCredits, 10);
      }
      await api.post(`/admin/topup-requests/${detail.id}/decide`, payload);
      toast.success(`Top-up ${action}d`);
      setDetail(null);
      loadList();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally {
      setBusy(false);
    }
  };

  const onQrFile = (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 1.5 * 1024 * 1024) return toast.error("QR image too large (max 1.5 MB)");
    const reader = new FileReader();
    reader.onload = () => setEditQrImage(reader.result);
    reader.readAsDataURL(f);
  };

  const saveSettings = async () => {
    setSavingSettings(true);
    try {
      const payload = {
        upi_id: settings.upi_id,
        instructions: settings.instructions,
      };
      if (editQrImage) payload.qr_image = editQrImage;
      const { data } = await api.put("/admin/settings/payment", payload);
      setSettings({ qr_image: data.qr_image, upi_id: data.upi_id || "", instructions: data.instructions || "" });
      setEditQrImage(null);
      toast.success("Payment settings saved");
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally {
      setSavingSettings(false);
    }
  };

  return (
    <div className="zs-shell">
      <TopBar usage={user?.usage || 0} usageLimit={user?.usage_limit || 15000} switchTo="classic" />

      <div className="px-6 pb-10 max-w-[1400px] mx-auto">
        <div className="flex items-center gap-3 mb-5">
          <div className="section-icon icon-emerald"><Wallet size={18} /></div>
          <h1 className="text-2xl font-bold text-white">Admin · Credit Top-ups</h1>
        </div>

        <div className="grid lg:grid-cols-3 gap-5">
          {/* LEFT 2 cols — requests list */}
          <div className="lg:col-span-2 space-y-5">
            <div className="zs-card-2 p-5">
              <div className="flex items-center justify-between mb-4 gap-2 flex-wrap">
                <div className="flex gap-2">
                  {["pending", "approved", "rejected"].map((t) => (
                    <button
                      key={t}
                      onClick={() => setTab(t)}
                      className={`zs-btn text-xs ${tab === t ? "zs-btn-primary" : "zs-btn-ghost"}`}
                      data-testid={`tab-${t}`}
                    >
                      {t.charAt(0).toUpperCase() + t.slice(1)}
                    </button>
                  ))}
                </div>
                <span className="text-white/40 text-xs">{list.length} request(s)</span>
              </div>

              {list.length === 0 ? (
                <div className="text-white/40 text-center py-10" data-testid="empty-topup-list">No {tab} requests</div>
              ) : (
                <div className="zs-table-wrap">
                  <table className="zs-table">
                    <thead>
                      <tr>
                        <th>User</th><th>Amount (₹)</th><th>Credits</th>
                        <th>Status</th><th>Submitted</th><th>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {list.map((t) => (
                        <tr key={t.id} data-testid={`topup-row-${t.id}`}>
                          <td>
                            <div className="text-white font-semibold text-sm">{t.user_name || "—"}</div>
                            <div className="text-white/50 font-mono text-xs">{t.user_email}</div>
                          </td>
                          <td className="text-emerald-300 font-bold">₹{t.amount_rs.toLocaleString()}</td>
                          <td>
                            <span className="text-white">{t.credits}</span>
                            <div className="text-white/40 text-xs">@ {t.credit_rate}/₹</div>
                          </td>
                          <td>{statusBadge(t.status)}</td>
                          <td className="text-white/60 text-xs">{fmt(t.created_at)}</td>
                          <td>
                            <button
                              onClick={() => openDetail(t)}
                              className="zs-btn zs-btn-secondary !py-1 !px-2 text-xs"
                              data-testid={`view-topup-${t.id}`}
                            >
                              <Eye size={12} /> View
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          {/* RIGHT — QR Settings */}
          <div className="zs-card-2 p-5">
            <h2 className="text-white font-bold text-lg mb-3 flex items-center gap-2">
              <QrCode size={16} /> Payment QR Settings
            </h2>

            <div className="space-y-3">
              <div>
                <div className="zs-label">Current QR (shown to users)</div>
                <div className="bg-white rounded-lg p-3 flex items-center justify-center min-h-[160px]">
                  {(editQrImage || settings.qr_image) ? (
                    <img
                      src={editQrImage || settings.qr_image}
                      alt="QR"
                      className="max-h-48 object-contain"
                      data-testid="current-qr-img"
                    />
                  ) : (
                    <div className="text-zinc-500 text-xs">No QR uploaded</div>
                  )}
                </div>
                <label className="zs-btn zs-btn-secondary w-full mt-2 cursor-pointer text-xs">
                  <Upload size={12} /> {editQrImage ? "Change selection" : "Upload new QR"}
                  <input type="file" accept="image/*" onChange={onQrFile} className="hidden" data-testid="qr-upload-input" />
                </label>
              </div>

              <div>
                <div className="zs-label">UPI ID (optional)</div>
                <input
                  className="zs-input"
                  placeholder="yourname@upi"
                  value={settings.upi_id}
                  onChange={(e) => setSettings({ ...settings, upi_id: e.target.value })}
                  data-testid="upi-id-input"
                />
              </div>

              <div>
                <div className="zs-label">Instructions (optional)</div>
                <textarea
                  className="zs-input"
                  rows={3}
                  placeholder="e.g. Pay & screenshot the success page. Credits added within 1 hour."
                  value={settings.instructions}
                  onChange={(e) => setSettings({ ...settings, instructions: e.target.value })}
                  data-testid="instructions-input"
                />
              </div>

              <button
                onClick={saveSettings}
                disabled={savingSettings}
                className="zs-btn zs-btn-primary w-full"
                data-testid="save-payment-settings-btn"
              >
                {savingSettings ? <span className="zs-spin" /> : (<><Save size={14} /> Save Payment Settings</>)}
              </button>
            </div>
          </div>
        </div>

        {/* Detail modal */}
        {detail && (
          <div
            className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4"
            onClick={() => setDetail(null)}
            data-testid="topup-detail-modal"
          >
            <div className="zs-card-2 p-6 max-w-2xl w-full max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
              <h3 className="text-white font-bold text-lg mb-4">Top-up Request Detail</h3>
              <div className="grid sm:grid-cols-2 gap-4 mb-4">
                <Field label="User"><div className="text-white">{detail.user_name}</div><div className="text-white/50 text-xs font-mono">{detail.user_email}</div></Field>
                <Field label="Status">{statusBadge(detail.status)}</Field>
                <Field label="Amount Paid"><b className="text-emerald-300 text-lg">₹{detail.amount_rs}</b></Field>
                <Field label="Credits">
                  <b className="text-amber-300 text-lg">{detail.credits}</b>
                  <div className="text-white/40 text-xs">@ rate {detail.credit_rate}/₹</div>
                </Field>
                <Field label="Submitted"><span className="text-white/80">{fmt(detail.created_at)}</span></Field>
                {detail.processed_at && <Field label="Processed"><span className="text-white/80">{fmt(detail.processed_at)}</span></Field>}
              </div>
              {detail.note && <div className="zs-card p-3 mb-3 text-sm"><b className="text-white">User note:</b> <span className="text-white/80">{detail.note}</span></div>}
              {detail.admin_note && <div className="zs-card p-3 mb-3 text-sm"><b className="text-amber-300">Admin note:</b> <span className="text-white/80">{detail.admin_note}</span></div>}
              <div className="mb-4">
                <div className="zs-label">Payment Screenshot</div>
                <div className="bg-white rounded-lg p-2 flex justify-center">
                  {detail.screenshot ? (
                    <img src={detail.screenshot} alt="proof" className="max-h-96 object-contain" data-testid="screenshot-img" />
                  ) : (
                    <div className="text-zinc-500 text-xs py-6">No screenshot</div>
                  )}
                </div>
              </div>

              {detail.status === "pending" && (
                <div className="space-y-3 border-t border-white/10 pt-4">
                  <Field label="Admin note (optional)">
                    <input
                      className="zs-input"
                      placeholder="e.g. UTR verified"
                      value={decisionNote}
                      onChange={(e) => setDecisionNote(e.target.value)}
                      data-testid="admin-note-input"
                    />
                  </Field>
                  <Field label={`Credits to grant (default ${detail.credits})`}>
                    <input
                      type="number" min="0" className="zs-input"
                      placeholder={detail.credits}
                      value={overrideCredits}
                      onChange={(e) => setOverrideCredits(e.target.value)}
                      data-testid="override-credits-input"
                    />
                  </Field>
                  <div className="flex gap-3 pt-2">
                    <button onClick={() => decide("approve")} disabled={busy} className="zs-btn zs-btn-success flex-1" data-testid="approve-btn">
                      <CheckCircle2 size={14} /> Approve & Credit
                    </button>
                    <button onClick={() => decide("reject")} disabled={busy} className="zs-btn zs-btn-danger flex-1" data-testid="reject-btn">
                      <XCircle size={14} /> Reject
                    </button>
                    <button onClick={() => setDetail(null)} className="zs-btn zs-btn-ghost">Close</button>
                  </div>
                </div>
              )}
              {detail.status !== "pending" && (
                <div className="flex justify-end pt-3">
                  <button onClick={() => setDetail(null)} className="zs-btn zs-btn-ghost">Close</button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <div className="zs-label">{label}</div>
      {children}
    </div>
  );
}
