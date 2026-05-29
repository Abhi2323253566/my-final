import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import { Eye, EyeOff } from "lucide-react";

export default function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const onSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    const res = await login(email.trim(), password);
    setBusy(false);
    if (!res.ok) setError(res.error || "Login failed");
  };

  return (
    <div className="zs-login-bg flex items-center justify-center px-4">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-[400px] zs-card-2 p-7 shadow-2xl"
        data-testid="login-form"
      >
        <h1 className="text-3xl font-extrabold text-center text-white mb-6 tracking-tight">Login</h1>

        <div className="space-y-4">
          <input
            type="email"
            placeholder="Email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="zs-input"
            data-testid="login-email-input"
            autoComplete="email"
          />

          <div className="relative">
            <input
              type={showPwd ? "text" : "password"}
              placeholder="Password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="zs-input pr-12"
              data-testid="login-password-input"
              autoComplete="current-password"
            />
            <button
              type="button"
              onClick={() => setShowPwd((v) => !v)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-white/60 hover:text-white"
              data-testid="login-password-toggle"
              aria-label="Toggle password"
            >
              {showPwd ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </div>

          {error && (
            <div className="text-sm text-red-400 bg-red-900/30 border border-red-700/40 rounded-md px-3 py-2" data-testid="login-error">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="zs-btn zs-btn-primary w-full mt-2"
            data-testid="login-submit-button"
          >
            {busy ? <span className="zs-spin" /> : "Let me in"}
          </button>
        </div>
      </form>
    </div>
  );
}
