import { Link, useNavigate } from "react-router-dom";
import { BarChart3, Plus, LogOut, LayoutDashboard, Server, Users, Wallet, IndianRupee, PieChart } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";

export default function TopBar({ usage = 0, usageLimit = 15000, switchTo = "classic" }) {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const isAdmin = user?.role === "admin";

  const doLogout = async () => {
    await logout();
    nav("/login");
  };

  return (
    <div className="flex items-center justify-between px-6 py-4">
      {/* Logo */}
      <Link to="/dashboard" className="flex items-center gap-2 select-none" data-testid="brand-link">
        <div className="w-11 h-11 rounded-xl flex items-center justify-center bg-gradient-to-br from-indigo-500/80 to-indigo-700/80 border border-white/10 shadow-lg">
          <span className="text-white font-extrabold text-lg tracking-tight">Z</span>
        </div>
        <span className="text-white font-extrabold tracking-[0.18em] text-sm">ZOOM</span>
      </Link>

      {/* Right cluster */}
      <div className="flex items-center gap-3 flex-wrap">
        <span
          className="zs-pill text-amber-400 border-amber-500/30 bg-amber-500/5"
          data-testid="usage-pill"
        >
          <BarChart3 size={14} />
          Usage: {usage} / {usageLimit}
        </span>

        {!isAdmin && (
          <Link
            to="/buy-credits"
            className="zs-pill text-emerald-300 border-emerald-500/30 bg-emerald-500/10 hover:bg-emerald-500/20 transition"
            data-testid="buy-credits-link"
          >
            <Wallet size={14} />
            Buy Credits
          </Link>
        )}

        {isAdmin && (
          <Link
            to="/admin/overview"
            className="zs-pill text-amber-300 border-amber-500/30 bg-amber-500/5 hover:bg-amber-500/10 transition"
            data-testid="admin-overview-link"
          >
            <PieChart size={14} />
            Overview
          </Link>
        )}

        {isAdmin && (
          <Link
            to="/admin/topups"
            className="zs-pill text-emerald-300 border-emerald-500/30 bg-emerald-500/5 hover:bg-emerald-500/10 transition"
            data-testid="admin-topups-link"
          >
            <IndianRupee size={14} />
            Top-ups
          </Link>
        )}

        {isAdmin && (
          <Link
            to="/admin/users"
            className="zs-pill text-purple-300 border-purple-500/30 bg-purple-500/5 hover:bg-purple-500/10 transition"
            data-testid="admin-users-link"
          >
            <Users size={14} />
            Users
          </Link>
        )}

        {isAdmin && (
          <Link
            to="/workers"
            className="zs-pill text-sky-300 border-sky-500/30 bg-sky-500/5 hover:bg-sky-500/10 transition"
            data-testid="workers-link"
          >
            <Server size={14} />
            Workers
          </Link>
        )}

        <Link
          to="/file-editor"
          className="zs-pill text-emerald-400 border-emerald-500/30 bg-emerald-500/5 hover:bg-emerald-500/10 transition"
          data-testid="add-custom-names-link"
        >
          <Plus size={14} />
          Add custom names
        </Link>

        <button
          onClick={doLogout}
          className="zs-pill text-red-400 border-red-500/30 bg-red-500/5 hover:bg-red-500/10 transition"
          data-testid="logout-button"
          aria-label="Logout"
          title="Logout"
        >
          <LogOut size={14} />
        </button>

        {switchTo === "classic" ? (
          <Link
            to="/classic"
            className="zs-pill text-indigo-300 border-indigo-500/30 bg-indigo-500/10 hover:bg-indigo-500/20 transition"
            data-testid="switch-classic-ui"
          >
            <LayoutDashboard size={14} />
            Classic UI
          </Link>
        ) : (
          <Link
            to="/dashboard"
            className="zs-pill text-indigo-300 border-indigo-500/30 bg-indigo-500/10 hover:bg-indigo-500/20 transition"
            data-testid="switch-new-ui"
          >
            <LayoutDashboard size={14} />
            New UI
          </Link>
        )}
      </div>
    </div>
  );
}
