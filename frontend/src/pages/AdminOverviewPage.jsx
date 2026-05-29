import { useEffect, useState, useCallback } from "react";
import TopBar from "@/components/TopBar";
import { api } from "@/lib/api";
import { useAuth } from "@/auth/AuthContext";
import { BarChart3, Users, IndianRupee, Clock, Activity, Server, RefreshCw } from "lucide-react";

export default function AdminOverviewPage() {
  const { user } = useAuth();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/overview");
      setData(data);
    } catch (e) { /* ignore */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { const id = setInterval(load, 8000); return () => clearInterval(id); }, [load]);

  return (
    <div className="zs-shell">
      <TopBar usage={user?.usage || 0} usageLimit={user?.usage_limit || 15000} switchTo="classic" />

      <div className="px-6 pb-10 max-w-[1400px] mx-auto">
        <div className="flex items-center gap-3 mb-6">
          <div className="section-icon icon-amber"><BarChart3 size={18} /></div>
          <h1 className="text-2xl font-bold text-white">Admin · Business Overview</h1>
          <button onClick={load} className="ml-auto zs-btn zs-btn-ghost text-xs" data-testid="refresh-overview-btn">
            <RefreshCw size={12} /> Refresh
          </button>
        </div>

        {loading || !data ? (
          <div className="text-white/50 text-center py-12">Loading…</div>
        ) : (
          <>
            {/* Revenue & Credits */}
            <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
              <StatCard
                testid="stat-revenue"
                icon={<IndianRupee size={20} />}
                label="Total Revenue"
                value={`₹${data.revenue.total_rs.toLocaleString(undefined, { maximumFractionDigits: 2 })}`}
                accent="emerald"
              />
              <StatCard
                testid="stat-credits-sold"
                icon={<BarChart3 size={20} />}
                label="Credits Sold"
                value={data.revenue.credits_sold.toLocaleString()}
                accent="indigo"
              />
              <StatCard
                testid="stat-pending-topups"
                icon={<Clock size={20} />}
                label="Pending Top-ups"
                value={data.revenue.pending_topups}
                accent={data.revenue.pending_topups > 0 ? "amber" : "neutral"}
              />
              <StatCard
                testid="stat-users"
                icon={<Users size={20} />}
                label="Total Users"
                value={data.users.total}
                accent="purple"
              />
            </div>

            {/* Credits backup */}
            <div className="zs-card-2 p-5 mb-6">
              <h2 className="text-white font-bold mb-3 flex items-center gap-2">
                <BarChart3 size={16} /> Credit Backup (Across All Clients)
              </h2>
              <div className="grid sm:grid-cols-3 gap-4">
                <Mini label="Total Assigned" value={data.users.total_credits_assigned} color="text-white" testid="credits-assigned" />
                <Mini label="Total Used" value={data.users.total_credits_used} color="text-amber-300" testid="credits-used" />
                <Mini label="Available Backup" value={data.users.total_credits_available} color="text-emerald-300" testid="credits-available" />
              </div>
              <div className="mt-4">
                <div className="h-3 bg-white/10 rounded-full overflow-hidden flex">
                  <div
                    className="bg-amber-500"
                    style={{ width: `${data.users.total_credits_assigned > 0 ? (data.users.total_credits_used / data.users.total_credits_assigned) * 100 : 0}%` }}
                  />
                  <div className="bg-emerald-500 flex-1" />
                </div>
                <div className="flex justify-between text-xs text-white/50 mt-1">
                  <span>Used</span>
                  <span>Available</span>
                </div>
              </div>
            </div>

            {/* Tasks + Workers */}
            <div className="grid lg:grid-cols-2 gap-5">
              <div className="zs-card-2 p-5">
                <h2 className="text-white font-bold mb-3 flex items-center gap-2"><Activity size={16} /> Live Tasks</h2>
                <div className="grid grid-cols-2 gap-4">
                  <Mini label="Active" value={data.tasks.active} color="text-emerald-300" testid="tasks-active" />
                  <Mini label="Scheduled" value={data.tasks.scheduled} color="text-indigo-300" testid="tasks-scheduled" />
                </div>
              </div>
              <div className="zs-card-2 p-5">
                <h2 className="text-white font-bold mb-3 flex items-center gap-2"><Server size={16} /> Worker Fleet</h2>
                <div className="grid grid-cols-2 gap-4">
                  <Mini label="Online" value={`${data.workers.online} / ${data.workers.total}`} color="text-emerald-300" testid="workers-online" />
                  <Mini label="Free Capacity" value={`${data.workers.free_capacity} / ${data.workers.total_capacity}`} color="text-amber-300" testid="workers-capacity" />
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function StatCard({ icon, label, value, accent = "neutral", testid }) {
  const colors = {
    emerald: "from-emerald-500/20 to-emerald-500/5 border-emerald-500/30 text-emerald-300",
    indigo: "from-indigo-500/20 to-indigo-500/5 border-indigo-500/30 text-indigo-300",
    amber: "from-amber-500/20 to-amber-500/5 border-amber-500/30 text-amber-300",
    purple: "from-purple-500/20 to-purple-500/5 border-purple-500/30 text-purple-300",
    neutral: "from-white/5 to-white/0 border-white/10 text-white/80",
  };
  return (
    <div className={`p-4 rounded-xl bg-gradient-to-br ${colors[accent]} border`} data-testid={testid}>
      <div className="flex items-center justify-between mb-2">
        <div className="opacity-70">{icon}</div>
      </div>
      <div className="text-2xl font-bold text-white">{value}</div>
      <div className="text-white/60 text-xs mt-1">{label}</div>
    </div>
  );
}

function Mini({ label, value, color = "text-white", testid }) {
  return (
    <div data-testid={testid}>
      <div className="text-white/50 text-xs mb-1">{label}</div>
      <div className={`text-2xl font-bold ${color}`}>{value}</div>
    </div>
  );
}
