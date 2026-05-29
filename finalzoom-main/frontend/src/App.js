import { useEffect } from "react";
import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "@/auth/AuthContext";
import LoginPage from "@/pages/LoginPage";
import DashboardPage from "@/pages/DashboardPage";
import ClassicDashboardPage from "@/pages/ClassicDashboardPage";
import FileEditorPage from "@/pages/FileEditorPage";
import WorkersPage from "@/pages/WorkersPage";
import AdminUsersPage from "@/pages/AdminUsersPage";
import AdminTopupPage from "@/pages/AdminTopupPage";
import AdminOverviewPage from "@/pages/AdminOverviewPage";
import BuyCreditsPage from "@/pages/BuyCreditsPage";
import { Toaster } from "sonner";

function Protected({ children, adminOnly = false }) {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#0b0d12] text-white/60">
        Loading...
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  if (adminOnly && user.role !== "admin") return <Navigate to="/dashboard" replace />;
  return children;
}

function RedirectIfAuthed({ children }) {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (user) return <Navigate to="/dashboard" replace />;
  return children;
}

function App() {
  return (
    <div className="App">
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<RedirectIfAuthed><LoginPage /></RedirectIfAuthed>} />
            <Route path="/dashboard" element={<Protected><DashboardPage /></Protected>} />
            <Route path="/classic" element={<Protected><ClassicDashboardPage /></Protected>} />
            <Route path="/file-editor" element={<Protected><FileEditorPage /></Protected>} />
            <Route path="/buy-credits" element={<Protected><BuyCreditsPage /></Protected>} />
            <Route path="/workers" element={<Protected adminOnly={true}><WorkersPage /></Protected>} />
            <Route path="/admin/users" element={<Protected adminOnly={true}><AdminUsersPage /></Protected>} />
            <Route path="/admin/topups" element={<Protected adminOnly={true}><AdminTopupPage /></Protected>} />
            <Route path="/admin/overview" element={<Protected adminOnly={true}><AdminOverviewPage /></Protected>} />
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </BrowserRouter>
        <Toaster theme="dark" position="top-right" richColors />
      </AuthProvider>
    </div>
  );
}

export default App;
