import { useEffect, useState } from "react";
import { api, formatApiErrorDetail } from "@/lib/api";
import { Plus } from "lucide-react";
import { toast } from "sonner";

const MEETING_TYPES = [
  "Normal Participants",
  "Co-Host Participants",
  "Webinar Attendees",
];

export default function CreateTaskPanel({ onCreated, variant = "new" }) {
  // Meeting ID & Password are PERSISTED to localStorage and survive submits,
  // navigation, and full page reloads — they only clear when the user hits
  // the explicit "Clear ID/Pwd" button. Per user request: meeting id/password
  // form field me tab tk rhe jbtk use khud na htaye.
  const [meetingId, setMeetingId] = useState(() => {
    try { return localStorage.getItem("zs.meetingId") || ""; } catch { return ""; }
  });
  const [password, setPassword] = useState(() => {
    try { return localStorage.getItem("zs.meetingPwd") || ""; } catch { return ""; }
  });
  const [members, setMembers] = useState("");
  const [nameSource, setNameSource] = useState("NamesIn");
  const [meetingType, setMeetingType] = useState(MEETING_TYPES[0]);
  const [timeout, setTimeoutSec] = useState(7200);
  const [floating, setFloating] = useState(false);
  const [reactions, setReactions] = useState(false);
  // v8.4: reaction interval (seconds). Random pick in [min,max] between
  // each emoji click. Only used when floating || reactions toggle is ON.
  const [reactMinSec, setReactMinSec] = useState(30);
  const [reactMaxSec, setReactMaxSec] = useState(90);
  const [schedEnabled, setSchedEnabled] = useState(false);
  const [schedAt, setSchedAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [nameOptions, setNameOptions] = useState([
    { id: "NamesIn", name: "NamesIn", builtin: true },
    { id: "Indian", name: "Indian", builtin: true },
    { id: "English", name: "English", builtin: true },
  ]);

  // Mirror meetingId / password to localStorage on every change.
  useEffect(() => {
    try { localStorage.setItem("zs.meetingId", meetingId); } catch {}
  }, [meetingId]);
  useEffect(() => {
    try { localStorage.setItem("zs.meetingPwd", password); } catch {}
  }, [password]);

  const loadOptions = async () => {
    try {
      const [b, f] = await Promise.all([
        api.get("/name-files/builtin"),
        api.get("/name-files"),
      ]);
      const opts = [...b.data, ...f.data.map((x) => ({ id: x.name, name: x.name, builtin: false }))];
      setNameOptions(opts);
    } catch {}
  };

  useEffect(() => { loadOptions(); }, []);

  // After-submit reset: KEEP meetingId & password (persistent until user
  // explicitly hits the "Clear ID/Pwd" button). Only members + scheduling
  // are wiped so the next task can be queued in 1 click.
  const reset = () => {
    setMembers("");
    setMeetingType(MEETING_TYPES[0]); setTimeoutSec(7200);
    setFloating(false); setReactions(false);
    setReactMinSec(30); setReactMaxSec(90);
    setSchedEnabled(false); setSchedAt("");
  };

  // Full clear including meetingId/password — only fires from the explicit
  // "Clear ID/Pwd" button.
  const clearMeeting = () => {
    setMeetingId(""); setPassword("");
    try {
      localStorage.removeItem("zs.meetingId");
      localStorage.removeItem("zs.meetingPwd");
    } catch {}
  };

  const submit = async (e) => {
    e?.preventDefault?.();
    const mid = meetingId.trim();
    const cleaned = mid.replace(/\D/g, "");
    if (!cleaned) return toast.error("Wrong Meeting ID: cannot be empty");
    if (cleaned.length < 9 || cleaned.length > 11)
      return toast.error(`Wrong Meeting ID: must be 9-11 digits (got ${cleaned.length})`);
    if (password) {
      if (password.length > 10)
        return toast.error("Wrong Meeting Password: max 10 characters allowed by Zoom");
      if (!/^[A-Za-z0-9]+$/.test(password))
        return toast.error("Wrong Meeting Password: only letters and digits allowed");
    }
    const n = parseInt(members, 10);
    if (!n || n < 1 || n > 500) return toast.error("Members must be 1-500");
    if (schedEnabled && !schedAt) return toast.error("Pick a schedule time");

    setBusy(true);
    try {
      const payload = {
        meeting_id: cleaned,
        meeting_password: password,
        members: n,
        name_source: nameSource,
        meeting_type: meetingType,
        timeout: parseInt(timeout, 10) || 7200,
        floating_emoji: floating,
        participant_reactions: reactions,
        reaction_interval_min: Math.max(5, parseInt(reactMinSec, 10) || 30),
        reaction_interval_max: Math.max(
          Math.max(5, parseInt(reactMinSec, 10) || 30),
          parseInt(reactMaxSec, 10) || 90,
        ),
        scheduled_at: schedEnabled && schedAt ? new Date(schedAt).toISOString() : null,
      };
      await api.post("/tasks", payload);
      toast.success(schedEnabled ? "Task scheduled" : "Task started");
      reset();
      onCreated?.();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Failed to create task");
    } finally {
      setBusy(false);
    }
  };

  const cls = variant === "classic" ? "zs-input-classic" : "zs-input";
  const isClassic = variant === "classic";

  return (
    <form onSubmit={submit} className={isClassic ? "zs-card-2 p-5" : "zs-card-2 p-5"} data-testid="create-task-panel">
      <div className="flex items-center gap-3 mb-5">
        <div className="section-icon icon-indigo"><Plus size={18} /></div>
        <h2 className="text-white text-xl font-bold">Create Task</h2>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="zs-label">{isClassic ? "Meeting ID:" : "Meeting ID"}</div>
          <input className={cls} placeholder="Enter meeting ID (9-11 digits)" value={meetingId}
            onChange={(e) => setMeetingId(e.target.value)} data-testid="task-meeting-id-input" />
          {meetingId && (() => {
            const cleaned = meetingId.replace(/\D/g, "");
            if (!cleaned) return <div className="text-rose-400 text-xs mt-1" data-testid="meeting-id-warn">Wrong Meeting ID: digits only</div>;
            if (cleaned.length < 9 || cleaned.length > 11)
              return <div className="text-rose-400 text-xs mt-1" data-testid="meeting-id-warn">Wrong Meeting ID: must be 9-11 digits (got {cleaned.length})</div>;
            return <div className="text-emerald-400 text-xs mt-1" data-testid="meeting-id-ok">Valid format</div>;
          })()}
        </div>
        <div>
          <div className="zs-label">{isClassic ? "Meeting Password:" : "Password"}</div>
          <input className={cls} placeholder="Enter password" value={password}
            onChange={(e) => setPassword(e.target.value)} data-testid="task-password-input" />
          {password && (() => {
            if (password.length > 10)
              return <div className="text-rose-400 text-xs mt-1" data-testid="meeting-pwd-warn">Wrong Password: max 10 characters</div>;
            if (!/^[A-Za-z0-9]+$/.test(password))
              return <div className="text-rose-400 text-xs mt-1" data-testid="meeting-pwd-warn">Wrong Password: only letters & digits</div>;
            return <div className="text-emerald-400 text-xs mt-1" data-testid="meeting-pwd-ok">Valid format</div>;
          })()}
        </div>

        <div>
          <div className="zs-label">{isClassic ? "Members (1-100):" : "Members (1\u2013100)"}</div>
          <input className={cls} placeholder="10, 20, 30..." inputMode="numeric"
            value={members} onChange={(e) => setMembers(e.target.value.replace(/[^0-9]/g, ""))}
            data-testid="task-members-input" />
        </div>
        <div>
          <div className="zs-label">Name</div>
          <select className={cls} value={nameSource} onChange={(e) => setNameSource(e.target.value)}
            data-testid="task-name-source-select">
            {nameOptions.map((o) => (
              <option key={o.id} value={o.id}>{o.name}{o.builtin ? "" : " (custom)"}</option>
            ))}
          </select>
          <a href="/file-editor" className="inline-flex items-center gap-1 text-emerald-400 hover:text-emerald-300 text-sm mt-2"
            data-testid="task-add-custom-names">
            <Plus size={14} /> Add custom names
          </a>
        </div>

        <div>
          <div className="zs-label">{isClassic ? "Meeting Type:" : "Meeting Type"}</div>
          <select className={cls} value={meetingType} onChange={(e) => setMeetingType(e.target.value)}
            data-testid="task-meeting-type-select">
            {MEETING_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div>
          <div className="zs-label">{isClassic ? "Timeout (in seconds):" : "Timeout (sec)"}</div>
          <input className={cls} type="number" min="10" max="86400" value={timeout}
            onChange={(e) => setTimeoutSec(e.target.value)} data-testid="task-timeout-input" />
        </div>
      </div>

      <div className="mt-5 space-y-3">
        <ToggleRow label={<><span aria-hidden>🎉</span> Floating Emoji Reactions</>}
          value={floating} onChange={setFloating} testid="task-floating-toggle" />
        <ToggleRow label={<><span aria-hidden>📢</span> Participant Reactions</>}
          value={reactions} onChange={setReactions} testid="task-reactions-toggle" />

        {(floating || reactions) && (
          <div className="zs-card p-3" data-testid="reaction-interval-block">
            <div className="text-white/80 text-xs mb-2">
              Reaction Interval — bot har <span className="text-emerald-400 font-semibold">{reactMinSec}s</span>
              {" – "}
              <span className="text-emerald-400 font-semibold">{reactMaxSec}s</span> me ek random emoji bhejega
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <div className="zs-label">Min (seconds)</div>
                <input
                  className={cls}
                  type="number"
                  min="5"
                  max="3600"
                  value={reactMinSec}
                  onChange={(e) => setReactMinSec(e.target.value)}
                  data-testid="task-reaction-min-input"
                />
              </div>
              <div>
                <div className="zs-label">Max (seconds)</div>
                <input
                  className={cls}
                  type="number"
                  min="5"
                  max="3600"
                  value={reactMaxSec}
                  onChange={(e) => setReactMaxSec(e.target.value)}
                  data-testid="task-reaction-max-input"
                />
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="mt-5">
        <div className="zs-label">Schedule Task (IST)</div>
        <label className="inline-flex items-center gap-2 text-white/80 cursor-pointer mb-2">
          <input type="checkbox" checked={schedEnabled} onChange={(e) => setSchedEnabled(e.target.checked)}
            className="w-4 h-4 accent-indigo-500" data-testid="task-enable-schedule" />
          Enable Scheduling
        </label>
        {schedEnabled && (
          <input
            type="datetime-local"
            value={schedAt}
            onChange={(e) => setSchedAt(e.target.value)}
            className={cls}
            data-testid="task-schedule-input"
          />
        )}
      </div>

      <div className="mt-5 flex gap-3">
        <button disabled={busy} className="zs-btn zs-btn-primary flex-1" data-testid="task-submit-button">
          {busy ? <span className="zs-spin" /> : "Submit"}
        </button>
        <button type="button" onClick={reset} className="zs-btn zs-btn-danger px-6" data-testid="task-cancel-button">
          Cancel
        </button>
        <button
          type="button"
          onClick={clearMeeting}
          className="zs-btn px-4 border border-white/15 text-white/80 hover:text-white"
          title="Clear saved Meeting ID & Password"
          data-testid="task-clear-meeting-button"
        >
          Clear ID/Pwd
        </button>
      </div>
    </form>
  );
}

function ToggleRow({ label, value, onChange, testid }) {
  return (
    <div className="flex items-center justify-between zs-card p-3">
      <div className="text-white/90 text-sm flex items-center gap-2">{label}</div>
      <div className="flex items-center gap-2">
        <span className={`text-xs font-bold ${value ? "text-emerald-400" : "text-red-400"}`}>{value ? "ON" : "OFF"}</span>
        <button type="button" onClick={() => onChange(!value)} className={`zs-toggle ${value ? "on" : ""}`}
          data-testid={testid} aria-pressed={value}>
          <span className="knob" />
        </button>
      </div>
    </div>
  );
}
