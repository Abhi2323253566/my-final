import { useEffect, useState, useCallback } from "react";
import TopBar from "@/components/TopBar";
import { api, formatApiErrorDetail } from "@/lib/api";
import { useAuth } from "@/auth/AuthContext";
import { toast } from "sonner";
import { Wallet, Upload, CheckCircle2, Clock, XCircle, IndianRupee, Image as ImageIcon } from "lucide-react";

function fmt(iso) {
  if (!iso) return "-";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function statusBadge(s) {
  if (s === "approved") return <span className="status-badge status-completed" data-testid="status-approved"><CheckCircle2 size={11} /> Approved</span>;
  if (s === "rejected") return <span className="status-badge status-cancelled" data-testid="status-rejected"><XCircle size={11} /> Rejected</span>;
  return <span className="status-badge status-scheduled" data-testid="status-pending"><Clock size={11} /> Pending</span>;
}

export default function BuyCreditsPage() {
  const { user, refreshMe } = useAuth();
  const [settings, setSettings] = useState({ qr_image: null, upi_id: null, instructions: null });
  const [amount, setAmount] = useState(100);
  const [screenshot, setScreenshot] = useState(null); // data URL
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [myTopups, setMyTopups] = useState([]);

  const rate = user?.credit_rate || 1.0;
  const previewCredits = Math.round((parseFloat(amount) || 0) * rate);

  const loadSettings = useCallback(async () => {
    try {
      const { data } = await api.get("/settings/payment");
      setSettings(data);
    } catch (e) { /* ignore */ }
  }, []);

  const loadMyTopups = useCallback(async () => {
    try {
      const { data } = await api.get("/topup/my");
      setMyTopups(data);
    } catch (e) { /* ignore */ }
  }, []);

  useEffect(() => { loadSettings(); loadMyTopups(); }, [loadSettings, loadMyTopups]);
  useEffect(() => { const id = setInterval(loadMyTopups, 8000); return () => clearInterval(id); }, [loadMyTopups]);

  const onFile = (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) {
      toast.error("File too large — max 2 MB");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setScreenshot(reader.result);
    reader.readAsDataURL(f);
  };

  const submit = async (e) => {
    e?.preventDefault?.();
    if (!screenshot) return toast.error("Please upload payment screenshot");
    const amt = parseFloat(amount);
    if (!amt || amt <= 0) return toast.error("Enter a valid amount");
    setSubmitting(true);
    try {
      await api.post("/topup/request", {
        amount_rs: amt,
        screenshot,
        note: note.trim(),
      });
      toast.success("Top-up request submitted! Admin will review shortly.");
      setAmount(100); setScreenshot(null); setNote("");
      loadMyTopups();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="zs-shell">
      <TopBar usage={user?.usage || 0} usageLimit={user?.usage_limit || 15000} switchTo="classic" />

      <div className="px-6 pb-10 max-w-[1400px] mx-auto">
        <div className="flex items-center gap-3 mb-5">
          <div className="section-icon icon-emerald"><Wallet size={18} /></div>
          <h1 className="text-2xl font-bold text-white">Buy Credits</h1>
          <button onClick={refreshMe} className="ml-auto zs-btn zs-btn-ghost text-xs" data-testid="refresh-balance-btn">Refresh balance</button>
        </div>

        <div className="grid lg:grid-cols-2 gap-5">
          {/* LEFT — QR + Form */}
          <div className="zs-card-2 p-5 space-y-4">
            <div>
              <div className="text-white/70 text-sm mb-2">Step 1 — Scan this QR &amp; pay</div>
              <div className="bg-white rounded-xl p-4 flex items-center justify-center" style={{ minHeight: 240 }}>
                {settings.qr_image ? (
                  <img src={settings.qr_image} alt="Payment QR" className="max-h-72 object-contain" data-testid="payment-qr-img" />
                ) : (
                  <div className="text-zinc-500 text-sm text-center" data-testid="no-qr-placeholder">
                    Admin hasn't uploaded a payment QR yet.<br />
                    Please contact support.
                  </div>
                )}
              </div>
              {settings.upi_id && (
                <div className="text-white/80 text-sm mt-3" data-testid="upi-id-display">
                  UPI ID: <code className="text-emerald-300 font-mono">{settings.upi_id}</code>
                </div>
              )}
              {settings.instructions && (
                <div className="text-white/60 text-xs mt-2 whitespace-pre-wrap" data-testid="upi-instructions">
                  {settings.instructions}
                </div>
              )}
            </div>

            <div className="zs-card p-3 text-xs text-emerald-300 flex items-center gap-2">
              <IndianRupee size={14} /> Your rate: <b>₹1 = {rate} credit{rate === 1 ? "" : "s"}</b>
              <span className="text-white/40">(set by admin)</span>
            </div>

            <form onSubmit={submit} className="space-y-3">
              <div>
                <div className="zs-label">Step 2 — Amount paid (₹)</div>
                <input
                  type="number" min="1" step="0.01" required
                  className="zs-input"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  data-testid="topup-amount-input"
                />
                <div className="text-xs text-emerald-400 mt-1" data-testid="preview-credits">
                  You'll get: <b>{previewCredits} credits</b> (after admin approval)
                </div>
              </div>

              <div>
                <div className="zs-label">Step 3 — Upload payment screenshot</div>
                <label className="zs-card p-4 flex flex-col items-center justify-center cursor-pointer hover:bg-white/5 border-dashed">
                  <input type="file" accept="image/*" onChange={onFile} className="hidden" data-testid="screenshot-upload-input" />
                  {screenshot ? (
                    <div className="text-center">
                      <img src={screenshot} alt="preview" className="max-h-32 object-contain mx-auto rounded" />
                      <div className="text-emerald-400 text-xs mt-2">✓ Screenshot ready (click to change)</div>
                    </div>
                  ) : (
                    <div className="text-white/60 text-sm flex flex-col items-center gap-2">
                      <Upload size={20} />
                      <span>Click to upload screenshot (max 2 MB)</span>
                    </div>
                  )}
                </label>
              </div>

              <div>
                <div className="zs-label">Note (optional)</div>
                <input
                  className="zs-input"
                  placeholder="e.g. Paid via PhonePe, UTR #1234"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  data-testid="topup-note-input"
                  maxLength={200}
                />
              </div>

              <button type="submit" disabled={submitting} className="zs-btn zs-btn-primary w-full" data-testid="submit-topup-btn">
                {submitting ? <span className="zs-spin" /> : "Submit Top-up Request"}
              </button>
            </form>
          </div>

          {/* RIGHT — History */}
          <div className="zs-card-2 p-5">
            <h2 className="text-white font-bold text-lg mb-3 flex items-center gap-2">
              <Clock size={16} /> Your Top-up History
            </h2>
            {myTopups.length === 0 ? (
              <div className="text-white/40 text-sm py-6 text-center" data-testid="no-history">No top-ups yet</div>
            ) : (
              <div className="space-y-2">
                {myTopups.map((t) => (
                  <div key={t.id} className="zs-card p-3" data-testid={`topup-row-${t.id}`}>
                    <div className="flex items-center justify-between">
                      <div>
                        <div className="text-white font-semibold">₹{t.amount_rs.toLocaleString()}</div>
                        <div className="text-white/60 text-xs">{fmt(t.created_at)}</div>
                      </div>
                      <div className="text-right">
                        {statusBadge(t.status)}
                        <div className="text-emerald-400 text-xs mt-1">+{t.credits} credits</div>
                      </div>
                    </div>
                    {t.note && <div className="text-white/50 text-xs mt-1">Note: {t.note}</div>}
                    {t.admin_note && <div className="text-amber-300 text-xs mt-1">Admin: {t.admin_note}</div>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
