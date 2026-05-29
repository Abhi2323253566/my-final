import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api, formatApiErrorDetail } from "@/lib/api";
import { ArrowLeft, FilePlus, Edit3, Trash2, Save, Code2, Info, FileText } from "lucide-react";
import { toast } from "sonner";

export default function FileEditorPage() {
  const nav = useNavigate();
  const [files, setFiles] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [activeName, setActiveName] = useState("No File Selected");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [renameMode, setRenameMode] = useState(false);
  const [renameInput, setRenameInput] = useState("");

  const loadFiles = useCallback(async () => {
    try {
      const { data } = await api.get("/name-files");
      setFiles(data);
    } catch {}
  }, []);

  useEffect(() => { loadFiles(); }, [loadFiles]);

  const openFile = async (id) => {
    try {
      const { data } = await api.get(`/name-files/${id}`);
      setActiveId(data.id);
      setActiveName(data.name);
      setContent(data.content || "");
      setRenameMode(false);
    } catch (e) { toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed"); }
  };

  const newFile = async () => {
    const name = window.prompt("Enter a name for the new file");
    if (!name || !name.trim()) return;
    try {
      const { data } = await api.post("/name-files", { name: name.trim() });
      await loadFiles();
      await openFile(data.id);
      toast.success("File created");
    } catch (e) { toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed"); }
  };

  const rename = async () => {
    if (!activeId) return;
    if (!renameInput.trim()) { setRenameMode(false); return; }
    try {
      await api.put(`/name-files/${activeId}/rename`, { name: renameInput.trim() });
      setActiveName(renameInput.trim());
      setRenameMode(false);
      loadFiles();
      toast.success("Renamed");
    } catch (e) { toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed"); }
  };

  const remove = async () => {
    if (!activeId) return;
    if (!window.confirm(`Delete file "${activeName}"?`)) return;
    try {
      await api.delete(`/name-files/${activeId}`);
      setActiveId(null); setActiveName("No File Selected"); setContent("");
      loadFiles();
      toast.success("Deleted");
    } catch (e) { toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed"); }
  };

  const save = async () => {
    if (!activeId) return toast.error("No file selected");
    setBusy(true);
    try {
      await api.put(`/name-files/${activeId}/content`, { content });
      toast.success("Saved");
      loadFiles();
    } catch (e) { toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed"); }
    finally { setBusy(false); }
  };

  return (
    <div className="zs-shell flex flex-col h-screen">
      {/* Top header bar */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-white/5">
        <div className="flex items-center gap-2 text-white">
          <Code2 size={20} className="text-indigo-400" />
          <span className="font-bold tracking-tight">Div File Editor</span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button onClick={() => nav("/dashboard")} className="zs-btn zs-btn-secondary text-sm" data-testid="back-button">
            <ArrowLeft size={14} /> Back
          </button>
          <button onClick={newFile} className="zs-btn zs-btn-primary text-sm" data-testid="new-file-button">
            <FilePlus size={14} /> New File
          </button>
          <button
            onClick={() => { if (activeId) { setRenameMode(true); setRenameInput(activeName); } }}
            disabled={!activeId}
            className="zs-btn zs-btn-warning text-sm" data-testid="rename-file-button">
            <Edit3 size={14} /> Rename File
          </button>
          <button onClick={remove} disabled={!activeId} className="zs-btn zs-btn-danger text-sm" data-testid="delete-file-button">
            <Trash2 size={14} /> Delete File
          </button>
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar */}
        <aside className="w-64 border-r border-white/5 p-4 overflow-y-auto" data-testid="files-sidebar">
          <div className="text-white/70 text-sm uppercase tracking-wider mb-3">Files</div>
          {files.length === 0 && <div className="text-white/40 text-sm">No files yet</div>}
          <ul className="space-y-1">
            {files.map((f) => (
              <li key={f.id}>
                <button
                  onClick={() => openFile(f.id)}
                  className={`w-full text-left px-3 py-2 rounded-lg flex items-center gap-2 transition
                    ${activeId === f.id ? "bg-indigo-500/15 text-indigo-200 border border-indigo-500/30" : "text-white/80 hover:bg-white/5 border border-transparent"}`}
                  data-testid={`file-item-${f.id}`}
                >
                  <FileText size={14} />
                  <span className="truncate flex-1">{f.name}</span>
                  <span className="text-xs text-white/40">{f.count}</span>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        {/* Editor */}
        <main className="flex-1 flex flex-col overflow-hidden">
          <div className="flex items-center justify-between px-6 py-3 border-b border-white/5">
            {renameMode ? (
              <div className="flex items-center gap-2">
                <input
                  autoFocus
                  value={renameInput}
                  onChange={(e) => setRenameInput(e.target.value)}
                  className="zs-input !py-1.5 max-w-[260px]"
                  onKeyDown={(e) => { if (e.key === "Enter") rename(); if (e.key === "Escape") setRenameMode(false); }}
                  data-testid="rename-input"
                />
                <button onClick={rename} className="zs-btn zs-btn-success !py-1.5 !px-3 text-sm" data-testid="rename-save">Save</button>
                <button onClick={() => setRenameMode(false)} className="zs-btn zs-btn-ghost !py-1.5 !px-3 text-sm">Cancel</button>
              </div>
            ) : (
              <div className="text-white font-semibold" data-testid="active-filename">{activeName}</div>
            )}
            <button onClick={save} disabled={!activeId || busy} className="zs-btn zs-btn-success text-sm" data-testid="save-file-button">
              {busy ? <span className="zs-spin" /> : <><Save size={14} /> Save</>}
            </button>
          </div>

          <div className="mx-6 mt-3 flex items-center gap-2 text-sm text-white/70 bg-indigo-500/10 border border-indigo-500/20 rounded-md px-3 py-2">
            <Info size={14} className="text-indigo-300" />
            <span>Write <b className="text-white">1 name per line</b>. No commas, no extra spaces. Empty lines are removed automatically on save.</span>
          </div>

          <div className="flex-1 p-6 overflow-hidden">
            <textarea
              className="zs-editor h-full"
              placeholder={"Enter names here \u2014 one name per line\nExample:\nJohn\nJane\nAlex"}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              disabled={!activeId}
              data-testid="editor-textarea"
            />
          </div>
        </main>
      </div>
    </div>
  );
}
